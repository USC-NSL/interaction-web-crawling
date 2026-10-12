#!/usr/bin/env python3
"""Static file server for the interaction corpus, with sentinel request logging.

Serves the corpus root, so pages live under /pages/... and sentinel assets under
/sentinel/.... Every request whose path starts with /sentinel/ is appended to the
request log as one JSON object per line, whether or not the asset exists. That
log is what makes the acceptance tests runnable without Docker or WARC parsing.

A GET or HEAD with a delay=<ms> query parameter is answered after that many
milliseconds (at most 5000), for pages whose data arrives late, as on a real server.
"""

import argparse
import json
import os
import socketserver
import sys
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent
SENTINEL_PREFIX = "/sentinel/"
MAX_DELAY_MS = 5000


class RequestLog:
    """Append-only JSONL log, safe across the server's threads."""

    def __init__(self, path: Path, reset: bool = False):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        if reset:
            self.path.write_text("")

    def append(self, path: str, referer: str, method: str = "GET") -> None:
        record = {"ts": time.time(), "path": path, "referer": referer}
        if method not in ("GET", "HEAD"):
            record["method"] = method          # GET records keep their original shape
        line = json.dumps(record, separators=(",", ":")) + "\n"
        with self._lock:
            with self.path.open("a") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())


class CorpusHandler(SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler plus sentinel logging and no-store caching."""

    def __init__(self, *args, request_log: RequestLog, **kwargs):
        self.request_log = request_log
        super().__init__(*args, **kwargs)

    # -- caching: repeat runs must never be served from cache -----------------

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def send_head(self):
        # Defeat SimpleHTTPRequestHandler's own If-Modified-Since handling, which
        # would otherwise answer 304 and make a real fetch invisible in the log.
        del self.headers["If-Modified-Since"]
        return super().send_head()

    # -- sentinel accounting --------------------------------------------------

    def _is_sentinel(self) -> bool:
        return urlsplit(self.path).path.startswith(SENTINEL_PREFIX)

    def _log_sentinel(self) -> None:
        self.request_log.append(urlsplit(self.path).path, self.headers.get("Referer", ""))

    def _delay(self) -> None:
        values = parse_qs(urlsplit(self.path).query).get("delay")
        if values and values[0].isdigit():
            time.sleep(min(int(values[0]), MAX_DELAY_MS) / 1000)

    def do_GET(self):
        self._delay()
        if self._is_sentinel():
            self._log_sentinel()
            if not self._sentinel_exists():
                self._empty_404()
                return
        super().do_GET()

    def do_HEAD(self):
        self._delay()
        if self._is_sentinel():
            self._log_sentinel()
            if not self._sentinel_exists():
                self._empty_404()
                return
        super().do_HEAD()

    # Writes (POST from fetch/XHR/forms, sendBeacon, PUT/PATCH/DELETE) are logged too:
    # the click-safety page uses them to detect a state-changing request that escaped
    # a crawler's request guard. Every write is answered 204 and changes nothing.
    def _write(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        if self._is_sentinel():
            self.request_log.append(urlsplit(self.path).path, self.headers.get("Referer", ""),
                                    self.command)
        self.send_response(204)
        self.end_headers()

    do_POST = do_PUT = do_PATCH = do_DELETE = _write

    def _sentinel_exists(self) -> bool:
        return Path(self.translate_path(self.path)).is_file()

    def _empty_404(self) -> None:
        # Empty body: a missing sentinel is a bug we want visible in the log,
        # not an error page that could itself be mistaken for content.
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


class ThreadingServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--log", default=str(REPO_ROOT / "logs" / "requests.jsonl"))
    ap.add_argument("--reset", action="store_true", help="truncate the log on start")
    ap.add_argument("--root", default=str(REPO_ROOT), help="directory to serve")
    args = ap.parse_args()

    request_log = RequestLog(Path(args.log), reset=args.reset)
    handler = partial(CorpusHandler, request_log=request_log, directory=args.root)

    with ThreadingServer(("0.0.0.0", args.port), handler) as httpd:
        print(f"serving {args.root} on http://0.0.0.0:{args.port}")
        print(f"logging {SENTINEL_PREFIX}* to {args.log}")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
