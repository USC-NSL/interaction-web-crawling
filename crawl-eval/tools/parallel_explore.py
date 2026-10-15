#!/usr/bin/env python3
"""Explorer runs over many pages with several workers (the Tranco study), optionally
followed per page by crawls (the hover evaluation).

Input: a JSON file {"pages": [{"id": ..., "url": ...}, ...]}. Each page is explored with
the same command eval_run.py uses (both explorer runs, matched to the crawler's browser,
--all-raw-urls), into <out>/<id>/explorer, with command.json, stdout.txt and done.json.

Etiquette, enforced across workers: at most one job per host at a time, and at least
--min-host-gap seconds from the end of one job on a host to the start of the next. The
host is the URL's host and port without a leading "www.". The last end per host is read
from every run directory's jobs.jsonl at start, as eval_run.py does, so a host loaded by
another run is respected too. Pages are taken round-robin across hosts, so one site's
pages do not queue up behind each other.

Reuse: with --reuse DIR, a page whose exact URL was already explored there (a done.json
with exit code 0) gets a copy of that result instead of a new run, and the copy is listed
in <out>/reused.json. Only valid while the explorer has not changed in between.

Timeout: an explorer still running after --job-timeout minutes is killed with its browser
(the explorer's own budget is checked only between actions, so a browser call that never
returns would otherwise hold the run open). The page gets done.json with exit code
"timeout", so a resume does not retry it.

Crawls: with --crawl, each page is a chain of jobs run in order, back-to-back: the
explorer, then the listed crawls (none, or <config>-<iso|comb> from eval_run.CONFIGS,
built with eval_run.crawl_cmd and its bundles, which must match bundles.json in
--bundles). The per-host rule applies to every job, so a page's chain advances one job
at a time while other hosts' chains run on the other workers. A crawl still running
after --crawl-timeout minutes has its container killed and gets exit code "timeout".

Resumable: a page whose explorer/done.json exists is skipped, so an interrupted run is
continued by running the same command again. On Ctrl-C the running explorers are stopped
and their pages get no done.json, so they run again on resume. Every finished job is appended to
<out>/jobs.jsonl with its start, end and exit code.
"""

import argparse
import json
import os
import shutil
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
from eval_run import ALL, CONFIGS, MIN_HOST_GAP, check_bundles, crawl_cmd, explorer_cmd  # noqa: E402


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
                # an empty queue is final only once no job is running: a running page
                # with jobs left comes back on release
                if self.stopping or (not self.todo and not self.busy):
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

    def release(self, h: str, end: float, requeue: dict | None = None) -> None:
        """Frees the host; a page with jobs left goes to the back of the queue."""
        with self.cond:
            self.busy.discard(h)
            self.last_end[h] = end
            if requeue is not None:
                self.todo.append(requeue)
            self.cond.notify_all()


def reuse(pages: list[dict], out: Path, dirs: list[Path], dry_run: bool) -> list[dict]:
    """Copy completed explorer results for the same URL from earlier run directories."""
    done: dict[str, Path] = {}
    for d in dirs:
        for cmd_file in sorted(d.resolve().glob("*/explorer/command.json")):
            job = cmd_file.parent
            cmd = json.loads(cmd_file.read_text())
            url = cmd[cmd.index("--url") + 1]
            status = job / "done.json"
            if status.exists() and json.loads(status.read_text()).get("exit_code") == 0:
                done.setdefault(url, job)
    reused = []
    for p in pages:
        target = out / p["id"] / "explorer"
        src = done.get(p["url"])
        if src is None or (target / "done.json").exists():
            continue
        reused.append({"id": p["id"], "url": p["url"], "from": str(src)})
        if not dry_run:
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(src, target)
    if reused and not dry_run:
        out.mkdir(parents=True, exist_ok=True)
        log = out / "reused.json"
        old = json.loads(log.read_text()) if log.exists() else []
        log.write_text(json.dumps(old + reused, indent=2) + "\n")
    return reused


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pages", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--min-host-gap", type=float, default=MIN_HOST_GAP)
    ap.add_argument("--reuse", type=Path, action="append", default=[],
                    help="a run directory whose completed results are reused for the same URL")
    ap.add_argument("--job-timeout", type=float, default=30.0, help="minutes per explorer run")
    ap.add_argument("--crawl", default="",
                    help="crawls after the explorer, in order: none and/or <config>-<iso|comb>")
    ap.add_argument("--bundles", type=Path, help="bundle directory (default <out>/bundles)")
    ap.add_argument("--behavior-timeout", type=int, default=300, help="crawler --behaviorTimeout")
    ap.add_argument("--crawl-timeout", type=float, default=15.0, help="minutes per crawl")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    out = args.out.resolve()
    pages = json.loads(args.pages.read_text())["pages"]
    crawls = [c for c in args.crawl.split(",") if c]
    for c in crawls:
        cfg, _, mode = c.rpartition("-")
        if c != "none" and (cfg not in CONFIGS or mode not in ("iso", "comb")):
            raise SystemExit(f"--crawl {c}: expected none or <config>-<iso|comb>")
    bundles = (args.bundles or out / "bundles").resolve()
    meta = check_bundles(bundles, [c.rpartition("-")[0] for c in crawls if c != "none"]) \
        if crawls else None
    chain = ["explorer"] + crawls

    def pending(p: dict) -> list[str]:
        return [j for j in chain if not (out / p["id"] / j / "done.json").exists()]
    reused = reuse(pages, out, args.reuse, args.dry_run)
    if reused:
        print(f"{len(reused)} page(s) reused from {', '.join(map(str, args.reuse))}", flush=True)
    reused_ids = {r["id"] for r in reused}
    todo = [p for p in round_robin(pages) if pending(p)]
    jobs_left = sum(len(pending(p)) for p in todo)
    print(f"{len(pages)} page(s), {len(todo)} with jobs to run ({jobs_left} job(s): "
          f"{', '.join(chain)}), {args.workers} worker(s), {args.min_host_gap:.0f}s per host"
          + (f"; bundles from browsertrix-behaviors {meta['commit']}" if meta else ""), flush=True)
    if args.dry_run:
        for p in todo:
            print(f"  {host(p['url']):30} {p['id']}: {', '.join(pending(p))}")
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
    total = jobs_left
    finished = [0]
    running: dict[int, subprocess.Popen] = {}
    containers: dict[int, str] = {}

    def job_cmd(page: dict, name: str, job_dir: Path, n: int) -> list[str]:
        if name == "explorer":
            return explorer_cmd({"page": page}, job_dir)
        cfg, _, mode = name.rpartition("-")
        job = {"page": page, "cfg": None if name == "none" else cfg,
               "behaviors": "" if name == "none" else ("autoclick" if mode == "iso" else ALL),
               "behavior_timeout": args.behavior_timeout}
        cmd = crawl_cmd(job, job_dir, bundles)
        # named, so a timed-out crawl can be killed: killing `docker run` leaves it running
        containers[n] = f"pe-{os.getpid()}-{n}-{int(time.time())}"
        return cmd[:2] + ["--name", containers[n]] + cmd[2:]

    def worker(n: int) -> None:
        while (page := sched.take()) is not None:
            h = host(page["url"])
            name = pending(page)[0]
            job_dir = out / page["id"] / name
            if job_dir.exists():
                subprocess.run(["rm", "-rf", str(job_dir)], check=True)
            job_dir.mkdir(parents=True)
            cmd = job_cmd(page, name, job_dir, n)
            (job_dir / "command.json").write_text(json.dumps(cmd, indent=2))
            start = time.time()
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    start_new_session=True)
            running[n] = proc
            timeout = args.job_timeout if name == "explorer" else args.crawl_timeout
            try:
                stdout, stderr = proc.communicate(timeout=timeout * 60)
                code = proc.returncode
            except subprocess.TimeoutExpired:
                if name != "explorer":
                    subprocess.run(["docker", "kill", containers[n]], capture_output=True)
                os.killpg(proc.pid, signal.SIGKILL)     # the explorer and its browser
                stdout, stderr = proc.communicate()
                code = "timeout"
            del running[n]
            containers.pop(n, None)
            end = time.time()
            (job_dir / "stdout.txt").write_text(stdout + "\n--- stderr ---\n" + stderr)
            interrupted = sched.stopping and code != "timeout"
            if not interrupted:
                (job_dir / "done.json").write_text(json.dumps({"exit_code": code}))
            more = not interrupted and len(pending(page)) > 0
            sched.release(h, end, page if more else None)
            with log_lock:
                finished[0] += 1
                # an interrupted job is logged too, so its host's gap holds on resume,
                # but gets no done.json, so it runs again
                with (out / "jobs.jsonl").open("a") as fh:
                    fh.write(json.dumps({"page": page["id"], "url": page["url"], "job": name,
                                         "host": h, "worker": n, "start": start, "end": end,
                                         "exit_code": "interrupted" if interrupted
                                         else code}) + "\n")
                print(f"[{finished[0]}/{total}] w{n} {h} {page['id']} {name}: "
                      f"{'interrupted' if interrupted else f'exit {code}'} "
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
        for name in list(containers.values()):
            subprocess.run(["docker", "kill", name], capture_output=True)
        for t in threads:
            t.join()
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
