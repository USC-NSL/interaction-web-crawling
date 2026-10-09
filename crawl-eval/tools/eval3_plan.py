#!/usr/bin/env python3
"""Which eval2 results the route-restore and media-link changes can affect, from eval2's logs.

RevealClick list: every page where an F or F2 log shows "Click changed URL". The new
build differs only in what happens after such a click, so on other pages it behaves
identically and the C/F/F2 results stand.

Explorer list: every page where the explorer clicked a link to another document that
has a click listener and does not point at an image or video (the old rule clicked it,
the new one does not), or recorded a route change during a click. The eval2 explorer
output has no href per action: a clicked target is matched against the re-screen's
vetoes, which recorded the href of every listener link the old-old rule refused.
eval2 recorded route changes only when the path changed, so a query or fragment change
during an explorer click cannot be derived from its logs.

With --prepare, links every result that stands from runs/eval2 into runs/eval3 (the
runner skips a job with done.json) and writes tools/eval3_pages.json with the pages
that have anything left to run:
  explorer list: explorer, none and every config, back-to-back (new pool)
  RevealClick only: none (an instrument check against the eval2 pool), C, F, F2
"""

import argparse
import json
import re
import urllib.parse
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
RUNS = PKG_ROOT / "runs"
MEDIA_PATH = re.compile(r"\.(jpe?g|png|gif|webp|avif|svg|bmp|mp4|webm|mov|m4v|ogv)$", re.IGNORECASE)


def explorer_json(d: Path) -> dict | None:
    files = [f for f in d.glob("*.json") if f.name not in ("done.json", "command.json")]
    return json.loads(files[0].read_text()) if files else None


def revealclick_evidence(page_dir: Path) -> dict:
    out = {}
    for cfg in ("F", "F2"):
        urls = []
        for jd in sorted(page_dir.glob(f"{cfg}-*-r*")):
            for line in (jd / "stdout.txt").read_text().splitlines():
                if '"Click changed URL' in line:
                    urls.append(json.loads(line)["message"].split("restoring: ", 1)[1])
        if urls:
            out[cfg] = {"clicks": len(urls), "examples": sorted(set(urls))[:3]}
    return out


def explorer_evidence(page_id: str, page_dir: Path) -> dict:
    ex = explorer_json(page_dir / "explorer")
    old = explorer_json(RUNS / "rescreen" / page_id)
    out: dict = {}
    if not ex:
        return out
    clicked = {a["target"] for r in ex["runs"] for a in r["actions"] if a["action"] == "click"}
    hrefs = set()
    for r in (old or {}).get("runs", []):
        for v in r.get("safety_vetoes", []):
            if (v.get("reason") == "link navigates away from the page under test"
                    and "cdp-listener" in (v.get("sources") or []) and v["target"] in clicked):
                hrefs.add(v.get("href", ""))
    non_media = sorted(h for h in hrefs if not MEDIA_PATH.search(urllib.parse.urlsplit(h).path))
    if non_media:
        out["listener_links"] = {"clicked": len(hrefs), "non_media": len(non_media),
                                 "examples": non_media[:3]}
    routes = []
    for r in ex["runs"]:
        acts = {a["id"]: a for a in r["actions"]}
        routes += [s["url"] for s in r.get("soft_navigations", [])
                   if acts.get(s["from_action"], {}).get("action") == "click"]
    if routes:
        out["route_changes"] = {"count": len(routes), "examples": sorted(set(routes))[:3]}
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src", type=Path, default=RUNS / "eval2")
    ap.add_argument("--dst", type=Path, default=RUNS / "eval3")
    ap.add_argument("--pages-file", type=Path, default=PKG_ROOT / "tools" / "eval2_pages.json")
    ap.add_argument("--prepare", action="store_true")
    args = ap.parse_args()
    pages = json.loads(args.pages_file.read_text())["pages"]
    plan = {}
    for p in pages:
        pd = args.src / p["id"]
        rc, ex = revealclick_evidence(pd), explorer_evidence(p["id"], pd)
        plan[p["id"]] = {"revealclick": rc, "explorer": ex}
        print(f"{p['id']}  [{p['set']}]")
        for cfg, e in rc.items():
            print(f"  RevealClick {cfg}: {e['clicks']} URL-changing clicks, e.g. {e['examples'][:2]}")
        for k, e in ex.items():
            print(f"  explorer {k}: {e}")
        if not rc and not ex:
            print("  no evidence: results stand")
    rc_ids = [i for i, v in plan.items() if v["revealclick"]]
    ex_ids = [i for i, v in plan.items() if v["explorer"]]
    print(f"\nRevealClick list ({len(rc_ids)}): {rc_ids}\nExplorer list ({len(ex_ids)}): {ex_ids}")
    if not args.prepare:
        return 0

    args.dst.mkdir(parents=True, exist_ok=True)
    rerun = []
    for p in pages:
        src, dst = args.src / p["id"], args.dst / p["id"]
        if p["id"] in ex_ids:
            standing = []
        elif p["id"] in rc_ids:
            standing = ["explorer"] + [j.name for j in src.iterdir()
                                       if j.name.startswith(("A-", "B-"))]
        else:
            standing = [j.name for j in src.iterdir() if j.is_dir()]
        dst.mkdir(exist_ok=True)
        for name in standing:
            link = dst / name
            if not link.exists():
                link.symlink_to((src / name).resolve())
        if p["id"] in ex_ids or p["id"] in rc_ids:
            rerun.append(p)
    out = PKG_ROOT / "tools" / "eval3_pages.json"
    out.write_text(json.dumps({"about": "eval2 pages with results to rerun (tools/eval3_plan.py)",
                               "pages": rerun}, indent=2) + "\n")
    print(f"linked standing results into {args.dst}; wrote {out} ({len(rerun)} pages)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
