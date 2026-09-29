#!/usr/bin/env python3
"""Summarise a screening run: the grid, the autofetch ordering, failures, groups.

Reads results.json and answers the questions the crawl list is picked on:
  - counts by reveal_channel x mechanism x markup_quality
  - pages ordered by what autofetch would miss
  - screened:false rows, which must never be silently absorbed into totals
  - reveal_channel distribution per group, since the groups encode predictions
"""

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent


def load(path: Path) -> list[dict]:
    return json.loads(path.read_text())


def primary_channel(row: dict) -> str:
    names = row.get("reveal_channel_names") or []
    for preferred in ("network", "dom-appended", "dom-hidden", "none", "invalid"):
        if preferred in names:
            return preferred
    return "none"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--results", default=str(PKG_ROOT / "out" / "results.json"))
    ap.add_argument("--group", help="restrict to one group letter")
    args = ap.parse_args()

    rows = load(Path(args.results))
    if args.group:
        rows = [r for r in rows if r.get("group") == args.group]
    screened = [r for r in rows if r.get("screened")]
    failed = [r for r in rows if not r.get("screened")]

    print(f"{len(rows)} row(s): {len(screened)} screened, {len(failed)} screened:false")
    print()

    # ---- the grid -----------------------------------------------------------
    print("== reveal_channel x mechanism x markup_quality ==")
    grid = Counter()
    for row in screened:
        channel = primary_channel(row)
        mechs = ",".join(sorted(row.get("mechanisms_present", {}))) or "(none)"
        verdict = (row.get("markup_quality") or {}).get("verdict", "?")
        grid[(channel, mechs, verdict)] += 1
    print(f"  {'channel':14}{'mechanisms':26}{'markup':16}count")
    for (channel, mechs, verdict), count in sorted(grid.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {channel:14}{mechs:26}{verdict:16}{count}")
    print()

    print("== marginals ==")
    for label, key in (("reveal_channel", lambda r: primary_channel(r)),
                       ("markup_quality", lambda r: (r.get("markup_quality") or {}).get("verdict", "?")),
                       ("stable", lambda r: "yes" if (r.get("stability") or {}).get("stable") else "no")):
        counts = Counter(key(r) for r in screened)
        print(f"  {label:16}" + "  ".join(f"{k}={v}" for k, v in counts.most_common()))
    mech_counts = Counter()
    for row in screened:
        for mech in row.get("mechanisms_present", {}):
            mech_counts[mech] += 1
    print(f"  {'mechanisms':16}" + "  ".join(f"{k}={v}" for k, v in mech_counts.most_common()))
    print()

    # ---- autofetch ordering -------------------------------------------------
    print("== ordered by autofetch_would_miss (strongest predicted stock-behaviour failures) ==")
    ranked = sorted(
        screened,
        key=lambda r: (-len((r.get("lazy_attributes") or {}).get("autofetch_would_miss", [])),
                       -len((r.get("lazy_attributes") or {}).get("attribute_names_found", [])),
                       r["url"]),
    )
    any_miss = False
    for row in ranked:
        lazy = row.get("lazy_attributes") or {}
        miss = lazy.get("autofetch_would_miss") or []
        if not miss:
            continue
        any_miss = True
        harvest = lazy.get("autofetch_would_harvest") or []
        print(f"  [{row.get('group', '?')}] {row['url']}")
        print(f"      would MISS: {', '.join(miss)}"
              + (f"   would harvest: {', '.join(harvest)}" if harvest else "")
              + f"   channel: {primary_channel(row)}")
    if not any_miss:
        print("  none: no screened page carries a lazy attribute autofetch would miss")
    print()

    # ---- per-group distribution --------------------------------------------
    print("== reveal_channel by group ==")
    by_group = defaultdict(Counter)
    markup_by_group = defaultdict(Counter)
    for row in screened:
        by_group[row.get("group", "?")][primary_channel(row)] += 1
        markup_by_group[row.get("group", "?")][(row.get("markup_quality") or {}).get("verdict", "?")] += 1
    for group in sorted(by_group):
        channels = "  ".join(f"{k}={v}" for k, v in by_group[group].most_common())
        markup = "  ".join(f"{k}={v}" for k, v in markup_by_group[group].most_common())
        print(f"  {group}: {channels}")
        print(f"     markup: {markup}")
    print()

    # ---- inert markup, wherever it is --------------------------------------
    print("== pages with any inert control (page-wide or actually clicked) ==")
    found = False
    for row in screened:
        mq = row.get("markup_quality") or {}
        clicked = mq.get("controls_actually_clicked") or {}
        inert = mq.get("inert_div_span_click_handlers", 0)
        clicked_inert = clicked.get("nonnative_inert", 0)
        unnamed = clicked.get("native_without_name", 0)
        if inert or clicked_inert or unnamed:
            found = True
            print(f"  [{row.get('group', '?')}] {row['url']}")
            print(f"      page-wide inert div/span handlers: {inert}; "
                  f"clicked inert: {clicked_inert}; clicked native-but-unnamed: {unnamed}")
    if not found:
        print("  none")
    print()

    # ---- three tiers, per group --------------------------------------------
    print("== control tiers actually clicked, by group ==")
    print("  native-named = <button>Next</button>; div-with-name = "
          "<div role=button aria-label=...>; inert = div/span, no ARIA, no name")
    tier_by_group = defaultdict(Counter)
    for row in screened:
        tiers = ((row.get("markup_quality") or {}).get("tiers") or {})
        for tier, count in tiers.items():
            tier_by_group[row.get("group", "?")][tier] += count
    for group in sorted(tier_by_group):
        counts = tier_by_group[group]
        total = sum(counts[t] for t in ("native_named", "div_with_name", "inert"))
        ranked = sorted(("native_named", "div_with_name", "inert"), key=lambda t: -counts[t])
        dominant = ranked[0] if counts[ranked[0]] else "(none clicked)"
        share = f"{counts[ranked[0]] / total * 100:.0f}%" if total else "-"
        print(f"  {group}: native-named={counts['native_named']}  "
              f"div-with-name={counts['div_with_name']}  inert={counts['inert']}"
              + (f"  (native-unnamed={counts['native_unnamed']})" if counts["native_unnamed"] else "")
              + f"   -> dominant: {dominant} {share}")
    print()

    # ---- every denylist veto, in one pass -----------------------------------
    print("== denylist vetoes (every one, grouped by term x group) ==")
    vetoes = defaultdict(list)
    for row in screened:
        for skip in (row.get("interaction_log") or {}).get("safety_skips") or []:
            vetoes[(skip.get("term", "?"), row.get("group", "?"))].append((row["url"], skip))
    if not vetoes:
        print("  none")
    for (term, group), items in sorted(vetoes.items()):
        print(f"  [{term} @ group {group}] {len(items)} veto(es)")
        for url, skip in items[:6]:
            print(f"      <{skip['tag']}> {skip['name']!r}  on {url}")
        if len(items) > 6:
            print(f"      ... and {len(items) - 6} more")
    print()

    print("== structural (non-denylist) safety skips, by kind ==")
    kinds = Counter()
    for row in screened:
        for skip in (row.get("interaction_log") or {}).get("form_skips") or []:
            kinds[skip.get("kind", "?")] += 1
    print("  " + ("  ".join(f"{k}={v}" for k, v in kinds.most_common()) or "none"))
    print()

    # ---- failures -----------------------------------------------------------
    print("== screened:false ==")
    if not failed:
        print("  none")
    for row in failed:
        print(f"  [{row.get('group', '?')}] {row['url']}")
        print(f"      {row.get('skip_reason', '(no reason recorded)')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
