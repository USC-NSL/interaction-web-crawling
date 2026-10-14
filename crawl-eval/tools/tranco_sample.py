#!/usr/bin/env python3
"""Draw and screen the Tranco sample, and pick each domain's internal pages.

Run with page-screener's venv (it has Playwright):

    ../page-screener/.venv/bin/python tools/tranco_sample.py --list-id 4672X --seed 20261012

1. List: the pinned Tranco list (--list-id), downloaded once and kept in the output dir.
2. Draw: --draw domains uniformly at random over the whole list, seeded, without
   replacement. The draw order is the order of preference.
3. Screen, in draw order: a domain is discarded for the first reason that applies, and
   the reason is recorded. Screening stops starting new domains once the first --keep
   survivors in draw order are known.
     adult content       domain name, then page title or description (before any page load
                         where the name already says so)
     robots.txt          the homepage is disallowed for our user agent
     unreachable         DNS, connection, TLS or timeout, on the domain and on www.
     bot challenge       a challenge or block page (never bypassed)
     not a website       not a 2xx text/html document after redirects
     other domain        the homepage redirects to another site
     login wall          a password form on a page with little else
     parked or thin      parking-service markers, under 500 characters of visible text,
                         or no same-site links
4. Internal pages, per kept domain, up to --pages: same-site links from the rendered
   homepage, filtered (login, account, cart, checkout, search, files, mailto, fragment-only)
   and grouped by path pattern (numeric and id-like segments as one), one page per pattern,
   picked at random with a per-domain seed. If fewer than --pages: sitemap.xml (the ones
   robots.txt lists, else /sitemap.xml; one child of a sitemap index). If still fewer, the
   domain goes on the manual list for a site: search by a person. No search engine is queried.

Etiquette: logged out, the explorer's crawler user agent with the contact URL, robots.txt
respected (through the explorer's robots cache, so the explorer does not fetch it again),
and at least 60s from the end of one load of a host to the start of the next (robots.txt,
homepage, sitemap each count). Every load is appended to <out>/jobs.jsonl, which
parallel_explore.py reads, so the gap also holds for the exploration that follows.

Writes, in --out: list-<id>.csv, draw.json, screening.jsonl, sample.json (the kept
domains with their pages and sources), pages.json (input for parallel_explore.py),
manual_search.txt, summary.md. Reads manual_pages.json (pages from a site: search by a
person), domain_notes.json (notes by hand, printed in summary.md) and domain_exclusions.json
(domains dropped by hand, with the reason; replaced by the next survivors) when present.
"""

import argparse
import asyncio
import gzip
import json
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, OrderedDict
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
SCREENER = PKG_ROOT.parent / "page-screener"
sys.path.insert(0, str(SCREENER / "tools"))
from explore import CRAWLER_ARGS, CRAWLER_HEADERS, CRAWLER_UA, CRAWLER_VIEWPORT  # noqa: E402
from screen import RobotsCache  # noqa: E402

MIN_HOST_GAP = 60.0
RENDERS = 4                 # homepages rendered at once
GOTO_TIMEOUT_MS = 30000
AFTER_LOAD_MS = 3000
THIN_CHARS = 500
LOGIN_WALL_CHARS = 1500

# Excluded categories, checked on the domain name before any load and on the homepage's
# title and description after it, for every drawn domain (also ones screened earlier).
GAMBLING = re.compile(r"casino|sportsbook|\bbet(s|ting)?\b|bet\d|\bslots?\b|jackpot|poker|"
                      r"roulette|baccarat|lottery|\blotto\b|bingo|togel|\bjudi\b|gambl|"
                      r"apuestas|scommesse|nổ hũ|lô đề|nhà cái|ставки|казино", re.I)
PIRACY = re.compile(r"torrent|warez|\bcrack(ed|s)?\b|keygen|nulled|subtitles?\b|"
                    r"(read|watch|listen|download)\b[^|]{0,40}\b(online )?(for )?free\b|"
                    r"free (movies|manga|anime|ebooks?|audiobooks?|series)|mangas? online|"
                    r"certifications? exams?|exam (questions|dumps)|braindumps?|"
                    r"слушать онлайн|смотреть онлайн|скачать бесплатно", re.I)
ADULT = re.compile(r"(^|[^a-z])(porn|xxx|sex|adult|nsfw|escort|hentai|nude|camgirl|"
                   r"onlyfans|erotic|fetish|milf|xvideos|xnxx|redtube|brazzers)", re.I)
CATEGORIES = [("adult content", ADULT), ("gambling or betting", GAMBLING),
              ("piracy-adjacent", PIRACY)]
# applied, but not yet confirmed by the study's advisor
PENDING = {"piracy-adjacent": " (pending the advisor's confirmation)"}


def excluded_category(text: str) -> str | None:
    for name, rule in CATEGORIES:
        if rule.search(text):
            return name
    return None


CHALLENGE = re.compile(r"just a moment|attention required|cf-chl|checking your browser|"
                       r"verify you are (a )?human|captcha-delivery|px-captcha|"
                       r"access denied|request unsuccessful|pardon our interruption|"
                       r"are you a robot|bot detection", re.I)
PARKED = re.compile(r"domain (name )?(is|may be) for sale|buy this domain|this domain is parked|"
                    r"parked free|sedoparking|sedo\.com|dan\.com|afternic|hugedomains|"
                    r"godaddy\.com/domainsearch|domain has expired|parkingcrew|bodis\.com", re.I)
SKIP_PATH = re.compile(r"/(log-?in|log-?out|sign-?in|sign-?up|sign-?out|register|account|"
                       r"my-?account|profile|cart|basket|bag|checkout|wish-?list|search|auth|"
                       r"password|subscribe|unsubscribe|"
                       r"recherche|suche|buscar|busca|ricerca|pesquisa|検索|my-?page)(/|$|\.)", re.I)
# transactional hosts: cart., checkout., order(s)., account(s)., login., my..., secure...
SKIP_HOST = re.compile(r"^(cart|checkout|order|account|login|my|secure)", re.I)


def rejected(url: str) -> bool:
    """A page the sample never uses: a login, account, cart or search page, by path or host."""
    u = urllib.parse.urlsplit(url)
    return bool(SKIP_PATH.search(urllib.parse.unquote(u.path) or "/")
                or SKIP_HOST.match((u.hostname or "").split(".")[0]))
FILE_EXT = re.compile(r"\.(pdf|zip|gz|rar|7z|tar|jpe?g|png|gif|webp|svg|ico|mp[34]|mov|avi|"
                      r"wav|docx?|xlsx?|pptx?|csv|exe|dmg|apk|msi|iso|xml|rss|json|txt)$", re.I)
ID_SEGMENT = re.compile(r"^(\d+|[0-9a-f]{8,}|[0-9a-f-]{20,}|.*\d{3,}.*)$", re.I)


def host_key(netloc: str) -> str:
    netloc = netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def same_site(url: str, domain: str) -> bool:
    h = urllib.parse.urlsplit(url).hostname or ""
    return h == domain or h.endswith("." + domain)


def pattern(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    segs = [s for s in parts.path.lower().split("/") if s]
    return "/" + "/".join("{id}" if ID_SEGMENT.match(s) else s for s in segs)


def candidate_urls(urls: list[str], domain: str, home: str) -> "OrderedDict[str, list[str]]":
    """Same-site page URLs, filtered, grouped by path pattern (first-seen order)."""
    home_key = urllib.parse.urlsplit(home)._replace(fragment="", query="").geturl().rstrip("/")
    groups: OrderedDict[str, list[str]] = OrderedDict()
    for raw in urls:
        u = urllib.parse.urlsplit(raw.strip())
        if u.scheme not in ("http", "https") or not same_site(raw, domain):
            continue
        url = u._replace(fragment="").geturl()
        path = urllib.parse.unquote(u.path) or "/"
        if (url.rstrip("/") == home_key or path in ("", "/") or rejected(url)
                or FILE_EXT.search(path)):
            continue
        groups.setdefault(pattern(url), [])
        if url not in groups[pattern(url)]:
            groups[pattern(url)].append(url)
    return groups


class Loads:
    """Per-host load bookkeeping: the 60s gap, and a log parallel_explore.py reads."""

    def __init__(self, out: Path):
        self.log = out / "jobs.jsonl"
        self.last_end: dict[str, float] = {}
        for prior in set(PKG_ROOT.glob("runs/*/jobs.jsonl")) | {self.log}:
            if prior.exists():
                for line in prior.read_text().splitlines():
                    rec = json.loads(line)
                    h = host_key(rec.get("host", ""))
                    self.last_end[h] = max(self.last_end.get(h, 0), rec["end"])

    async def wait(self, host: str) -> None:
        delay = self.last_end.get(host_key(host), 0) + MIN_HOST_GAP - time.time()
        if delay > 0:
            await asyncio.sleep(delay)

    def done(self, host: str, url: str, kind: str, start: float) -> None:
        end = time.time()
        self.last_end[host_key(host)] = end
        with self.log.open("a") as fh:
            fh.write(json.dumps({"page": url, "url": url, "job": f"screen-{kind}",
                                 "host": host_key(host), "start": start, "end": end}) + "\n")


def http_get(url: str, limit: int = 5_000_000) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": CRAWLER_UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read(limit)
    except urllib.error.HTTPError as exc:
        return exc.code, b""
    except Exception:  # noqa: BLE001 - network is allowed to fail
        return 0, b""


def sitemap_locs(body: bytes) -> tuple[list[str], bool]:
    if body[:2] == b"\x1f\x8b":
        try:
            body = gzip.decompress(body)
        except OSError:
            return [], False
    text = body.decode("utf-8", "replace")
    return re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", text), "<sitemapindex" in text


HOME_JS = """() => ({
  title: document.title || '',
  description: (document.querySelector('meta[name="description"]') || {}).content || '',
  text: (document.body && document.body.innerText || '').length,
  password: !!Array.from(document.querySelectorAll('input[type=password]'))
              .find((i) => i.offsetWidth > 0 && i.offsetHeight > 0),
  links: Array.from(document.querySelectorAll('a[href]')).map((a) => a.href),
  html: document.documentElement.outerHTML.slice(0, 200000),
})"""


async def render(browser, url: str) -> dict:
    ctx = await browser.new_context(user_agent=CRAWLER_UA, viewport=CRAWLER_VIEWPORT,
                                    extra_http_headers=CRAWLER_HEADERS)
    page = await ctx.new_page()
    try:
        resp = await page.goto(url, timeout=GOTO_TIMEOUT_MS, wait_until="load")
        await page.wait_for_timeout(AFTER_LOAD_MS)
        info = await page.evaluate(HOME_JS)
        info.update(status=resp.status if resp else 0,
                    content_type=(resp.headers.get("content-type", "") if resp else ""),
                    final_url=page.url)
        return info
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    finally:
        await ctx.close()


async def screen_domain(i: int, rank: int, domain: str, browser, sem, loads: Loads,
                        robots: RobotsCache, args, rng_seed: str) -> dict:
    rec = {"draw": i, "rank": rank, "domain": domain}
    category = excluded_category(re.sub(r"[.-]", " ", domain))
    if category:
        return {**rec, "kept": False, "reason": category, "detail": "domain name"}

    # robots.txt for the domain, then www. if the domain itself does not answer
    home, robots_res = None, None
    for host in (domain, "www." + domain):
        await loads.wait(host)
        start = time.time()
        res = await asyncio.to_thread(robots.check, f"https://{host}/")
        loads.done(host, f"https://{host}/robots.txt", "robots", start)
        if not robots._raw.get(host, "").startswith("# fetch failed"):
            home, robots_res = f"https://{host}/", res
            break
    if home is None:
        return {**rec, "kept": False, "reason": "unreachable",
                "detail": robots._raw.get(domain, "")[:60]}
    if not robots_res["allowed"]:
        return {**rec, "kept": False, "reason": "robots.txt", "detail": home}

    info = None
    for url in dict.fromkeys([home, f"https://www.{domain}/"]):
        await loads.wait(urllib.parse.urlsplit(url).netloc)
        async with sem:
            start = time.time()
            info = await render(browser, url)
            loads.done(urllib.parse.urlsplit(url).netloc, url, "homepage", start)
        if "error" not in info:
            break
    if "error" in info:
        return {**rec, "kept": False, "reason": "unreachable", "detail": info["error"]}
    final = info["final_url"]
    rec.update(homepage=final, status=info["status"], title=info["title"][:120],
               description=info["description"][:300], text_chars=info["text"])
    category = excluded_category(info["title"] + " " + info["description"])
    if category:
        return {**rec, "kept": False, "reason": category, "detail": "title/description"}
    if CHALLENGE.search(info["title"]) or (info["status"] in (401, 403, 429, 503)
                                           and CHALLENGE.search(info["html"])):
        return {**rec, "kept": False, "reason": "bot challenge", "detail": info["title"][:80]}
    if not (200 <= info["status"] < 300) or "text/html" not in info["content_type"].lower():
        return {**rec, "kept": False, "reason": "not a website",
                "detail": f"{info['status']} {info['content_type'][:40]}"}
    if not same_site(final, domain):
        return {**rec, "kept": False, "reason": "other domain", "detail": final}
    links = candidate_urls(info["links"], domain, final)
    if info["password"] and info["text"] < LOGIN_WALL_CHARS:
        return {**rec, "kept": False, "reason": "login wall", "detail": final}
    if PARKED.search(info["html"]) or info["text"] < THIN_CHARS or not links:
        why = ("parking markers" if PARKED.search(info["html"]) else
               f"{info['text']} chars" if info["text"] < THIN_CHARS else "no same-site links")
        return {**rec, "kept": False, "reason": "parked or thin", "detail": why}

    rec["links"] = {p: urls[:20] for p, urls in links.items()}
    return {**rec, **await pick_pages(domain, final, links, robots, loads, args, rng_seed)}


async def robots_allowed(url: str, robots: RobotsCache, loads: Loads) -> bool:
    host = urllib.parse.urlsplit(url).netloc
    if host not in robots._parsers:         # a host whose robots.txt is not loaded yet
        await loads.wait(host)
        start = time.time()
        res = await asyncio.to_thread(robots.check, url)
        loads.done(host, f"https://{host}/robots.txt", "robots", start)
        return res["allowed"]
    return robots.check(url)["allowed"]


async def pick_pages(domain: str, home: str, links: dict, robots: RobotsCache, loads: Loads,
                     args, rng_seed: str) -> dict:
    """Random homepage-link patterns, then the sitemap; pages robots.txt disallows are skipped."""
    async def allowed(url: str) -> bool:
        return await robots_allowed(url, robots, loads)

    rng = random.Random(f"{rng_seed}:{domain}")
    pages, used, skipped, note = [], set(), [], None

    async def take(groups: dict, source: str) -> None:
        pats = [p for p in groups if p not in used]
        rng.shuffle(pats)
        for p in pats:
            if len(pages) >= args.pages:
                return
            url = rng.choice(groups[p])
            used.add(p)
            if await allowed(url):
                pages.append({"url": url, "source": source})
            else:
                skipped.append(url)

    await take(links, "homepage links")
    if len(pages) < args.pages:
        found = await sitemap_pages(domain, home, robots, loads)
        note = found["note"]
        await take(candidate_urls(found["locs"], domain, home), "sitemap.xml")
    out = {"kept": True, "pages": pages, "link_patterns": len(links),
           "manual_search_needed": len(pages) < args.pages}
    if note:
        out["sitemap"] = note
    if skipped:
        out["robots_skipped"] = skipped
    return out


async def sitemap_pages(domain: str, home: str, robots: RobotsCache, loads: Loads) -> dict:
    host = urllib.parse.urlsplit(home).netloc
    raw = robots._raw.get(host) or robots._raw.get(domain) or ""
    listed = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", raw)
    urls = [u for u in listed if same_site(u, domain)][:1] or [f"https://{host}/sitemap.xml"]
    locs: list[str] = []
    note = []
    for _ in range(2):          # the sitemap, and one child if it is an index
        url = urls[0]
        await loads.wait(urllib.parse.urlsplit(url).netloc)
        start = time.time()
        status, body = await asyncio.to_thread(http_get, url)
        loads.done(urllib.parse.urlsplit(url).netloc, url, "sitemap", start)
        found, is_index = sitemap_locs(body) if status == 200 else ([], False)
        note.append(f"{url} -> {status}, {len(found)} loc{'s (index)' if is_index else 's'}")
        if not is_index:
            locs = found
            break
        children = [u for u in found if same_site(u, domain)]
        if not children:
            break
        urls = [random.Random(domain).choice(children)]
    return {"locs": locs, "note": "; ".join(note)}


def download_list(list_id: str, out: Path) -> list[str]:
    path = out / f"list-{list_id}.csv"
    if not path.exists():
        req = urllib.request.Request(f"https://tranco-list.eu/download/{list_id}/1000000",
                                     headers={"User-Agent": CRAWLER_UA})
        with urllib.request.urlopen(req, timeout=120) as resp:
            path.write_bytes(resp.read())
    return [line.split(",", 1)[1].strip() for line in path.read_text().splitlines() if "," in line]


async def main_async(args) -> int:
    from playwright.async_api import async_playwright

    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    domains = download_list(args.list_id, out)
    order = random.Random(args.seed).sample(range(len(domains)), args.draw)
    draw = [{"draw": i, "rank": r + 1, "domain": domains[r]} for i, r in enumerate(order)]
    (out / "draw.json").write_text(json.dumps({"list_id": args.list_id, "list_size": len(domains),
                                               "seed": args.seed, "draw": draw}, indent=2) + "\n")
    print(f"Tranco list {args.list_id}: {len(domains)} domains; drew {len(draw)} with seed {args.seed}",
          flush=True)

    done_path = out / "screening.jsonl"
    results: dict[int, dict] = {}
    if done_path.exists():
        for line in done_path.read_text().splitlines():
            r = json.loads(line)
            results[r["draw"]] = r
    loads = Loads(out)
    robots = RobotsCache(SCREENER / "cache" / "robots")
    sem = asyncio.Semaphore(RENDERS)
    stop = asyncio.Event()

    def record(r: dict) -> None:
        results[r["draw"]] = r
        with done_path.open("a") as fh:
            fh.write(json.dumps(r) + "\n")

    # domains dropped by hand, with the reason: {"domain": "why"}; replaced like any other
    dropped_path = out / "domain_exclusions.json"
    dropped = json.loads(dropped_path.read_text()) if dropped_path.exists() else {}
    for r in list(results.values()):
        if r["domain"] in dropped and r.get("reason") != "dropped by hand":
            record({**r, "kept": False, "reason": "dropped by hand", "was": r.get("reason", "kept"),
                    "detail": dropped[r["domain"]], "reclassified": True})
            print(f"  [{r['draw']:3}] {r['domain']:40} dropped by hand (was {r.get('reason', 'kept')})",
                  flush=True)

    # the exclusion rule applies to the whole draw, including domains screened earlier: a
    # domain name match comes before every other reason, a title match before the reasons
    # screening decides after reading the title
    after_title = {"bot challenge", "not a website", "other domain", "login wall", "parked or thin"}
    for r in list(results.values()):
        if r.get("reason") in {name for name, _ in CATEGORIES} | {"dropped by hand"}:
            continue
        by_name = excluded_category(re.sub(r"[.-]", " ", r["domain"]))
        by_title = excluded_category(r.get("title", "") + " " + r.get("description", ""))
        if by_name or (by_title and (r["kept"] or r.get("reason") in after_title)):
            category = by_name or by_title
            record({**r, "kept": False, "reason": category, "was": r.get("reason", "kept"),
                    "detail": "reclassified: " + ("domain name" if by_name else "title"),
                    "reclassified": True})
            print(f"  [{r['draw']:3}] {r['domain']:40} excluded: {category} "
                  f"(was {r.get('reason', 'kept')})", flush=True)

    def enough() -> bool:
        kept = 0
        for d in draw:
            r = results.get(d["draw"])
            if r is None:
                return False
            kept += r["kept"]
            if kept >= args.keep:
                return True
        return False

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, channel="chromium", args=CRAWLER_ARGS,
                                           ignore_default_args=["--enable-automation",
                                                                "--hide-scrollbars"])
        # kept domains whose pages a newer path filter rejects: re-render the homepage
        # (one load) and pick again, for those domains only
        def disallowed(url: str) -> bool:
            # only where robots.txt is already cached: checking costs no load
            host = urllib.parse.urlsplit(url).netloc
            cached = (SCREENER / "cache" / "robots" / f"{re.sub(r'[^A-Za-z0-9.-]', '_', host)}.txt")
            return cached.exists() and not robots.check(url)["allowed"]

        for r in [r for r in results.values() if r["kept"]]:
            stale = [p for p in r["pages"] if rejected(p["url"]) or disallowed(p["url"])]
            if not stale:
                continue
            host = urllib.parse.urlsplit(r["homepage"]).netloc
            await loads.wait(host)
            start = time.time()
            info = await render(browser, r["homepage"])
            loads.done(host, r["homepage"], "homepage", start)
            if "error" in info:
                print(f"  [{r['draw']:3}] {r['domain']:40} re-pick failed: {info['error']}")
                continue
            links = candidate_urls(info["links"], r["domain"], info["final_url"])
            picked = await pick_pages(r["domain"], info["final_url"], links, robots, loads, args,
                                      str(args.seed))
            record({**r, **picked, "links": {p: u[:20] for p, u in links.items()},
                    "repicked": [p["url"] for p in stale]})
            print(f"  [{r['draw']:3}] {r['domain']:40} re-picked: {len(stale)} page(s) replaced "
                  f"({', '.join(p['url'] for p in stale)[:120]})", flush=True)

        # pages added by hand from a site: search are checked against robots.txt too
        manual_path = out / "manual_pages.json"
        manual = json.loads(manual_path.read_text()) if manual_path.exists() else {}
        manual_robots = {u: await robots_allowed(u, robots, loads)
                         for urls in manual.values() for u in urls}
        (out / "manual_robots.json").write_text(json.dumps(manual_robots, indent=2) + "\n")
        for u, ok in manual_robots.items():
            print(f"  manual page {'allowed' if ok else 'DISALLOWED by robots.txt'}: {u}", flush=True)

        queue = [d for d in draw if d["draw"] not in results]
        active: set[asyncio.Task] = set()
        if enough():
            stop.set()      # the first --keep survivors are already known

        async def run_one(d: dict) -> None:
            r = await screen_domain(d["draw"], d["rank"], d["domain"], browser, sem, loads,
                                    robots, args, str(args.seed))
            record(r)
            mark = "kept" if r["kept"] else f"discard: {r['reason']}"
            print(f"  [{d['draw']:3}] #{d['rank']:<7} {d['domain']:40} {mark}"
                  + (f" ({len(r['pages'])} pages)" if r["kept"] else ""), flush=True)
            if enough():
                stop.set()

        while (queue or active) and not stop.is_set():
            while queue and len(active) < args.concurrency and not stop.is_set():
                t = asyncio.create_task(run_one(queue.pop(0)))
                active.add(t)
                t.add_done_callback(active.discard)
            if active:
                await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
        if active:
            await asyncio.wait(active)      # domains already started are finished, not cut
        await browser.close()

    write_outputs(out, draw, results, args)
    return 0


def write_outputs(out: Path, draw: list[dict], results: dict[int, dict], args) -> None:
    screened = [results[d["draw"]] for d in draw if d["draw"] in results]
    kept = [r for r in screened if r["kept"]][:args.keep]
    # pages a person found with a site: search, kept in their own file so a re-run never
    # overwrites them: {"domain": ["https://...", ...]}
    manual_path = out / "manual_pages.json"
    manual_pages = json.loads(manual_path.read_text()) if manual_path.exists() else {}
    robots_ok = (json.loads((out / "manual_robots.json").read_text())
                 if (out / "manual_robots.json").exists() else {})
    manual_pages = {d: [u for u in urls if robots_ok.get(u, False)]
                    for d, urls in manual_pages.items()}
    for r in kept:
        have = {p["url"] for p in r["pages"]}
        r["pages"] = r["pages"] + [{"url": u, "source": "manual site: search"}
                                   for u in manual_pages.get(r["domain"], []) if u not in have]
        r["manual_search_needed"] = len(r["pages"]) < args.pages
    sample = {"list_id": args.list_id, "seed": args.seed, "pages_per_domain": args.pages,
              "domains": [{k: r[k] for k in ("draw", "rank", "domain", "homepage", "pages")}
                          for r in kept]}
    (out / "sample.json").write_text(json.dumps(sample, indent=2) + "\n")
    pages = []
    for r in kept:
        slug = re.sub(r"[^a-z0-9]+", "-", r["domain"].lower()).strip("-")
        pages.append({"id": f"{slug}-0", "url": r["homepage"], "source": "homepage"})
        for k, p in enumerate(r["pages"], 1):
            pages.append({"id": f"{slug}-{k}", "url": p["url"], "source": p["source"]})
    (out / "pages.json").write_text(json.dumps({"pages": pages}, indent=2) + "\n")
    manual = [r for r in kept if r["manual_search_needed"]]
    if not manual_path.exists():
        manual_path.write_text(json.dumps({r["domain"]: [] for r in manual}, indent=2) + "\n")
    (out / "manual_search.txt").write_text(
        "# Kept domains with fewer than %d internal pages from homepage links and sitemap.xml.\n"
        "# Run a site: search by hand for each, and put the URLs in manual_pages.json. Not automated.\n"
        % args.pages + "".join(f"{r['domain']}\t{len(r['pages'])} found\tsite:{r['domain']}\n"
                               for r in manual))
    reasons = Counter(r["reason"] for r in screened if not r["kept"])
    by_source = Counter(p["source"] for r in kept for p in r["pages"])
    excluded = [r for r in screened if r.get("reclassified") and r.get("was") == "kept"]
    # what the first --keep survivors were before the exclusion rule, to name the replacements
    before = [r for r in screened if r["kept"] or r.get("was") == "kept"][:args.keep]
    replaced_by = [r["domain"] for r in kept if r["domain"] not in {b["domain"] for b in before}]
    lines = [f"# Tranco sample: list {args.list_id}, seed {args.seed}", "",
             f"Drawn {len(draw)}, screened {len(screened)}, kept {len(kept)} "
             f"(first {args.keep} survivors in draw order).", "", "| discard reason | domains |",
             "|---|---|"] + [f"| {k} | {v} |" for k, v in reasons.most_common()] + [
             "", f"Internal pages: {dict(by_source)}; domains needing a manual site: search: "
             f"{len(manual)}", "",
             "Exclusion rule (whole draw, domain name before any load, title and description "
             "after it):"] + [f"- {name}{PENDING.get(name, '')}: `{rule.pattern}`"
                              for name, rule in CATEGORIES] + [
             "", "Excluded after screening (were survivors): "
             + (", ".join(f"{r['domain']} ({r['reason']})" for r in excluded) or "none"),
             *[f"Dropped by hand: {r['domain']}: {r['detail']}" for r in excluded
               if r["reason"] == "dropped by hand"],
             "Replaced by the next survivors in draw order: " + (", ".join(replaced_by) or "none"),
             "Re-picked (page filter or robots.txt): "
             + (", ".join(r["domain"] for r in kept if r.get("repicked")) or "none")]
    # notes kept by hand, per domain (why a domain has fewer pages, for instance)
    notes_path = out / "domain_notes.json"
    notes = json.loads(notes_path.read_text()) if notes_path.exists() else {}
    if notes:
        lines += ["", "Domain notes:"] + [f"- {d}: {n}" for d, n in notes.items()]
    (out / "summary.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines), flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list-id", required=True)
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--draw", type=int, default=125)
    ap.add_argument("--keep", type=int, default=50)
    ap.add_argument("--pages", type=int, default=5)
    ap.add_argument("--concurrency", type=int, default=16, help="domains screened at once")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    args.out = args.out or PKG_ROOT / "runs" / f"tranco-{args.list_id}"
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
