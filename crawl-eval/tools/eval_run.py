#!/usr/bin/env python3
"""The evaluation crawls: RevealClick against stock autoclick on the evaluation set.

Pages: tools/eval_pages.json (demo click pages, production click pages, negatives).
Per page:
  none     one --behaviors "" crawl, for the instrument check
  A        shipped stock autoclick, selector "a"                    all pages
  B        shipped stock autoclick, --clickSelector "a, button"     DEMO click pages only
           (no request guard, no denylist: never on a production site)
  C        RevealClick, native controls only, MAX_CLICKS=1         all pages
  F        RevealClick, committed build                            all pages
each config isolated (--behaviors autoclick) and combined (all four), 2 repeats.

The no-behavior crawls run first, so the instrument check can be computed before the
config crawls finish. Etiquette as in pilot_run.py: one crawl at a time, at least
MIN_HOST_GAP seconds from the end of a load of a host to the start of the next
(counted across every run directory's jobs.jsonl), jobs interleaved round-robin across
hosts, the explorer's CONTACT_URL in the user agent. Resumable: a job with done.json
is skipped. Bundles C and F are fixed files under runs/eval/bundles/, built once from
browsertrix-behaviors and recorded with their commit and SHA-256 in bundles.json.
"""

import argparse
import hashlib
import json
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
OUT = PKG_ROOT / "runs" / "eval"
BUNDLES = OUT / "bundles"
IMAGE = "webrecorder/browsertrix-crawler:latest"
MIN_HOST_GAP = 60.0
ALL = "autoplay,autofetch,autoscroll,autoclick"

sys.path.insert(0, str(PKG_ROOT.parent / "page-screener" / "tools"))
from explore import CRAWLER_UA_SUFFIX as UA_SUFFIX  # noqa: E402

CONFIGS = {
    # name: (bundle file or None for the shipped one, clickSelector, page sets)
    "A": (None, None, ("demo", "production", "negative")),
    "B": (None, "a, button", ("demo",)),
    "C": ("C.js", None, ("demo", "production", "negative")),
    "F": ("F.js", None, ("demo", "production", "negative")),
}


def host(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc


def round_robin(per_page: dict[str, list[dict]]) -> list[dict]:
    out, queues = [], list(per_page.values())
    while any(queues):
        for q in queues:
            if q:
                out.append(q.pop(0))
    return out


def jobs_for(pages: list[dict]) -> list[dict]:
    first = round_robin({p["id"]: [{"page": p, "name": "none", "behaviors": "", "cfg": None}]
                         for p in pages})
    per_page: dict[str, list[dict]] = {p["id"]: [] for p in pages}
    for p in pages:
        for rep in (1, 2):
            for cfg, (_bundle, _sel, sets) in CONFIGS.items():
                if p["set"] not in sets:
                    continue
                for mode, behaviors in (("iso", "autoclick"), ("comb", ALL)):
                    per_page[p["id"]].append({"page": p, "name": f"{cfg}-{mode}-r{rep}",
                                              "behaviors": behaviors, "cfg": cfg})
    return first + round_robin(per_page)


def crawl_cmd(job: dict, job_dir: Path) -> list[str]:
    bundle, selector = (CONFIGS[job["cfg"]][:2] if job["cfg"] else (None, None))
    cmd = ["docker", "run", "--rm", "-v", f"{job_dir}:/crawls"]
    if bundle:
        cmd += ["-v", f"{BUNDLES / bundle}:/app/node_modules/browsertrix-behaviors/dist/behaviors.js:ro"]
    cmd += [IMAGE, "crawl", "--url", job["page"]["url"], "--scopeType", "page", "--workers", "1",
            "--behaviors", job["behaviors"], "--behaviorTimeout", "300", "--postLoadDelay", "10",
            "--userAgentSuffix", UA_SUFFIX, "--logging", "behaviors,debug", "--collection", "c"]
    if selector:
        cmd += ["--clickSelector", selector]
    return cmd


def check_bundles() -> dict:
    meta = json.loads((BUNDLES / "bundles.json").read_text())
    for name, info in meta["bundles"].items():
        digest = hashlib.sha256((BUNDLES / name).read_bytes()).hexdigest()
        if digest != info["sha256"]:
            raise SystemExit(f"bundle {name} does not match bundles.json; refusing to run")
    return meta


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    pages = json.loads((PKG_ROOT / "tools" / "eval_pages.json").read_text())["pages"]
    meta = check_bundles()
    jobs = jobs_for(pages)
    OUT.mkdir(parents=True, exist_ok=True)
    log = OUT / "jobs.jsonl"
    last_end: dict[str, float] = {}
    for prior in PKG_ROOT.glob("runs/*/jobs.jsonl"):
        for line in prior.read_text().splitlines():
            rec = json.loads(line)
            last_end[rec["host"]] = max(last_end.get(rec["host"], 0), rec["end"])

    todo = [j for j in jobs if not (OUT / j["page"]["id"] / j["name"] / "done.json").exists()]
    print(f"{len(jobs)} job(s), {len(todo)} to run; bundles from browsertrix-behaviors "
          f"{meta['commit']}; UA suffix: {UA_SUFFIX}", flush=True)
    for i, job in enumerate(jobs, 1):
        page = job["page"]
        job_dir = OUT / page["id"] / job["name"]
        label = f"[{i}/{len(jobs)}] {page['set'][:4]} {page['id'][:40]} {job['name']}"
        if (job_dir / "done.json").exists():
            continue
        if args.dry_run:
            print(label, flush=True)
            continue
        h = host(page["url"])
        wait = last_end.get(h, 0) + MIN_HOST_GAP - time.time()
        if wait > 0:
            print(f"{label}: waiting {wait:.0f}s for {h}", flush=True)
            time.sleep(wait)
        if job_dir.exists():
            subprocess.run(["rm", "-rf", str(job_dir)], check=True)
        job_dir.mkdir(parents=True)
        cmd = crawl_cmd(job, job_dir)
        (job_dir / "command.json").write_text(json.dumps(cmd, indent=2))
        start = time.time()
        proc = subprocess.run(cmd, capture_output=True, text=True)
        end = time.time()
        (job_dir / "stdout.txt").write_text(proc.stdout + "\n--- stderr ---\n" + proc.stderr)
        (job_dir / "done.json").write_text(json.dumps({"exit_code": proc.returncode}))
        last_end[h] = end
        with log.open("a") as fh:
            fh.write(json.dumps({"page": page["id"], "job": job["name"], "host": h,
                                 "start": start, "end": end, "exit_code": proc.returncode}) + "\n")
        print(f"{label}: exit {proc.returncode} in {end - start:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
