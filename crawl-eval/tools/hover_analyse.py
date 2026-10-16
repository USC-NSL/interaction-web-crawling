#!/usr/bin/env python3
"""Analyse the hover evaluation (parallel_explore.py --crawl over runs/hover-eval).

Per page, back-to-back: the explorer, a no-behavior crawl, and five configs, one run
each: RC-iso (RevealClick alone), AHS-iso (AutoHover, synthetic events), AHR-iso
(AutoHover, real pointer input), SEQ-iso (AutoHover then RevealClick), SEQ-comb (the
same with autoplay, autofetch and autoscroll).

1. Instrument check: the no-behavior crawl against the explorer's baseline, filtered as
   in eval_analyse (icons, the post-hoc lists, periodic patterns). Reported per page;
   pages are scored whatever the IoU, and the report lists the low ones.
2. Pools: the explorer's stable resources credited to a hover, and to a click, per class
   (content, functional, presentational), minus anything either baseline fetched. The
   hover pool is split by how the explorer found the hovered element: css (a :hover
   rule), listener (a CDP hover listener), native (a native control only). An element
   found by both css and listener counts as both.
3. Recall of each config against each pool: per page, then the mean over pages with a
   non-empty pool (each page counts once), with the pooled figure. A config's finds are
   its WARC 200 set minus the baseline, filtered as the pool is.
4. AutoHover from its logs: hovers, real and synthetic, flow timeouts, time per hover
   and its wait, its own time, skips by reason. Behavior time from the crawl log.
5. By page source (pages.json tags), and the pages with no hover pool (negatives, hover
   artifacts, menu-only): hovers made, time, and anything new fetched.

Writes <run>/report.json and <run>/report.md.
"""

import argparse
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG_ROOT / "tools"))
from eval_analyse import behavior_time, explorer_baseline, filtered, revealclick_log, warc_meta  # noqa: E402
from pilot_analyse import PostHocBlocker, crawl_log, iou  # noqa: E402
from rescreen_analyse import analyse as explorer_analyse, explorer_result  # noqa: E402

CLASSES = ("content", "functional", "presentational")
CONFIGS = ("RC-iso", "AHS-iso", "AHR-iso", "SEQ-iso", "SEQ-comb")
TARGETS = ("css", "listener", "native")
HOVERED = re.compile(r"^Hovered \((\w+)\) in (\d+)ms \[waited (\d+)ms\]")
DONE = re.compile(r"^AutoHover done in (\d+)ms: (\d+) hovers \((\d+) real, (\d+) synthetic\), "
                  r"(\d+) path hovers, (\d+) flow timeouts")
SKIPPED = re.compile(r"^Skipping element \(([\w-]+)")


def hover_targets(g: dict) -> set[str]:
    """How the explorer found the element whose hover revealed the resource."""
    srcs = set()
    for run in ("run1", "run2"):
        att = (g.get("attribution") or {}).get(run) or {}
        if att.get("action") == "hover":
            srcs |= set(att.get("sources") or [])
    out = set()
    if "css-hover" in srcs:
        out.add("css")
    if "cdp-listener" in srcs:
        out.add("listener")
    if not out and "native" in srcs:
        out.add("native")
    return out


def autohover_log(log: list[dict]) -> dict:
    hovers, done, skips = [], [], Counter()
    for rec in log:
        msg = rec.get("message", "")
        m = HOVERED.match(msg)
        if m:
            hovers.append((m.group(1), int(m.group(2)), int(m.group(3))))
        m = DONE.match(msg)
        if m:
            done.append(int(m.group(1)))
            # flow timeouts are only in the summary line, one per frame
            skips["_flow_timeouts"] += int(m.group(6))
            skips["_path_hovers"] += int(m.group(5))
        m = SKIPPED.match(msg)
        if m:
            skips[m.group(1)] += 1
    ms = [h[1] for h in hovers]
    waited = [h[2] for h in hovers]
    return {"hovers": len(hovers), "real": sum(1 for h in hovers if h[0] == "real"),
            "synthetic": sum(1 for h in hovers if h[0] == "synthetic"),
            "flow_timeouts": skips.pop("_flow_timeouts", 0), "path_hovers": skips.pop("_path_hovers", 0),
            "ms_median": statistics.median(ms) if ms else None,
            "wait_median": statistics.median(waited) if waited else None,
            "autohover_ms": sum(done) if done else None, "frames": len(done),
            "budget": any("Time budget reached" in r.get("message", "") for r in log),
            "skips": dict(skips)}


def safety_log(log: list[dict]) -> dict:
    out = {"blocked_requests": 0, "blocked_navigations": 0, "blocked_opens": 0, "open_not_stubbed": 0}
    for rec in log:
        msg = rec.get("message", "")
        out["blocked_requests"] += bool(re.match(r"^Blocked \w+ request: ", msg))
        out["blocked_navigations"] += msg.startswith("Blocked navigation to: ")
        out["blocked_opens"] += msg.startswith("Blocked window.open: ")
        out["open_not_stubbed"] += msg == "window.open overridden by page, not stubbed"
    return out


def mean_recall(rows: list[tuple[int, int]]) -> dict | None:
    rows = [(got, size) for got, size in rows if size]
    if not rows:
        return None
    return {"mean": round(statistics.mean(g / s for g, s in rows), 3), "pages": len(rows),
            "pooled": f"{sum(g for g, _ in rows)}/{sum(s for _, s in rows)}"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", type=Path, default=PKG_ROOT / "runs" / "hover-eval")
    args = ap.parse_args()
    run = args.run.resolve()
    pages = json.loads((run / "pages.json").read_text())["pages"]
    blocker = PostHocBlocker()
    report: dict = {"pages": {}}

    for p in pages:
        pd = run / p["id"]
        entry: dict = {"url": p["url"], "sources": p["sources"]}
        report["pages"][p["id"]] = entry
        ex = explorer_result(pd / "explorer")
        if ex is None or not ex.get("explored") or not (pd / "none" / "done.json").exists():
            entry["status"] = "no explorer result or no-behavior crawl"
            continue
        row, _ = explorer_analyse({**p, "tag": "hover", "dir": pd / "explorer", "group": None,
                                   "screener_stable": None}, blocker)
        ex_base, periodic = explorer_baseline(ex)
        bt_f = filtered({u: m["type"] for u, m in warc_meta(pd / "none").items()}, p["url"], blocker, periodic)
        ex_f = filtered(ex_base, p["url"], blocker, periodic)
        entry["instrument_iou"] = iou(bt_f, ex_f)
        baseline = bt_f | ex_f
        gated = {u: g for u, g in row.get("gated", {}).items() if u not in baseline}
        hover = {u: g for u, g in gated.items() if "hover" in g["actions"]}
        click = {u: g for u, g in gated.items() if "click" in g["actions"]}
        pools = {"hover": {c: {u for u, g in hover.items() if g["class"] == c} for c in CLASSES},
                 "click": {c: {u for u, g in click.items() if g["class"] == c} for c in CLASSES}}
        by_target = {t: {u for u, g in hover.items() if t in hover_targets(g) and g["class"] in CLASSES}
                     for t in TARGETS}
        entry["pool"] = {k: {c: len(v) for c, v in d.items()} for k, d in pools.items()}
        entry["hover_by_target"] = {t: len(v) for t, v in by_target.items()}
        entry["explorer_failed_hovers"] = sum(
            1 for r in ex["runs"] for t in r.get("time_on_page", []) if t.get("reason") == "failed hover")

        configs = {}
        for cfg in CONFIGS:
            jd = pd / cfg
            done = jd / "done.json"
            if not done.exists():
                continue
            code = json.loads(done.read_text()).get("exit_code")
            if code != 0:
                configs[cfg] = {"exit_code": code}
                continue
            meta = warc_meta(jd)
            new = filtered({u: m["type"] for u, m in meta.items()}, p["url"], blocker, periodic) - baseline
            log = crawl_log(jd)
            t, timed_out = behavior_time(log)
            rc = revealclick_log(log)
            configs[cfg] = {
                "exit_code": 0, "time_s": t, "timed_out": timed_out, "new": len(new),
                "recall": {k: {c: [len(new & d[c]), len(d[c])] for c in CLASSES} for k, d in pools.items()},
                "hover_by_target": {tg: [len(new & v), len(v)] for tg, v in by_target.items()},
                "autohover": autohover_log(log) if cfg != "RC-iso" else None,
                "clicks": rc["clicks_total"], "safety": safety_log(log),
                "new_outside_pools": len(new - set(hover) - set(click)),
            }
        entry["configs"] = configs

    # ---- summaries -------------------------------------------------------------------
    scored = {pid: e for pid, e in report["pages"].items() if "configs" in e}

    def recall_table(subset: dict[str, dict]) -> dict:
        out = {}
        for cfg in CONFIGS:
            for pool in ("hover", "click"):
                for c in CLASSES:
                    r = mean_recall([tuple(e["configs"][cfg]["recall"][pool][c]) for e in subset.values()
                                     if e["configs"].get(cfg, {}).get("exit_code") == 0])
                    if r:
                        out.setdefault(cfg, {}).setdefault(pool, {})[c] = r
            for tg in TARGETS:
                r = mean_recall([tuple(e["configs"][cfg]["hover_by_target"][tg]) for e in subset.values()
                                 if e["configs"].get(cfg, {}).get("exit_code") == 0])
                if r:
                    out.setdefault(cfg, {}).setdefault("hover_by_target", {})[tg] = r
        return out

    report["summary"] = {"all": recall_table(scored)}
    tags = sorted({s for e in scored.values() for s in e["sources"]})
    report["summary"]["by_source"] = {t: recall_table({k: e for k, e in scored.items() if t in e["sources"]})
                                      for t in tags}

    cost = defaultdict(lambda: defaultdict(list))
    for e in scored.values():
        for cfg, c in e["configs"].items():
            if c.get("exit_code") != 0:
                continue
            for tag in e["sources"]:
                cost[tag][cfg].append(c)
    report["summary"]["cost_by_source"] = {
        tag: {cfg: {"pages": len(cs),
                    "behavior_s_median": statistics.median([c["time_s"] for c in cs if c["time_s"] is not None] or [0]),
                    "hovers_median": statistics.median([(c["autohover"] or {}).get("hovers", 0) for c in cs]),
                    "clicks_median": statistics.median([c["clicks"] for c in cs]),
                    "pages_with_new_fetches": sum(1 for c in cs if c["new"])}
              for cfg, cs in by.items()} for tag, by in cost.items()}

    ah = defaultdict(lambda: Counter())
    waits = defaultdict(list)
    for e in scored.values():
        for cfg, c in e["configs"].items():
            a = c.get("autohover")
            if not a:
                continue
            for k in ("hovers", "real", "synthetic", "flow_timeouts", "path_hovers"):
                ah[cfg][k] += a[k]
            ah[cfg]["budget_hits"] += a["budget"]
            ah[cfg]["timed_out"] += bool(c["timed_out"])
            for k, n in a["skips"].items():
                ah[cfg]["skip:" + k] += n
            if a["wait_median"] is not None:
                waits[cfg].append(a["wait_median"])
    report["summary"]["autohover"] = {cfg: {**dict(v), "per_page_wait_median_ms":
                                            statistics.median(waits[cfg]) if waits[cfg] else None}
                                      for cfg, v in ah.items()}
    report["summary"]["safety"] = {cfg: dict(sum((Counter(e["configs"][cfg]["safety"]) for e in scored.values()
                                                  if e["configs"].get(cfg, {}).get("exit_code") == 0), Counter()))
                                   for cfg in CONFIGS}
    report["summary"]["instrument"] = {
        "pages": len(scored),
        "iou_1": sum(1 for e in scored.values() if e["instrument_iou"] == 1.0),
        "iou_below_0.9": sorted((pid, round(e["instrument_iou"], 2)) for pid, e in scored.items()
                                if e["instrument_iou"] is not None and e["instrument_iou"] < 0.9),
        "iou_undefined": sorted(pid for pid, e in scored.items() if e["instrument_iou"] is None)}
    report["summary"]["failed_cells"] = sorted((pid, cfg, c["exit_code"]) for pid, e in report["pages"].items()
                                               for cfg, c in e.get("configs", {}).items() if c.get("exit_code") != 0)
    report["summary"]["pools"] = {
        "pages_with_hover_pool": sum(1 for e in scored.values() if sum(e["pool"]["hover"].values())),
        "hover_resources": {c: sum(e["pool"]["hover"][c] for e in scored.values()) for c in CLASSES},
        "hover_by_target": {t: sum(e["hover_by_target"][t] for e in scored.values()) for t in TARGETS},
        "click_resources": {c: sum(e["pool"]["click"][c] for e in scored.values()) for c in CLASSES},
        "explorer_failed_hovers": sum(e["explorer_failed_hovers"] for e in scored.values())}
    (run / "report.json").write_text(json.dumps(report, indent=2, default=str) + "\n")

    # ---- markdown ----------------------------------------------------------------
    s = report["summary"]
    md = [f"# Hover evaluation: {len(scored)} pages scored of {len(pages)}", "",
          "Recall: mean over pages with a non-empty pool (each page once), pooled in brackets.", "",
          f"Pools: {s['pools']}", f"Instrument: {s['instrument']}", f"Failed cells: {s['failed_cells']}", "",
          "## Hover pool recall", "", "| config | content | functional | presentational | by target: css | listener | native |",
          "|---|---|---|---|---|---|---|"]
    fmt = lambda r: f"{r['mean']:.2f} ({r['pooled']}, {r['pages']}p)" if r else "—"  # noqa: E731
    for cfg in CONFIGS:
        t = s["all"].get(cfg, {})
        md.append(f"| {cfg} | " + " | ".join(fmt(t.get("hover", {}).get(c)) for c in CLASSES) + " | "
                  + " | ".join(fmt(t.get("hover_by_target", {}).get(tg)) for tg in TARGETS) + " |")
    md += ["", "## Click pool recall", "", "| config | content | functional | presentational |", "|---|---|---|---|"]
    for cfg in CONFIGS:
        t = s["all"].get(cfg, {})
        md.append(f"| {cfg} | " + " | ".join(fmt(t.get("click", {}).get(c)) for c in CLASSES) + " |")
    md += ["", "## Hover content recall by page source", "", "| source | " + " | ".join(CONFIGS) + " |",
           "|---|" + "---|" * len(CONFIGS)]
    for tag, t in s["by_source"].items():
        md.append(f"| {tag} | " + " | ".join(fmt(t.get(cfg, {}).get("hover", {}).get("content")) for cfg in CONFIGS) + " |")
    md += ["", "## AutoHover", "", "```", json.dumps(s["autohover"], indent=1), "```", "",
           "## Cost by source (median behavior seconds, hovers, clicks; pages with new fetches)", "", "```",
           json.dumps(s["cost_by_source"], indent=1), "```", "", "## Safety", "", "```",
           json.dumps(s["safety"], indent=1), "```"]
    (run / "report.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))
    return 0


if __name__ == "__main__":
    sys.exit(main())
