#!/usr/bin/env python3
"""Prevalence of interaction-gated resources in the Tranco sample (explorer only).

    .venv/bin/python tools/tranco_analyse.py --sample runs/tranco-4672X --run runs/tranco-4672X-explore

A resource is gated by an action when it is in the explorer's stable new-content set (both
runs), attributed to that action, after the post-hoc list filtering, icon removal and
classification of rescreen_analyse.analyse (classes: content, functional, presentational,
media, speculative; residual-tracking is reported apart).

Domain level: the share of domains with at least one explored page that has a resource of
that class gated by that action, with a 95% Wilson interval over domains.

Page level: the share of explored pages, a ratio estimate over domains (pages cluster within
a domain). Its variance is the cluster (ratio) estimator's; the design effect against simple
random sampling of pages gives an effective sample size, and the 95% Wilson interval is
computed on that effective size.

Writes <run>/tranco_report.json and <run>/tranco_report.md.
"""

import argparse
import json
import math
import re
import sys
import urllib.parse
from collections import Counter, defaultdict
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG_ROOT / "tools"))
from pilot_analyse import PostHocBlocker  # noqa: E402
from rescreen_analyse import CLASSES, TRACKING, analyse, classify_block  # noqa: E402

ACTIONS = ("click", "hover", "scroll")
BANDS = [(1, 1_000, "1-1k"), (1_001, 10_000, "1k-10k"), (10_001, 100_000, "10k-100k"),
         (100_001, 1_000_000, "100k-1M")]
CONSENT = re.compile(r"onetrust|\bot-|qc-cmp|sp_message|cky-|cookie|consent|tarteaucitron|"
                     r"didomi|cmp|usercentrics|first-visit", re.I)
MODAL = re.compile(r"modal|dialog|overlay|backdrop|popup|search|lightbox|drawer", re.I)


def wilson(k: float, n: float, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def domain_level(flags: dict[str, bool]) -> dict:
    n, k = len(flags), sum(flags.values())
    lo, hi = wilson(k, n)
    return {"k": k, "n": n, "p": k / n if n else float("nan"), "lo": lo, "hi": hi}


def page_level(by_domain: dict[str, list[bool]]) -> dict:
    """Ratio estimate over clusters; Wilson interval on the effective sample size."""
    ys = [sum(v) for v in by_domain.values()]
    ms = [len(v) for v in by_domain.values()]
    n, k, g = sum(ms), sum(ys), len(ms)
    if n == 0:
        return {"k": 0, "n": 0, "p": float("nan"), "lo": float("nan"), "hi": float("nan"), "deff": None}
    p = k / n
    var_cluster = (g / (g - 1)) * sum((y - p * m) ** 2 for y, m in zip(ys, ms)) / n ** 2 if g > 1 else 0
    var_srs = p * (1 - p) / n
    deff = max(1.0, var_cluster / var_srs) if var_srs > 0 else 1.0
    lo, hi = wilson(p * n / deff, n / deff)
    return {"k": k, "n": n, "p": p, "lo": lo, "hi": hi, "deff": round(deff, 2),
            "n_eff": round(n / deff, 1)}


def fmt(r: dict, page: bool = False) -> str:
    if not r["n"]:
        return "-"
    s = f"{100 * r['p']:.0f}% [{100 * r['lo']:.0f}-{100 * r['hi']:.0f}] ({r['k']}/{r['n']})"
    return s + (f" deff {r['deff']}" if page and r.get("deff") else "")


def interceptor_kind(i: str) -> str:
    if i.startswith("(off-screen"):
        return "off-screen or zero size"
    if CONSENT.search(i):
        return "consent layer"
    if MODAL.search(i):
        return "modal or overlay"
    return "other element"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sample", type=Path, required=True)
    ap.add_argument("--run", type=Path, required=True)
    args = ap.parse_args()
    sample = json.loads((args.sample / "sample.json").read_text())
    pages = json.loads((args.sample / "pages.json").read_text())["pages"]
    rank = {}
    for d in sample["domains"]:
        slug = re.sub(r"[^a-z0-9]+", "-", d["domain"].lower()).strip("-")
        rank[slug] = (d["domain"], d["rank"])
    blocker = PostHocBlocker()

    rows = []
    for p in pages:
        slug = p["id"].rsplit("-", 1)[0]
        domain, rk = rank[slug]
        job = args.run / p["id"] / "explorer"
        files = [f for f in job.glob("*.json") if f.name not in ("done.json", "command.json")]
        ex = json.loads(files[0].read_text()) if files else None
        row = {"id": p["id"], "url": p["url"], "domain": domain, "rank": rk,
               "homepage": p["source"] == "homepage", "source": p["source"]}
        if not ex or not ex.get("explored"):
            row["explored"] = False
            row["why"] = (ex or {}).get("skip_reason", "no result")
            rows.append(row)
            continue
        a, _ = analyse({**p, "tag": "tranco", "dir": job, "group": None, "screener_stable": None},
                       blocker)
        gated = a.get("gated", {})
        runs = [r for r in ex["runs"] if "actions" in r]
        row.update(explored=True,
                   gated={act: sorted({g["class"] for g in gated.values() if act in g["actions"]})
                          for act in ACTIONS},
                   gated_any=sorted({g["class"] for g in gated.values()}),
                   counts={act: dict(Counter(g["class"] for g in gated.values() if act in g["actions"]))
                           for act in ACTIONS},
                   capped=sorted({ph for r in runs for ph, c in r["caps"].get("phase_caps", {}).items() if c}
                                 | {r["caps"]["ended_by"] for r in runs
                                    if r["caps"].get("ended_by") == "wall_clock_backstop"}),
                   reach=[r.get("click_reachability") or {} for r in runs],
                   vetoes=[v for r in runs for v in r.get("safety_vetoes", [])],
                   blocked=[b for r in runs for b in r.get("blocked_writes", [])],
                   allowed=[w for r in runs for w in r.get("allowed_writes", [])],
                   opens=sum(len(r.get("blocked_opens", [])) for r in runs),
                   navs=sum(len(r.get("blocked_navigations", [])) for r in runs),
                   minutes=None)
        rows.append(row)

    explored = [r for r in rows if r["explored"]]
    report: dict = {"pages": len(rows), "explored": len(explored),
                    "not_explored": [{k: r[k] for k in ("id", "url", "why")} for r in rows
                                     if not r["explored"]]}

    def has(r, act, cls):
        found = r["gated_any"] if act == "any" else r["gated"][act]
        return (bool(set(found) & set(CLASSES)) if cls == "any class" else cls in found)

    def prevalence(subset, act, cls, page_only=False):
        by_dom = defaultdict(list)
        for r in subset:
            by_dom[r["domain"]].append(has(r, act, cls))
        out = {"page": page_level(by_dom)}
        if not page_only:
            out["domain"] = domain_level({d: any(v) for d, v in by_dom.items()})
        return out

    classes = list(CLASSES) + ["any class"]
    table = {f"{act}/{cls}": prevalence(explored, act, cls)
             for act in (*ACTIONS, "any") for cls in classes}
    report["prevalence"] = table
    homes = [r for r in explored if r["homepage"]]
    internals = [r for r in explored if not r["homepage"]]
    report["by_page_type"] = {
        name: {act: prevalence(sub, act, "content") for act in (*ACTIONS, "any")}
        for name, sub in (("homepage", homes), ("internal", internals))}
    report["by_rank_band"] = {}
    for lo, hi, name in BANDS:
        sub = [r for r in explored if lo <= r["rank"] <= hi]
        report["by_rank_band"][name] = {"domains": len({r["domain"] for r in sub}),
                                        **{act: prevalence(sub, act, "content") for act in (*ACTIONS, "any")}}

    # coverage
    capped = Counter(ph for r in explored for ph in r["capped"])
    clicks = sum(x.get("clicks", 0) for r in explored for x in r["reach"])
    unreach = sum(x.get("unreachable", 0) for r in explored for x in r["reach"])
    kinds = Counter()
    for r in explored:
        for x in r["reach"]:
            for i, n in (x.get("by_interceptor") or {}).items():
                kinds[interceptor_kind(i)] += n
    report["coverage"] = {"pages_any_phase_capped": sum(1 for r in explored if r["capped"]),
                          "capped_by_phase": dict(capped), "clicks": clicks, "unreachable": unreach,
                          "unreachable_by_kind_top10": dict(kinds)}

    # safety
    vetoes = Counter((v["kind"], v.get("rule") or v["reason"][:50]) for r in explored for v in r["vetoes"])
    groups = defaultdict(Counter)
    state = []
    for r in explored:
        for b in r["blocked"]:
            g, why = classify_block(b)
            host = urllib.parse.urlsplit(b["url"]).netloc
            groups[g][f"{why} | {host}"] += 1
            if g == "state change":
                state.append({"domain": r["domain"], "page": r["url"], "method": b.get("method"),
                              "url": b["url"].split("?")[0], "why": why})
    report["safety"] = {
        "vetoes": [{"kind": k, "rule": rule, "n": n} for (k, rule), n in vetoes.most_common()],
        "blocked_writes": {g: dict(c.most_common()) for g, c in groups.items()},
        "state_change_blocked": state,
        "allowed_writes": [w for r in explored for w in r["allowed"]],
        "popups_refused": sum(r["opens"] for r in explored),
        "navigations_blocked": sum(r["navs"] for r in explored)}
    report["rows"] = rows
    (args.run / "tranco_report.json").write_text(json.dumps(report, indent=2, default=str) + "\n")

    md = [f"# Tranco prevalence: {len(explored)} explored pages of {len(rows)}, "
          f"{len({r['domain'] for r in explored})} domains", "",
          "Domain level: % [95% Wilson] (k/n). Page level: ratio estimate over domains, Wilson on "
          "the effective size (deff = design effect).", "",
          "| action / class | domains | pages |", "|---|---|---|"]
    for key, v in table.items():
        md.append(f"| {key} | {fmt(v['domain'])} | {fmt(v['page'], True)} |")
    md += ["", "## Gated content by page type", "", "| | action | domains | pages |", "|---|---|---|---|"]
    for name, d in report["by_page_type"].items():
        for act, v in d.items():
            md.append(f"| {name} | {act} | {fmt(v['domain'])} | {fmt(v['page'], True)} |")
    md += ["", "## Gated content by rank band (indicative at n=50)", "",
           "| band | domains | action | domains with | pages |", "|---|---|---|---|---|"]
    for name, d in report["by_rank_band"].items():
        for act in (*ACTIONS, "any"):
            md.append(f"| {name} | {d['domains']} | {act} | {fmt(d[act]['domain'])} | {fmt(d[act]['page'], True)} |")
    (args.run / "tranco_report.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))
    return 0


if __name__ == "__main__":
    sys.exit(main())
