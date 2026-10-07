#!/usr/bin/env python3
"""Analyse the real-page pilot written by pilot_run.py.

  check   instrument check: the Browsertrix no-behavior request set against the
          explorer's baseline, per page. Run this first; if they disagree
          substantially, recall built on them is not meaningful.
  recall  pool, recall per config, RevealClick log metrics and the gap analysis.

URL identity is the same on both sides: the URL as requested with the fragment
dropped and the query kept. Every number is also given at origin+path (the
explorer's own key, screen.normalise_request), so whether the query matters is
visible rather than assumed.

Browsertrix request set: WARC response and revisit records with HTTP status 200.
Explorer sets: its stable new content, expanded to the raw URLs recorded under each
origin+path key (at most 5 per key, a cap of the explorer's bookkeeping), and its
per-run baseline_raw, status 200.

Periodic: the explorer's 30s idle-window patterns (origin+path) are removed from
BOTH sides before anything is compared.

Icons: favicon and touch-icon requests are removed from both sides. The crawler's
browser fetches them and a headless one does not; they are never page content.

Blocking is symmetric by construction: the explorer runs unblocked, and the same
adblock engine with the same five Brave lists (page-screener's blocking.Blocker) is
applied to both request sets here. One decision per URL, made once and applied to
both sides: resource type from the Browsertrix WARC-Resource-Type when the crawl has
the URL, else the explorer's recorded type, else "other"; source URL the page. Every
removal is recorded with its list and rule text (rule text from an unoptimised debug
engine over the same lists, checked to agree with Blocker on every decision).
"""

import argparse
import json
import re
import sys
import urllib.parse
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from warcio.archiveiterator import ArchiveIterator

PKG_ROOT = Path(__file__).resolve().parent.parent
OUT = PKG_ROOT / "runs" / "pilot"
sys.path.insert(0, str(PKG_ROOT.parent / "page-screener" / "tools"))
from screen import normalise_request as path_key  # noqa: E402
import blocking  # noqa: E402

ICON_RE = re.compile(r"(^|/)(favicon[^/]*|apple-touch-icon[^/]*|[^/]+\.ico)$", re.I)


def is_icon(url: str) -> bool:
    return bool(ICON_RE.search(urllib.parse.urlsplit(url).path))


class PostHocBlocker:
    """blocking.Blocker for decisions, plus rule text from debug engines."""

    def __init__(self):
        import adblock
        self.blocker = blocking.Blocker()
        self.debug = {}
        for entry in self.blocker.manifest["lists"]:
            fs = adblock.FilterSet(debug=True)
            fs.add_filter_list((self.blocker.lists_dir / entry["path"]).read_text(errors="replace"))
            self.debug[entry["component_name"]] = adblock.Engine(filter_set=fs, optimize=False)
        self.cache: dict[tuple, dict | None] = {}

    def match(self, url: str, source: str, rtype: str) -> dict | None:
        key = (url, source, rtype)
        if key not in self.cache:
            hit = self.blocker.match(url, source, rtype)
            if hit:
                r = self.debug[hit["list"]].check_network_urls(
                    url=url, source_url=source, request_type=hit["request_type"])
                if not r.matched:
                    raise RuntimeError(f"debug engine disagrees with Blocker on {url}")
                hit = {**hit, "rule": r.filter}
            self.cache[key] = hit
        return self.cache[key]


def url_key(url: str) -> str:
    return urllib.parse.urldefrag(url)[0]


def host_of(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc


def warc_set(job_dir: Path) -> set[str]:
    return set(warc_types(job_dir))


def warc_types(job_dir: Path) -> dict[str, str]:
    urls: dict[str, str] = {}
    for warc in sorted(job_dir.glob("collections/*/archive/*.warc.gz")):
        with open(warc, "rb") as fh:
            for rec in ArchiveIterator(fh):
                if rec.rec_type not in ("response", "revisit") or rec.http_headers is None:
                    continue
                target = rec.rec_headers.get_header("WARC-Target-URI") or ""
                if not target.startswith("http"):
                    continue
                try:
                    status = int(rec.http_headers.get_statuscode())
                except (TypeError, ValueError):
                    continue
                if status == 200:
                    urls.setdefault(url_key(target),
                                    rec.rec_headers.get_header("WARC-Resource-Type") or "")
    return urls


def crawl_log(job_dir: Path) -> list[dict]:
    out = []
    for log in sorted(job_dir.glob("collections/*/logs/*.log")):
        for line in log.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def explorer(page_dir: Path) -> dict | None:
    for f in sorted((page_dir / "explorer").glob("*.json")):
        d = json.loads(f.read_text())
        if "runs" in d:
            return d
    return None


def explorer_sets(ex: dict) -> dict:
    runs = [r for r in ex["runs"] if "baseline_raw" in r]
    periodic = set(ex.get("periodic_patterns", []))
    base = set()
    for r in runs:
        for raw in r["baseline_raw"]:
            if r["baseline_status"].get(path_key(raw)) == 200:
                base.add(url_key(raw))
    new, attribution, unstable = set(), {}, set()
    for key, v in ex["stable_new_content"].items():
        if (v.get("response") or {}).get("status") != 200:
            continue
        for r in runs:
            for raw in (r["new_content"].get(key) or {}).get("raw", []):
                new.add(url_key(raw))
                attribution[url_key(raw)] = {"run1": v.get("run1"), "run2": v.get("run2")}
    for key in ex.get("unstable_new_content", {}):
        for r in runs:
            for raw in (r["new_content"].get(key) or {}).get("raw", []):
                unstable.add(url_key(raw))
    types: dict[str, str] = {}
    for r in runs:
        for raw, t in (r.get("raw_types") or {}).items():
            types.setdefault(url_key(raw), t)
    return {"base": base, "new": new, "attribution": attribution, "periodic": periodic,
            "unstable": unstable, "runs_with_raw": len(runs), "runs": len(ex["runs"]),
            "types": types, "blocked_at_runtime": bool(ex["parameters"].get("block_ads")),
            "match_crawler": any(r.get("raw_types") is not None for r in runs)}


def by_host(urls: set[str]) -> dict[str, list[str]]:
    out = defaultdict(list)
    for u in sorted(urls):
        out[host_of(u)].append(u)
    return dict(out)


def iou(a: set, b: set) -> float | None:
    return round(len(a & b) / len(a | b), 3) if a | b else None


# ---- instrument check --------------------------------------------------------------

def stage_row(bt: set, ex: set) -> dict:
    return {"bt": len(bt), "ex": len(ex), "iou_url": iou(bt, ex),
            "iou_path": iou({path_key(u) for u in bt}, {path_key(u) for u in ex})}


def check(run_dir: Path, pages: list[dict]) -> dict:
    blocker = PostHocBlocker()
    result = {}
    for p in pages:
        pd = run_dir / p["id"]
        ex = explorer(pd)
        if ex is None or not (pd / "none").exists():
            result[p["id"]] = {"missing": True}
            continue
        es = explorer_sets(ex)
        bt_types = warc_types(pd / "none")
        per = es["periodic"]
        bt0 = {u for u in bt_types if path_key(u) not in per}
        ex0 = {u for u in es["base"] if path_key(u) not in per}
        icons = sorted(u for u in bt0 | ex0 if is_icon(u))
        bt1, ex1 = bt0 - set(icons), ex0 - set(icons)
        removed = []
        for u in sorted(bt1 | ex1):
            rtype = bt_types.get(u) or es["types"].get(u) or "other"
            hit = blocker.match(u, p["url"], rtype)
            if hit:
                removed.append({"url": u, "sides": [s for s, st in (("browsertrix", bt1), ("explorer", ex1))
                                                    if u in st],
                                "type": rtype, "list": hit["list"], "rule": hit["rule"]})
        blocked = {r["url"] for r in removed}
        bt2, ex2 = bt1 - blocked, ex1 - blocked
        bt2_path, ex2_path = {path_key(u) for u in bt2}, {path_key(u) for u in ex2}
        typed = lambda us: {h: [f"{u}  [{bt_types.get(u) or es['types'].get(u) or '?'}]"  # noqa: E731
                                for u in v] for h, v in by_host(us).items()}
        result[p["id"]] = {
            "url": p["url"],
            "explorer": {"blocked_at_runtime": es["blocked_at_runtime"],
                         "runs_with_baseline_raw": f"{es['runs_with_raw']}/{es['runs']}"},
            "periodic_patterns": len(per),
            "stages": {"0 raw": stage_row(bt0, ex0), "1 -icons": stage_row(bt1, ex1),
                       "2 -blocked": stage_row(bt2, ex2)},
            "icons_removed": icons,
            "blocked_removed": removed,
            "only_browsertrix": typed(bt2 - ex2),
            "only_explorer": typed(ex2 - bt2),
            "query_only_diffs": sorted(u for u in (bt2 ^ ex2) if path_key(u) in (bt2_path & ex2_path)),
        }
    return result


# ---- recall ------------------------------------------------------------------------

def ts(rec: dict) -> float:
    return datetime.fromisoformat(rec["timestamp"].replace("Z", "+00:00")).timestamp()


SKIP_RE = re.compile(r"^Skipping element \((form|submit|path|consent|label \"([^\"]*)\": (.*))\): ")


def revealclick_metrics(log: list[dict]) -> dict:
    elements, sources, skips, labels, blocked = [], defaultdict(int), defaultdict(int), [], []
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
        m = re.match(r"^Clicking on element \(([^)]*)\): (.*)", msg)
        if m:
            srcs = [s for s in m.group(1).split(", ") if s]
            for s in srcs:
                sources[s] += 1
            elements.append({"sources": srcs, "html": m.group(2)[:100], "clicks": None})
        elif msg.startswith("Clicked ") and elements:
            elements[-1]["clicks"] = int(msg.split()[1])
        m = SKIP_RE.match(msg)
        if m:
            kind = "label" if m.group(2) is not None else m.group(1)
            skips[kind] += 1
            if kind == "label":
                labels.append({"term": m.group(2), "name": m.group(3)})
        if msg.startswith("Blocked navigation to: "):
            blocked.append(msg[len("Blocked navigation to: "):])
    clicks = [e["clicks"] for e in elements if e["clicks"] is not None]
    return {"elements": len(elements), "clicks_total": sum(clicks),
            "clicks_per_element": sorted(clicks, reverse=True),
            "cap_hits": sum(1 for c in clicks if c >= 10),
            "sources": dict(sources), "skips": dict(skips), "label_skips": labels,
            "blocked_navigations": blocked, "timed_out": timed_out,
            "behavior_s": round(end - start, 1) if start and end else None,
            "clicked": elements}


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


def recall(pages: list[dict]) -> dict:
    result = {}
    for p in pages:
        pd = OUT / p["id"]
        ex = explorer(pd)
        es = explorer_sets(ex)
        per = es["periodic"]
        keep = lambda s: {u for u in s if path_key(u) not in per}  # noqa: E731
        base = keep(warc_set(pd / "none")) | keep(es["base"])
        crawls = {}
        for job_dir in sorted(d for d in pd.iterdir() if d.is_dir() and d.name[:2] in ("A-", "B-", "F-")):
            log = crawl_log(job_dir)
            t, to = behavior_time(log)
            crawls[job_dir.name] = {"new": keep(warc_set(job_dir)) - base, "time": t, "timed_out": to,
                                    "log": log}
        ex_new = keep(es["new"]) - base
        pool = ex_new.union(*(c["new"] for c in crawls.values()))
        explorer_any = es["new"] | es["unstable"]
        iso_new = set().union(*(c["new"] for n, c in crawls.items() if "-iso-" in n))
        click_attr = {u for u in ex_new
                      if any((es["attribution"][u].get(r) or {}).get("action") == "click"
                             for r in ("run1", "run2"))}
        click_pool = click_attr | (iso_new - explorer_any)

        rows = {}
        for cfg in ("A", "B", "F"):
            for mode in ("iso", "comb"):
                reps = [crawls.get(f"{cfg}-{mode}-r{r}") for r in (1, 2)]
                reps = [c for c in reps if c]
                if not reps:
                    continue
                union = set().union(*(c["new"] for c in reps))
                rows[f"{cfg}-{mode}"] = {
                    "pool": [len(c["new"] & pool) for c in reps],
                    "pool_union": len(union & pool),
                    "click_pool": [len(c["new"] & click_pool) for c in reps],
                    "click_pool_union": len(union & click_pool),
                    "pool_path": len({path_key(u) for u in union} & {path_key(u) for u in pool}),
                    "time": [c["time"] for c in reps],
                    "timed_out": [c["timed_out"] for c in reps],
                }
        rows["explorer"] = {"pool_union": len(ex_new & pool), "click_pool_union": len(ex_new & click_pool),
                            "pool_path": len({path_key(u) for u in ex_new} & {path_key(u) for u in pool})}

        pool_paths = defaultdict(set)
        for u in pool | base:
            pool_paths[path_key(u)].add(u)
        query_only = sorted(u for u in pool if len(pool_paths[path_key(u)]) > 1)

        f_logs = {n: revealclick_metrics(c["log"]) for n, c in crawls.items() if n.startswith("F-")}
        f_found = set().union(*(c["new"] for n, c in crawls.items() if n.startswith("F-")))
        gaps = [{"url": u, "explorer": es["attribution"].get(u),
                 "f_iso": any(u in c["new"] for n, c in crawls.items() if n.startswith("F-iso")),
                 "f_comb": any(u in c["new"] for n, c in crawls.items() if n.startswith("F-comb"))}
                for u in sorted(click_pool - f_found)]
        result[p["id"]] = {
            "url": p["url"], "tag": p["tag"], "role": p["role"],
            "baseline": len(base), "periodic_patterns": len(per),
            "pool": len(pool), "pool_paths": len({path_key(u) for u in pool}),
            "click_pool": len(click_pool), "click_pool_explorer": len(click_attr),
            "click_pool_config_only": len(click_pool - click_attr),
            "query_only_in_pool": query_only,
            "rows": rows, "revealclick": f_logs, "gaps": gaps,
            "pool_urls": sorted(pool), "click_pool_urls": sorted(click_pool),
        }
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mode", choices=["check", "recall"])
    ap.add_argument("--run-dir", type=Path, default=OUT)
    args = ap.parse_args()
    pages = json.loads((PKG_ROOT / "tools" / "pilot_pages.json").read_text())["pages"]
    res = check(args.run_dir, pages) if args.mode == "check" else recall(pages)
    out = args.run_dir / f"{args.mode}.json"
    out.write_text(json.dumps(res, indent=2, default=list))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
