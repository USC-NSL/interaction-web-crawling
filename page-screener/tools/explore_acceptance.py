#!/usr/bin/env python3
"""Acceptance test for the exploration oracle (explore.py) on the interaction corpus.

Starts a fresh serve.py with its own request log, runs explore.py on every corpus page,
and requires:

  1. every resource sentinel in the manifest is in the explorer's STABLE new-content set
  2. no resource sentinel is in either run's baseline, on either channel
  3. the explorer's channel (/sentinel/ paths the browser requested) equals the server's
     channel (/sentinel/ paths serve.py logged): the two must agree

Text sentinels (continuereading-*) are out of scope for pass/fail, since they reveal
text rather than resources, but whether they became visible is reported.

The corpus is never modified. If a sentinel is missed, the report says which page,
which sentinel, what the manifest says the required interaction is, and what the
explorer actually did on that page.
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
CORPUS = PKG_ROOT.parent / "interaction-corpus"

# Every corpus sentinel is a 68-byte 1x1 PNG. The screener's response rule treats
# anything under 1KB as a pixel or cookie sync, which is exactly right on real pages
# and exactly wrong here. The exemption is scoped to the corpus server's sentinel path
# only, and the report states how many sentinels it rescued.
EXEMPT_REASON = ("interaction-corpus sentinels are deliberately 68-byte 1x1 PNGs, which the "
                 "screener's response rule (body under 1KB) classifies as tracking pixels "
                 "by design; exempted on the corpus server's /sentinel/ path only")


def wait_for(url: str, seconds: float = 15) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2).read(1)
            return True
        except Exception:  # noqa: BLE001
            time.sleep(0.3)
    return False


def sentinel_paths(urls) -> set[str]:
    return {urllib.parse.urlsplit(u).path for u in urls
            if urllib.parse.urlsplit(u).path.startswith("/sentinel/")}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8010)
    ap.add_argument("--out", default=str(PKG_ROOT / "explore" / "out"))
    ap.add_argument("--analyse-only", action="store_true",
                    help="re-check existing outputs and server log without re-running")
    ap.add_argument("--only", action="append", default=[])
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    log_path = out / "corpus-server-requests.jsonl"
    base = f"http://127.0.0.1:{args.port}"
    manifest = json.loads((CORPUS / "manifest.json").read_text())
    pages = [p for p in manifest["pages"] if not args.only or p["id"] in args.only]

    if not args.analyse_only:
        # serve.py is stdlib-only, so a fresh clone with no corpus venv still works.
        corpus_python = CORPUS / ".venv" / "bin" / "python"
        python = str(corpus_python) if corpus_python.is_file() else sys.executable
        server = subprocess.Popen(
            [python, str(CORPUS / "tools" / "serve.py"),
             "--port", str(args.port), "--log", str(log_path), "--reset"],
            stdout=open(out / "corpus-server.out", "w"), stderr=subprocess.STDOUT)
        try:
            if not wait_for(f"{base}/manifest.json"):
                print("serve.py did not come up")
                return 2
            cmd = [sys.executable, "-u", str(PKG_ROOT / "tools" / "explore.py"),
                   "--corpus-manifest", str(CORPUS / "manifest.json"), "--corpus-base", base,
                   "--out", str(out),
                   "--noise-exempt", f"{base}/sentinel/", "--noise-exempt-reason", EXEMPT_REASON]
            for pid in args.only:
                cmd += ["--only", pid]
            rc = subprocess.call(cmd)
            if rc != 0:
                print(f"explore.py exited {rc}")
                return rc
        finally:
            server.terminate()
            server.wait(timeout=10)

    # ---- server channel -------------------------------------------------------
    server_log = [json.loads(ln) for ln in log_path.read_text().splitlines() if ln.strip()]
    server_all = {e["path"] for e in server_log}

    rows, failures = [], []
    explorer_all: set[str] = set()
    rescued_total = 0
    for p in pages:
        data = json.loads((out / f"{p['id']}.json").read_text())
        runs = [r for r in data.get("runs", []) if "error" not in r]
        page_path = "/" + p["path"]
        expected = set(p["resource_sentinels"])
        stable = sentinel_paths(data.get("stable_new_content", {}))
        unstable = sentinel_paths(data.get("unstable_new_content", {}))
        baseline_explorer = sentinel_paths(data.get("baseline", []))
        requested = set().union(*(sentinel_paths(r["all_requests_raw"]) for r in runs)) if runs else set()
        explorer_all |= requested
        # Server-side baseline: any sentinel for this page logged inside a baseline window.
        baseline_server = set()
        for r in runs:
            t0, t1 = r["baseline_window"]
            for e in server_log:
                if t0 <= e["ts"] <= t1 and urllib.parse.urlsplit(e["referer"]).path == page_path:
                    baseline_server.add(e["path"])
        server_page = {e["path"] for e in server_log
                       if urllib.parse.urlsplit(e["referer"]).path == page_path}
        rescued = {urllib.parse.urlsplit(x["url"]).path for r in runs for x in r["noise_exempted"]}
        rescued_total += len(rescued & expected)
        missed = sorted(expected - stable)
        attribution = {}
        for u, v in data.get("stable_new_content", {}).items():
            path = urllib.parse.urlsplit(u).path
            if path in expected:
                a = v["run1"]
                attribution[path.rsplit("/", 1)[-1]] = f"{a['action']}:{a['target']}#{a['iteration']}"
        cp = data.get("click_sources", {}).get("cursor-pointer", {})
        # Did any sentinel come from an element ONLY the cursor:pointer source found?
        cursor_only_sentinels = sorted(
            urllib.parse.urlsplit(u).path.rsplit("/", 1)[-1]
            for u, v in data.get("stable_new_content", {}).items()
            if urllib.parse.urlsplit(u).path in expected
            and v["run1"].get("sources") == ["cursor-pointer"])
        text = None
        if p["text_sentinels"]:
            text = {}
            for s in p["text_sentinels"]:
                text[s] = [any(s in frag for frag in r["novel_text_fragments"]) for r in runs]
        row = {"id": p["id"], "expected": len(expected), "stable": len(stable & expected),
               "unstable": sorted(unstable & expected), "missed": missed,
               "baseline_explorer": sorted(baseline_explorer),
               "baseline_server": sorted(baseline_server),
               "explorer_channel": sorted(requested), "server_channel": sorted(server_page),
               "channels_agree": requested == server_page,
               "rescued_by_exemption": len(rescued & expected),
               "attribution": attribution, "text_sentinels": text,
               "cursor_pointer_candidates": cp.get("candidates", 0),
               "cursor_pointer_only_source": cp.get("only_source", 0),
               "cursor_pointer_elements_clicked": cp.get("elements_clicked", 0),
               "cursor_only_sentinels": cursor_only_sentinels,
               "size_filtered": len(data.get("size_filtered", [])),
               "blocked_ads": len(data.get("blocked_ads", [])),
               "blocked_ads_requests": data.get("blocked_ads_requests", 0),
               "periodic_patterns": data.get("periodic_patterns", []),
               "periodic_excluded": data.get("periodic_excluded_requests", 0),
               "click_sources": data.get("click_sources", {}),
               "caps_hit": data.get("caps_hit"), "elapsed_s": data.get("elapsed_s")}
        rows.append(row)
        if missed:
            did = []
            for r in runs:
                did.append(sorted({f"{a['action']}:{a['target']}" for a in r["actions"]
                                   if a["action"] in ("hover", "click", "scroll")}))
            failures.append(f"{p['id']}: missed {missed}; manifest requires "
                            f"{p['required_actions']}; explorer performed {did}")
        if baseline_explorer or baseline_server:
            failures.append(f"{p['id']}: sentinel(s) in baseline: explorer {sorted(baseline_explorer)}"
                            f", server {sorted(baseline_server)}")
        if not row["channels_agree"]:
            failures.append(f"{p['id']}: channels disagree: only explorer "
                            f"{sorted(requested - server_page)}, only server "
                            f"{sorted(server_page - requested)}")

    total_expected = sum(r["expected"] for r in rows)
    total_stable = sum(r["stable"] for r in rows)
    global_agree = explorer_all == server_all
    if not global_agree:
        failures.append(f"global channel mismatch: only explorer {sorted(explorer_all - server_all)}"
                        f", only server {sorted(server_all - explorer_all)}")

    lines = ["# Explorer acceptance on the interaction corpus", "",
             f"Resource sentinels in the stable set: **{total_stable}/{total_expected}**. "
             f"Baseline sentinels: explorer channel **{sum(len(r['baseline_explorer']) for r in rows)}**, "
             f"server channel **{sum(len(r['baseline_server']) for r in rows)}**. "
             f"Channels agree: **{'yes' if global_agree and all(r['channels_agree'] for r in rows) else 'NO'}** "
             f"({len(explorer_all)} explorer / {len(server_all)} server sentinel paths).", "",
             f"Noise exemption: {rescued_total}/{total_expected} sentinels would have been "
             "filtered by the screener's `body under 1KB` rule without the corpus exemption.", "",
             f"cursor:pointer source: {sum(r['cursor_pointer_candidates'] for r in rows)} click "
             f"candidate(s) across the corpus, {sum(r['cursor_pointer_only_source'] for r in rows)} "
             f"of them found by that source alone, "
             f"{sum(r['cursor_pointer_elements_clicked'] for r in rows)} clicked. "
             f"Size-rule-only removals: {sum(r['size_filtered'] for r in rows)}.", "",
             f"Ad/tracker blocking: {sum(r['blocked_ads_requests'] for r in rows)} request(s) "
             f"blocked across the corpus. Periodic-fetch detection: "
             f"{sum(len(r['periodic_patterns']) for r in rows)} pattern(s) found, "
             f"{sum(r['periodic_excluded'] for r in rows)} request(s) excluded. "
             "Both are expected to be zero here: the corpus serves no ads and runs no timers.",
             "",
             "| page | sentinels (stable/expected) | attribution (run 1) | baseline (expl/server) | "
             "channels agree | cursor:pointer cand/only/clicked | size-adv | blocked | "
             "periodic pat/excl | text sentinels visible (run1, run2) | caps | elapsed |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        attr = ", ".join(f"{k} ← {v}" for k, v in sorted(r["attribution"].items())) or "—"
        text = ("; ".join(f"{s[-2:]}: {'/'.join('yes' if x else 'no' for x in v)}"
                          for s, v in r["text_sentinels"].items())
                if r["text_sentinels"] else "—")
        lines.append(f"| {r['id']} | {r['stable']}/{r['expected']} | {attr} | "
                     f"{len(r['baseline_explorer'])}/{len(r['baseline_server'])} | "
                     f"{'yes' if r['channels_agree'] else 'NO'} | "
                     f"{r['cursor_pointer_candidates']}/{r['cursor_pointer_only_source']}/"
                     f"{r['cursor_pointer_elements_clicked']} | {r['size_filtered']} | "
                     f"{r['blocked_ads_requests']} | "
                     f"{len(r['periodic_patterns'])}/{r['periodic_excluded']} | {text} | "
                     f"{', '.join(r['caps_hit'] or []) or 'none'} | {r['elapsed_s']}s |")
    if failures:
        lines += ["", "## Failures", ""] + [f"- {f}" for f in failures]
    report = "\n".join(lines) + "\n"
    (out / "acceptance.md").write_text(report)
    (out / "acceptance.json").write_text(json.dumps(rows, indent=2))
    print(report)
    print("PASS" if not failures else "FAIL")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
