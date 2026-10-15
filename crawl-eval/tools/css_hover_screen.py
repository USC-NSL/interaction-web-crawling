#!/usr/bin/env python3
"""Static screen for CSS :hover reveals: load each page once and read its stylesheets.

The Tranco pages were chosen for the hover evaluation from explorer results that
predate the explorer's :hover-rule source, so pure CSS hover reveals are missing from
that selection. This screen finds them without interacting: each page is loaded once
(the explorer's crawler-matched browser, no scroll, no hover, no click), and the
explorer's own parser (explore.CSS_HOVER_JS) lists every same-origin :hover rule that
changes display, visibility or opacity or sets a background-image url, with whether
its subject (the selector up to the compound carrying :hover) exists in the DOM.
Cross-origin stylesheets cannot be read from the page and are counted as unreadable.

Input: --pages, a JSON file {"pages": [{"id", "url"}, ...]}; with --explored DIR only
the pages that have an explorer result there. Output: <out>/<id>.json per page and
<out>/summary.json. Etiquette as parallel_explore.py (its scheduler): at most one job
per host at a time, --min-host-gap seconds between loads of a host counted across
every run directory's jobs.jsonl, round-robin across hosts, the explorer's contact URL
in the user agent. Resumable: a page with a result file is skipped.
"""

import argparse
import json
import sys
import threading
import time
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG_ROOT / "tools"))
sys.path.insert(0, str(PKG_ROOT.parent / "page-screener" / "tools"))
import explore  # noqa: E402
from eval_run import MIN_HOST_GAP  # noqa: E402
from parallel_explore import Scheduler, host, round_robin  # noqa: E402

LOAD_TIMEOUT_MS = 45000
IDLE_TIMEOUT_MS = 15000
SETTLE_MS = 3000

REPORT_JS = "() => {" + explore.CSS_HOVER_JS + r"""
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && (!el.checkVisibility || el.checkVisibility());
  };
  const {rules, sheets, unreadable} = cssHoverRules(document);
  return {
    sheets, unreadable,
    rules: rules.map((r) => ({
      selector: r.selector.slice(0, 200), target: r.target.slice(0, 200), props: r.props,
      parsed: r.subjects !== null,
      subjects: r.subjects ? r.subjects.length : 0,
      visible_subjects: r.subjects ? r.subjects.filter(visible).length : 0,
    })),
  };
}"""


def screen(browser, url: str) -> dict:
    ctx = browser.new_context(viewport=explore.CRAWLER_VIEWPORT, user_agent=explore.CRAWLER_UA,
                              device_scale_factor=1, extra_http_headers=explore.CRAWLER_HEADERS,
                              accept_downloads=False, service_workers="block")
    try:
        page = ctx.new_page()
        page.on("dialog", lambda d: d.dismiss())
        ctx.on("page", lambda p: p.close() if p != page else None)
        page.goto(url, wait_until="load", timeout=LOAD_TIMEOUT_MS)
        try:
            page.wait_for_load_state("networkidle", timeout=IDLE_TIMEOUT_MS)
        except Exception:  # noqa: BLE001 - a page that never goes idle is still read
            pass
        page.wait_for_timeout(SETTLE_MS)
        report = page.evaluate(REPORT_JS)
        report["final_url"] = page.url
        return report
    finally:
        ctx.close()


FETCH_PROPS = {"background-image", "border-image-source", "list-style-image", "cursor", "content"}


def summarise(rec: dict) -> dict:
    """Counts beside the rule list, which is kept: a selection reads the rules."""
    rules = rec.get("rules", [])
    fetch = [r for r in rules if FETCH_PROPS & set(r["props"])]
    return {"rule_count": len(rules),
            "rules_with_subject": sum(1 for r in rules if r["subjects"]),
            "rules_with_visible_subject": sum(1 for r in rules if r["visible_subjects"]),
            "fetch_rules_with_subject": sum(1 for r in fetch if r["subjects"])}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pages", type=Path, required=True)
    ap.add_argument("--explored", type=Path,
                    help="keep only pages with an explorer result file here (failed runs included)")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--min-host-gap", type=float, default=MIN_HOST_GAP)
    args = ap.parse_args()

    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    pages = json.loads(args.pages.read_text())["pages"]
    if args.explored:
        have = {p.parent.parent.name for p in args.explored.glob("*/explorer/*.json")
                if p.name not in ("done.json", "command.json")}
        pages = [p for p in pages if p["id"] in have]
    todo = [p for p in round_robin(pages) if not (out / f"{p['id']}.json").exists()]
    print(f"{len(pages)} page(s), {len(todo)} to screen, {args.workers} worker(s), "
          f"{args.min_host_gap:.0f}s per host", flush=True)

    last_end: dict[str, float] = {}
    for prior in set(PKG_ROOT.glob("runs/*/jobs.jsonl")) | {out / "jobs.jsonl"}:
        if prior.exists():
            for line in prior.read_text().splitlines():
                rec = json.loads(line)
                h = host(rec.get("url") or "https://" + rec["host"])
                last_end[h] = max(last_end.get(h, 0), rec["end"])
    total = len(todo)   # the scheduler takes pages off todo
    sched = Scheduler(todo, last_end, args.min_host_gap)
    lock = threading.Lock()
    finished = [0]

    def worker(n: int) -> None:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="chromium", args=explore.CRAWLER_ARGS,
                                         ignore_default_args=["--enable-automation",
                                                              "--hide-scrollbars"])
            while (page := sched.take()) is not None:
                h = host(page["url"])
                start = time.time()
                try:
                    rec = {"id": page["id"], "url": page["url"], **screen(browser, page["url"])}
                    code = 0
                except Exception as exc:  # noqa: BLE001 - one bad page must not end the batch
                    rec = {"id": page["id"], "url": page["url"],
                           "error": f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"}
                    code = 1
                end = time.time()
                sched.release(h, end)
                rec.update(summarise(rec), elapsed_s=round(end - start, 1))
                (out / f"{page['id']}.json").write_text(json.dumps(rec, indent=1))
                with lock:
                    finished[0] += 1
                    with (out / "jobs.jsonl").open("a") as fh:
                        fh.write(json.dumps({"page": page["id"], "url": page["url"],
                                             "job": "css-screen", "host": h, "worker": n,
                                             "start": start, "end": end, "exit_code": code}) + "\n")
                    print(f"[{finished[0]}/{total}] w{n} {h} {page['id']}: "
                          f"{rec.get('error') or str(rec['rule_count']) + ' rules, ' + str(rec['rules_with_subject']) + ' with subject, ' + str(rec['fetch_rules_with_subject']) + ' fetching'}",
                          flush=True)
            browser.close()

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(1, args.workers + 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    recs = [json.loads((out / f"{p['id']}.json").read_text()) for p in pages
            if (out / f"{p['id']}.json").exists()]
    ok = [r for r in recs if "error" not in r]
    summary = {
        "pages": len(pages), "screened": len(ok), "errors": [r["id"] for r in recs if "error" in r],
        "pages_with_rule": sum(1 for r in ok if r["rule_count"]),
        "pages_with_rule_and_subject": sum(1 for r in ok if r["rules_with_subject"]),
        "pages_with_rule_and_visible_subject": sum(1 for r in ok if r["rules_with_visible_subject"]),
        "hosts_with_rule_and_subject": len({host(r["url"]) for r in ok if r["rules_with_subject"]}),
        "pages_with_fetch_rule_and_subject": sum(1 for r in ok if r["fetch_rules_with_subject"]),
        "hosts_with_fetch_rule_and_subject": len({host(r["url"]) for r in ok if r["fetch_rules_with_subject"]}),
        "pages_with_unreadable_sheets": sum(1 for r in ok if r.get("unreadable")),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
