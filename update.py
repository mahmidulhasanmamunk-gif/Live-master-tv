#!/usr/bin/env python3
"""
IPTV Master Playlist Generator
==============================

পাইপলাইন:
  1. sources.json থেকে Source তালিকা পড়া
  2. প্রতিটি Source ডাউনলোড (Retry সহ); ব্যর্থ হলে আগের সফল ডেটা ব্যবহার
  3. M3U Parse
  4. নিরাপত্তা/বৈধতা ফিল্টার (Token, Authentication, নকল Header, DRM বাদ)
  5. Channel Merge (tvg-id + নাম Normalization + Fuzzy Matching)
  6. Category নির্ধারণ ও নির্দিষ্ট ক্রমে সাজানো
  7. Stream Validation (Playlist + Segment পর্যন্ত যাচাই)
  8. master.m3u, channels.json, failed_sources.json (+ ঐচ্ছিক master_failover.m3u) তৈরি

ব্যবহার:
  python update.py                 # পূর্ণ আপডেট
  python update.py --no-validate   # Stream যাচাই ছাড়া
  python update.py --config my.json --out-dir out/
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import parse_qsl, urljoin, urlparse

import requests

BASE_DIR = Path(__file__).resolve().parent
log = logging.getLogger("iptv-master")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CATEGORY_ORDER = [
    "Bangla News",
    "Sports",
    "Bangladeshi Entertainment",
    "Bangladeshi General TV",
    "International News",
    "International Sports",
    "International Entertainment",
    "Movies",
    "Music",
    "Kids",
    "Documentary",
    "Others",
]

DEFAULT_SETTINGS = {
    "fetch_timeout": 30,
    "fetch_retries": 3,
    "validate": True,
    "validation_workers": 32,
    "connect_timeout": 6,
    "read_timeout": 10,
    "validation_retries": 2,
    "validation_time_budget_seconds": 780,
    "recheck_ok_minutes": 90,
    "failure_threshold": 3,
    "drop_failed_channels": False,
    "only_working_in_master": False,
    "embed_alternatives_in_master": False,
    "proxy_base_url": "",
    "fuzzy_threshold": 0.92,
    "min_source_shrink_ratio": 0.3,
    "min_total_ratio": 0.5,
}

DEFAULT_FILTERS = {
    "allowed_schemes": ["http", "https"],
    "blocked_domains": [],
    "blocked_query_keys": [
        "token", "tokens", "e", "u", "expires", "expire", "exp", "st", "sig", "signature",
        "hdnea", "hdnts", "hmac", "auth", "authorization", "key", "apikey", "api_key",
        "access_token", "jwt", "user", "username", "uid", "userid", "pass", "password",
        "pwd", "md5", "policy", "key-pair-id", "x-amz-signature", "x-amz-credential",
        "x-amz-security-token", "remote",
    ],
    "blocked_name_patterns": [],
    "allow_custom_headers": False,
    "exclude_radio": True,
}

# validator-এর নিজস্ব পরিচয় (কোনো অ্যাপের User-Agent নকল করা হয় না)
VALIDATOR_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; IPTVMasterValidator/1.0)"}
FETCH_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; IPTVMasterFetcher/1.0)"}

ATTR_RE = re.compile(r'([A-Za-z0-9_\-\.]+)\s*=\s*"([^"]*)"')
OPTION_PREFIXES = ("#EXTVLCOPT", "#EXTHTTP", "#KODIPROP", "#EXTGRP")

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class Entry:
    name: str
    url: str
    group: str = ""
    logo: str = ""
    tvg_id: str = ""
    tvg_name: str = ""
    options: list = field(default_factory=list)
    source_id: str = ""
    source_name: str = ""
    priority: int = 0


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def load_json(path: Path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)


def merge_dict(defaults: dict, override: dict | None) -> dict:
    merged = dict(defaults)
    if override:
        merged.update({k: v for k, v in override.items() if not k.startswith("_")})
    return merged


# ---------------------------------------------------------------------------
# M3U parsing
# ---------------------------------------------------------------------------
def split_extinf(line: str) -> tuple[str, str]:
    """#EXTINF লাইনকে (attributes, name) এ ভাগ করে; quote-এর ভেতরের কমা উপেক্ষা করে।"""
    body = line[len("#EXTINF:"):]
    in_quote = False
    for i, ch in enumerate(body):
        if ch == '"':
            in_quote = not in_quote
        elif ch == "," and not in_quote:
            return body[:i], body[i + 1:].strip()
    return body, ""


def parse_m3u(text: str, source: dict) -> list[Entry]:
    entries: list[Entry] = []
    pending: dict | None = None
    last: Entry | None = None
    text = text.lstrip("\ufeff")

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        upper = line.upper()
        if upper.startswith("#EXTINF"):
            head, name = split_extinf(line)
            attrs = {k.lower(): v.strip() for k, v in ATTR_RE.findall(head)}
            pending = {"attrs": attrs, "name": name, "options": []}
            last = None
        elif line.startswith("#"):
            if upper.startswith(OPTION_PREFIXES):
                if pending is not None:
                    pending["options"].append(line)
                elif last is not None:
                    # কিছু Playlist-এ Option লাইন URL-এর পরে আসে
                    last.options.append(line)
        else:
            if pending is None:
                continue
            url, _, header_suffix = line.partition("|")
            url = url.strip()
            options = list(pending["options"])
            if header_suffix.strip():
                options.append("#PIPEHEADERS:" + header_suffix.strip())
            attrs = pending["attrs"]
            group = attrs.get("group-title", "")
            if not group:
                for opt in options:
                    if opt.upper().startswith("#EXTGRP:"):
                        group = opt.split(":", 1)[1].strip()
            name = pending["name"] or attrs.get("tvg-name", "")
            entry = Entry(
                name=name.strip(),
                url=url,
                group=group,
                logo=attrs.get("tvg-logo", ""),
                tvg_id=attrs.get("tvg-id", ""),
                tvg_name=attrs.get("tvg-name", ""),
                options=options,
                source_id=source["id"],
                source_name=source.get("name", source["id"]),
                priority=int(source.get("priority", 0)),
            )
            entries.append(entry)
            last = entry
            pending = None
    return entries


# ---------------------------------------------------------------------------
# Safety / legality filters
# ---------------------------------------------------------------------------
XTREAM_PATH_RE = re.compile(r"/(?:live|movie|series)/[^/]+/[^/]+/\d+(?:\.\w+)?$", re.I)
AUDIO_EXT = (".mp3", ".aac", ".ogg", ".pls", ".opus")


def check_entry(entry: Entry, flt: dict, name_patterns: list) -> str | None:
    """Entry বাদ দেওয়ার কারণ ফেরত দেয়; ব্যবহারযোগ্য হলে None।"""
    url = entry.url
    if not url:
        return "empty_url"
    try:
        parsed = urlparse(url)
    except ValueError:
        return "invalid_url"
    if parsed.scheme.lower() not in flt["allowed_schemes"] or not parsed.hostname:
        return "unsupported_scheme_or_host"

    host = parsed.hostname.lower()
    for dom in flt["blocked_domains"]:
        dom = dom.lower().lstrip(".")
        if host == dom or host.endswith("." + dom):
            return "blocked_domain"

    if parsed.username or parsed.password or "@" in parsed.netloc:
        return "credentials_in_url"
    if XTREAM_PATH_RE.search(parsed.path):
        return "account_credentials_in_path"

    blocked_keys = {k.lower() for k in flt["blocked_query_keys"]}
    for key, _ in parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower() in blocked_keys:
            return "token_or_auth_in_query"

    for opt in entry.options:
        up = opt.upper()
        low = opt.lower()
        if up.startswith("#KODIPROP"):
            if "license" in low or "drm" in low or "widevine" in low or "playready" in low:
                return "drm_protected"
        elif up.startswith("#EXTVLCOPT"):
            key = low.split(":", 1)[1].split("=", 1)[0].strip() if ":" in low else ""
            if key.startswith("http-") and not flt["allow_custom_headers"]:
                return "needs_custom_headers"
        elif up.startswith("#EXTHTTP") or up.startswith("#PIPEHEADERS"):
            if not flt["allow_custom_headers"]:
                return "needs_custom_headers"

    label = f"{entry.name} {entry.tvg_name}".casefold()
    for pat in name_patterns:
        if pat.search(label) or pat.search(entry.group.casefold()):
            return "promo_or_placeholder"

    if flt["exclude_radio"]:
        if (re.search(r"\bradio\b", entry.group.casefold()) or re.search(r"\bradio\b", label)
                or parsed.path.lower().endswith(AUDIO_EXT)):
            return "radio_or_audio_only"
    return None


# ---------------------------------------------------------------------------
# Name normalisation & fuzzy matching
# ---------------------------------------------------------------------------
QUALITY_RE = re.compile(
    r"\b(?:\d{3,4}[pi]|[248]k|hd|fhd|uhd|sd|hq|hevc|backup|alt\w*|geo[- ]?blocked|"
    r"not 24/7|24/7|live)\b",
    re.I,
)
STOP_TOKENS = {"and", "the", "tv", "television"}
DISTINCT_WORDS = {
    "news", "sports", "sport", "movies", "movie", "music", "kids", "plus", "extra", "world",
    "gold", "bangla", "cinema", "classic", "international", "bollywood", "action", "second",
}


def _is_noise_group(text: str) -> bool:
    rest = QUALITY_RE.sub("", text.lower())
    return not re.sub(r"[\W\d_]+", "", rest, flags=re.UNICODE)


def clean_name(name: str) -> str:
    s = unicodedata.normalize("NFKC", name or "")
    s = re.sub(r"\[[^\]]*\]", " ", s)
    s = re.sub(r"\(([^)]*)\)", lambda m: " " if _is_noise_group(m.group(1)) else f" {m.group(1)} ", s)
    return re.sub(r"\s+", " ", s).strip()


DISPLAY_QUALITY_RE = re.compile(r"\b(?:\d{3,4}[pi]|[248]k|fhd|uhd|hd|sd)\b", re.I)


def display_name(name: str) -> str:
    cleaned = clean_name(name)
    shown = re.sub(r"\s+", " ", DISPLAY_QUALITY_RE.sub("", cleaned)).strip()
    return shown or cleaned


def normalize_name(name: str) -> str:
    s = clean_name(name).casefold().replace("&", " and ")
    s = QUALITY_RE.sub(" ", s)
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE).replace("_", " ")
    tokens = [str(int(t)) if t.isdigit() else t for t in s.split()]
    out = " ".join(tokens)
    return out or re.sub(r"\s+", " ", (name or "").casefold()).strip()


def fuzzy_key(norm: str) -> str:
    tokens = [t for t in norm.split() if t not in ("tv", "television")]
    return " ".join(tokens) or norm


def norm_tvg_id(tid: str) -> str:
    return (tid or "").split("@")[0].strip().casefold()


def fuzzy_match(a: str, b: str, threshold: float) -> bool:
    if a == b:
        return True
    la, lb = len(a), len(b)
    if min(la, lb) < 5:
        return False
    if 2 * min(la, lb) / (la + lb) < threshold:
        return False
    ta, tb = a.split(), b.split()
    if [t for t in ta if t.isdigit()] != [t for t in tb if t.isdigit()]:
        return False
    if (set(ta) ^ set(tb)) & DISTINCT_WORDS:
        return False
    sm = SequenceMatcher(None, a, b)
    return sm.quick_ratio() >= threshold and sm.ratio() >= threshold


def names_compatible(norms_a: set, norms_b: str) -> bool:
    """tvg-id মিললেও নাম সম্পূর্ণ আলাদা হলে Merge করা হবে না (ভুল Merge প্রতিরোধ)।"""
    kb = fuzzy_key(norms_b)
    tb = set(kb.split()) - STOP_TOKENS
    for na in norms_a:
        ka = fuzzy_key(na)
        if ka == kb:
            return True
        if SequenceMatcher(None, ka, kb).ratio() >= 0.6:
            return True
        if (set(ka.split()) - STOP_TOKENS) & tb:
            return True
    return False


def slugify(norm: str) -> str:
    ascii_part = unicodedata.normalize("NFKD", norm).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_part.lower()).strip("-")
    if len(slug) < 2:
        slug = "ch-" + hashlib.sha1(norm.encode("utf-8")).hexdigest()[:8]
    return slug


# ---------------------------------------------------------------------------
# Category classification
# ---------------------------------------------------------------------------
_R = lambda p: re.compile(p, re.I)  # noqa: E731
TOPIC_PATTERNS = [
    ("kids", _R(r"\bkids?\b|cartoon|\bnick(?:elodeon|toons|jr)?\b|disney|\btoons?\b|junior|\bjr\b|pogo|"
                r"\bbaby|boomerang|moonbug|anime|animation|doraemon|motu|cbeebies|chhota|shin chan")),
    ("music", _R(r"music|\bmtv\b|\bvh1\b|\bhits?\b|\bsongs?\b|sangeet|trace (?:urban|music|latina|toca)|"
                 r"\bgaan\b|\bfm\b")),
    ("movies", _R(r"movies?|cinema|\bfilms?\b|\bpix\b|\bmax\b|cinemax|bollywood|hollywood|\bhbo\b|"
                  r"flix|thriller|\baction\b|\bepix\b|filmax|picture")),
    ("docu", _R(r"documentar|\bdocu\b|discovery|nat(?:ional)? ?geo|history|animal planet|science|\btravel|"
                r"\bearth\b|\btlc\b|\bwild\b|nature|investigation|explor|adventure|\bplanet\b|wildlife")),
    ("news", _R(r"news|khabar|sangbad|\bbusiness\b|cnbc|bloomberg|weather|\bcnn\b|bbc world|al ?jazeera|"
                r"\bdw\b|france ?24|trt world|\bnhk\b|euronews|\bwion\b|ndtv|\babp\b|aaj tak|\bpress\b|"
                r"\bcp24\b|\bmsnbc\b|reuters|\bnews\b")),
    ("sports", _R(r"sports?\b|cricket|football|soccer|\bespn|bein|eurosport|\bnba\b|\bnfl\b|\bnhl\b|\bmlb\b|"
                  r"golf|tennis|\bf1\b|racing|wrestling|\bufc\b|willow|ten sports|star sports|\btsn\b|"
                  r"\bgoal\b|\bfifa|\btnt sport|bt sport|dazn|supersport|sportsnet|stadium|arena|motor|"
                  r"boxing|\bwof\b|marquee|talk ?sport")),
    ("ent", _R(r"entertain|general|comedy|series|drama|lifestyle|reality|showbiz|serial|natok|hindi|urdu|urdhu|"
               r"indian bangla|lifetime|\bzee\b|colors|star plus|\bsony\b|bravo|\bfx\b|\bamc\b|\bfood\b|"
               r"cooking|fashion|variety|family")),
]

BD_KEYS = {
    "btv", "btv world", "btv news", "btv national", "btv chattogram", "ntv", "rtv", "atn bangla",
    "atn news", "channel i", "channel 9", "channel 24", "ekattor", "jamuna", "somoy", "independent",
    "dbc news", "news24", "news 24", "ekushey", "maasranga", "deepto", "bangla vision", "banglavision",
    "boishakhi", "gazi", "gtv", "t sports", "tsports", "desh", "mohona", "nagorik", "bijoy", "ekhon",
    "duronto", "nexus", "channel s", "thikana", "green", "ananda",
}
BD_NEWS_KEYS = {
    "jamuna", "ekattor", "somoy", "independent", "news24", "news 24", "channel 24", "dbc news",
    "btv news", "atn news", "ekhon",
}
BD_SPORTS_KEYS = {"t sports", "tsports", "gtv", "gazi", "btv sports"}
FOREIGN_GROUP_RE = _R(r"indian|india|kolkata|west bengal|hindi|urdu|urdhu|pakistan")


def topic_of(text: str) -> str | None:
    for topic, pattern in TOPIC_PATTERNS:
        if pattern.search(text):
            return topic
    return None


def detect_bd(fkeys: set, name: str, groups: list, tvg_ids: list) -> bool:
    g = " ".join(groups).casefold()
    if any(re.search(r"\.bd(?:@|$)", t.casefold()) for t in tvg_ids):
        return True
    if re.search(r"[\u0980-\u09FF]", name):
        return True
    foreign = bool(FOREIGN_GROUP_RE.search(g))
    if foreign:
        return False
    if re.search(r"bangladesh|\bbdix\b|\bbd\b", g) or re.match(r"\s*bangla\b", g):
        return True
    return bool(fkeys & BD_KEYS)


def classify(name: str, fkeys: set, groups: list, tvg_ids: list) -> str:
    n = name.casefold()
    topic = topic_of(n) or next((t for t in (topic_of(g.casefold()) for g in groups) if t), None)
    is_bd = detect_bd(fkeys, name, groups, tvg_ids)
    group_text = " ".join(groups).casefold()

    if is_bd:
        if topic == "news" or fkeys & BD_NEWS_KEYS:
            return "Bangla News"
        if topic == "sports" or fkeys & BD_SPORTS_KEYS:
            return "Sports"
        if topic == "kids":
            return "Kids"
        if topic == "music":
            return "Music"
        if topic == "movies":
            return "Movies"
        if topic == "docu":
            return "Documentary"
        if re.search(r"entertain|drama|natok|serial|comedy", n + " " + group_text):
            return "Bangladeshi Entertainment"
        return "Bangladeshi General TV"

    return {
        "news": "International News",
        "sports": "International Sports",
        "ent": "International Entertainment",
        "movies": "Movies",
        "music": "Music",
        "kids": "Kids",
        "docu": "Documentary",
    }.get(topic, "Others")


# ---------------------------------------------------------------------------
# Channel merging
# ---------------------------------------------------------------------------
class Channel:
    def __init__(self, entry: Entry, norm: str, fkey: str):
        self.name = display_name(entry.name or entry.tvg_name) or (entry.name or entry.tvg_name or "Unknown")
        self._name_rank = (0 if entry.tvg_id else 1, len(self.name))
        self.norms = {norm}
        self.fkeys = {fkey}
        self.tvg_ids: list[str] = []
        self.groups: list[str] = []
        self.logo = ""
        self.tvg_id = ""
        self.tvg_name = ""
        self.streams: list[dict] = []
        self.category = "Others"
        self.id = ""

    def add(self, entry: Entry, norm: str, fkey: str, now: str, prev_state: dict) -> None:
        candidate = display_name(entry.name or entry.tvg_name)
        if candidate:
            rank = (0 if entry.tvg_id else 1, len(candidate))
            if rank < self._name_rank:
                self.name, self._name_rank = candidate, rank
        self.norms.add(norm)
        self.fkeys.add(fkey)
        tid = norm_tvg_id(entry.tvg_id)
        if tid and tid not in self.tvg_ids:
            self.tvg_ids.append(tid)
        if entry.group and entry.group not in self.groups:
            self.groups.append(entry.group)
        if not self.logo and entry.logo.startswith(("http://", "https://")):
            self.logo = entry.logo
        if not self.tvg_id and entry.tvg_id:
            self.tvg_id = entry.tvg_id.strip()
        if not self.tvg_name and entry.tvg_name:
            self.tvg_name = entry.tvg_name.strip()

        for st in self.streams:
            if st["url"] == entry.url:
                if entry.source_id != st["source_id"] and entry.source_id not in st["also_listed_in"]:
                    st["also_listed_in"].append(entry.source_id)
                return
        prev = prev_state.get(entry.url, {})
        self.streams.append({
            "url": entry.url,
            "source_id": entry.source_id,
            "source_name": entry.source_name,
            "priority": entry.priority,
            "also_listed_in": [],
            "original_name": entry.name,
            "original_group": entry.group,
            "original_logo": entry.logo,
            "original_tvg_id": entry.tvg_id,
            "original_tvg_name": entry.tvg_name,
            "status": prev.get("status", "unverified"),
            "reason": prev.get("reason", ""),
            "latency_ms": prev.get("latency_ms"),
            "first_seen": prev.get("first_seen", now),
            "last_seen": now,
            "last_checked": prev.get("last_checked"),
            "last_ok": prev.get("last_ok"),
            "fail_count": prev.get("fail_count", 0),
        })


def merge_entries(entries: list[Entry], settings: dict, prev_state: dict, now: str) -> list[Channel]:
    threshold = float(settings["fuzzy_threshold"])
    channels: list[Channel] = []
    by_tvg: dict[str, Channel] = {}
    by_norm: dict[str, Channel] = {}
    by_url: dict[str, Channel] = {}
    buckets: dict[str, list[Channel]] = {}

    ordered = sorted(enumerate(entries), key=lambda p: (p[1].priority, p[0]))
    for _, entry in ordered:
        norm = normalize_name(entry.name or entry.tvg_name)
        if not norm:
            continue
        fkey = fuzzy_key(norm)
        tid = norm_tvg_id(entry.tvg_id)
        target: Channel | None = None

        if tid and tid in by_tvg and names_compatible(by_tvg[tid].norms, norm):
            target = by_tvg[tid]
        if target is None and entry.url in by_url and names_compatible(by_url[entry.url].norms, norm):
            cand = by_url[entry.url]
            if not (tid and cand.tvg_ids and tid not in cand.tvg_ids):
                target = cand
        if target is None and norm in by_norm:
            cand = by_norm[norm]
            if not (tid and cand.tvg_ids and tid not in cand.tvg_ids):
                target = cand
        if target is None:
            for cand in buckets.get(fkey[:1], []):
                if tid and cand.tvg_ids and tid not in cand.tvg_ids:
                    continue
                if any(fuzzy_match(fkey, k, threshold) for k in cand.fkeys):
                    target = cand
                    break
        if target is None:
            target = Channel(entry, norm, fkey)
            channels.append(target)
            buckets.setdefault(fkey[:1], []).append(target)
        elif fkey[:1] != next(iter(target.fkeys))[:1]:
            buckets.setdefault(fkey[:1], []).append(target)

        target.add(entry, norm, fkey, now, prev_state)
        by_norm.setdefault(norm, target)
        by_url.setdefault(entry.url, target)
        if tid:
            by_tvg.setdefault(tid, target)
    return channels


def finalize_channels(channels: list[Channel]) -> None:
    used: set[str] = set()
    for ch in channels:
        main_norm = sorted(ch.norms)[0] if ch.norms else ch.name
        fkeys = set(ch.fkeys) | {fuzzy_key(normalize_name(ch.name))}
        ch.category = classify(ch.name, fkeys, ch.groups, ch.tvg_ids)
        base = slugify(normalize_name(ch.name) or main_norm)
        slug, n = base, 2
        while slug in used:
            slug = f"{base}-{n}"
            n += 1
        used.add(slug)
        ch.id = slug


# ---------------------------------------------------------------------------
# Fetching sources
# ---------------------------------------------------------------------------
def fetch_text(url: str, settings: dict) -> str:
    if url.startswith("file://"):
        return Path(url[7:]).read_text(encoding="utf-8", errors="replace")
    if not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.I) and Path(url).exists():
        return Path(url).read_text(encoding="utf-8", errors="replace")

    last_err: Exception | None = None
    retries = int(settings["fetch_retries"])
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=FETCH_HEADERS, timeout=int(settings["fetch_timeout"]))
            if resp.status_code >= 500 or resp.status_code == 429:
                raise requests.HTTPError(f"HTTP {resp.status_code}")
            resp.raise_for_status()
            resp.encoding = resp.encoding if resp.encoding and resp.encoding.lower() != "iso-8859-1" else "utf-8"
            text = resp.text
            if "#EXTM3U" not in text[:4096] and "#EXTINF" not in text:
                raise ValueError("response is not an M3U playlist")
            return text
        except (requests.RequestException, ValueError) as exc:
            last_err = exc
            log.warning("fetch %s attempt %d/%d failed: %s", url, attempt, retries, exc)
            if attempt < retries:
                time.sleep(2 * attempt)
    raise RuntimeError(f"{type(last_err).__name__}: {last_err}")


def entries_from_previous(prev_channels: list, source: dict) -> list[Entry]:
    """Source বন্ধ থাকলে আগের সফল রান থেকে ওই Source-এর Stream পুনর্গঠন।"""
    out: list[Entry] = []
    for ch in prev_channels:
        for st in ch.get("streams", []):
            if st.get("source_id") != source["id"]:
                continue
            out.append(Entry(
                name=st.get("original_name") or ch.get("name", ""),
                url=st["url"],
                group=st.get("original_group", ""),
                logo=st.get("original_logo", ""),
                tvg_id=st.get("original_tvg_id", ""),
                tvg_name=st.get("original_tvg_name", ""),
                source_id=source["id"],
                source_name=source.get("name", source["id"]),
                priority=int(source.get("priority", 0)),
            ))
    return out


# ---------------------------------------------------------------------------
# Stream validation
# ---------------------------------------------------------------------------
_thread_local = threading.local()


def _session() -> requests.Session:
    sess = getattr(_thread_local, "session", None)
    if sess is None:
        sess = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=8)
        sess.mount("http://", adapter)
        sess.mount("https://", adapter)
        _thread_local.session = sess
    return sess


def _read_limited(resp: requests.Response, limit: int) -> bytes:
    buf = bytearray()
    for chunk in resp.iter_content(chunk_size=8192):
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) >= limit:
            break
    return bytes(buf[:limit])


class _Probe:
    """একটি যাচাইয়ের ফলাফল: ok / reason / retry করা উচিত কিনা।"""
    def __init__(self, ok: bool, reason: str, retry: bool = False):
        self.ok, self.reason, self.retry = ok, reason, retry


def _http_get(url: str, timeout: tuple, limit: int, headers: dict | None = None):
    hdrs = dict(VALIDATOR_HEADERS)
    if headers:
        hdrs.update(headers)
    resp = _session().get(url, headers=hdrs, stream=True, timeout=timeout, allow_redirects=True)
    try:
        code = resp.status_code
        data = _read_limited(resp, limit) if code < 400 else b""
        return code, data, resp.headers.get("Content-Type", "").lower(), resp.url
    finally:
        resp.close()


def _status_probe(code: int) -> _Probe | None:
    if code in (401, 403):
        return _Probe(False, f"http_{code}_access_restricted")
    if code in (404, 410):
        return _Probe(False, f"http_{code}_not_found")
    if code == 429 or code >= 500:
        return _Probe(False, f"http_{code}", retry=True)
    if code >= 400:
        return _Probe(False, f"http_{code}")
    return None


DRM_KEY_RE = re.compile(r'METHOD=SAMPLE-AES|KEYFORMAT="[^"]*(?:widevine|fairplay|playready|streamingkeydelivery)', re.I)


def _probe_playlist(text: str, base: str, timeout: tuple, depth: int = 0) -> _Probe:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if any(DRM_KEY_RE.search(ln) for ln in lines if ln.startswith("#EXT-X-KEY") or ln.startswith("#EXT-X-SESSION-KEY")):
        return _Probe(False, "drm_protected")

    if depth < 3 and any(ln.startswith("#EXT-X-STREAM-INF") for ln in lines):
        for i, ln in enumerate(lines):
            if ln.startswith("#EXT-X-STREAM-INF"):
                for nxt in lines[i + 1:]:
                    if not nxt.startswith("#"):
                        variant = urljoin(base, nxt)
                        code, data, ctype, final = _http_get(variant, timeout, 1_000_000)
                        bad = _status_probe(code)
                        if bad:
                            return _Probe(False, "variant_" + bad.reason, bad.retry)
                        return _probe_playlist(data.decode("utf-8", "replace"), final, timeout, depth + 1)
                break
        return _Probe(False, "master_without_variant")

    segments = [ln for ln in lines if not ln.startswith("#")]
    if not segments:
        return _Probe(False, "playlist_has_no_segments")
    seg_url = urljoin(base, segments[-1])
    if depth < 3 and urlparse(seg_url).path.lower().endswith(".m3u8"):
        code, data, ctype, final = _http_get(seg_url, timeout, 1_000_000)
        bad = _status_probe(code)
        if bad:
            return _Probe(False, "nested_" + bad.reason, bad.retry)
        return _probe_playlist(data.decode("utf-8", "replace"), final, timeout, depth + 1)

    code, data, ctype, _ = _http_get(seg_url, timeout, 4096, {"Range": "bytes=0-4095"})
    if code not in (200, 206):
        bad = _status_probe(code)
        return _Probe(False, "segment_" + (bad.reason if bad else f"http_{code}"), bool(bad and bad.retry))
    if not data:
        return _Probe(False, "segment_empty")
    if data[:5].lower().startswith((b"<!doc", b"<html", b"<?xml")) or "text/html" in ctype:
        return _Probe(False, "segment_is_html")
    if urlparse(seg_url).path.lower().endswith(".ts") and len(data) >= 188 and data[0] != 0x47:
        return _Probe(False, "segment_invalid_mpegts")
    return _Probe(True, "hls_segment_ok")


def _probe_once(url: str, timeout: tuple) -> _Probe:
    code, data, ctype, final = _http_get(url, timeout, 1_000_000)
    bad = _status_probe(code)
    if bad:
        return bad
    head = data[:2048].lstrip()
    if head.startswith(b"#EXTM3U") or "mpegurl" in ctype or urlparse(final).path.lower().endswith(".m3u8"):
        if not head.startswith(b"#EXTM3U"):
            return _Probe(False, "not_a_valid_playlist")
        return _probe_playlist(data.decode("utf-8", "replace"), final, timeout)
    if head.startswith(b"<?xml") and b"<MPD" in data[:4096]:
        if b"ContentProtection" in data:
            return _Probe(False, "drm_protected")
        return _Probe(True, "dash_manifest_ok")
    if not data:
        return _Probe(False, "empty_response", retry=True)
    if head[:5].lower().startswith((b"<!doc", b"<html")) or "text/html" in ctype:
        return _Probe(False, "html_response")
    return _Probe(True, "raw_stream_bytes_ok")


def check_stream(url: str, connect_timeout: float = 6, read_timeout: float = 10, retries: int = 2) -> dict:
    """Stream যাচাই: Playlist → Variant → Segment (সম্ভব হলে) পর্যন্ত; শুধু HTTP 200 যথেষ্ট নয়।"""
    started = time.monotonic()
    result = _Probe(False, "unknown_error")
    for attempt in range(retries + 1):
        try:
            result = _probe_once(url, (connect_timeout, read_timeout))
        except requests.exceptions.Timeout:
            result = _Probe(False, "timeout", retry=True)
        except requests.exceptions.SSLError:
            result = _Probe(False, "ssl_error")
        except requests.exceptions.TooManyRedirects:
            result = _Probe(False, "too_many_redirects")
        except requests.exceptions.ConnectionError as exc:
            result = _Probe(False, f"connection_error: {type(exc).__name__}", retry=True)
        except requests.exceptions.RequestException as exc:
            result = _Probe(False, f"request_error: {type(exc).__name__}")
        except Exception as exc:  # noqa: BLE001 - validator কখনো পুরো রান ভাঙবে না
            result = _Probe(False, f"validator_error: {type(exc).__name__}")
        if result.ok or not result.retry or attempt >= retries:
            break
        time.sleep(1.5 * (attempt + 1))
    return {
        "status": "ok" if result.ok else "fail",
        "reason": result.reason,
        "latency_ms": int((time.monotonic() - started) * 1000),
    }


def validate_channels(channels: list[Channel], settings: dict, now_dt: datetime) -> dict:
    by_url: dict[str, list[dict]] = {}
    for ch in channels:
        for st in ch.streams:
            by_url.setdefault(st["url"], []).append(st)

    recheck = int(settings["recheck_ok_minutes"]) * 60
    todo = []
    for url, sts in by_url.items():
        st = sts[0]
        checked = parse_iso(st.get("last_checked"))
        age = (now_dt - checked).total_seconds() if checked else None
        if st["status"] == "ok" and age is not None and age < recheck:
            continue
        rank = 0 if checked is None else (1 if st["status"] == "fail" else 2)
        todo.append((rank, age if age is not None else 1e12, url))
    todo.sort(key=lambda t: (t[0], -t[1]))

    deadline = time.monotonic() + float(settings["validation_time_budget_seconds"])

    def worker(url: str):
        if time.monotonic() > deadline:
            return url, None
        return url, check_stream(
            url, float(settings["connect_timeout"]), float(settings["read_timeout"]),
            int(settings["validation_retries"]),
        )

    results: dict[str, dict] = {}
    with cf.ThreadPoolExecutor(max_workers=int(settings["validation_workers"])) as ex:
        for url, res in ex.map(worker, [t[2] for t in todo]):
            if res is not None:
                results[url] = res

    checked_n = len(results)
    failed_n = sum(1 for r in results.values() if r["status"] == "fail")
    summary = {"candidates": len(todo), "checked": checked_n, "failed": failed_n,
               "skipped_time_budget": len(todo) - checked_n, "network_anomaly": False}

    # সব/প্রায় সব Stream ব্যর্থ হলে এটি সম্ভবত Runner-এর নেটওয়ার্ক সমস্যা; ফলাফল প্রয়োগ করা হবে না।
    if checked_n >= 20 and failed_n / checked_n > 0.95:
        log.warning("%d/%d checks failed; treating as network anomaly and keeping previous state", failed_n, checked_n)
        summary["network_anomaly"] = True
        return summary

    stamp = iso(now_dt)
    for url, res in results.items():
        for st in by_url[url]:
            st["last_checked"] = stamp
            st["status"] = res["status"]
            st["reason"] = res["reason"]
            st["latency_ms"] = res["latency_ms"]
            if res["status"] == "ok":
                st["last_ok"] = stamp
                st["fail_count"] = 0
            else:
                st["fail_count"] = int(st.get("fail_count", 0)) + 1
    return summary


def is_confirmed_failed(stream: dict, settings: dict) -> bool:
    return stream["status"] == "fail" and int(stream.get("fail_count", 0)) >= int(settings["failure_threshold"])


def is_working(stream: dict, settings: dict) -> bool:
    """যাচাইয়ে কার্যকর। একবারের সাময়িক ব্যর্থতায় (আগে কার্যকর ছিল) Stream সঙ্গে সঙ্গে বাদ যায় না।"""
    if stream["status"] == "ok":
        return True
    return (stream["status"] == "fail" and bool(stream.get("last_ok"))
            and int(stream.get("fail_count", 0)) < int(settings["failure_threshold"]))


def sort_streams(channels: list[Channel], settings: dict) -> None:
    for ch in channels:
        # সর্বশেষ যাচাইয়ে ব্যর্থ Stream পিছনে যায়; নিশ্চিতভাবে ব্যর্থ (পরপর N বার) সবচেয়ে পিছনে। বাকিগুলো Source Priority অনুযায়ী।
        ch.streams.sort(key=lambda s: (2 if is_confirmed_failed(s, settings) else (1 if s["status"] == "fail" else 0), s["priority"]))


# ---------------------------------------------------------------------------
# Output rendering
# ---------------------------------------------------------------------------
def _attr(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").replace('"', "'")).strip()


def channels_to_json(channels: list[Channel]) -> list[dict]:
    order = {c: i for i, c in enumerate(CATEGORY_ORDER)}
    out = []
    for ch in sorted(channels, key=lambda c: (order.get(c.category, 99), c.name.casefold())):
        out.append({
            "id": ch.id, "name": ch.name, "category": ch.category,
            "logo": ch.logo, "tvg_id": ch.tvg_id, "tvg_name": ch.tvg_name or ch.name,
            "original_groups": ch.groups, "streams": ch.streams,
        })
    return out


def render_playlist(channel_dicts: list[dict], settings: dict, mode: str = "direct", base_url: str = "") -> str:
    """mode='direct' → মূল Stream URL; mode='failover' → Proxy URL (base_url/ch/<id>)।"""
    lines = ["#EXTM3U", f"# Generated {iso()} by IPTV-MASTER ({mode})"]
    order = {c: i for i, c in enumerate(CATEGORY_ORDER)}
    ranked = sorted(channel_dicts, key=lambda c: (order.get(c["category"], 99), c["name"].casefold()))
    for ch in ranked:
        streams = ch.get("streams", [])
        if not streams:
            continue
        if mode == "direct" and settings.get("only_working_in_master"):
            streams = [s for s in streams if is_working(s, settings)]
            if not streams:
                continue
        elif settings.get("drop_failed_channels") and all(is_confirmed_failed(s, settings) for s in streams):
            continue
        name = _attr(ch["name"])
        lines.append(
            f'#EXTINF:-1 tvg-id="{_attr(ch.get("tvg_id", ""))}" tvg-name="{_attr(ch.get("tvg_name") or ch["name"])}" '
            f'tvg-logo="{_attr(ch.get("logo", ""))}" group-title="{_attr(ch["category"])}",{name}'
        )
        if mode == "failover":
            lines.append(f"{base_url.rstrip('/')}/ch/{ch['id']}")
        else:
            if settings.get("embed_alternatives_in_master"):
                for n, st in enumerate(streams[1:], start=2):
                    lines.append(f"#ALT-SOURCE:{n}|{st['source_id']}|{st['status']}|{st['url']}")
            lines.append(streams[0]["url"])
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def run(args: argparse.Namespace) -> int:
    config = load_json(Path(args.config), None)
    if not config or not config.get("sources"):
        log.error("config %s missing or has no sources", args.config)
        return 1
    settings = merge_dict(DEFAULT_SETTINGS, config.get("settings"))
    flt = merge_dict(DEFAULT_FILTERS, config.get("filters"))
    if os.environ.get("PROXY_BASE_URL"):
        settings["proxy_base_url"] = os.environ["PROXY_BASE_URL"]
    if args.no_validate or os.environ.get("SKIP_VALIDATION", "").lower() in ("1", "true", "yes"):
        settings["validate"] = False
    name_patterns = [re.compile(p, re.I) for p in flt["blocked_name_patterns"]]

    out_dir = Path(args.out_dir)
    prev = load_json(out_dir / "channels.json", {"meta": {}, "channels": []})
    prev_failed = load_json(out_dir / "failed_sources.json", {"sources": {}})
    prev_channels = prev.get("channels", [])
    prev_state = {st["url"]: st for ch in prev_channels for st in ch.get("streams", [])}
    prev_source_counts = {sid: info.get("raw_entries", 0) for sid, info in prev.get("meta", {}).get("sources", {}).items()}

    now_dt = utcnow()
    now = iso(now_dt)
    sources = sorted([s for s in config["sources"] if s.get("enabled", True)], key=lambda s: s.get("priority", 0))
    seen_ids = set()
    for s in sources:
        if s["id"] in seen_ids:
            log.error("duplicate source id: %s", s["id"])
            return 1
        seen_ids.add(s["id"])

    # ---- 1) fetch ---------------------------------------------------------
    def fetch_one(src: dict):
        try:
            text = fetch_text(src["url"], settings)
            return src, parse_m3u(text, src), None
        except Exception as exc:  # noqa: BLE001
            return src, None, str(exc)

    all_entries: list[Entry] = []
    source_meta: dict[str, dict] = {}
    failed_sources: dict[str, dict] = {}
    with cf.ThreadPoolExecutor(max_workers=max(1, min(8, len(sources)))) as ex:
        fetched = list(ex.map(fetch_one, sources))

    for src, entries, error in fetched:
        sid = src["id"]
        prev_info = prev_failed.get("sources", {}).get(sid, {})
        raw_count = len(entries) if entries is not None else 0
        prev_count = int(prev_source_counts.get(sid, 0))
        if entries is not None and prev_count >= 10 and raw_count < prev_count * float(settings["min_source_shrink_ratio"]):
            error = f"suspicious_shrink ({raw_count} entries vs {prev_count} previously)"
            entries = None

        using_cache = False
        if entries is None:
            entries = entries_from_previous(prev_channels, src)
            using_cache = True
            raw_count = prev_count
            log.warning("source %s failed (%s); using %d cached streams", sid, error, len(entries))
            failed_sources[sid] = {
                "name": src.get("name", sid), "url": src["url"], "status": "failed",
                "last_attempt": now, "last_success": prev_info.get("last_success"),
                "consecutive_failures": int(prev_info.get("consecutive_failures", 0)) + 1,
                "last_error": error, "using_cached_data": True,
            }
        else:
            failed_sources[sid] = {
                "name": src.get("name", sid), "url": src["url"], "status": "ok",
                "last_attempt": now, "last_success": now, "consecutive_failures": 0,
                "last_error": None, "using_cached_data": False,
            }

        excluded: dict[str, int] = {}
        kept = 0
        for entry in entries:
            reason = check_entry(entry, flt, name_patterns)
            if reason:
                excluded[reason] = excluded.get(reason, 0) + 1
                continue
            if not (entry.name or entry.tvg_name):
                excluded["no_name"] = excluded.get("no_name", 0) + 1
                continue
            all_entries.append(entry)
            kept += 1
        source_meta[sid] = {
            "name": src.get("name", sid), "raw_entries": raw_count, "kept": kept,
            "excluded_total": sum(excluded.values()), "excluded_by_reason": excluded,
            "using_cached_data": using_cache,
        }
        log.info("source %-18s raw=%-5d kept=%-5d excluded=%s", sid, raw_count, kept, excluded or "{}")

    # ---- 2) merge / classify ---------------------------------------------
    channels = merge_entries(all_entries, settings, prev_state, now)
    finalize_channels(channels)

    prev_total = len(prev_channels)
    if not channels:
        log.error("no usable channels collected; keeping previous playlist untouched")
        return 1
    if prev_total >= 20 and len(channels) < prev_total * float(settings["min_total_ratio"]):
        log.error("channel count dropped from %d to %d (<%.0f%%); keeping previous playlist",
                  prev_total, len(channels), float(settings["min_total_ratio"]) * 100)
        return 1

    # ---- 3) validate ------------------------------------------------------
    validation = {"enabled": bool(settings["validate"])}
    if settings["validate"]:
        validation.update(validate_channels(channels, settings, now_dt))
        log.info("validation: %s", validation)
    sort_streams(channels, settings)

    # ---- 4) write outputs -------------------------------------------------
    channel_dicts = channels_to_json(channels)
    cat_counts = {c: 0 for c in CATEGORY_ORDER}
    for ch in channel_dicts:
        cat_counts[ch["category"]] += 1
    total_streams = sum(len(c["streams"]) for c in channel_dicts)
    status_counts = {"ok": 0, "fail": 0, "unverified": 0}
    for c in channel_dicts:
        for st in c["streams"]:
            status_counts[st["status"]] = status_counts.get(st["status"], 0) + 1

    meta = {
        "generated_at": now, "category_order": CATEGORY_ORDER,
        "channel_count": len(channel_dicts), "stream_count": total_streams,
        "channels_per_category": cat_counts, "stream_status": status_counts,
        "validation": validation, "sources": source_meta,
    }
    write_atomic(out_dir / "channels.json", json.dumps({"meta": meta, "channels": channel_dicts}, ensure_ascii=False, indent=2) + "\n")
    write_atomic(out_dir / "master.m3u", render_playlist(channel_dicts, settings, "direct"))
    if settings["proxy_base_url"]:
        write_atomic(out_dir / "master_failover.m3u",
                     render_playlist(channel_dicts, settings, "failover", settings["proxy_base_url"]))

    failed_streams = [
        {"channel_id": c["id"], "channel": c["name"], "source_id": st["source_id"], "url": st["url"],
         "reason": st["reason"], "fail_count": st["fail_count"], "last_checked": st["last_checked"],
         "confirmed_failed": is_confirmed_failed(st, settings)}
        for c in channel_dicts for st in c["streams"] if st["status"] == "fail"
    ]
    write_atomic(out_dir / "failed_sources.json", json.dumps(
        {"updated_at": now, "sources": failed_sources, "failed_streams": failed_streams},
        ensure_ascii=False, indent=2) + "\n")

    log.info("done: %d channels, %d streams (ok=%d fail=%d unverified=%d)",
             len(channel_dicts), total_streams, status_counts["ok"], status_counts["fail"], status_counts["unverified"])
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="IPTV Master Playlist generator")
    parser.add_argument("--config", default=str(BASE_DIR / "sources.json"))
    parser.add_argument("--out-dir", default=str(BASE_DIR))
    parser.add_argument("--no-validate", action="store_true", help="Stream যাচাই বাদ দিন")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        return run(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
