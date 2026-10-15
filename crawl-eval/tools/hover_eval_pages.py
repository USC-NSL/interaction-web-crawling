#!/usr/bin/env python3
"""Page set for the hover evaluation, tagged by source.

Sources, each page tagged with every one that selected it:
  tranco-hover      per Tranco host with hover-attributed explorer content (an earlier
                    explorer, before its :hover-rule source), the page with the most
  tranco-css-fetch  per Tranco host, the page with the most :hover rules that make the
                    browser fetch a url() (background-image, border-image, list-style-image,
                    cursor, content) and whose subject exists (css_hover_screen.py)
  eval-hover        evaluation-set pages with hover-attributed explorer content
  eval-negative     evaluation-set pages without
  css-menu-only     a seeded random cost sample of Tranco pages whose matching :hover rules
                    only show or hide (display, visibility) and none fetches
Pages per registrable domain (eTLD+1) are capped; a page that would exceed the cap is
dropped from the lowest-priority source first (the order above). "Host" is
parallel_explore's (hostname without "www."), the key the per-host rule uses.
"""

import argparse
import json
import random
import sys
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG_ROOT / "tools"))
from parallel_explore import host  # noqa: E402

FETCH_PROPS = {"background-image", "border-image-source", "list-style-image", "cursor", "content"}
MENU_PROPS = {"display", "visibility"}
# the multi-label public suffixes among the hosts this draws from (Public Suffix List)
MULTI_SUFFIXES = {"co.nz", "co.uk", "com.ar", "com.br", "gov.uk", "org.cn", "nhs.uk",
                  "github.io", "service.gov.uk"}
ORDER = ["eval-hover", "eval-negative", "tranco-hover", "tranco-css-fetch", "css-menu-only"]


def domain(url: str) -> str:
    labels = host(url).split(":")[0].split(".")
    for n in (3, 2):
        if ".".join(labels[-n:]) in MULTI_SUFFIXES:
            return ".".join(labels[-n - 1:])
    return ".".join(labels[-2:])


def load_json_dir(d: Path, skip: set[str]) -> dict[str, dict]:
    out = {}
    for f in d.glob("*.json"):
        if f.name not in skip:
            rec = json.loads(f.read_text())
            out[rec["id"]] = rec
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--explored", type=Path, default=PKG_ROOT / "runs" / "tranco-4672X-explore")
    ap.add_argument("--screen", type=Path, default=PKG_ROOT / "runs" / "tranco-4672X-css-screen2")
    ap.add_argument("--eval-explored", type=Path, default=PKG_ROOT / "runs" / "eval4")
    ap.add_argument("--negatives", default="design-system-service-gov-uk-components-accordion,"
                    "getbootstrap-com-docs-5-3-components-accordion,www-nhs-uk-conditions,"
                    "admission-usc-edu-apply-faq")
    ap.add_argument("--menu-sample", type=int, default=8)
    ap.add_argument("--seed", type=int, default=4672)
    ap.add_argument("--domain-cap", type=int, default=2)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    def hover_resources(d: Path) -> dict[str, tuple[int, str]]:
        res = {}
        for f in d.glob("*/explorer/*.json"):
            if f.name in ("done.json", "command.json"):
                continue
            rec = json.loads(f.read_text())
            if "stable_new_content" in rec:
                res[f.parent.parent.name] = (
                    sum(1 for v in rec["stable_new_content"].values()
                        if v.get("run1", {}).get("action") == "hover"), rec["url"])
        return res

    tranco = hover_resources(args.explored)
    screen = {k: v for k, v in load_json_dir(args.screen, {"summary.json"}).items() if k in tranco}
    ok = {k: v for k, v in screen.items() if "error" not in v}

    def fetch_rules(rec):
        return [r for r in rec["rules"] if r["subjects"] and FETCH_PROPS & set(r["props"])]

    def menu_only(rec):
        with_subject = [r for r in rec["rules"] if r["subjects"]]
        return (not any(FETCH_PROPS & set(r["props"]) for r in rec["rules"])
                and any(MENU_PROPS & set(r["props"]) for r in with_subject))

    picks: dict[str, dict] = {}

    def add(pid: str, url: str, source: str, **info):
        page = picks.setdefault(pid, {"id": pid, "url": url, "set": "hover", "sources": []})
        page["sources"].append(source)
        page.update(info)

    best: dict[str, tuple] = {}
    for pid, (n, url) in tranco.items():
        if n and (host(url) not in best or n > best[host(url)][0]):
            best[host(url)] = (n, pid, url)
    for n, pid, url in best.values():
        add(pid, url, "tranco-hover", old_hover_resources=n)

    best = {}
    for pid, rec in ok.items():
        f = fetch_rules(rec)
        if f:
            key = (sum(1 for r in f if r["visible_subjects"]), len(f))
            if host(rec["url"]) not in best or key > best[host(rec["url"])][0]:
                best[host(rec["url"])] = (key, pid, rec["url"])
    for (vis, n), pid, url in best.values():
        add(pid, url, "tranco-css-fetch", css_fetch_rules=n, css_fetch_rules_visible=vis)

    evalr = hover_resources(args.eval_explored)
    negatives = args.negatives.split(",")
    for pid, (n, url) in sorted(evalr.items()):
        if n:
            add(pid, url, "eval-hover", old_hover_resources=n)
    for pid in negatives:
        n, url = evalr[pid]
        if n:
            raise SystemExit(f"negative {pid} has hover-attributed content")
        add(pid, url, "eval-negative")

    # cap per registrable domain, highest-priority sources first
    def rank(p):
        return min(ORDER.index(s) for s in p["sources"])
    per_domain: dict[str, int] = {}
    kept, dropped = [], []
    for p in sorted(picks.values(), key=rank):
        d = domain(p["url"])
        if per_domain.get(d, 0) >= args.domain_cap:
            dropped.append({"id": p["id"], "sources": p["sources"], "domain": d})
            continue
        per_domain[d] = per_domain.get(d, 0) + 1
        kept.append(p)

    pool = sorted(pid for pid, rec in ok.items() if menu_only(rec) and pid not in picks)
    rng = random.Random(args.seed)
    rng.shuffle(pool)
    sampled = []
    for pid in pool:
        if len(sampled) >= args.menu_sample:
            break
        d = domain(ok[pid]["url"])
        if per_domain.get(d, 0) >= args.domain_cap:
            continue
        per_domain[d] = per_domain.get(d, 0) + 1
        rec = ok[pid]
        sampled.append({"id": pid, "url": rec["url"], "set": "hover", "sources": ["css-menu-only"],
                        "css_menu_rules": sum(1 for r in rec["rules"] if r["subjects"])})
    kept += sampled

    # every page carries the screen's figures, whatever selected it
    for p in kept:
        rec = ok.get(p["id"])
        if rec:
            p.setdefault("css_fetch_rules", len(fetch_rules(rec)))
            p["css_rules_with_subject"] = rec["rules_with_subject"]
        p["domain"] = domain(p["url"])

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"seed": args.seed, "domain_cap": args.domain_cap,
                                    "dropped_by_cap": dropped, "pages": kept}, indent=1) + "\n")
    stats = {
        "tranco_explored": len(tranco), "screened": len(ok),
        "screen_errors": sorted(k for k, v in screen.items() if "error" in v),
        "pages_with_fetch_rule_and_subject": sum(1 for r in ok.values() if fetch_rules(r)),
        "hosts_with_fetch_rule_and_subject": len({host(r["url"]) for r in ok.values() if fetch_rules(r)}),
        "fetch_props": sorted({p for r in ok.values() for x in fetch_rules(r) for p in x["props"]
                               if p in FETCH_PROPS}),
        "menu_only_pool": len(pool),
    }
    print(json.dumps(stats, indent=1))
    by_source: dict[str, int] = {}
    for p in kept:
        k = "+".join(p["sources"])
        by_source[k] = by_source.get(k, 0) + 1
    print(f"{len(kept)} pages; by source {by_source}; dropped by domain cap: {dropped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
