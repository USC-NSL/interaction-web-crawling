#!/usr/bin/env python3
"""Run Browsertrix over the interaction corpus under isolated behavior conditions.

One crawl per condition, each enabling exactly one behavior (or none), never
cumulative. The question is not "how much do behaviors recover" but "which subset
of behaviors does each page actually need, and what does running unnecessary ones
cost", so each behavior has to be measured on its own.

Every condition gets a freshly started serve.py with its own request log, so the
WARC can be cross-checked against what the server actually saw for that run.
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
CORPUS = PKG_ROOT.parent / "interaction-corpus"
IMAGE = "webrecorder/browsertrix-crawler:latest"
HOST_BASE = "http://host.docker.internal:8000"

# Behavior names are validated against Browsertrix's BEHAVIOR_TYPES
# (autoplay, autofetch, autoscroll, autoclick, siteSpecific). An unknown name is
# only warned about and silently ignored, so a typo here would quietly run a
# no-behavior crawl - run_crawls checks the crawl log to rule that out.
#
# Disabling: `--behaviors ""`. This works because the arg coercion splits on commas
# and drops empty strings, leaving [], and an empty list sets behaviorOpts to "".
# Verified per run from the log, not assumed.
CONDITIONS = {
    "c1-none": "",
    "c2-autoscroll": "autoscroll",
    "c3-autoclick": "autoclick",
    "c4-autofetch": "autofetch",
    "c5-autoplay": "autoplay",
}


def write_url_file(path: Path) -> list[dict]:
    manifest = json.loads((CORPUS / "manifest.json").read_text())
    pages = manifest["pages"]
    path.write_text("".join(f"{HOST_BASE}/{p['path']}\n" for p in pages))
    return pages


def wait_for(url: str, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2).read()
            return True
        except Exception:  # noqa: BLE001
            time.sleep(0.3)
    return False


def run_condition(name: str, behaviors: str, url_file: Path, rerun: bool) -> dict:
    run_dir = PKG_ROOT / "runs" / name
    if run_dir.exists() and not rerun:
        print(f"[{name}] exists, skipping (use --rerun)", flush=True)
        return json.loads((run_dir / "run.json").read_text())
    if run_dir.exists():
        subprocess.run(["rm", "-rf", str(run_dir)], check=True)
    (run_dir / "crawls").mkdir(parents=True)

    server = subprocess.Popen(
        [str(CORPUS / ".venv" / "bin" / "python"), str(CORPUS / "tools" / "serve.py"),
         "--port", "8000", "--log", str(run_dir / "requests.jsonl"), "--reset"],
        stdout=open(run_dir / "server.out", "w"), stderr=subprocess.STDOUT,
    )
    try:
        if not wait_for("http://127.0.0.1:8000/manifest.json"):
            raise RuntimeError("serve.py did not come up on :8000")

        cmd = [
            "docker", "run", "--rm",
            "-v", f"{run_dir / 'crawls'}:/crawls",
            "-v", f"{url_file}:/crawls-config/urls.txt:ro",
            IMAGE, "crawl",
            "--urlFile", "/crawls-config/urls.txt",
            "--scopeType", "page",
            "--workers", "1",
            # to-pages is exactly what bare --text resolves to. final-to-warc is an
            # addition: to-pages text is snapshotted BEFORE behaviors run, so it can
            # never reflect a reveal. final-to-warc snapshots after behaviors, into a
            # WARC resource record (not a response body), as a supplementary column.
            "--text", "to-pages,final-to-warc",
            "--generateWACZ",
            "--behaviorTimeout", "120",
            "--postLoadDelay", "2",
            "--behaviors", behaviors,
            "--collection", name,
            "--logging", "stats,debug",
        ]
        (run_dir / "command.json").write_text(json.dumps(cmd, indent=2))
        print(f"[{name}] behaviors={behaviors!r} crawling ...", flush=True)
        started = time.time()
        proc = subprocess.run(cmd, capture_output=True, text=True)
        elapsed = time.time() - started
        (run_dir / "docker.out").write_text(proc.stdout + "\n--- stderr ---\n" + proc.stderr)
        info = {"condition": name, "behaviors": behaviors, "exit_code": proc.returncode,
                "crawl_wall_s": round(elapsed, 1)}
        (run_dir / "run.json").write_text(json.dumps(info, indent=2))
        print(f"[{name}] exit={proc.returncode} in {elapsed:.0f}s", flush=True)
        if proc.returncode != 0:
            raise RuntimeError(f"crawl {name} exited {proc.returncode}; see {run_dir/'docker.out'}")
        return info
    finally:
        server.terminate()
        server.wait(timeout=10)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", action="append", default=[], help="run only these conditions")
    ap.add_argument("--rerun", action="store_true", help="overwrite existing run directories")
    args = ap.parse_args()

    url_file = PKG_ROOT / "runs" / "urls.txt"
    url_file.parent.mkdir(parents=True, exist_ok=True)
    pages = write_url_file(url_file)
    print(f"{len(pages)} corpus page(s) -> {url_file}", flush=True)

    for name, behaviors in CONDITIONS.items():
        if args.only and name not in args.only:
            continue
        run_condition(name, behaviors, url_file, args.rerun)
    return 0


if __name__ == "__main__":
    sys.exit(main())
