#!/usr/bin/env python3
"""Re-screen the screener's pages with the explorer in the crawler's real browser settings.

Pages: every page in page-screener/out/results.json with HTTP 200, no bot challenge
and robots allowed, minus the five pilot pages (their runs/pilot2 explorer results
are reused). Tagged demo (groups A, B, C and the ungrouped library pages) or
production (everything else).

Explorer flags: --match-crawler, --no-block-ads (blocking is applied post hoc by
rescreen_analyse.py, as in the pilot), --all-raw-urls, --acknowledge-skips (the
label rule is fixed and the denylist reviewed; every veto is collected afterwards).

Etiquette as in pilot_run.py: one page at a time, at least MIN_HOST_GAP seconds from
the end of any load of a host to the start of the next, counted across every run
directory's jobs.jsonl; pages ordered round-robin by host; the explorer gets the same
interval for its own reloads.
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
SCREENER = PKG_ROOT.parent / "page-screener"
OUT = PKG_ROOT / "runs" / "rescreen"
MIN_HOST_GAP = 60.0
DEMO_GROUPS = {"A", "B", "C", None}     # None: the ungrouped Bootstrap and Splide pages

sys.path.insert(0, str(SCREENER / "tools"))
from explore import page_id_for  # noqa: E402

PILOT = json.loads((PKG_ROOT / "tools" / "pilot_pages.json").read_text())["pages"]


def select() -> list[dict]:
    rows = json.loads((SCREENER / "out" / "results.json").read_text())
    pilot_urls = {p["url"] for p in PILOT}
    pages = []
    for r in rows:
        a = r.get("access") or {}
        if not (r.get("screened") and a.get("http_status") == 200 and not a.get("bot_challenge")
                and (a.get("robots") or {}).get("allowed")) or r["url"] in pilot_urls:
            continue
        pages.append({"id": page_id_for(r["url"]), "url": r["url"], "group": r.get("group"),
                      "tag": "demo" if r.get("group") in DEMO_GROUPS else "production",
                      "screener_stable": (r.get("stability") or {}).get("stable")})
    return pages


def round_robin(pages: list[dict]) -> list[dict]:
    by_host: dict[str, list[dict]] = {}
    for p in pages:
        by_host.setdefault(urllib.parse.urlsplit(p["url"]).netloc, []).append(p)
    out, queues = [], list(by_host.values())
    while any(queues):
        for q in queues:
            if q:
                out.append(q.pop(0))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    pages = round_robin(select())
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "pages.json").write_text(json.dumps(pages, indent=2))
    log = OUT / "jobs.jsonl"
    last_end: dict[str, float] = {}
    for prior in PKG_ROOT.glob("runs/*/jobs.jsonl"):
        for line in prior.read_text().splitlines():
            rec = json.loads(line)
            last_end[rec["host"]] = max(last_end.get(rec["host"], 0), rec["end"])

    print(f"{len(pages)} page(s)", flush=True)
    for i, p in enumerate(pages, 1):
        host = urllib.parse.urlsplit(p["url"]).netloc
        label = f"[{i}/{len(pages)}] {p['tag']} {p['url']}"
        page_dir = OUT / p["id"]
        if args.dry_run:
            print(label, flush=True)
            continue
        if (page_dir / "done.json").exists():
            print(f"{label}: already done", flush=True)
            continue
        wait = last_end.get(host, 0) + MIN_HOST_GAP - time.time()
        if wait > 0:
            print(f"{label}: waiting {wait:.0f}s for {host}", flush=True)
            time.sleep(wait)
        if page_dir.exists():
            subprocess.run(["rm", "-rf", str(page_dir)], check=True)
        page_dir.mkdir(parents=True)
        cmd = [str(SCREENER / ".venv" / "bin" / "python"), "-u", str(SCREENER / "tools" / "explore.py"),
               "--url", p["url"], "--out", str(page_dir),
               "--min-host-interval", str(MIN_HOST_GAP),
               "--match-crawler", "--no-block-ads", "--all-raw-urls", "--acknowledge-skips"]
        (page_dir / "command.json").write_text(json.dumps(cmd, indent=2))
        start = time.time()
        proc = subprocess.run(cmd, capture_output=True, text=True)
        end = time.time()
        (page_dir / "stdout.txt").write_text(proc.stdout + "\n--- stderr ---\n" + proc.stderr)
        (page_dir / "done.json").write_text(json.dumps({"exit_code": proc.returncode}))
        last_end[host] = end
        with log.open("a") as fh:
            fh.write(json.dumps({"page": p["id"], "job": "explorer", "host": host,
                                 "start": start, "end": end, "exit_code": proc.returncode}) + "\n")
        print(f"{label}: exit {proc.returncode} in {end - start:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
