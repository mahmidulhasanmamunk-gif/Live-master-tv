import hashlib
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

SOURCES: List[Dict[str, Any]] = [
    {
        "id": "source_1",
        "priority": 1,
        "name": "Mrgify-BDIX-IPTV",
        "url": "https://raw.githubusercontent.com/abusaeeidx/Mrgify-BDIX-IPTV/main/playlist.m3u",
    },
    {
        "id": "source_2",
        "priority": 2,
        "name": "T-Sports-Playlist-Auto-Update",
        "url": "https://raw.githubusercontent.com/abusaeeidx/T-Sports-Playlist-Auto-Update/refs/heads/main/universal_player.m3u",
    },
    {
        "id": "source_3",
        "priority": 3,
        "name": "Ayna-Playlists-free-Version",
        "url": "https://raw.githubusercontent.com/abusaeeidx/Ayna-Playlists-free-Version/refs/heads/main/playlist.m3u",
    },
    {
        "id": "source_4",
        "priority": 4,
        "name": "toffee-bd",
        "url": "https://raw.githubusercontent.com/srhady/toffee-bd/refs/heads/main/toffee_playlist.m3u",
    },
]

CATEGORY_ORDER: List[str] = [
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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MASTER_M3U_PATH = os.path.join(BASE_DIR, "master.m3u")
CHANNELS_JSON_PATH = os.path.join(BASE_DIR, "channels.json")
FAILED_SOURCES_JSON_PATH = os.path.join(BASE_DIR, "failed_sources.json")

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "*/*",
}

STREAM_CONTENT_TYPES = (
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "audio/mpegurl",
    "audio/x-mpegurl",
    "video/mp2t",
    "video/mp4",
    "video/webm",
    "video/ogg",
    "application/octet-stream",
    "binary/octet-stream",
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def create_session(retries: int = 3, backoff_factor: float = 1.0) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=retries,
        read=retries,
        connect=retries,
        backoff_factor=backoff_factor,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update(DEFAULT_HEADERS)
    return session


def load_json_safe(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                return default
            return json.loads(content)
    except Exception:
        return default


def save_json_atomic(path: str, data: Any) -> None:
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def save_text_atomic(path: str, text: str) -> None:
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp_path, path)


def fetch_playlist_text(
    session: requests.Session, source: Dict[str, Any], timeout: int = 20
) -> Tuple[Optional[str], Optional[str]]:
    url = source["url"]
    last_error = None
    for attempt in range(1, 4):
        try:
            resp = session.get(url, timeout=timeout)
            if resp.status_code == 200 and resp.text and "#EXTM3U" in resp.text:
                return resp.text, None
            if resp.status_code == 200 and resp.text and "#EXTINF" in resp.text:
                return resp.text, None
            last_error = f"HTTP {resp.status_code} or invalid M3U content"
        except Exception as exc:
            last_error = str(exc)
        if attempt < 3:
            time.sleep(attempt * 1.5)
    return None, last_error or "Unknown fetch error"


def extract_attr(extinf_line: str, attr_name: str) -> str:
    pattern = rf'{re.escape(attr_name)}\s*=\s*"([^"]*)"'
    match = re.search(pattern, extinf_line, flags=re.IGNORECASE)
    if match:
        return match.group(1).strip()
    pattern_single = rf"{re.escape(attr_name)}\s*=\s*'([^']*)'"
    match_single = re.search(pattern_single, extinf_line, flags=re.IGNORECASE)
    return match_single.group(1).strip() if match_single else ""


def parse_m3u_entries(
    m3u_text: str, source: Dict[str, Any]
) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    lines = m3u_text.splitlines()
    current_extinf: Optional[str] = None
    current_vlc_opts: List[str] = []

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("#EXTINF:"):
            current_extinf = line
            current_vlc_opts = []
        elif line.startswith("#EXTVLCOPT:"):
            if current_extinf is not None:
                current_vlc_opts.append(line)
        elif line.startswith("#"):
            continue
        else:
            if current_extinf is not None:
                stream_url = line
                if not (stream_url.startswith("http://") or stream_url.startswith("https://")):
                    current_extinf = None
                    current_vlc_opts = []
                    continue

                tvg_id = extract_attr(current_extinf, "tvg-id")
                tvg_name = extract_attr(current_extinf, "tvg-name")
                tvg_logo = extract_attr(current_extinf, "tvg-logo")
                group_title = extract_attr(current_extinf, "group-title")

                after_comma = current_extinf.split(",", 1)[1] if "," in current_extinf else ""
                channel_name = re.sub(r"\s+", " ", after_comma).strip()
                if not channel_name:
                    channel_name = tvg_name or tvg_id or "Unknown Channel"

                entries.append(
                    {
                        "source_id": source["id"],
                        "source_priority": source["priority"],
                        "source_name": source["name"],
                        "channel_name": channel_name,
                        "tvg_id": tvg_id,
                        "tvg_name": tvg_name,
                        "tvg_logo": tvg_logo,
                        "group_title": group_title,
                        "stream_url": stream_url,
                        "vlc_opts": list(current_vlc_opts),
                    }
                )
                current_extinf = None
                current_vlc_opts = []

    return entries


def normalize_channel_name(name: str) -> str:
    text = name.lower().strip()
    text = text.replace("&", " and ").replace("+", " plus ")
    # Remove common quality / region / live tags that shouldn't split identical channels
    noise_patterns = [
        r"\b(fhd|uhd|4k|1080p|720p|480p|hd|sd|hq|lq|hevc|h265|h264|60fps|50fps|30fps)\b",
        r"\b(live|stream|feed|backup|alt|server\s*\d*|bd|bangladesh|bengali|bangla)\b",
        r"\[[^\]]*\]",
        r"\([^\)]*\)",
    ]
    for pat in noise_patterns:
        text = re.sub(pat, " ", text, flags=re.IGNORECASE)
    text = re.sub(r"[^a-z0-9\u0980-\u09ff\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_numbers(text: str) -> List[str]:
    return re.findall(r"\d+", text.lower())


def is_same_channel(entry: Dict[str, Any], existing: Dict[str, Any]) -> bool:
    e_id = (entry.get("tvg_id") or "").strip().lower()
    x_id = (existing.get("tvg_id") or "").strip().lower()

    # Exact tvg-id match (ignore generic placeholder IDs)
    generic_ids = {"", "none", "null", "na", "n/a", "live", "tv"}
    if e_id and x_id and e_id not in generic_ids and x_id not in generic_ids:
        if e_id == x_id:
            # Guard against mismatched channel numbers if names clearly differ
            e_nums = extract_numbers(entry.get("channel_name", ""))
            x_nums = extract_numbers(existing.get("channel_name", ""))
            if e_nums and x_nums and e_nums != x_nums:
                return False
            return True

    norm_a = normalize_channel_name(entry.get("channel_name") or entry.get("tvg_name") or "")
    norm_b = normalize_channel_name(existing.get("name") or existing.get("tvg_name") or "")

    if not norm_a or not norm_b:
        return False

    # Prevent merging distinct numbered channels (e.g., Sony Ten 1 vs Sony Ten 2, Star Plus vs Star Gold)
    nums_a = extract_numbers(entry.get("channel_name", ""))
    nums_b = extract_numbers(existing.get("name", ""))
    if nums_a != nums_b:
        return False

    if norm_a == norm_b:
        return True

    # Token-level check to prevent false positives on short names
    tokens_a = set(norm_a.split())
    tokens_b = set(norm_b.split())
    if len(tokens_a) >= 2 and len(tokens_b) >= 2:
        overlap = len(tokens_a & tokens_b) / max(len(tokens_a), len(tokens_b))
        ratio = SequenceMatcher(None, norm_a, norm_b).ratio()
        if ratio >= 0.88 and overlap >= 0.67:
            return True

    return False


def classify_category(
    channel_name: str, group_title: str, tvg_name: str, tvg_id: str
) -> str:
    combined = f"{channel_name} {tvg_name} {tvg_id} {group_title}".lower()
    grp = group_title.lower()

    bangla_news_kw = [
        "somoy", "jamuna", "channel 24", "ch 24", "71 tv", "ekattor", "dbc news",
        "independent tv", "independent", "rtv", "ntv", "atn news", "desh tv",
        "boishakhi", "my tv", "mytv", "ekushey", "banglavision", "sa tv", "satv",
        "mohona", "asian tv", "channel i", "news24", "news 24", "ekhon", "ekhon tv",
        "nagorik", "green tv", "globaltv", "global tv", "bangla news", "bd news",
        "সময়", "যমুনা", "একাত্তর", "চ্যানেল ২৪", "চ্যানেল আই", "এনটিভি", "আরটিভি",
        "দেশ টিভি", "ইন্ডিপেন্ডেন্ট", "ডিবিসি", "একুশে", "বাংলাভিশন", "নাগরিক", "এখন",
    ]
    bd_indicator = any(
        k in combined
        for k in [
            "bangladesh", "bd", "bangla", "bengali", "dhaka", "baksal", "bti",
            "titas", "ayna", "toffee", "bdix", "বাংলা", "দেশ",
        ]
    )

    is_news = any(
        k in combined
        for k in [
            "news", "সংবাদ", "বার্তা", "cnn", "bbc", "al jazeera", "aljazeera",
            "wion", "ndtv", "aaj tak", "republic", "sky news", "france 24",
            "dw", "trt world", "cnbc", "bloomberg", "fox news", "msnbc", "euronews",
        ]
    )

    if is_news and (any(k in combined for k in bangla_news_kw) or bd_indicator or "bangla" in grp):
        return "Bangla News"

    sports_kw = [
        "t sports", "tsports", "t-sports", "gazi tv", "gtv", "star sports",
        "sony ten", "sony six", "ten sports", "willow", "espn", "sky sports",
        "bein sports", "bt sport", "tnt sports", "fox sports", "supersport",
        "astro cricket", "ptv sports", "a sports", "cric", "cricket", "football",
        "soccer", "sports", "sport", "ufc", "wwe", "f1", "formula 1", "motogp",
        "ipl", "bpl", "psl", "cpl", "fifa", "uefa", "premier league", "la liga",
    ]
    is_sports = any(k in combined for k in sports_kw) or "sport" in grp

    if is_sports:
        if bd_indicator or any(k in combined for k in ["t sports", "tsports", "gazi", "gtv", "bpl"]):
            return "Sports"
        # Put major sports under Sports (2) as requested: News then Sports, plus International Sports (6)
        if any(k in grp for k in ["international", "world", "global"]):
            return "International Sports"
        return "Sports"

    kids_kw = [
        "kids", "cartoon", "nick", "nickelodeon", "disney", "pogo", "sonic",
        "baby", "junior", "cbeebies", "boomerang", "animax", "toonami",
        "kid", "কিশোর", "শিশু",
    ]
    if any(k in combined for k in kids_kw) or "kid" in grp or "cartoon" in grp:
        return "Kids"

    doc_kw = [
        "documentary", "discovery", "national geographic", "nat geo", "natgeo",
        "history", "animal planet", "bbc earth", "love nature", "science",
        "wild", "nature",
    ]
    if any(k in combined for k in doc_kw) or "documentary" in grp:
        return "Documentary"

    music_kw = [
        "music", "mtv", "9xm", "9x jalwa", "b4u music", "zoom", "vh1",
        "song", "songs", "গান", "মিউজিক",
    ]
    if any(k in combined for k in music_kw) or "music" in grp:
        return "Music"

    movies_kw = [
        "movie", "movies", "cinema", "film", "films", "hbo", "star gold",
        "sony max", "zee cinema", "&pictures", "and pictures", "mnx", "romedy",
        "flix", "action", "bollywood", "hollywood", "tollywood", "বিনোদন সিনেমা",
    ]
    if any(k in combined for k in movies_kw) or "movie" in grp or "cinema" in grp:
        return "Movies"

    if is_news:
        return "International News"

    bd_ent_kw = [
        "star jalsha", "jalsha", "zee bangla", "sun bangla", "colors bangla",
        "akasha", "rupashi bangla", "bangla entertainment",
    ]
    if bd_indicator or any(k in combined for k in bd_ent_kw):
        if any(
            k in combined
            for k in [
                "btv", "bangladesh television", "sangbad", "sansad", "general",
                "বিটিভি", "সংসদ",
            ]
        ):
            return "Bangladeshi General TV"
        if any(k in grp for k in ["general", "govt", "state"]):
            return "Bangladeshi General TV"
        return "Bangladeshi Entertainment"

    if any(
        k in combined
        for k in [
            "star plus", "zee tv", "sony", "colors", "star bharat", "&tv",
            "and tv", "sab tv", "sony sab", "zee cafe", "star world", "axn",
            "comedy central", "entertainment", "tv",
        ]
    ):
        return "International Entertainment"

    return "Others"


def validate_single_stream(
    stream_url: str, timeout: int = 8, retries: int = 2
) -> Dict[str, Any]:
    checked_at = utc_now_iso()
    session = create_session(retries=1, backoff_factor=0.3)
    last_reason = "Unreachable"

    for attempt in range(1, retries + 1):
        try:
            # First try HEAD with small fallback to GET with stream=True
            resp = session.head(stream_url, timeout=timeout, allow_redirects=True)
            if resp.status_code in (403, 405, 501) or resp.status_code >= 500:
                resp.close()
                resp = session.get(stream_url, timeout=timeout, stream=True, allow_redirects=True)

            status_code = resp.status_code
            content_type = (resp.headers.get("Content-Type") or "").lower()

            if 200 <= status_code < 400:
                # Read up to 2KB to verify actual stream/playlist content rather than empty/error HTML
                chunk = b""
                try:
                    for part in resp.iter_content(chunk_size=1024):
                        if part:
                            chunk += part
                        if len(chunk) >= 2048:
                            break
                except Exception:
                    pass
                resp.close()

                decoded_sample = chunk.decode("utf-8", errors="ignore").strip()
                is_m3u8 = ".m3u8" in stream_url.lower() or "#EXTM3U" in decoded_sample
                is_stream_type = any(ct in content_type for ct in STREAM_CONTENT_TYPES)

                if is_m3u8:
                    # Ensure playlist isn't an empty or error page
                    if "#EXTINF" in decoded_sample or "#EXT-X-STREAM-INF" in decoded_sample or len(decoded_sample) > 10:
                        return {
                            "valid": True,
                            "status_code": status_code,
                            "content_type": content_type,
                            "reason": "Verified HLS/M3U8 stream",
                            "checked_at": checked_at,
                        }
                elif is_stream_type and len(chunk) > 0:
                    return {
                        "valid": True,
                        "status_code": status_code,
                        "content_type": content_type,
                        "reason": "Verified media stream response",
                        "checked_at": checked_at,
                    }
                elif len(chunk) > 0 and "text/html" not in content_type:
                    return {
                        "valid": True,
                        "status_code": status_code,
                        "content_type": content_type,
                        "reason": "Reachable non-HTML stream endpoint",
                        "checked_at": checked_at,
                    }
                else:
                    last_reason = f"HTTP {status_code} with unexpected/empty body ({content_type})"
            else:
                resp.close()
                last_reason = f"HTTP {status_code}"
        except requests.Timeout:
            last_reason = "Timeout"
        except requests.ConnectionError:
            last_reason = "Connection error"
        except Exception as exc:
            last_reason = str(exc)[:120]

        if attempt < retries:
            time.sleep(0.5)

    return {
        "valid": False,
        "status_code": None,
        "content_type": "",
        "reason": last_reason,
        "checked_at": checked_at,
    }


def build_channel_key(entry: Dict[str, Any]) -> str:
    tvg_id = (entry.get("tvg_id") or "").strip().lower()
    norm_name = normalize_channel_name(entry.get("channel_name") or entry.get("tvg_name") or "")
    raw_basis = f"{tvg_id}|{norm_name}" if tvg_id else norm_name
    if not raw_basis:
        raw_basis = entry.get("stream_url", "")
    return hashlib.sha1(raw_basis.encode("utf-8")).hexdigest()[:12]


def merge_entries(
    parsed_entries: List[Dict[str, Any]],
    previous_channels_map: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    # Sort parsed entries by source priority first so Source 1 comes first
    sorted_entries = sorted(parsed_entries, key=lambda x: (x["source_priority"], x["channel_name"]))
    merged_list: List[Dict[str, Any]] = []

    for entry in sorted_entries:
        matched_channel: Optional[Dict[str, Any]] = None
        for ch in merged_list:
            if is_same_channel(entry, ch):
                matched_channel = ch
                break

        if matched_channel is None:
            key = build_channel_key(entry)
            prev_ch = previous_channels_map.get(key)
            category = classify_category(
                entry["channel_name"],
                entry["group_title"],
                entry["tvg_name"],
                entry["tvg_id"],
            )
            if prev_ch and prev_ch.get("category") in CATEGORY_ORDER and not entry["group_title"]:
                category = prev_ch["category"]

            matched_channel = {
                "id": key,
                "name": entry["channel_name"],
                "tvg_id": entry["tvg_id"],
                "tvg_name": entry["tvg_name"] or entry["channel_name"],
                "tvg_logo": entry["tvg_logo"],
                "category": category,
                "group_title": category,
                "raw_group_titles": [entry["group_title"]] if entry["group_title"] else [],
                "primary_url": entry["stream_url"],
                "primary_source": entry["source_name"],
                "primary_source_id": entry["source_id"],
                "sources": [],
            }
            merged_list.append(matched_channel)
        else:
            if not matched_channel["tvg_id"] and entry["tvg_id"]:
                matched_channel["tvg_id"] = entry["tvg_id"]
            if not matched_channel["tvg_logo"] and entry["tvg_logo"]:
                matched_channel["tvg_logo"] = entry["tvg_logo"]
            if entry["group_title"] and entry["group_title"] not in matched_channel["raw_group_titles"]:
                matched_channel["raw_group_titles"].append(entry["group_title"])

        # Add source stream if not already present for this source_id + stream_url
        existing_urls = {s["url"] for s in matched_channel["sources"]}
        if entry["stream_url"] not in existing_urls:
            matched_channel["sources"].append(
                {
                    "source_id": entry["source_id"],
                    "source_name": entry["source_name"],
                    "priority": entry["source_priority"],
                    "url": entry["stream_url"],
                    "vlc_opts": entry["vlc_opts"],
                    "verified": False,
                    "status": "unverified",
                    "last_checked": None,
                    "last_error": None,
                    "consecutive_failures": 0,
                }
            )

    # Sort sources inside each channel by priority (Source 1 -> Source 2 -> Source 3 -> Source 4)
    for ch in merged_list:
        ch["sources"].sort(key=lambda s: (s["priority"], s["source_id"]))
        if ch["sources"]:
            ch["primary_url"] = ch["sources"][0]["url"]
            ch["primary_source"] = ch["sources"][0]["source_name"]
            ch["primary_source_id"] = ch["sources"][0]["source_id"]

    return merged_list


def apply_previous_state_for_failed_sources(
    merged_list: List[Dict[str, Any]],
    failed_source_ids: List[str],
    previous_channels_list: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if not failed_source_ids or not previous_channels_list:
        return merged_list

    failed_set = set(failed_source_ids)
    prev_by_id = {ch.get("id"): ch for ch in previous_channels_list if ch.get("id")}
    current_by_id = {ch.get("id"): ch for ch in merged_list if ch.get("id")}

    # Restore channels from failed sources that weren't captured from active sources
    for prev_ch in previous_channels_list:
        prev_sources = prev_ch.get("sources", [])
        has_failed_src = any(s.get("source_id") in failed_set for s in prev_sources)
        if not has_failed_src:
            continue

        ch_id = prev_ch.get("id")
        if ch_id in current_by_id:
            target = current_by_id[ch_id]
            existing_urls = {s["url"] for s in target["sources"]}
            for ps in prev_sources:
                if ps.get("source_id") in failed_set and ps.get("url") not in existing_urls:
                    restored_src = dict(ps)
                    restored_src["status"] = "carried_over_source_unavailable"
                    target["sources"].append(restored_src)
                    existing_urls.add(ps.get("url"))
            target["sources"].sort(key=lambda s: (s["priority"], s["source_id"]))
        else:
            restored_ch = json.loads(json.dumps(prev_ch))
            for s in restored_ch.get("sources", []):
                if s.get("source_id") in failed_set:
                    s["status"] = "carried_over_source_unavailable"
            merged_list.append(restored_ch)
            current_by_id[ch_id] = restored_ch

    return merged_list


def validate_streams_concurrently(
    channels: List[Dict[str, Any]],
    previous_channels_list: List[Dict[str, Any]],
    max_workers: int = 24,
    max_validations_per_run: int = 600,
) -> List[Dict[str, Any]]:
    # Build lookup of previous validation stats per URL
    prev_url_state: Dict[str, Dict[str, Any]] = {}
    for ch in previous_channels_list:
        for s in ch.get("sources", []):
            u = s.get("url")
            if u:
                prev_url_state[u] = s

    tasks: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for ch in channels:
        for src in ch.get("sources", []):
            prev = prev_url_state.get(src["url"])
            if prev:
                src["consecutive_failures"] = int(prev.get("consecutive_failures", 0))
            tasks.append((ch, src))

    # Prioritize primary streams and source 1-4 streams first within runtime budget
    tasks.sort(key=lambda item: (item[1]["priority"], 0 if item[1]["url"] == item[0]["primary_url"] else 1))
    selected_tasks = tasks[:max_validations_per_run]
    deferred_tasks = tasks[max_validations_per_run:]

    # Carry forward previous status for deferred items without marking unverified as working
    for _, src in deferred_tasks:
        prev = prev_url_state.get(src["url"])
        if prev and prev.get("last_checked"):
            src["verified"] = bool(prev.get("verified", False))
            src["status"] = prev.get("status", "unverified")
            src["last_checked"] = prev.get("last_checked")
            src["last_error"] = prev.get("last_error")
            src["consecutive_failures"] = int(prev.get("consecutive_failures", 0))
        else:
            src["verified"] = False
            src["status"] = "unverified"
            src["last_checked"] = None
            src["last_error"] = "Not yet validated"

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(validate_single_stream, src["url"]): (ch, src)
            for ch, src in selected_tasks
        }
        for future in as_completed(future_map):
            ch, src = future_map[future]
            result = future.result()
            src["last_checked"] = result["checked_at"]

            if result["valid"]:
                src["verified"] = True
                src["status"] = "active"
                src["last_error"] = None
                src["consecutive_failures"] = 0
            else:
                src["verified"] = False
                src["consecutive_failures"] = int(src.get("consecutive_failures", 0)) + 1
                src["last_error"] = result["reason"]
                # Do not immediately delete temporarily down streams; mark degraded/temporary_down
                if src["consecutive_failures"] >= 5:
                    src["status"] = "inactive"
                else:
                    src["status"] = "temporary_down"

    # Choose best working/available stream in priority order (Source 1 -> Source 4)
    for ch in channels:
        sources = ch.get("sources", [])
        sources.sort(key=lambda s: (s["priority"], s["source_id"]))

        verified_sources = [s for s in sources if s.get("verified") is True]
        carried_or_unverified = [
            s for s in sources if s.get("status") not in ("inactive",)
        ]

        if verified_sources:
            best = verified_sources[0]
        elif carried_or_unverified:
            best = carried_or_unverified[0]
        elif sources:
            best = sources[0]
        else:
            best = None

        if best:
            ch["primary_url"] = best["url"]
            ch["primary_source"] = best["source_name"]
            ch["primary_source_id"] = best["source_id"]
            ch["stream_verified"] = bool(best.get("verified", False))
            ch["stream_status"] = best.get("status", "unverified")

    return channels


def sort_channels_by_category(channels: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    order_index = {cat: idx for idx, cat in enumerate(CATEGORY_ORDER)}
    return sorted(
        channels,
        key=lambda ch: (
            order_index.get(ch.get("category", "Others"), len(CATEGORY_ORDER) - 1),
            ch.get("name", "").lower(),
        ),
    )


def escape_m3u_attr(value: str) -> str:
    return (value or "").replace('"', "'").replace("\n", " ").replace("\r", " ").strip()


def generate_master_m3u(channels: List[Dict[str, Any]]) -> str:
    lines: List[str] = [
        "#EXTM3U",
        f"#PLAYLIST:IPTV-MASTER Automatic Multi-Source Playlist (Updated {utc_now_iso()})",
    ]

    for ch in channels:
        if not ch.get("primary_url"):
            continue

        tvg_id = escape_m3u_attr(ch.get("tvg_id", ""))
        tvg_name = escape_m3u_attr(ch.get("tvg_name") or ch.get("name", ""))
        tvg_logo = escape_m3u_attr(ch.get("tvg_logo", ""))
        group_title = escape_m3u_attr(ch.get("category") or "Others")
        display_name = escape_m3u_attr(ch.get("name") or "Channel")

        alt_sources = [
            f"{s['source_name']} (P{s['priority']}): {s['url']}"
            for s in ch.get("sources", [])
        ]
        if alt_sources:
            lines.append(f"#EXT-ALT-SOURCES: {' | '.join(alt_sources)}")

        extinf = (
            f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-name="{tvg_name}" '
            f'tvg-logo="{tvg_logo}" group-title="{group_title}",{display_name}'
        )
        lines.append(extinf)

        # Include VLC options from chosen primary source if available
        primary_url = ch["primary_url"]
        for s in ch.get("sources", []):
            if s.get("url") == primary_url:
                for opt in s.get("vlc_opts", []):
                    lines.append(opt)
                break

        lines.append(primary_url)

    return "\n".join(lines) + "\n"


def build_failed_sources_report(
    source_results: List[Dict[str, Any]],
    channels: List[Dict[str, Any]],
) -> Dict[str, Any]:
    broken_streams: List[Dict[str, Any]] = []
    for ch in channels:
        for s in ch.get("sources", []):
            if not s.get("verified", False):
                broken_streams.append(
                    {
                        "channel_id": ch.get("id"),
                        "channel_name": ch.get("name"),
                        "category": ch.get("category"),
                        "source_id": s.get("source_id"),
                        "source_name": s.get("source_name"),
                        "priority": s.get("priority"),
                        "url": s.get("url"),
                        "status": s.get("status"),
                        "consecutive_failures": s.get("consecutive_failures", 0),
                        "last_error": s.get("last_error"),
                        "last_checked": s.get("last_checked"),
                    }
                )

    failed_playlists = [r for r in source_results if not r.get("success")]

    return {
        "updated_at": utc_now_iso(),
        "failed_playlists_count": len(failed_playlists),
        "failed_playlists": failed_playlists,
        "unverified_or_failed_streams_count": len(broken_streams),
        "unverified_or_failed_streams": broken_streams,
    }


def main() -> int:
    start_time = time.time()
    print(f"[{utc_now_iso()}] Starting Automatic IPTV Master Playlist Update...")

    prev_channels_data = load_json_safe(CHANNELS_JSON_PATH, {"channels": []})
    previous_channels_list: List[Dict[str, Any]] = (
        prev_channels_data.get("channels", [])
        if isinstance(prev_channels_data, dict)
        else []
    )
    previous_channels_map = {
        ch["id"]: ch for ch in previous_channels_list if isinstance(ch, dict) and ch.get("id")
    }

    session = create_session(retries=3, backoff_factor=1.0)
    all_parsed_entries: List[Dict[str, Any]] = []
    source_results: List[Dict[str, Any]] = []
    failed_source_ids: List[str] = []

    for src in SOURCES:
        print(f"Fetching {src['name']} ({src['url']})...")
        text, err = fetch_playlist_text(session, src)
        if text:
            parsed = parse_m3u_entries(text, src)
            print(f"  -> Collected {len(parsed)} entries from {src['name']}.")
            all_parsed_entries.extend(parsed)
            source_results.append(
                {
                    "source_id": src["id"],
                    "source_name": src["name"],
                    "priority": src["priority"],
                    "url": src["url"],
                    "success": True,
                    "entries_found": len(parsed),
                    "error": None,
                    "checked_at": utc_now_iso(),
                }
            )
        else:
            print(f"  -> FAILED {src['name']}: {err}")
            failed_source_ids.append(src["id"])
            source_results.append(
                {
                    "source_id": src["id"],
                    "source_name": src["name"],
                    "priority": src["priority"],
                    "url": src["url"],
                    "success": False,
                    "entries_found": 0,
                    "error": err,
                    "checked_at": utc_now_iso(),
                }
            )

    if not all_parsed_entries and not previous_channels_list:
        print("ERROR: No channels fetched from any source and no previous state available.")
        failed_report = build_failed_sources_report(source_results, [])
        save_json_atomic(FAILED_SOURCES_JSON_PATH, failed_report)
        return 1

    merged_channels = merge_entries(all_parsed_entries, previous_channels_map)
    merged_channels = apply_previous_state_for_failed_sources(
        merged_channels, failed_source_ids, previous_channels_list
    )

    max_workers = int(os.environ.get("STREAM_VALIDATE_WORKERS", "24"))
    max_checks = int(os.environ.get("STREAM_VALIDATE_LIMIT", "600"))
    merged_channels = validate_streams_concurrently(
        merged_channels,
        previous_channels_list,
        max_workers=max_workers,
        max_validations_per_run=max_checks,
    )

    ordered_channels = sort_channels_by_category(merged_channels)

    master_m3u_content = generate_master_m3u(ordered_channels)
    save_text_atomic(MASTER_M3U_PATH, master_m3u_content)

    channels_payload = {
        "updated_at": utc_now_iso(),
        "category_order": CATEGORY_ORDER,
        "total_channels": len(ordered_channels),
        "sources": source_results,
        "channels": ordered_channels,
    }
    save_json_atomic(CHANNELS_JSON_PATH, channels_payload)

    failed_report = build_failed_sources_report(source_results, ordered_channels)
    save_json_atomic(FAILED_SOURCES_JSON_PATH, failed_report)

    elapsed = round(time.time() - start_time, 2)
    print(
        f"[{utc_now_iso()}] Done in {elapsed}s. Total unique channels: {len(ordered_channels)}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
