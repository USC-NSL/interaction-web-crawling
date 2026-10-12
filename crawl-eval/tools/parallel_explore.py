#!/usr/bin/env python3
"""Explorer-only runs over many pages with several workers (the Tranco study).

Input: a JSON file {"pages": [{"id": ..., "url": ...}, ...]}. Each page is explored with
the same command eval_run.py uses (both explorer runs, matched to the crawler's browser,
--all-raw-urls), into <out>/<id>/explorer, with command.json, stdout.txt and done.json.

Etiquette, enforced across workers: at most one job per host at a time, and at least
--min-host-gap seconds from the end of one job on a host to the start of the next. The
host is the URL's host and port without a leading "www.". The last end per host is read
from every run directory's jobs.jsonl at start, as eval_run.py does, so a host loaded by
another run is respected too. Pages are taken round-robin across hosts, so one site's
pages do not queue up behind each other.

Resumable: a page whose explorer/done.json exists is skipped, so an interrupted run is
continued by running the same command again. On Ctrl-C the running explorers are stopped
and their pages get no done.json, so they run again on resume. Every finished job is appended to
<out>/jobs.jsonl with its start, end and exit code.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from collections import OrderedDict
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG_ROOT / "tools"))
from eval_run import MIN_HOST_GAP, explorer_cmd  # noqa: E402


def host(url: str) -> str:
    netloc = urllib.parse.urlsplit(url).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def round_robin(pages: list[dict]) -> list[dict]:
    by_host: OrderedDict[str, list[dict]] = OrderedDict()
    for p in pages:
        by_host.setdefault(host(p["url"]), []).append(p)
    out, queues = [], list(by_host.values())
    while any(queues):
        for q in queues:
            if q:
                out.append(q.pop(0))
    return out


class Scheduler:
    """Hands out the next page whose host is free and past its gap."""

    def __init__(self, todo: list[dict], last_end: dict[str, float], gap: float):
        self.todo = todo
        self.last_end = last_end
        self.gap = gap
        self.busy: set[str] = set()
        self.cond = threading.Condition()
        self.stopping = False

    def stop(self) -> None:
        with self.cond:
            self.stopping = True
            self.cond.notify_all()

    def take(self) -> dict | None:
        with self.cond:
            while True:
                if not self.todo or self.stopping:
                    return None
                now = time.time()
                wait = None
                for i, p in enumerate(self.todo):
                    h = host(p["url"])
                    if h in self.busy:
                        continue
                    ready = self.last_end.get(h, 0) + self.gap
                    if ready <= now:
                        self.busy.add(h)
                        return self.todo.pop(i)
                    wait = ready - now if wait is None else min(wait, ready - now)
                # nothing ready: sleep until the earliest gap ends, or a host is freed
                self.cond.wait(timeout=wait if wait is not None else None)

    def release(self, h: str, end: float) -> None:
        with self.cond:
            self.busy.discard(h)
            self.last_end[h] = end
            self.cond.notify_all()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pages", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--min-host-gap", type=float, default=MIN_HOST_GAP)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    out = args.out.resolve()
    pages = json.loads(args.pages.read_text())["pages"]
    todo = [p for p in round_robin(pages) if not (out / p["id"] / "explorer" / "done.json").exists()]
    print(f"{len(pages)} page(s), {len(todo)} to run, {args.workers} worker(s), "
          f"{args.min_host_gap:.0f}s per host", flush=True)
    if args.dry_run:
        for p in todo:
            print(f"  {host(p['url']):30} {p['id']}")
        return 0

    last_end: dict[str, float] = {}
    logs = set(PKG_ROOT.glob("runs/*/jobs.jsonl")) | {out / "jobs.jsonl"}
    for prior in (f for f in logs if f.exists()):
        for line in prior.read_text().splitlines():
            rec = json.loads(line)
            h = host(rec.get("url") or "https://" + rec["host"])
            last_end[h] = max(last_end.get(h, 0), rec["end"])
    out.mkdir(parents=True, exist_ok=True)
    sched = Scheduler(todo, last_end, args.min_host_gap)
    log_lock = threading.Lock()
    total = len(todo)
    finished = [0]
    running: dict[int, subprocess.Popen] = {}

    def worker(n: int) -> None:
        while (page := sched.take()) is not None:
            h = host(page["url"])
            job_dir = out / page["id"] / "explorer"
            if job_dir.exists():
                subprocess.run(["rm", "-rf", str(job_dir)], check=True)
            job_dir.mkdir(parents=True)
            cmd = explorer_cmd({"page": page}, job_dir)
            (job_dir / "command.json").write_text(json.dumps(cmd, indent=2))
            start = time.time()
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    start_new_session=True)
            running[n] = proc
            stdout, stderr = proc.communicate()
            del running[n]
            end = time.time()
            (job_dir / "stdout.txt").write_text(stdout + "\n--- stderr ---\n" + stderr)
            sched.release(h, end)
            interrupted = sched.stopping
            if not interrupted:
                (job_dir / "done.json").write_text(json.dumps({"exit_code": proc.returncode}))
            with log_lock:
                finished[0] += 1
                # an interrupted job is logged too, so its host's gap holds on resume,
                # but gets no done.json, so it runs again
                with (out / "jobs.jsonl").open("a") as fh:
                    fh.write(json.dumps({"page": page["id"], "url": page["url"], "job": "explorer",
                                         "host": h, "worker": n, "start": start, "end": end,
                                         "exit_code": "interrupted" if interrupted
                                         else proc.returncode}) + "\n")
                print(f"[{finished[0]}/{total}] w{n} {h} {page['id']}: "
                      f"{'interrupted' if interrupted else f'exit {proc.returncode}'} "
                      f"in {end - start:.0f}s", flush=True)
            if interrupted:
                return

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(1, args.workers + 1)]
    # Ctrl-C, or a kill, stops the run cleanly (also when started in the background,
    # where SIGINT would otherwise be ignored)
    def interrupt(_sig, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGINT, interrupt)
    signal.signal(signal.SIGTERM, interrupt)
    for t in threads:
        t.start()
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("interrupted: stopping running explorers; run the same command to resume", flush=True)
        sched.stop()
        for proc in list(running.values()):
            os.killpg(proc.pid, signal.SIGTERM)    # the explorer and its browser
        for t in threads:
            t.join()
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
