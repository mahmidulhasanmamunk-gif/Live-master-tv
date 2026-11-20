#!/usr/bin/env python3
"""
Failover Proxy
==============

Static M3U নিজে Failover করতে পারে না। এই ছোট সার্ভারটি আপনার নিজের মেশিনে/সার্ভারে চালান।
Player-কে `http://<server>:8080/master.m3u` দিন। প্রতিটি Channel-এর লিংক হবে `/ch/<id>`।
Player যখন লিংকটি খোলে, সার্ভার Alternative Stream-গুলো ক্রমানুসারে যাচাই করে প্রথম কার্যকর Stream-এ
HTTP 302 Redirect করে। ভিডিও ডেটা এই সার্ভারের ভেতর দিয়ে যায় না, তাই এটি খুব হালকা।

চালানোর উপায়:
  python failover_proxy.py --channels channels.json --port 8080
  python failover_proxy.py --channels https://raw.githubusercontent.com/<user>/<repo>/main/channels.json

Endpoints:
  /master.m3u   Proxy-লিংকসহ Playlist (Host হেডার থেকে স্বয়ংক্রিয় Base URL)
  /ch/<id>      কার্যকর Stream-এ Redirect (কোনোটি না চললে 503)
  /health       স্ট্যাটাস JSON
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import requests

import update

log = logging.getLogger("failover-proxy")


class Catalog:
    def __init__(self, source: str, refresh_seconds: int):
        self.source = source
        self.refresh_seconds = refresh_seconds
        self.channels: dict[str, dict] = {}
        self.ordered: list[dict] = []
        self.loaded_at = 0.0
        self.lock = threading.Lock()
        self.reload()

    def _read(self) -> dict:
        if self.source.startswith(("http://", "https://")):
            resp = requests.get(self.source, timeout=30)
            resp.raise_for_status()
            return resp.json()
        return json.loads(Path(self.source).read_text(encoding="utf-8"))

    def reload(self) -> None:
        try:
            data = self._read()
            channels = data.get("channels", [])
            with self.lock:
                self.ordered = channels
                self.channels = {c["id"]: c for c in channels}
                self.loaded_at = time.time()
            log.info("catalog loaded: %d channels", len(channels))
        except Exception as exc:  # noqa: BLE001
            log.error("catalog reload failed (%s); keeping previous data", exc)

    def get(self, channel_id: str) -> dict | None:
        if time.time() - self.loaded_at > self.refresh_seconds:
            self.reload()
        with self.lock:
            return self.channels.get(channel_id)


class ProbeCache:
    """একই URL বারবার যাচাই এড়াতে ছোট TTL ক্যাশ।"""
    def __init__(self, ok_ttl: int, fail_ttl: int):
        self.ok_ttl, self.fail_ttl = ok_ttl, fail_ttl
        self.data: dict[str, tuple[bool, float]] = {}
        self.lock = threading.Lock()

    def get(self, url: str):
        with self.lock:
            hit = self.data.get(url)
        if not hit:
            return None
        ok, ts = hit
        if time.time() - ts < (self.ok_ttl if ok else self.fail_ttl):
            return ok
        return None

    def put(self, url: str, ok: bool) -> None:
        with self.lock:
            self.data[url] = (ok, time.time())


def pick_stream(channel: dict, cache: ProbeCache, pool: cf.ThreadPoolExecutor, max_candidates: int):
    streams = channel.get("streams", [])[:max_candidates]
    futures = []
    for st in streams:
        url = st["url"]
        cached = cache.get(url)
        if cached is None:
            futures.append((st, pool.submit(update.check_stream, url, 4, 6, 0)))
        else:
            futures.append((st, cached))

    for st, fut in futures:  # মূল অগ্রাধিকার ক্রম বজায় রেখে প্রথম কার্যকরটি নেওয়া হয়
        if isinstance(fut, bool):
            ok = fut
        else:
            try:
                ok = fut.result(timeout=25)["status"] == "ok"
            except Exception:  # noqa: BLE001
                ok = False
            cache.put(st["url"], ok)
        if ok:
            return st
    return None


def make_handler(catalog: Catalog, cache: ProbeCache, pool: cf.ThreadPoolExecutor, args):
    class Handler(BaseHTTPRequestHandler):
        server_version = "IPTVFailover/1.0"

        def log_message(self, fmt, *a):  # noqa: D401
            log.info("%s - %s", self.address_string(), fmt % a)

        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            path = urlparse(self.path).path
            if path == "/health":
                body = json.dumps({"status": "ok", "channels": len(catalog.ordered),
                                   "catalog_loaded_at": catalog.loaded_at}).encode()
                return self._send(200, body, "application/json")

            if path in ("/master.m3u", "/playlist.m3u"):
                base = args.public_base or f"http://{self.headers.get('Host', 'localhost')}"
                with catalog.lock:
                    channels = list(catalog.ordered)
                text = update.render_playlist(channels, {"drop_failed_channels": False}, "failover", base)
                return self._send(200, text.encode("utf-8"), "audio/x-mpegurl; charset=utf-8")

            if path.startswith("/ch/"):
                channel_id = path[4:].split(".")[0].strip("/")
                channel = catalog.get(channel_id)
                if not channel:
                    return self._send(404, b'{"error":"unknown channel"}', "application/json")
                chosen = pick_stream(channel, cache, pool, args.max_candidates)
                if not chosen:
                    return self._send(503, b'{"error":"no working stream"}', "application/json")
                return self._send(302, b"", "text/plain", {"Location": chosen["url"], "Cache-Control": "no-store"})

            return self._send(404, b'{"error":"not found"}', "application/json")

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="IPTV failover redirect proxy")
    parser.add_argument("--channels", default="channels.json", help="channels.json পাথ বা URL")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--refresh", type=int, default=600, help="Catalog রিফ্রেশ (সেকেন্ড)")
    parser.add_argument("--public-base", default="", help="যেমন http://192.168.1.10:8080 (ঐচ্ছিক)")
    parser.add_argument("--max-candidates", type=int, default=6, help="প্রতি Channel-এ সর্বোচ্চ কত Stream যাচাই")
    parser.add_argument("--ok-ttl", type=int, default=60)
    parser.add_argument("--fail-ttl", type=int, default=120)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    catalog = Catalog(args.channels, args.refresh)
    cache = ProbeCache(args.ok_ttl, args.fail_ttl)
    pool = cf.ThreadPoolExecutor(max_workers=16)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(catalog, cache, pool, args))
    log.info("listening on %s:%d", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
