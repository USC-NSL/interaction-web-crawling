#!/usr/bin/env python3
"""Real-page pilot: Browsertrix click configs, a no-behavior baseline, and the explorer.

Two phases, so the instrument check can stop the pilot before most of the load is
put on anyone's server:

  baseline  explorer (two runs internally) + one --behaviors "" crawl per page
  configs   A, B, F, each isolated and combined, two repeats each, per page

Etiquette, enforced here rather than by convention:
  - at least MIN_HOST_GAP seconds from the END of any load of a host to the START of
    the next one, crawl or explorer; jobs are interleaved round-robin across pages so
    the wait overlaps with other hosts' jobs. The explorer is passed the same interval
    for its own internal loads (start to start, its throttle's rule).
  - one job at a time; logged out; no browser profile
  - the explorer's CONTACT_URL in the crawler's user agent, via --userAgentSuffix

Every job's start and end time is appended to runs/pilot/jobs.jsonl, so the spacing
can be audited afterwards rather than trusted.
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
OUT = PKG_ROOT / "runs" / "pilot"
IMAGE = "webrecorder/browsertrix-crawler:latest"
MIN_HOST_GAP = 60.0

sys.path.insert(0, str(SCREENER / "tools"))
# one definition, so the explorer's --match-crawler user agent is the crawler's exactly
from explore import CRAWLER_UA_SUFFIX as UA_SUFFIX  # noqa: E402

ALL = "autoplay,autofetch,autoscroll,autoclick"
CONFIGS = {
    # name: (bundle, clickSelector or None)
    "A": ("shipped", None),
    "B": ("shipped", "a, button"),
    "F": ("revealclick", None),
}


def host(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc


def jobs_for(phase: str, pages: list[dict]) -> list[dict]:
    per_page: dict[str, list[dict]] = {p["id"]: [] for p in pages}
    for p in pages:
        if phase == "baseline":
            per_page[p["id"]] += [
                {"page": p, "kind": "explorer", "name": "explorer"},
                {"page": p, "kind": "crawl", "name": "none", "behaviors": "", "bundle": "shipped",
                 "selector": None},
            ]
        else:
            for rep in (1, 2):
                for cfg, (bundle, sel) in CONFIGS.items():
                    for mode, behaviors in (("iso", "autoclick"), ("comb", ALL)):
                        per_page[p["id"]].append(
                            {"page": p, "kind": "crawl", "name": f"{cfg}-{mode}-r{rep}",
                             "behaviors": behaviors, "bundle": bundle, "selector": sel})
    # round-robin, so consecutive jobs hit different hosts
    out, queues = [], list(per_page.values())
    while any(queues):
        for q in queues:
            if q:
                out.append(q.pop(0))
    return out


def crawl_cmd(job: dict, coll_dir: Path, bundle_path: Path | None) -> list[str]:
    cmd = ["docker", "run", "--rm", "-v", f"{coll_dir}:/crawls"]
    if job["bundle"] == "revealclick":
        cmd += ["-v", f"{bundle_path}:/app/node_modules/browsertrix-behaviors/dist/behaviors.js:ro"]
    cmd += [IMAGE, "crawl",
            "--url", job["page"]["url"],
            "--scopeType", "page",
            "--workers", "1",
            "--behaviors", job["behaviors"],
            "--behaviorTimeout", "300",
            # matches the explorer's 10s settle after networkidle before its baseline
            "--postLoadDelay", "10",
            "--userAgentSuffix", UA_SUFFIX,
            "--logging", "behaviors,debug",
            "--collection", "c"]
    if job["selector"]:
        cmd += ["--clickSelector", job["selector"]]
    return cmd


def run_job(job: dict, bundle_path: Path | None, out: Path) -> dict:
    page = job["page"]
    job_dir = out / page["id"] / job["name"]
    if (job_dir / "done.json").exists():
        return {"skipped": True}
    if job_dir.exists():
        subprocess.run(["rm", "-rf", str(job_dir)], check=True)
    job_dir.mkdir(parents=True)
    if job["kind"] == "explorer":
        cmd = [str(SCREENER / ".venv" / "bin" / "python"), "-u", str(SCREENER / "tools" / "explore.py"),
               "--url", page["url"], "--out", str(job_dir),
               "--min-host-interval", str(MIN_HOST_GAP),
               # blocking is applied post hoc to both sides by pilot_analyse.py
               "--no-block-ads", "--match-crawler", "--all-raw-urls",
               # the four vetoes seen in the first pilot were reviewed by a human
               "--acknowledge-skips"]
    else:
        cmd = crawl_cmd(job, job_dir, bundle_path)
    (job_dir / "command.json").write_text(json.dumps(cmd, indent=2))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    (job_dir / "stdout.txt").write_text(proc.stdout + "\n--- stderr ---\n" + proc.stderr)
    info = {"exit_code": proc.returncode}
    (job_dir / "done.json").write_text(json.dumps(info))
    return info


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("phase", choices=["baseline", "configs"])
    ap.add_argument("--bundle", type=Path, help="RevealClick behaviors.js (configs phase)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", type=Path, default=OUT, help="run directory (default runs/pilot)")
    args = ap.parse_args()
    if args.phase == "configs" and not (args.bundle and args.bundle.exists()):
        ap.error("configs phase needs --bundle pointing at the RevealClick build")

    pages = json.loads((PKG_ROOT / "tools" / "pilot_pages.json").read_text())["pages"]
    jobs = jobs_for(args.phase, pages)
    args.out.mkdir(parents=True, exist_ok=True)
    log = args.out / "jobs.jsonl"

    # last load END per host, seeded from earlier jobs so a restart keeps the spacing
    last_end: dict[str, float] = {}
    # every run directory's job log counts, so a new run keeps its distance from the last
    for prior in PKG_ROOT.glob("runs/*/jobs.jsonl"):
        for line in prior.read_text().splitlines():
            rec = json.loads(line)
            last_end[rec["host"]] = max(last_end.get(rec["host"], 0), rec["end"])

    print(f"{args.phase}: {len(jobs)} job(s); UA suffix: {UA_SUFFIX}", flush=True)
    for i, job in enumerate(jobs, 1):
        h = host(job["page"]["url"])
        label = f"[{i}/{len(jobs)}] {job['page']['id']} {job['name']}"
        if args.dry_run:
            print(label, flush=True)
            continue
        wait = last_end.get(h, 0) + MIN_HOST_GAP - time.time()
        if wait > 0:
            print(f"{label}: waiting {wait:.0f}s for {h}", flush=True)
            time.sleep(wait)
        start = time.time()
        info = run_job(job, args.bundle, args.out)
        end = time.time()
        if info.get("skipped"):
            print(f"{label}: already done", flush=True)
            continue
        last_end[h] = end
        with log.open("a") as fh:
            fh.write(json.dumps({"page": job["page"]["id"], "job": job["name"], "host": h,
                                 "start": start, "end": end, **info}) + "\n")
        print(f"{label}: exit {info['exit_code']} in {end - start:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
