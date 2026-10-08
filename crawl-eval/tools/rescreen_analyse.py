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
from explore import page_id_for  # noqa: E402
from screen import normalise_request as path_key  # noqa: E402

ACTIONS = ("click", "scroll", "hover")
ICONISH = re.compile(r"icon|sprite|\.ico$|/symbols?/", re.I)
CLASSES = ("content", "functional", "presentational", "media", "speculative")
TRACKING = "residual-tracking"     # outside the five classes: ad/tracking the lists missed

MEDIA_EXT = re.compile(r"\.(m3u8|mpd|ts|m4s|m4a|m4v|mp4|webm|mp3|aac|vtt|srt)$", re.I)
# Structural shapes of cookie syncs, pixels and analytics beacons the lists missed.
SYNC_HOST = re.compile(r"(^|[.-])(sync|pixel|beacon)[.-]", re.I)
SYNC_PATH = re.compile(r"(^|/)(setuid|user_?sync|usersync|psync|sync|pixels?|cs|cookie|"
                       r"pageview[\w-]*|collect|beacon|track(ing)?|telemetry)(/|$|\?|\.)", re.I)
ANALYTICS_HOSTS = re.compile(r"(^|\.)(braze\.com|segment\.io|mixpanel\.com|amplitude\.com|"
                             r"hotjar\.com|fullstory\.com|clarity\.ms|spot\.im|adobedtm\.com|omtrdc\.net|"
                             r"demdex\.net|qualtrics\.com)$", re.I)
PROTECTION = re.compile(r"awswaf\.com|recaptcha|hcaptcha|turnstile|/Waa/", re.I)
SEARCH = re.compile(r"search|algolia|docsearch|lunr|pagefind|typesense|meilisearch", re.I)
CONSENT = re.compile(r"cookieyes|onetrust|cookiebot|consent|didomi|trustarc|usercentrics", re.I)
WIDGET_HOSTS = re.compile(r"(^|\.)(googleapis\.com|maps\.gstatic\.com|youtube\.com|"
                          r"ytimg\.com|vimeo\.com|player\.vimeo\.com|twitter\.com|x\.com)$", re.I)
ROUTE_CHUNK = re.compile(r"component---|\.md\.[\w-]+\.js$|/chunks/pages/|/chunks/app/", re.I)


def data_route(url: str) -> str | None:
    """The route a router data request is for, or None if it is not router data."""
    parts = urllib.parse.urlsplit(url)
    m = re.match(r"^/_next/data/[^/]+(/.*)\.json$", parts.path)
    if m:                                               # Next.js pages router
        route = re.sub(r"/index$", "", m.group(1)) or "/"
        return route
    if "_rsc=" in parts.query:                          # Next.js app router (RSC)
        return parts.path
    m = re.match(r"^/page-data(/.*)?/page-data\.json$", parts.path)
    if m:                                               # Gatsby; the home page is "index"
        return re.sub(r"^/index$", "/", m.group(1) or "/")
    m = re.match(r"^(.*)/__data\.json$", parts.path)   # SvelteKit
    if m:
        return m.group(1) or "/"
    return None


def same_route(a: str, b: str) -> bool:
    norm = lambda p: (p.rstrip("/") or "/").lower()  # noqa: E731
    return norm(a) == norm(b)


def classify(u: str, g: dict, page_url: str, noise: set[str], meta: dict) -> tuple[str, str]:
    """(class, rule) for one gated resource. First matching rule wins."""
    rtype, ctype = g["type"], (g["content_type"] or "").lower()
    host = urllib.parse.urlsplit(u).netloc
    page = urllib.parse.urlsplit(page_url)
    target = (g.get("target") or "")
    purpose = (meta.get("purpose") or "").lower()
    action = g["first_action"]
    js = rtype == "script" or "javascript" in ctype
    parts = urllib.parse.urlsplit(u)
    if path_key(u) in noise:
        return TRACKING, "T1 explorer noise rule (host or sync/pixel path) after list filtering"
    if SYNC_HOST.search(host) or SYNC_PATH.search(parts.path):
        return TRACKING, "T2 cookie-sync / pixel / beacon URL shape"
    if ANALYTICS_HOSTS.search(host):
        return TRACKING, "T3 analytics or ad SDK host"
    if "prefetch" in purpose or "prerender" in purpose:
        return "speculative", f"S1 request carried Sec-Purpose/Purpose: {purpose}"
    if rtype not in ("document",) and "text/html" in ctype and rtype in ("other", "fetch", "xhr"):
        return "speculative", "S2 another page's HTML fetched outside navigation"
    route = data_route(u)
    if route is not None and host == page.netloc and not same_route(route, page.path):
        return "speculative", f"S3 router data for another route ({route})"
    on_link = bool(re.search(r"(^|[ >])a([.#:\[]|$)", target.split(">")[-1].strip()))
    if js and host == page.netloc and action in ("hover", "scroll") and (ROUTE_CHUNK.search(u) or on_link):
        return "speculative", "S4 same-site route code loaded by hovering/scrolling links"
    if host == page.netloc and action == "hover" and on_link and rtype in ("fetch", "xhr") \
            and "json" in ctype:
        return "speculative", "S5 same-site data loaded by hovering a link"
    if ctype.startswith(("video/", "audio/")) or "mpegurl" in ctype or "dash+xml" in ctype \
            or rtype == "media" or MEDIA_EXT.search(urllib.parse.urlsplit(u).path):
        return "media", "M1 media type, segment or manifest"
    if rtype == "font" or "font" in ctype:
        return "presentational", "P1 font"
    if ("svg" in ctype or "icon" in ctype) and (ICONISH.search(u) or (g.get("size") or 0) < 4096):
        return "presentational", "P2 icon (svg/icon type, icon path or under 4KB)"
    if rtype == "stylesheet" or "text/css" in ctype:
        return "presentational", "P3 stylesheet"
    if SEARCH.search(u) or SEARCH.search(target):
        return "functional", "F1 search (URL or triggering control)"
    if CONSENT.search(host + parts.path):
        return "functional", "F2 consent manager (host/path)"
    if PROTECTION.search(u):
        return "functional", "F5 bot/abuse protection"
    if WIDGET_HOSTS.search(host) and rtype != "image":
        return "functional", "F3 embedded widget API/script (maps, video player)"
    if js:
        return "functional", "F4 script (feature code)"
    if rtype == "image" or ctype.startswith("image/"):
        return "content", "C1 image"
    if route is not None:
        return "content", f"C2 router data for this page ({route})"
    if rtype in ("fetch", "xhr"):
        return "content", "C3 fetch/xhr data"
    if rtype == "document":
        return "content", "C4 subdocument"
    return "content", f"C9 fallback (type {rtype or '?'}, {ctype or 'no content-type'})"


# Judged on the URL path (a body mentions consent or reviews too often to mean
# anything), except for GraphQL mutations, which only the body shows.
STATE_CHANGE = re.compile(r"cart|basket|wish\s?list|favou?rite|vote|subscri|sign\s?up|newsletter|"
                          r"consent|login|signin|checkout|/orders?\b|comment|reply|helpful|feedback",
                          re.I)
# Reads that happen to use POST: gRPC-web methods named as reads, and query bodies.
READ_RPC = re.compile(r"/\$?rpc/.*/(Get|List|Search|Find|Lookup|Query|Init|BatchGet)\w*$", re.I)
READ_BODY = re.compile(r'"(filter|sort|page|pageSize|query|search|q)"\s*:', re.I)
TELEMETRY = re.compile(r"analytics|collect|beacon|track|telemetry|metrics|/log/|/logs?\b|/events?\b|"
                       r"pixel|rum\b|stats|insights|sentry|datadog|newrelic|nr-data|measurement|"
                       r"conversion|openrtb|prebid|/bid\b|bidder|auction|/hb\b|header/auction|"
                       r"cookie_sync|usersync|user_sync|challenge-platform|/Waa/|/v\d+/[a-z]+/batch",
                       re.I)


def classify_block(b: dict) -> tuple[str, str]:
    """Group a blocked write: would it have changed state, or was it a lost reveal?"""
    parts = urllib.parse.urlsplit(b["url"])
    body = b.get("body") or ""
    if b.get("resource_type") == "document":
        return "state change", "form submission"
    if re.search(r"\bmutation\b", body):
        return "state change", "GraphQL mutation"
    if STATE_CHANGE.search(parts.path):
        return "state change", "URL path names a state change"
    if READ_RPC.search(parts.path):
        return "possible lost reveal", "RPC method named as a read (Get/List/Search/...)"
    if TELEMETRY.search(parts.netloc + parts.path) or b.get("resource_type") == "ping":
        return "telemetry", "analytics, ad auction, error report or beacon"
    if re.search(r'"path"\s*:.*"referrer"\s*:', body):
        return "telemetry", "pageview body (path + referrer)"
    if READ_BODY.search(body):
        return "possible lost reveal", "query body (filter/sort/page/search)"
    return "undetermined", "no rule matched"


def lost_content(b: dict) -> str:
    """What a blocked read would have loaded, from its method name or body keys."""
    path = urllib.parse.urlsplit(b["url"]).path
    rpc = re.search(r"/\$?rpc/([^/]+)/(\w+)$", path)
    if rpc:
        return f"RPC {rpc.group(1).rsplit('.', 1)[-1]}.{rpc.group(2)}"
    keys = re.findall(r'"(\w+)"\s*:', b.get("body") or "")
    return f"{path[-60:]} (body keys: {', '.join(dict.fromkeys(keys[:6]))})" if keys else path[-60:]


def explorer_result(page_dir: Path) -> dict | None:
    for f in sorted(page_dir.glob("*.json")):
        d = json.loads(f.read_text())
        if "page_id" in d and "url" in d:
            return d
    return None


def pages() -> list[dict]:
    out, urls = [], set()
    for p in json.loads((OUT / "pages.json").read_text()):
        if p.get("pilot"):
            continue                    # re-run pilot pages are picked up below
        out.append({**p, "dir": OUT / p["id"]})
    for p in json.loads((PKG_ROOT / "tools" / "pilot_pages.json").read_text())["pages"]:
        rerun = OUT / page_id_for(p["url"])
        page_dir = rerun if (rerun / "done.json").exists() else RUNS / "pilot2" / p["id"] / "explorer"
        out.append({"id": p["id"], "url": p["url"], "group": p["group"], "tag": p["tag"],
                    "dir": page_dir, "pilot": True})
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
    meta: dict[str, dict] = {}
    noise: set[str] = set()
    for r in runs:
        for raw, m in (r.get("request_meta") or {}).items():
            meta.setdefault(url_key(raw), m)
        for n in r.get("noise_filtered") or []:
            noise.add(path_key(n["url"]))

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
                run1 = v.get("run1") or v.get("run2") or {}
                gated[u] = {"actions": sorted(actions), "type": rtype,
                            "first_action": run1.get("action"), "target": run1.get("target"),
                            "initiator": {k: (meta.get(u) or {}).get(k)
                                          for k in ("initiator_type", "initiator_url", "purpose")},
                            "content_type": resp.get("content_type") or "", "status": resp.get("status"),
                            "size": resp.get("size"),
                            "kind": kind_of(u, rtype, resp.get("content_type") or ""),
                            "attribution": {k: v.get(k) for k in ("run1", "run2")}}
    for u, g in gated.items():
        g["class"], g["rule"] = classify(u, g, source, noise, meta.get(u) or {})
    by_action = {a: sorted(u for u, g in gated.items() if a in g["actions"]) for a in ACTIONS}
    # A cap that ends a run before clicking means click content was never looked for.
    ended = sorted({r["caps"]["ended_by"] for r in runs if r["caps"]["ended_by"]})
    clicks_done = min((r.get("action_counts") or {}).get("click", 0) for r in runs) if runs else 0
    if all("phase_caps" in r["caps"] for r in runs):
        # per-phase budgets: a hover cap says nothing about click coverage
        capped = {p for r in runs for p, c in r["caps"]["phase_caps"].items() if c}
        if any(r["caps"]["ended_by"] == "wall_clock_backstop" for r in runs):
            capped |= {"backstop"}
        click_cov = ("complete" if "click" not in capped and "backstop" not in capped else
                     f"partial ({clicks_done} clicks before cap)")
        coverage = "complete" if not capped else f"capped: {', '.join(sorted(capped))}"
    else:
        click_cov = coverage = ("complete" if not ended else
                                "no clicks (cap hit before clicking)" if clicks_done == 0 else
                                f"partial ({clicks_done} clicks before cap)")
    click = by_action["click"]
    kinds = Counter(gated[u]["kind"] for u in click)
    row.update({
        "status": "ok" if not errors else f"partial: {errors}",
        "runs_ok": len(runs),
        "ended_by": ended,
        "coverage": coverage,
        "click_coverage": click_cov,
        "gated_total": len(gated),
        "by_action": {a: len(v) for a, v in by_action.items()},
        "click_pool": len(click),
        "click_kinds": dict(kinds),
        "click_fonts_icons_only": bool(click) and set(kinds) <= {"font", "icon/svg"},
        "click_urls": click,
        "click_classes": dict(Counter(gated[u]["class"] for u in click)),
        "classes_any": dict(Counter(g["class"] for g in gated.values())),
        "class_by_action": {a: dict(Counter(gated[u]["class"] for u in by_action[a])) for a in ACTIONS},
        "has_request_meta": bool(meta),
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
    blocks: dict[tuple, dict] = {}
    for r in runs:
        for b in r.get("blocked_writes") or []:
            key = (b["method"], b["url"].split("?")[0])
            rec = blocks.setdefault(key, {**b, "count": 0, "runs": set()})
            rec["count"] += 1
            rec["runs"].add(r["run"])
    for rec in blocks.values():
        rec["group"], rec["group_rule"] = classify_block(rec)
        rec["runs"] = sorted(rec["runs"])
    row["blocked_writes"] = sorted(blocks.values(), key=lambda b: (b["group"], b["url"]))
    row["blocked_opens"] = sorted({o["url"] for r in runs for o in r.get("blocked_opens") or []})
    row["phase_caps"] = [r["caps"].get("phase_caps") for r in runs]
    row["allowed_writes"] = sorted({(w["method"], w["url"].split("?")[0], w["allowance"])
                                    for r in runs for w in r.get("allowed_writes") or []})
    # denylist skips per rule (explorer runs that record a rule id), counted once per
    # (element, rule) per page so repeated discovery passes do not inflate it
    rule_hits = {(v.get("target"), v.get("rule")) for items in (ex.get("safety_vetoes") or {}).values()
                 for v in items if v.get("rule") and v.get("kind", "").startswith("safety")}
    row["skips_by_rule"] = dict(Counter(rule for _t, rule in rule_hits))
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

    # Guard cost, recorded as a result: what the request guard blocked that looked
    # like a read, per page.
    cost = ["# Request-guard cost: blocked requests that look like lost reveals", "",
            "Per page, every blocked request grouped 'possible lost reveal', with what it "
            "would have loaded (from its RPC method name or body keys).", "",
            "| page | method | host | would have loaded | requests | after | rule |",
            "|---|---|---|---|---|---|---|"]
    for r in rows:
        for b in r.get("blocked_writes") or []:
            if b["group"] == "possible lost reveal":
                cost.append(f"| {r['url']} | {b['method']} | {urllib.parse.urlsplit(b['url']).netloc} | "
                            f"{lost_content(b)} | {b['count']} | {b.get('action')} | "
                            f"{b['group_rule']} |")
    (OUT / "guard_cost.md").write_text("\n".join(cost) + "\n")

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
