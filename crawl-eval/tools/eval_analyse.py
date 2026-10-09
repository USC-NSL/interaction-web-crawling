#!/usr/bin/env python3
"""Analyse the evaluation crawls written by eval_run.py.

1. Instrument check, per page: the Browsertrix no-behavior crawl against the
   explorer's baseline (runs/rescreen), both filtered the same way (icons removed,
   the five Brave lists applied post hoc, periodic patterns removed). A page below
   IoU 1.0 is listed with its differences and excluded from scoring unless they are
   marked non-content in NONCONTENT_OK.
2. Recall of each config (A, B, C, F; isolated and combined; 2 repeats) against the
   explorer's click-attributed pool, per class (content, functional, presentational).
   Per page, then the mean across pages (each page counts once); pooled recall is a
   secondary figure. A config's finds are its WARC 200 set minus the baseline, minus
   icons and list-blocked URLs, so they are comparable with the pool.
3. Negatives: clicks made, behavior time, anything new fetched.
4. RevealClick's logs (C and F), per page.
5. Gap analysis: every pool resource F missed (union of its isolated runs), with the
   explorer's attribution and whether F found the element, skipped it, or stopped early.

Writes runs/eval/report.json and runs/eval/report.md.
"""

import json
import re
import statistics
import sys
import urllib.parse
from collections import Counter, defaultdict
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
RUNS = PKG_ROOT / "runs"
EVAL = RUNS / "eval"
sys.path.insert(0, str(PKG_ROOT / "tools"))
from pilot_analyse import (PostHocBlocker, crawl_log, iou, is_icon, ts,  # noqa: E402
                           url_key, warc_types)
from rescreen_analyse import analyse as explorer_analyse, explorer_result  # noqa: E402
from screen import normalise_request as path_key  # noqa: E402

CLASSES = ("content", "functional", "presentational")
CONFIGS = ("A", "B", "C", "F", "F2")
MODES = ("iso", "comb")
# Pages whose instrument-check differences were inspected and judged non-content.
# Judged from the listed differences (runs/eval/report.json, "instrument").
NONCONTENT_OK: dict[str, str] = {
    "biati-digital-github-io-glightbox": "explorer-only Google Fonts CSS request",
    "flickity-metafizzy-co": "Twitter widget syndication URL differs by query only",
    "keen-slider-io-examples": "web app manifest and its android-chrome icon (browser-only fetches)",
    "splidejs-com": "web app manifest and icon; decorative CSS mask image (images/masks/cloud01.jpg)",
    "admission-usc-edu-apply-faq": "web app manifest; WordPress emoji polyfill, loaded by feature detection",
    "developer-mozilla-org-en-us-docs-web-css-display":
        "example runner on a random mdnplay.dev subdomain per load, rotating ad images, "
        "telemetry, manifest; the click pool (search index, static icons) is unaffected",
    "design-system-service-gov-uk-components-accordion": "web app manifest",
    "getbootstrap-com-docs-5-3-components-accordion": "web app manifest and its icon",
}
# Excluded from scoring, with the reason (differences are content, or the pool drifted).
EXCLUDED: dict[str, str] = {
    "afarkas-github-io-lazysizes-index-html": "2 content images only in the explorer baseline",
    "kenwheeler-github-io-slick": "crawls loaded http://, the explorer https://: pool URLs cannot match",
    "www-gymshark-com-collections-all-products-mens": "product images rotated; site redeployed",
    "www-seriouseats-com-classic-panzanella-salad-recipe": "video segments differ; ads",
    "food52-com-recipes": "content data query (Sanity) only in the explorer baseline; redeploy",
    "www-ikea-com-us-en-cat-sofas-fu003": "redeploy (bundle names), location-based store lookup, product images",
    "vite-dev-guide": "site redeployed: every hashed asset name changed, including the pool's",
    "www-ssa-gov-faqs-en": "HTTP 403 to every Browsertrix crawl",
}


# ---- instrument check ----------------------------------------------------------------

def filtered(urls: dict[str, str], page_url: str, blocker: PostHocBlocker,
             periodic: set[str]) -> set[str]:
    out = set()
    for u, rtype in urls.items():
        if is_icon(u) or path_key(u) in periodic:
            continue
        if blocker.match(u, page_url, rtype or "other"):
            continue
        out.add(u)
    return out


def explorer_baseline(ex: dict) -> tuple[dict[str, str], set[str]]:
    runs = [r for r in ex["runs"] if "baseline_raw" in r]
    types: dict[str, str] = {}
    for r in runs:
        for raw, t in (r.get("raw_types") or {}).items():
            types.setdefault(url_key(raw), t)
    base = {}
    for r in runs:
        for raw in r["baseline_raw"]:
            if r["baseline_status"].get(path_key(raw)) == 200:
                base[url_key(raw)] = types.get(url_key(raw), "")
    return base, set(ex.get("periodic_patterns", []))


# ---- logs ----------------------------------------------------------------------------

def behavior_time(log: list[dict]) -> tuple[float | None, bool]:
    start = end = None
    timed_out = False
    for rec in log:
        msg = rec.get("message", "")
        if msg == "Run Behaviors Started" and start is None:
            start = ts(rec)
        elif msg == "Behaviors finished":
            end = ts(rec)
        elif msg.startswith("Behaviors timed out"):
            timed_out = True
    return (round(end - start, 1) if start and end else None), timed_out


SKIP = re.compile(r'^Skipping element \((form|submit|path|consent|label "([^"]*)": (.*?))\): (.*)$')
PHRASE = re.compile(r"add\b.*\bto\s|buy|check\s?out|wish\s?list|favou?rite|rate|rating|vote|helpful",
                    re.I)


def skip_rule(kind: str, term: str | None) -> str:
    if term is None:
        return kind
    t = term.lower()
    if t.startswith("add ") and " to " in t:
        t = "add ... to " + t.rsplit(" to ", 1)[1]
    return ("phrase:" if PHRASE.search(term) else "word:") + t


def revealclick_log(log: list[dict]) -> dict:
    elements, sources, skips, skipped = [], Counter(), Counter(), []
    blocked, opens, navs, settle = [], [], [], None
    for rec in log:
        msg = rec.get("message", "")
        m = re.match(r"^Waited (\d+)ms for the page to settle", msg)
        if m:
            settle = int(m.group(1))
        m = re.match(r"^Clicking on element \(([^)]*)\)(?: \[scan \d+ms\])?: (.*)", msg)
        if m:
            srcs = [s for s in m.group(1).split(", ") if s]
            sources.update(srcs)
            elements.append({"sources": srcs, "html": m.group(2)[:100], "clicks": None})
        elif msg.startswith("Clicked ") and elements:
            elements[-1]["clicks"] = int(msg.split()[1])
        m = SKIP.match(msg)
        if m:
            rule = skip_rule(m.group(1).split(" ")[0] if m.group(2) is None else "label", m.group(2))
            skips[rule] += 1
            skipped.append({"rule": rule, "html": m.group(4)[:100]})
        m = re.match(r"^Blocked (\w+) request: (.*)", msg)
        if m:
            blocked.append({"method": m.group(1), "url": m.group(2)})
        if msg.startswith("Blocked window.open: "):
            opens.append(msg[len("Blocked window.open: "):])
        if msg.startswith("Blocked navigation to: "):
            navs.append(msg[len("Blocked navigation to: "):])
    clicks = [e["clicks"] for e in elements if e["clicks"] is not None]
    return {"settle_ms": settle, "elements": len(elements), "clicks_total": sum(clicks),
            "clicks_per_element": sorted(clicks, reverse=True), "cap_hits": sum(c >= 10 for c in clicks),
            "sources": dict(sources), "skips_by_rule": dict(skips), "blocked_requests": blocked,
            "window_open": opens, "blocked_navigations": navs, "clicked": elements, "skipped": skipped}


def stock_clicks(log: list[dict]) -> int:
    return sum(1 for r in log if str(r.get("message", "")).startswith(("Clicking on link", "Click empty link")))


# ---- gap analysis --------------------------------------------------------------------

def element_sig(css_last: str) -> tuple[str, str | None, set[str]]:
    m = re.match(r"^([a-zA-Z0-9-]+)?(#[\w-]+)?((?:\.[\w\[\]\-]+)*)", css_last.strip())
    tag, ident, cls = (m.group(1) or "").lower(), (m.group(2) or "")[1:] or None, m.group(3) or ""
    return tag, ident, {c for c in cls.split(".") if c}


def html_sig(html: str) -> tuple[str, str | None, set[str]]:
    tag = (re.match(r"^<([a-zA-Z0-9-]+)", html) or [None, ""])[1].lower()
    ident = (re.search(r'\bid="([^"]+)"', html) or [None, None])[1]
    cls = set(((re.search(r'\bclass="([^"]*)"', html) or [None, ""])[1]).split())
    return tag, ident, cls


def same_element(target: str, html: str) -> bool:
    t_tag, t_id, t_cls = element_sig(target.split(">")[-1].split(":")[0])
    h_tag, h_id, h_cls = html_sig(html)
    if t_tag and h_tag and t_tag != h_tag:
        return False
    if t_id and h_id:
        return t_id == h_id
    return bool(t_cls & h_cls) if t_cls else False


def gap_reason(attr: dict, f_logs: list[dict]) -> str:
    run = attr.get("run1") or attr.get("run2") or {}
    target, iteration = run.get("target") or "", run.get("iteration") or 1
    clicked = [e for log in f_logs for e in log["clicked"] if same_element(target, e["html"])]
    if clicked:
        most = max((e["clicks"] or 0) for e in clicked)
        if most < iteration:
            return f"stopped early: F clicked it {most}x, the explorer needed {iteration}"
        return f"found and clicked {most}x (explorer: iteration {iteration}); missed for another reason"
    skipped = [s for log in f_logs for s in log["skipped"] if same_element(target, s["html"])]
    if skipped:
        return f"skipped by RevealClick: {skipped[0]['rule']}"
    return "never found (no matching clicked or skipped element)"


# ---- main ----------------------------------------------------------------------------

def main() -> int:
    import argparse
    global EVAL
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-dir", type=Path, default=EVAL)
    ap.add_argument("--pages-file", type=Path, default=PKG_ROOT / "tools" / "eval_pages.json")
    ap.add_argument("--judgments", type=Path,
                    help="JSON with noncontent_ok and excluded, replacing the eval judgments")
    args = ap.parse_args()
    EVAL = args.run_dir.resolve()
    if args.judgments:
        judged = json.loads(args.judgments.read_text())
        NONCONTENT_OK.clear(), NONCONTENT_OK.update(judged["noncontent_ok"])
        EXCLUDED.clear(), EXCLUDED.update(judged["excluded"])
    pages = json.loads(args.pages_file.read_text())["pages"]
    blocker = PostHocBlocker()
    report: dict = {"pages": {}}
    for p in pages:
        pd = EVAL / p["id"]
        # explored back-to-back with its crawls when the run did so, else the re-screen
        ex_dir = pd / "explorer" if (pd / "explorer" / "done.json").exists() else RUNS / "rescreen" / p["id"]
        ex = explorer_result(ex_dir)
        row, _v = explorer_analyse({**p, "tag": p["set"], "dir": ex_dir}, blocker)
        entry: dict = {"url": p["url"], "set": p["set"], "explorer_dir": str(ex_dir)}
        report["pages"][p["id"]] = entry
        if ex is None or not (pd / "none" / "done.json").exists():
            entry["status"] = "missing explorer result or no-behavior crawl"
            continue
        ex_base, periodic = explorer_baseline(ex)
        bt = warc_types(pd / "none")
        bt_f = filtered(bt, p["url"], blocker, periodic)
        ex_f = filtered(ex_base, p["url"], blocker, periodic)
        entry["instrument"] = {"iou": iou(bt_f, ex_f), "only_browsertrix": sorted(bt_f - ex_f),
                               "only_explorer": sorted(ex_f - bt_f)}
        ok = (entry["instrument"]["iou"] == 1.0 or p["id"] in NONCONTENT_OK) and p["id"] not in EXCLUDED
        entry["scored"] = ok
        entry["instrument"]["judgment"] = (EXCLUDED.get(p["id"]) or NONCONTENT_OK.get(p["id"])
                                           or ("identical" if entry["instrument"]["iou"] == 1.0 else ""))
        baseline = bt_f | ex_f

        # the pool is the explorer's click-attributed finds only, minus the baseline: a
        # resource either load already fetched cannot be a reveal for any config
        click = {u: g for u, g in row["gated"].items() if "click" in g["actions"]}
        pool = {c: {u for u, g in click.items() if g["class"] == c and u not in baseline}
                for c in CLASSES}
        entry["pool"] = {c: len(v) for c, v in pool.items()}
        entry["pool_in_baseline"] = sorted(u for u in click if u in baseline)[:50]

        configs: dict = {}
        f_iso_logs, f_iso_new = [], set()
        # A fetch counts as click-caused only if no zero-click run of this page made it:
        # the no-behavior crawl (already the baseline) and every config run with 0 clicks.
        # Compared by scheme, host and path: a query that changes per request (a
        # cache-buster, a sensor's nonce) would make every repeat look new.
        rep_new: dict[str, list[set]] = {}
        zero_click: set = set()
        for cfg in CONFIGS:
            for mode in MODES:
                reps = []
                for rep in (1, 2):
                    jd = pd / f"{cfg}-{mode}-r{rep}"
                    if not (jd / "done.json").exists():
                        continue
                    types = warc_types(jd)
                    new = filtered(types, p["url"], blocker, periodic) - baseline
                    log = crawl_log(jd)
                    t, to = behavior_time(log)
                    rec = {"new": new, "time": t, "timed_out": to}
                    if cfg in ("C", "F", "F2"):
                        rec["rc"] = revealclick_log(log)
                        rec["clicks"] = rec["rc"]["clicks_total"]
                    else:
                        rec["clicks"] = stock_clicks(log)
                    reps.append(rec)
                    if rec["clicks"] == 0:
                        zero_click |= {path_key(u) for u in new}
                    if cfg == "F" and mode == "iso":
                        f_iso_logs.append(rec["rc"])
                        f_iso_new |= new
                if not reps:
                    continue
                union = set().union(*(r["new"] for r in reps))
                rep_new[f"{cfg}-{mode}"] = [r["new"] for r in reps]
                configs[f"{cfg}-{mode}"] = {
                    "recall": {c: ([len(r["new"] & pool[c]) for r in reps], len(union & pool[c]), len(pool[c]))
                               for c in CLASSES},
                    "time": [r["time"] for r in reps], "timed_out": [r["timed_out"] for r in reps],
                    "clicks": [r["clicks"] for r in reps],
                    "new_total": [len(r["new"]) for r in reps],
                    "new_outside_pool": sorted(union - set().union(*pool.values()))[:50],
                    "revealclick": [r["rc"] for r in reps] if cfg in ("C", "F", "F2") else None,
                }
        for key, news in rep_new.items():
            caused = [{u for u in n if path_key(u) not in zero_click} for n in news]
            configs[key]["click_caused"] = [len(c) for c in caused]
            configs[key]["click_caused_urls"] = sorted(set().union(*caused))[:50]
        entry["zero_click_runs_new"] = sorted(zero_click)[:50]
        entry["configs"] = configs
        missed = sorted(set().union(*pool.values()) - f_iso_new)
        entry["gaps_F"] = [{"url": u, "class": row["gated"][u]["class"],
                            "explorer": row["gated"][u]["attribution"],
                            "reason": gap_reason(row["gated"][u]["attribution"], f_iso_logs)}
                           for u in missed] if f_iso_logs else []

    # ---- summaries: mean across pages (each page once), pooled secondary --------------
    summary = {}
    for s in ("demo", "production"):
        for cfg in CONFIGS:
            for mode in MODES:
                key = f"{cfg}-{mode}"
                for c in CLASSES:
                    per_page, num, den = [], 0, 0
                    for e in report["pages"].values():
                        if e.get("set") != s or not e.get("scored") or key not in e.get("configs", {}):
                            continue
                        _reps, got, size = e["configs"][key]["recall"][c]
                        if size:
                            per_page.append(got / size)
                            num, den = num + got, den + size
                    if per_page:
                        summary.setdefault(s, {}).setdefault(key, {})[c] = {
                            "mean_recall": round(statistics.mean(per_page), 3), "pages": len(per_page),
                            "pooled_recall": round(num / den, 3), "pooled": f"{num}/{den}"}
    report["summary"] = summary
    (EVAL / "report.json").write_text(json.dumps(report, indent=2, default=sorted))
    print(f"wrote {EVAL / 'report.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
