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
config crawls finish. With --explore, each page is instead explored (both explorer
runs) and then crawled back-to-back: explorer, no-behavior crawl, configs, so the pool
and the crawls see the same version of the site. Pages go through in groups of
--group, interleaved within a group so the per-host wait overlaps with other hosts. Etiquette as in pilot_run.py: one crawl at a time, at least
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
SCREENER = PKG_ROOT.parent / "page-screener"
OUT = PKG_ROOT / "runs" / "eval"
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
    "F2": ("F2.js", None, ("demo", "production", "negative")),
    # the hover evaluation (parallel_explore.py --crawl): RevealClick alone, AutoHover
    # alone with synthetic and with real pointer input, and both in sequence
    "RC": ("RC.js", None, ("hover",)),
    "AHS": ("AHS.js", None, ("hover",)),
    "AHR": ("AHR.js", None, ("hover",)),
    "SEQ": ("SEQ.js", None, ("hover",)),
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


def config_jobs(p: dict, configs: list[str]) -> list[dict]:
    out = []
    for rep in (1, 2):
        for cfg in configs:
            if p["set"] not in CONFIGS[cfg][2]:
                continue
            for mode, behaviors in (("iso", "autoclick"), ("comb", ALL)):
                out.append({"page": p, "name": f"{cfg}-{mode}-r{rep}", "behaviors": behaviors,
                            "cfg": cfg})
    return out


def jobs_for(pages: list[dict], configs: list[str], explore: bool, group: int) -> list[dict]:
    none = lambda p: {"page": p, "name": "none", "behaviors": "", "cfg": None}  # noqa: E731
    if not explore:
        first = round_robin({p["id"]: [none(p)] for p in pages})
        return first + round_robin({p["id"]: config_jobs(p, configs) for p in pages})
    out = []
    for i in range(0, len(pages), group):
        chunk = pages[i:i + group]
        out += [{"page": p, "name": "explorer", "cfg": None, "kind": "explorer"} for p in chunk]
        out += round_robin({p["id"]: [none(p)] for p in chunk})
        out += round_robin({p["id"]: config_jobs(p, configs) for p in chunk})
    return out


def explorer_cmd(job: dict, job_dir: Path) -> list[str]:
    return [str(SCREENER / ".venv" / "bin" / "python"), "-u", str(SCREENER / "tools" / "explore.py"),
            "--url", job["page"]["url"], "--out", str(job_dir),
            "--min-host-interval", str(MIN_HOST_GAP),
            "--match-crawler", "--no-block-ads", "--all-raw-urls", "--acknowledge-skips"]


def crawl_cmd(job: dict, job_dir: Path, bundles: Path) -> list[str]:
    bundle, selector = (CONFIGS[job["cfg"]][:2] if job["cfg"] else (None, None))
    cmd = ["docker", "run", "--rm", "-v", f"{job_dir}:/crawls"]
    if bundle:
        cmd += ["-v", f"{bundles / bundle}:/app/node_modules/browsertrix-behaviors/dist/behaviors.js:ro"]
    cmd += [IMAGE, "crawl", "--url", job["page"]["url"], "--scopeType", "page", "--workers", "1",
            "--behaviors", job["behaviors"],
            "--behaviorTimeout", str(job.get("behavior_timeout", 300)), "--postLoadDelay", "10",
            "--userAgentSuffix", UA_SUFFIX, "--logging", "behaviors,debug", "--collection", "c"]
    if selector:
        cmd += ["--clickSelector", selector]
    return cmd


def check_bundles(bundles: Path, configs: list[str]) -> dict:
    meta = json.loads((bundles / "bundles.json").read_text())
    needed = {CONFIGS[c][0] for c in configs if CONFIGS[c][0]}
    missing = needed - set(meta["bundles"])
    if missing:
        raise SystemExit(f"bundles.json lacks {sorted(missing)}; refusing to run")
    for name, info in meta["bundles"].items():
        digest = hashlib.sha256((bundles / name).read_bytes()).hexdigest()
        if digest != info["sha256"]:
            raise SystemExit(f"bundle {name} does not match bundles.json; refusing to run")
    return meta


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--pages-file", type=Path, default=PKG_ROOT / "tools" / "eval_pages.json")
    ap.add_argument("--configs", default="A,B,C,F")
    ap.add_argument("--explore", action="store_true",
                    help="explore each page, then crawl it, back-to-back")
    ap.add_argument("--group", type=int, default=3, help="pages per back-to-back group")
    args = ap.parse_args()

    out_dir = args.out.resolve()
    configs = [c for c in args.configs.split(",") if c]
    pages = json.loads(args.pages_file.read_text())["pages"]
    meta = check_bundles(out_dir / "bundles", configs)
    jobs = jobs_for(pages, configs, args.explore, args.group)
    out_dir.mkdir(parents=True, exist_ok=True)
    log = out_dir / "jobs.jsonl"
    last_end: dict[str, float] = {}
    for prior in PKG_ROOT.glob("runs/*/jobs.jsonl"):
        for line in prior.read_text().splitlines():
            rec = json.loads(line)
            last_end[rec["host"]] = max(last_end.get(rec["host"], 0), rec["end"])

    todo = [j for j in jobs if not (out_dir / j["page"]["id"] / j["name"] / "done.json").exists()]
    print(f"{len(jobs)} job(s), {len(todo)} to run; bundles from browsertrix-behaviors "
          f"{meta['commit']}; UA suffix: {UA_SUFFIX}", flush=True)
    for i, job in enumerate(jobs, 1):
        page = job["page"]
        job_dir = out_dir / page["id"] / job["name"]
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
        cmd = (explorer_cmd(job, job_dir) if job.get("kind") == "explorer"
               else crawl_cmd(job, job_dir, out_dir / "bundles"))
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
