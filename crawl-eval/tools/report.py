#!/usr/bin/env python3
"""14 pages x 5 conditions: recall and time, then the minimal behavior subset per page.

The minimal subset is the ground-truth label an LLM will later be asked to predict:
the smallest set of behaviors that achieves full recall on the page.

Derivation from isolated conditions only:
  - c1 (no behaviors) already at full recall   -> {} (the page needs nothing)
  - otherwise, every single behavior whose isolated crawl hits full recall is a
    minimal subset; if several do, any one of them suffices
  - if none does, the label is "not reached by any tested subset". Combinations
    were not crawled, by design, so a page needing two behaviors together cannot be
    labelled from this data - that is reported, never guessed.
"""

import json
import sys
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
CONDITIONS = [("c1-none", "none"), ("c2-autoscroll", "autoscroll"),
              ("c3-autoclick", "autoclick"), ("c4-autofetch", "autofetch"),
              ("c5-autoplay", "autoplay")]


def main() -> int:
    runs = {}
    for cond, label in CONDITIONS:
        path = PKG_ROOT / "runs" / cond / "recall.json"
        if not path.exists():
            print(f"missing {path}; run warc_recall.py first")
            return 2
        runs[label] = json.loads(path.read_text())

    bad = [l for l, r in runs.items() if not r["cross_check"]["agree"]]
    if bad:
        print(f"REFUSING: cross-check failed for {bad}; numbers are not trustworthy")
        return 1

    ids = [p["id"] for p in runs["none"]["pages"]]
    by = {label: {p["id"]: p for p in r["pages"]} for label, r in runs.items()}
    labels = [l for _, l in CONDITIONS]

    lines = ["# Browsertrix behavior isolation: 14 pages x 5 conditions", ""]
    lines += ["Cell = hits/declared, then per-page wall-clock seconds "
              "(`Awaiting page load` to `Page Finished`).", ""]
    lines += ["| page | " + " | ".join(labels) + " |",
              "|---|" + "---|" * len(labels)]
    for pid in ids:
        cells = []
        for l in labels:
            p = by[l][pid]
            mark = "**" if p["recall"] == 1.0 else ""
            cells.append(f"{mark}{p['hits']}/{p['declared']}{mark} · {p['time_s']}s")
        lines.append(f"| {pid} | " + " | ".join(cells) + " |")
    totals = [f"{runs[l]['total_hits']}/{runs[l]['total_declared']}" for l in labels]
    lines.append("| **total** | " + " | ".join(totals) + " |")
    lines.append("")

    # ---- minimal subsets ---------------------------------------------------------
    lines += ["## Minimal behavior subset per page (ground-truth label)", "",
              "| page | minimal subset | cost of the others (s over no-behavior baseline) |",
              "|---|---|---|"]
    labels_out = {}
    for pid in ids:
        base_t = by["none"][pid]["time_s"] or 0
        if by["none"][pid]["recall"] == 1.0:
            minimal = "{} (needs nothing)"
            sufficient = []
        else:
            sufficient = [l for l in labels[1:] if by[l][pid]["recall"] == 1.0]
            minimal = (" or ".join("{" + s + "}" for s in sufficient) if sufficient
                       else "not reached by any tested subset")
        costs = []
        for l in labels[1:]:
            if l in sufficient:
                continue
            extra = (by[l][pid]["time_s"] or 0) - base_t
            costs.append(f"{l} +{extra:.1f}")
        labels_out[pid] = {"minimal": minimal, "sufficient_single": sufficient}
        lines.append(f"| {pid} | {minimal} | {', '.join(costs)} |")
    lines.append("")

    # ---- supplementary: post-behavior text ---------------------------------------
    lines += ["## Supplementary: text sentinels before vs after behaviors", "",
              "`pages.jsonl` text is snapshotted before behaviors run; `urn:textFinal` "
              "after. Final text exists only when a behavior was enabled.", "",
              "| page | condition | pages.jsonl (pre-behavior) | textFinal (post-behavior) |",
              "|---|---|---|---|"]
    for pid in ids:
        if not by["none"][pid]["declared"] or not runs["none"]["pages"]:
            continue
        declared_txt = [p for p in runs["none"]["pages"] if p["id"] == pid]
        if not any(by[l][pid]["text_hits_pages_jsonl"] or by[l][pid]["text_hits_final"]
                   for l in labels) and "continuereading" not in pid:
            continue
        for l in labels:
            p = by[l][pid]
            final = p["text_hits_final"]
            lines.append(f"| {pid} | {l} | {len(p['text_hits_pages_jsonl'])} | "
                         f"{'n/a' if final is None else len(final)} |")
    out = PKG_ROOT / "runs" / "report.md"
    out.write_text("\n".join(lines) + "\n")
    (PKG_ROOT / "runs" / "labels.json").write_text(json.dumps(labels_out, indent=2))
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
