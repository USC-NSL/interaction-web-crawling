#!/usr/bin/env python3
"""Distribution of interaction-gated content in the crawler's real browser settings.

Reads the explorer results written by rescreen_run.py (runs/rescreen) and the five
pilot pages' runs/pilot2 explorer results. Per page, the gated content is the
explorer's stable new content (found in both runs), expanded to raw URLs, minus
icons and minus whatever the five Brave lists block, decided post hoc exactly as in
pilot_analyse.py. Attribution is the action that first caused each request; a URL
counts for an action if either run attributes it there.

Writes runs/rescreen/distribution.json, vetoes.json and vetoes.md.
"""

import json
import re
import sys
import urllib.parse
from collections import Counter, defaultdict
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
RUNS = PKG_ROOT / "runs"
OUT = RUNS / "rescreen"
sys.path.insert(0, str(PKG_ROOT / "tools"))
from pilot_analyse import PostHocBlocker, is_icon, url_key  # noqa: E402

ACTIONS = ("click", "scroll", "hover")
ICONISH = re.compile(r"icon|sprite|\.ico$|/symbols?/", re.I)


def explorer_result(page_dir: Path) -> dict | None:
    for f in sorted(page_dir.glob("*.json")):
        d = json.loads(f.read_text())
        if "page_id" in d and "url" in d:
            return d
    return None


def pages() -> list[dict]:
    out = []
    for p in json.loads((OUT / "pages.json").read_text()):
        out.append({**p, "dir": OUT / p["id"]})
    for p in json.loads((PKG_ROOT / "tools" / "pilot_pages.json").read_text())["pages"]:
        out.append({"id": p["id"], "url": p["url"], "group": p["group"], "tag": p["tag"],
                    "dir": RUNS / "pilot2" / p["id"] / "explorer", "pilot": True})
    return out


def kind_of(url: str, rtype: str, ctype: str) -> str:
    if rtype == "font" or "font" in ctype:
        return "font"
    if ICONISH.search(urllib.parse.urlsplit(url).path) or "svg" in ctype or "icon" in ctype:
        return "icon/svg"
    return rtype or (ctype.split("/")[0] if ctype else "other")


def analyse(p: dict, blocker: PostHocBlocker) -> tuple[dict, list[dict]]:
    ex = explorer_result(p["dir"])
    row = {k: p.get(k) for k in ("id", "url", "group", "tag", "screener_stable")}
    if ex is None:
        done = p["dir"] / "done.json"
        row["status"] = f"no result ({json.loads(done.read_text()) if done.exists() else 'not run'})"
        return row, []
    if not ex.get("explored"):
        row["status"] = f"not explored: {ex.get('skip_reason')}"
        return row, []
    runs = [r for r in ex["runs"] if "new_content" in r]
    errors = [r.get("error") for r in ex["runs"] if "error" in r]
    types: dict[str, str] = {}
    for r in runs:
        for raw, t in (r.get("raw_types") or {}).items():
            types.setdefault(url_key(raw), t)
    source = runs[0]["settled_url"] if runs else p["url"]

    gated, removed = {}, []
    for key, v in ex["stable_new_content"].items():
        resp = v.get("response") or {}
        actions = {v[r]["action"] for r in ("run1", "run2") if v.get(r)}
        for r in runs:
            for raw in (r["new_content"].get(key) or {}).get("raw", []):
                u = url_key(raw)
                if u in gated:
                    continue
                rtype = types.get(u) or r["new_content"][key].get("resource_type") or "other"
                if is_icon(u):
                    removed.append({"url": u, "why": "icon"})
                    continue
                hit = blocker.match(u, source, rtype)
                if hit:
                    removed.append({"url": u, "why": "blocked", "list": hit["list"], "rule": hit["rule"]})
                    continue
                gated[u] = {"actions": sorted(actions), "type": rtype,
                            "content_type": resp.get("content_type") or "", "status": resp.get("status"),
                            "size": resp.get("size"),
                            "kind": kind_of(u, rtype, resp.get("content_type") or ""),
                            "attribution": {k: v.get(k) for k in ("run1", "run2")}}
    by_action = {a: sorted(u for u, g in gated.items() if a in g["actions"]) for a in ACTIONS}
    # A cap that ends a run before clicking means click content was never looked for.
    ended = sorted({r["caps"]["ended_by"] for r in runs if r["caps"]["ended_by"]})
    clicks_done = min((r.get("action_counts") or {}).get("click", 0) for r in runs) if runs else 0
    coverage = ("complete" if not ended else
                "no clicks (cap hit before clicking)" if clicks_done == 0 else
                f"partial ({clicks_done} clicks before cap)")
    click = by_action["click"]
    kinds = Counter(gated[u]["kind"] for u in click)
    row.update({
        "status": "ok" if not errors else f"partial: {errors}",
        "runs_ok": len(runs),
        "ended_by": ended,
        "coverage": coverage,
        "gated_total": len(gated),
        "by_action": {a: len(v) for a, v in by_action.items()},
        "click_pool": len(click),
        "click_kinds": dict(kinds),
        "click_fonts_icons_only": bool(click) and set(kinds) <= {"font", "icon/svg"},
        "click_urls": click,
        "unstable_new": len(ex.get("unstable_new_content", {})),
        "removed": removed,
        "caps_hit": ex.get("caps_hit"),
        "click_sources": ex.get("click_sources"),
        "gated": gated,
    })

    vetoes = []
    seen = {}
    for run_name, items in (ex.get("safety_vetoes") or {}).items():
        for v in items:
            if not v.get("kind", "").startswith("safety"):
                continue
            k = (v.get("target"), v.get("reason"))
            if k in seen:
                seen[k]["runs"].append(run_name)
                continue
            rec = {"page": p["url"], "tag": p["tag"], "runs": [run_name], "kind": v.get("kind"),
                   "element": v.get("tag"), "label": v.get("name"), "reason": v.get("reason"),
                   "term": v.get("term"), "nearby": (v.get("nearby") or "")[:160],
                   "href": v.get("href"), "target": v.get("target")}
            seen[k] = rec
            vetoes.append(rec)
    row["unsuitable_skips"] = sum(1 for items in (ex.get("safety_vetoes") or {}).values()
                                  for v in items if v.get("kind") == "unsuitable")
    row["safety_vetoes"] = len(vetoes)
    return row, vetoes


def main() -> int:
    blocker = PostHocBlocker()
    rows, vetoes = [], []
    for p in pages():
        row, v = analyse(p, blocker)
        rows.append(row)
        vetoes += v
    (OUT / "distribution.json").write_text(json.dumps(rows, indent=2, default=list))
    (OUT / "vetoes.json").write_text(json.dumps(vetoes, indent=2))

    lines = ["# Explorer safety vetoes (re-screen, --acknowledge-skips)", "",
             "One row per (element, reason), with the runs it occurred in.", "",
             "| page | kind | element | label | reason | nearby / href |", "|---|---|---|---|---|---|"]
    esc = lambda s: (s or "").replace("|", "\\|").replace("\n", " ")[:120]  # noqa: E731
    for v in sorted(vetoes, key=lambda v: (v["page"], v["kind"])):
        lines.append(f"| {v['page']} | {v['kind']} | {v['element']} | {esc(v['label'])} | "
                     f"{esc(v['reason'])} | {esc(v['href'] or v['nearby'])} |")
    (OUT / "vetoes.md").write_text("\n".join(lines) + "\n")

    for tag in ("demo", "production"):
        sel = [r for r in rows if r["tag"] == tag]
        ok = [r for r in sel if r.get("status") == "ok"]
        none = [r for r in ok if r["gated_total"] == 0 and r["coverage"] == "complete"]
        print(f"\n{tag}: {len(sel)} pages, {len(ok)} explored cleanly; "
              f"no gated content (complete coverage): {len(none)}; capped: "
              f"{sum(1 for r in ok if r['coverage'] != 'complete')}; "
              + ", ".join(f"{a}: {sum(1 for r in ok if r['by_action'][a])}" for a in ACTIONS))
        for r in sorted(sel, key=lambda r: -(r.get("click_pool") or 0)):
            if r.get("status") != "ok":
                print(f"   !! {r['url']}  {r['status']}")
                continue
            flag = "  [fonts/icons only]" if r["click_fonts_icons_only"] else ""
            cov = "" if r["coverage"] == "complete" else f"  <{r['coverage']}, {'/'.join(r['ended_by'])}>"
            print(f"   click {r['click_pool']:3}  scroll {r['by_action']['scroll']:4}  "
                  f"hover {r['by_action']['hover']:3}  {r['url']}  {r['click_kinds']}{flag}{cov}")
    print(f"\n{len(vetoes)} safety veto(es) -> {OUT / 'vetoes.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
