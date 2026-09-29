#!/usr/bin/env python3
"""Screen real web pages by what a user interaction reveals on them.

For each URL this runs a baseline pass (no interaction at all) and then separate
scroll-only, hover-only and click-only passes on fresh page loads, so every delta
can be attributed to the action that caused it. A combined pass reproduces the
scroll-then-hover-then-click sequence, which is what the reveal_channel is
computed from; the single-mechanism passes are what mechanisms_present is
computed from.

Output: results.json (full detail) and results.md (a table, network-channel pages
first, because those are the ones a resource-count metric can measure).

Etiquette is not optional here: one page in flight at a time, at least
MIN_HOST_INTERVAL seconds between page loads to any one host, a descriptive
research User-Agent, robots.txt honoured per host, and a click denylist that
skips anything transactional and logs every skip.
"""

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass, field
from pathlib import Path

# The page-screener/ directory itself.
PKG_ROOT = Path(__file__).resolve().parent.parent

# --- etiquette --------------------------------------------------------------

# REQUIRED before any real-page run: replace with a page a site operator can read
# to find out who is requesting and why. A URL, not an email address.
CONTACT_URL = "https://github.com/Gautham176/CURVE-web-interaction-crawl-contact"
CONTACT_PLACEHOLDER = "REPLACE-ME"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36 "
    f"USC-CURVE-AcademicResearch-PageScreener/1.0 (+{CONTACT_URL}; "
    "academic research; classifying interaction-revealed content; not a crawler; "
    "one page at a time, >=5s per host)"
)
# Optional ad/tracker blocker (tools/blocking.py). None means no blocking, which is
# the default and the behavior every result recorded so far was measured under. The
# screener sets it only when run with --block-ads; the exploration oracle sets it
# always, so that its pool and a Browsertrix crawl are filtered comparably.
BLOCKER = None

MIN_HOST_INTERVAL = 5.0        # seconds between page loads to the same host
MAX_CLICKS_PER_PAGE = 15
MAX_CLICK_ATTEMPTS = 40
RETRY_BACKOFF_S = 60.0         # wait for connectivity before retrying a failed URL        # bounds cost when candidates fail actionability
CLICK_TIMEOUT_MS = 3000
HOVER_MS = 500
SCROLL_STEP_PX = 400
SCROLL_PAUSE_MS = 300
BASELINE_SETTLE_S = 10.0
POST_ACTION_SETTLE_S = 2.0
NETWORKIDLE_TIMEOUT_MS = 30000
NAV_TIMEOUT_MS = 60000
STABILITY_GAP_S = 60.0
VIEWPORT = {"width": 1280, "height": 720}

# Transactional or destructive controls are never clicked. The spec's list plus a
# few obvious neighbours; every skip is logged with the text that matched.
# Exactly the specified list, no speculative neighbours. Matched ONLY against a
# control's own accessible name (or its own text when unnamed) - never against
# enclosing block text. Scope is what matters: "Install" once matched "Installation"
# 200 characters away in a docs sidebar, and word boundaries alone would not have
# prevented it. Boundaries are applied on top of that scoping.
SAFETY_DENY = re.compile(
    r"\b("
    r"buy|check\s?out|checkout|subscribe|unsubscribe|delete|post|submit|"
    r"sign\s?in|add\s+to\s+cart"
    r")\b",
    re.IGNORECASE,
)

# Structural vetoes. These matter more than the word list because they do not depend
# on how a control is labelled, so they keep working on non-English pages.
SAFETY_HREF_PATHS = ("/cart", "/checkout", "/account", "/order", "/signin", "/login",
                     "/subscribe")
SAFETY_HREF_SCHEMES = ("mailto:", "tel:")

# Lazy-loading attributes that matter downstream. Browsertrix autofetch harvests
# data-src and srcset only, so a page using data-lazy is a different test case
# from one using data-src even though the user action is identical.
LAZY_ATTRS = ["data-src", "data-srcset", "data-lazy", "data-original", "data-bg"]
IMAGE_URL_RE = re.compile(r"\.(?:jpe?g|png|gif|webp|avif|svg)(?:[?#]|$)", re.IGNORECASE)
DATA_ATTR_RE = re.compile(r"""\b(data-[a-z0-9_-]+)\s*=\s*["']([^"']{1,400})["']""", re.IGNORECASE)

# Ad, analytics and error-reporting hosts. Their URLs carry a fresh cache-buster or
# impression id on every load, so even origin+path comparison reports them as new
# requests and a page with ads classifies as "network" while revealing nothing.
# They are still loaded (the page behaves normally) and still recorded; they are
# excluded from the request-set comparison that decides the reveal channel.
NOISE_HOST_RE = re.compile(
    r"(^|\.)("
    r"doubleclick\.net|googlesyndication\.com|googletagmanager\.com|google-analytics\.com|"
    r"googleadservices\.com|adservice\.google\.[a-z.]+|analytics\.google\.com|"
    r"carbonads\.(net|com)|buysellads\.(net|com)|adnxs\.com|criteo\.(com|net)|"
    r"taboola\.com|outbrain\.com|scorecardresearch\.com|quantserve\.com|"
    r"facebook\.(net|com)|connect\.facebook\.net|hotjar\.(com|io)|mixpanel\.com|"
    r"segment\.(io|com)|amplitude\.com|intercom\.(io|com)|clarity\.ms|"
    r"sentry\.io|newrelic\.com|nr-data\.net|cloudflareinsights\.com|"
    r"optimizely\.com|plausible\.io|matomo\.cloud|statcounter\.com|"
    r"bugsnag\.com|datadoghq\.com|fullstory\.com|logrocket\.(io|com)"
    r")$",
    re.IGNORECASE,
)


# Cookie syncs and tracking pixels are identifiable by shape rather than by host,
# which is what makes this rule generalise to ad-tech we have not seen before.
NOISE_PATH_RE = re.compile(r"(/sync\b|/sync/|/cm/|/cm\b|/match\b|/match/|/pixel\b|/pixel/|/usersync)",
                           re.IGNORECASE)
NOISE_MIN_BODY_BYTES = 1024


def noise_rule(url: str, meta: dict | None = None) -> str | None:
    """Which rule, if any, marks this request as noise. None means it counts.

    Three rules, in order of confidence:
      host    - a known ad/analytics host
      path    - a cookie-sync or pixel endpoint shape
      response- a 3xx redirect, or a body under 1KB
    Cookie syncs are redirects or tiny; a real lazy image is kilobytes. Deliberately
    NOT first-party-only: owlcarousel's actual lazy images come from placehold.it,
    a third party, and a first-party rule would discard our cleanest known answer.
    """
    host = urllib.parse.urlsplit(url).netloc.split(":")[0].lower()
    if NOISE_HOST_RE.search(host):
        return "host: known ad/analytics host"
    path = urllib.parse.urlsplit(url).path
    if NOISE_PATH_RE.search(path):
        return "path: cookie-sync/pixel endpoint shape"
    if meta:
        status = meta.get("status")
        size = meta.get("size")
        if isinstance(status, int) and 300 <= status < 400:
            return f"response: {status} redirect"
        if isinstance(size, int) and size < NOISE_MIN_BODY_BYTES:
            return f"response: body {size}B under {NOISE_MIN_BODY_BYTES}B"
    return None


def is_noise_request(url: str, meta: dict | None = None) -> bool:
    return noise_rule(url, meta) is not None


BOT_MARKERS = {
    "cloudflare": ["cf-chl", "cf_chl_opt", "__cf_bm", "just a moment", "cf-browser-verification",
                   "checking your browser", "cdn-cgi/challenge-platform", "turnstile"],
    "perimeterx": ["px-captcha", "_px", "perimeterx", "px-cloud"],
    "datadome": ["datadome", "dd_cookie", "geo.captcha-delivery.com"],
    "akamai": ["akamai bot manager", "_abck"],
    "generic-captcha": ["recaptcha", "hcaptcha", "g-recaptcha", "are you a robot",
                        "verify you are human", "unusual traffic"],
}
LOGIN_MARKERS = ["sign in to continue", "log in to continue", "please sign in", "please log in",
                 "create a free account", "members only", "subscribe to read", "subscribers only",
                 "you must be logged in"]
THIN_BODY_BYTES = 2048


# --- rate limiting ----------------------------------------------------------

class HostThrottle:
    """At least MIN_HOST_INTERVAL between page loads to any one host.

    Only one page is ever in flight because screening is sequential, which also
    satisfies the one-request-per-host-at-a-time rule.
    """

    def __init__(self, min_interval: float = MIN_HOST_INTERVAL):
        self.min_interval = min_interval
        self._last: dict[str, float] = {}

    def wait(self, url: str) -> float:
        host = urllib.parse.urlsplit(url).netloc
        now = time.time()
        prev = self._last.get(host)
        slept = 0.0
        if prev is not None:
            remaining = self.min_interval - (now - prev)
            if remaining > 0:
                time.sleep(remaining)
                slept = remaining
        self._last[host] = time.time()
        return slept


# --- robots.txt -------------------------------------------------------------

class RobotsCache:
    """One robots.txt per host, cached on disk and in memory."""

    def __init__(self, cache_dir: Path, user_agent: str = USER_AGENT):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.user_agent = user_agent
        self._parsers: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._raw: dict[str, str] = {}

    def _fetch(self, scheme: str, host: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9.-]", "_", host)
        cached = self.cache_dir / f"{safe}.txt"
        if cached.exists():
            return cached.read_text(errors="replace")
        url = f"{scheme}://{host}/robots.txt"
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = resp.read(512_000).decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            body = "" if exc.code in (404, 410) else f"# HTTP {exc.code}"
        except Exception as exc:  # noqa: BLE001 - network is allowed to fail
            body = f"# fetch failed: {type(exc).__name__}"
        cached.write_text(body)
        return body

    def check(self, url: str) -> dict:
        parts = urllib.parse.urlsplit(url)
        host, scheme = parts.netloc, parts.scheme or "https"
        if host not in self._parsers:
            raw = self._fetch(scheme, host)
            self._raw[host] = raw
            parser = urllib.robotparser.RobotFileParser()
            parser.parse(raw.splitlines())
            self._parsers[host] = parser
        parser = self._parsers[host]
        path = parts.path or "/"
        allowed = parser.can_fetch(self.user_agent, url)
        # Report the rules that actually mention this path prefix, for the record.
        rules = [
            line.strip()
            for line in self._raw[host].splitlines()
            if line.strip().lower().startswith(("disallow:", "allow:", "user-agent:", "crawl-delay:"))
        ]
        delay = None
        try:
            delay = parser.crawl_delay(self.user_agent)
        except Exception:  # noqa: BLE001
            delay = None
        return {
            "host": host,
            "path": path,
            "allowed": bool(allowed),
            "crawl_delay": delay,
            "robots_lines": rules[:40],
            "robots_present": bool(self._raw[host].strip()) and not self._raw[host].startswith("#"),
        }


# --- measurement ------------------------------------------------------------

MEASURE_JS = """
() => {
  const body = document.body;
  return {
    inner_text_len: body ? body.innerText.length : 0,
    text_content_len: body ? body.textContent.length : 0,
    node_count: document.getElementsByTagName('*').length,
    inner_text: body ? body.innerText : '',
  };
}
"""

NATIVE_CONTROL_SEL = ("button, summary, [role=\'button\'], [role=\'tab\'], "
                      "input[type=\'button\'], input[type=\'submit\'], a[href]")

# A control that is a real <button> with a text label is exactly what the
# accessible half of the hypothesis looks like, so it has to be counted. Counting
# only ARIA attributes scored Slick - whose arrows are
# <button class="slick-prev" aria-label="Previous">Previous</button> - as having
# no controls at all, which would make every JS-heavy page read none-detected.
MARKUP_JS = """
(nativeSel) => {
  const n = (s) => document.querySelectorAll(s).length;
  const accName = (el) => (
    el.getAttribute('aria-label') || el.getAttribute('title') ||
    (el.tagName === 'INPUT' ? (el.value || '') : '') ||
    (el.innerText || el.textContent || '')
  ).trim();
  let named = 0, unnamed = 0;
  document.querySelectorAll(nativeSel).forEach((el) => {
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return;
    if (rect.width === 0 && rect.height === 0) return;
    if (accName(el)) named++; else unnamed++;
  });
  return {
    aria_expanded: n('[aria-expanded]'),
    role_tab: n('[role="tab"]'),
    role_tabpanel: n('[role="tabpanel"]'),
    details: n('details'),
    buttons: n('button'),
    role_button: n('[role="button"]'),
    native_controls_named: named,
    native_controls_unnamed: unnamed,
  };
}
"""

# Rendered-DOM lazy attributes, for comparison with what was actually served.
RENDERED_LAZY_JS = """
(attrs) => {
  const out = {};
  for (const a of attrs) out[a] = document.querySelectorAll('[' + a + ']').length;
  out['loading="lazy"'] = document.querySelectorAll('[loading="lazy"]').length;
  out['srcset'] = document.querySelectorAll('[srcset]').length;
  return out;
}
"""


def normalise_request(url: str) -> str:
    """origin + path, dropping the query string.

    Real pages append cache-busting and analytics parameters, so raw URL sets
    over-report "new" requests. Classification uses this key; the raw URLs are
    kept in the JSON.
    """
    parts = urllib.parse.urlsplit(url)
    path = parts.path or "/"
    # "/slick" and "/slick/" are the same page; so are "https://x.com" and
    # "https://x.com/". Sites redirect to the canonical form on load, and treating
    # that as navigating away discarded the whole measurement.
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return f"{parts.scheme}://{parts.netloc}{path}"


def new_text_chars(before: str, after: str) -> int:
    """Characters on lines present after an action but not before.

    A plain length delta misses swaps: a carousel that replaces one caption with
    another of similar length reveals real content while the delta reads ~0.
    """
    before_lines = {ln.strip() for ln in before.splitlines() if ln.strip()}
    return sum(len(ln.strip()) for ln in after.splitlines()
               if ln.strip() and ln.strip() not in before_lines)


@dataclass
class PassResult:
    name: str
    blocked_navigations: list[str] = field(default_factory=list)
    safety_skips: list[dict] = field(default_factory=list)
    form_skips: list[dict] = field(default_factory=list)
    response_meta: dict = field(default_factory=dict)
    noise_log: list[dict] = field(default_factory=list)
    clicked_controls: list[dict] = field(default_factory=list)
    allowed_controls: list[dict] = field(default_factory=list)
    measurement_valid: bool = True
    settled_url: str = ""
    # Within-load reference, taken after settle and before any interaction. Comparing
    # against this instead of against a different page load removes load-to-load
    # variance (rotating ads, timestamps, view counters) from every delta.
    pre_inner_text: str = ""
    pre_inner_text_len: int = 0
    pre_node_count: int = 0
    pre_request_count: int = 0
    pre_requests_norm: set = field(default_factory=set)
    requests_raw: list[str] = field(default_factory=list)
    requests_norm: set[str] = field(default_factory=set)
    inner_text_len: int = 0
    text_content_len: int = 0
    node_count: int = 0
    inner_text: str = ""
    markup: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    skipped_clicks: list[dict] = field(default_factory=list)
    clicked: list[str] = field(default_factory=list)
    hovered: int = 0
    status: int | None = None
    blocked_requests: list[dict] = field(default_factory=list)
    served_html: str = ""
    navigated_away: str | None = None


# --- click-handler detection (CDP) ------------------------------------------

LISTENER_SCAN_LIMIT = 500


def inert_click_handlers(page) -> dict:
    """Count div/span elements carrying a real click listener but no semantics.

    Uses DOMDebugger.getEventListeners over CDP rather than guessing from
    cursor:pointer, because the count feeds markup_quality. Falls back to an
    attribute/cursor heuristic if CDP is unavailable.
    """
    try:
        cdp = page.context.new_cdp_session(page)
    except Exception as exc:  # noqa: BLE001
        return {"method": f"unavailable ({type(exc).__name__})", "inert": 0, "scanned": 0,
                "truncated": False}
    try:
        doc = cdp.send("DOM.getDocument", {"depth": -1, "pierce": False})
        found = cdp.send("DOM.querySelectorAll",
                         {"nodeId": doc["root"]["nodeId"], "selector": "div,span"})
        node_ids = found.get("nodeIds", [])
        truncated = len(node_ids) > LISTENER_SCAN_LIMIT
        inert = 0
        semantic_but_div = 0
        for node_id in node_ids[:LISTENER_SCAN_LIMIT]:
            try:
                obj = cdp.send("DOM.resolveNode", {"nodeId": node_id})["object"]
                listeners = cdp.send("DOMDebugger.getEventListeners",
                                     {"objectId": obj["objectId"]}).get("listeners", [])
            except Exception:  # noqa: BLE001 - node may have gone away
                continue
            if not any(l.get("type") == "click" for l in listeners):
                continue
            try:
                attrs = cdp.send("DOM.getAttributes", {"nodeId": node_id}).get("attributes", [])
            except Exception:  # noqa: BLE001
                attrs = []
            pairs = dict(zip(attrs[::2], attrs[1::2]))
            has_aria = any(k.startswith("aria-") or k == "role" for k in pairs)
            has_name = bool((pairs.get("aria-label") or pairs.get("title") or "").strip())
            has_tabindex = "tabindex" in pairs
            if has_aria or has_name or has_tabindex:
                semantic_but_div += 1
            else:
                inert += 1
        return {"method": "cdp", "inert": inert, "div_with_semantics": semantic_but_div,
                "scanned": min(len(node_ids), LISTENER_SCAN_LIMIT), "truncated": truncated}
    except Exception as exc:  # noqa: BLE001
        return {"method": f"failed ({type(exc).__name__})", "inert": 0, "scanned": 0,
                "truncated": False}


# --- candidate collection ---------------------------------------------------

CLICK_SELECTOR = ", ".join([
    "button", "[role='button']", "[role='tab']", "summary",
    "[aria-expanded]", "[data-toggle]", "[data-bs-toggle]",
    "[class*='accordion']", "[class*='tab']:not([role='tabpanel'])",
    "[class*='carousel'] button", "[class*='slider'] button",
    "[class*='next']", "[class*='prev']", "[class*='arrow']",
    "[class*='toggle']", "[class*='expand']", "[class*='more']",
    "a[href^='#']", "[onclick]",
])

HOVER_SELECTOR = ", ".join([
    "nav a", "nav li", "[role='menuitem']",
    "[class*='nav'] li", "[class*='menu'] li", "[class*='dropdown']",
    "[class*='card']", "[class*='product']", "[class*='thumb']", "[class*='tile']",
])

CANDIDATE_JS = """
({sel, prefix}) => {
  // The element's OWN accessible name, or its own text when unnamed. Never the
  // enclosing block: that is what produced the "Skip to content" false positive.
  const acc = (el) => (
    el.getAttribute('aria-label') || el.getAttribute('title') ||
    el.getAttribute('alt') || (el.innerText || el.textContent || '')
  ).trim().slice(0, 200);
  const nearby = (el) => {
    const p = el.closest('li, td, th, figure, article, section, div');
    return ((p && (p.innerText || '')) || '').trim().slice(0, 200);
  };
  // Candidates come back in document order, which on a docs or shop page spends
  // the whole click budget on nav and sidebar before reaching the component under
  // test. Rank by how likely the element is to be a reveal control.
  const classOf = (el) => {
    const c = el.className;
    return ((c && c.baseVal !== undefined ? c.baseVal : c) || '').toString().toLowerCase();
  };
  const score = (el) => {
    let s = 0;
    const c = classOf(el);
    if (el.hasAttribute('aria-expanded') || el.hasAttribute('data-bs-toggle') ||
        el.hasAttribute('data-toggle')) s += 5;
    if (el.getAttribute('role') === 'tab' || el.tagName === 'SUMMARY') s += 5;
    if (/(accordion|carousel|slider|slick|owl|swiper|splide|gallery|lightbox)/.test(c)) s += 4;
    if (/(\btab\b|tab-|-tab|toggle|expand|collapse|lazy|load-?more)/.test(c)) s += 3;
    if (/(next|prev|arrow|dot|bullet|thumb)/.test(c)) s += 2;
    if (el.closest('nav, header, footer, aside, [role="navigation"], .navbar, .sidebar, .bd-sidebar, .bd-navbar')) s -= 4;
    if (el.closest('main, article, [role="main"], .bd-content')) s += 2;
    return s;
  };
  const out = [];
  const seen = new Set();
  document.querySelectorAll(sel).forEach((el, i) => {
    if (seen.has(el)) return;
    seen.add(el);
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    const visible = rect.width > 0 && rect.height > 0 &&
                    style.visibility !== 'hidden' && style.display !== 'none' &&
                    style.pointerEvents !== 'none';
    if (!visible) return;
    // Always overwrite, namespaced per scan: the hover scan and the click scan run
    // on the same page in the combined pass, and a shared id space made
    // locator('[data-screener-id=...]') resolve to the other scan's element.
    el.setAttribute('data-screener-id', prefix + '-' + i);
    const anchor = el.closest('a');
    out.push({
      id: el.getAttribute('data-screener-id'),
      tag: el.tagName.toLowerCase(),
      name: acc(el),
      nearby: nearby(el),
      in_form: !!el.closest('form'),
      is_submit: (el.getAttribute('type') || '').toLowerCase() === 'submit',
      in_method_post: !!el.closest('[method="post" i]'),
      href: anchor ? (anchor.getAttribute('href') || '') : '',
      target: anchor ? (anchor.getAttribute('target') || '') : '',
      role: el.getAttribute('role') || '',
      aria_expanded: el.getAttribute('aria-expanded'),
      native: ['BUTTON', 'SUMMARY', 'A', 'INPUT', 'SELECT'].indexOf(el.tagName) !== -1 ||
              ['button', 'tab', 'link', 'menuitem'].indexOf(el.getAttribute('role') || '') !== -1,
      has_aria: el.getAttributeNames().some((a) => a.startsWith('aria-')) || !!el.getAttribute('role'),
      has_name: !!acc(el),
      score: score(el),
    });
  });
  out.sort((a, b) => b.score - a.score);
  return out;
}
"""


def own_name_of(cand: dict) -> str:
    """A control's own accessible name, trimmed for logging."""
    return (cand.get("name") or "").strip()[:80] or "(unnamed)"


def click_is_safe(cand: dict, origin: str) -> tuple[bool, str, str]:
    """Never click transactional, destructive, or navigating controls.

    The failure modes are asymmetric: a false positive costs coverage quietly, a
    false negative clicks "Buy". So the structural rules stay broad while the text
    rule is scoped tightly to the control's own name.

    Returns (safe, reason, kind). Only `safety-denylist` trips the halt gate; the
    structural kinds fire routinely (every site with a cart link) and are logged.
    """
    # --- text rule: the control's own name only -----------------------------
    own_name = (cand.get("name") or "").strip()
    match = SAFETY_DENY.search(own_name)
    if match:
        return (False,
                f"denylist matched {match.group(0)!r} in this control's own name",
                "safety-denylist")

    # --- structural rules: independent of labelling, and of language ---------
    if cand.get("is_submit"):
        return False, "type=submit", "safety-submit"
    if cand["in_form"]:
        return False, "inside a <form>", "safety-form"
    if cand.get("in_method_post"):
        return False, 'inside an element with method="post"', "safety-method-post"

    href = (cand.get("href") or "").strip()
    if href:
        lowered = href.lower()
        if lowered.startswith(SAFETY_HREF_SCHEMES):
            return False, f"href scheme {lowered.split(':')[0]!r}", "safety-href-scheme"
        path = urllib.parse.urlsplit(urllib.parse.urljoin(origin, href)).path.lower()
        for marker in SAFETY_HREF_PATHS:
            if marker in path:
                return False, f"href path contains {marker!r}", "safety-href-path"
        if not href.startswith("#"):
            if lowered.startswith("javascript:"):
                return False, "non-navigating href scheme 'javascript'", "unsuitable"
            target = urllib.parse.urljoin(origin, href)
            if normalise_request(target) != normalise_request(origin):
                return False, "link navigates away from the page under test", "unsuitable"
    if (cand.get("target") or "").lower() == "_blank":
        return False, "opens a new tab", "unsuitable"
    return True, "", "ok"


# --- passes -----------------------------------------------------------------

def new_context(browser):
    ctx = browser.new_context(
        viewport=VIEWPORT,
        user_agent=USER_AGENT,
        accept_downloads=False,
        java_script_enabled=True,
    )
    ctx.set_default_timeout(CLICK_TIMEOUT_MS)
    ctx.set_default_navigation_timeout(NAV_TIMEOUT_MS)
    return ctx


def install_nav_guard(page, target_url: str, result: PassResult, armed: dict) -> None:
    """Cancel main-frame navigations away from the page under test.

    A click that navigates does not just end the pass: the snapshot then measures a
    different page, which is how a docs page came back with half the nodes and a
    "network" verdict. Aborting the navigation request leaves the click's own JS to
    run normally and keeps the measurement on the intended page. Armed only after
    the initial load, so redirects to the target still work.
    """
    armed.setdefault("target", normalise_request(target_url))

    def handler(route, request):
        try:
            if (armed["on"] and request.resource_type == "document"
                    and request.frame == page.main_frame
                    and normalise_request(request.url) != armed["target"]):
                result.blocked_navigations.append(request.url)
                route.abort()
                return
        except Exception:  # noqa: BLE001 - never let the guard break the page
            pass
        route.continue_()

    page.route("**/*", handler)


def load_page(ctx, url: str, throttle: HostThrottle, result: PassResult):
    """Navigate, settle to networkidle, and capture the served HTML."""
    page = ctx.new_page()
    page.on("dialog", lambda d: d.dismiss())
    ctx.on("page", lambda p: p.close())          # never let a popup survive
    page.on("request", lambda r: result.requests_raw.append(r.url))

    def record_response(request):
        """Status and body size per URL, for the response-based noise rule."""
        try:
            response = request.response()
            status = response.status if response else None
            try:
                size = request.sizes().get("responseBodySize")
            except Exception:  # noqa: BLE001 - sizes unavailable for some requests
                size = None
            result.response_meta[normalise_request(request.url)] = {
                "status": status, "size": size,
                "content_type": (response.header_value("content-type") or "").lower()
                if response else ""}
        except Exception:  # noqa: BLE001 - never let bookkeeping break a pass
            pass

    page.on("requestfinished", record_response)

    armed = {"on": False}
    install_nav_guard(page, url, result, armed)

    if BLOCKER is not None:
        # After the nav guard and before goto. Route handlers run in reverse
        # registration order and the nav guard ends every route it sees, so a blocker
        # registered before it would never run; and a blocker installed after the
        # load would leave the baseline measured under different conditions than
        # everything after it.
        import blocking
        blocking.install(
            page, BLOCKER,
            lambda u, rtype, hit: result.blocked_requests.append(
                {"url": u, "resource_type": rtype, "t": time.time(), **hit}))

    throttle.wait(url)
    response = page.goto(url, wait_until="load")
    if response is not None:
        result.status = response.status
        try:
            result.served_html = response.text()
        except Exception:  # noqa: BLE001 - body may not be retrievable
            result.served_html = ""
    try:
        page.wait_for_load_state("networkidle", timeout=NETWORKIDLE_TIMEOUT_MS)
    except Exception:  # noqa: BLE001
        result.notes.append("networkidle not reached within 30s")
    # Re-target at where the load actually settled, so a canonical redirect
    # (trailing slash, http->https, www) is the page under test rather than an
    # escape from it.
    result.settled_url = page.url
    armed["target"] = normalise_request(page.url)
    armed["on"] = True     # only guard navigations caused by our own interaction
    return page


def pre_snapshot(page, result: PassResult):
    """The within-load reference, taken after settle and before any interaction."""
    data = page.evaluate(MEASURE_JS)
    result.pre_inner_text = data["inner_text"]
    result.pre_inner_text_len = data["inner_text_len"]
    result.pre_node_count = data["node_count"]
    result.pre_request_count = len(result.requests_raw)
    result.pre_requests_norm = {normalise_request(u) for u in result.requests_raw}


def snapshot(page, result: PassResult):
    data = page.evaluate(MEASURE_JS)
    result.inner_text_len = data["inner_text_len"]
    result.text_content_len = data["text_content_len"]
    result.node_count = data["node_count"]
    result.inner_text = data["inner_text"]
    try:
        result.markup = page.evaluate(MARKUP_JS, NATIVE_CONTROL_SEL)
    except Exception:  # noqa: BLE001
        result.markup = {}
    result.requests_norm = {normalise_request(u) for u in result.requests_raw}


def do_scroll(page, result: PassResult):
    height = page.evaluate("document.body.scrollHeight")
    position = 0
    steps = 0
    while position < height and steps < 200:
        position += SCROLL_STEP_PX
        page.evaluate(f"window.scrollTo(0, {position})")
        page.wait_for_timeout(SCROLL_PAUSE_MS)
        steps += 1
        height = page.evaluate("document.body.scrollHeight")   # pages that grow
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(SCROLL_PAUSE_MS)
    result.notes.append(f"scrolled {steps} steps to {height}px")


def do_hover(page, result: PassResult, limit: int = 12):
    try:
        candidates = page.evaluate(CANDIDATE_JS, {"sel": HOVER_SELECTOR, "prefix": "hov"})
    except Exception as exc:  # noqa: BLE001
        result.notes.append(f"hover candidate scan failed: {type(exc).__name__}")
        return
    for cand in candidates[:limit]:
        try:
            page.locator(f"[data-screener-id='{cand['id']}']").first.hover(timeout=CLICK_TIMEOUT_MS)
            page.wait_for_timeout(HOVER_MS)
            result.hovered += 1
        except Exception:  # noqa: BLE001 - not hoverable, moved, or covered
            continue


def do_click(page, result: PassResult, origin: str):
    try:
        candidates = page.evaluate(CANDIDATE_JS, {"sel": CLICK_SELECTOR, "prefix": "clk"})
    except Exception as exc:  # noqa: BLE001
        result.notes.append(f"click candidate scan failed: {type(exc).__name__}")
        return
    clicks = 0
    attempts = 0
    for cand in candidates:
        if clicks >= MAX_CLICKS_PER_PAGE:
            result.notes.append(f"click cap of {MAX_CLICKS_PER_PAGE} reached; "
                                f"{len(candidates) - clicks} candidate(s) left unclicked")
            break
        # The click cap counts successes, so without an attempt cap a page whose
        # candidates all fail actionability costs CLICK_TIMEOUT_MS each, unbounded.
        if attempts >= MAX_CLICK_ATTEMPTS:
            result.notes.append(f"click attempt cap of {MAX_CLICK_ATTEMPTS} reached after "
                                f"{clicks} successful click(s)")
            break
        safe, reason, kind = click_is_safe(cand, origin)
        if not safe:
            record = {"tag": cand["tag"], "name": own_name_of(cand),
                      "nearby": cand["nearby"][:120], "reason": reason, "kind": kind}
            if kind == "safety-denylist":
                term = SAFETY_DENY.search(own_name_of(cand))
                record["term"] = term.group(0).lower() if term else "?"
            result.skipped_clicks.append(record)
            if kind.startswith("safety-") and kind != "safety-denylist":
                result.form_skips.append(record)
            if kind == "safety-denylist":
                # Only the denylist trips the halt gate. Form skips fire on almost every
                # page that has a search box, so gating on them would halt on page one
                # and tell us nothing.
                result.safety_skips.append(record)

            continue
        attempts += 1
        # Every allowed control is logged with its name, so a false negative - the
        # filter permitting something it should have refused - is as auditable as a
        # false positive. Skips alone only ever showed one direction.
        allowed_record = {"tag": cand["tag"], "name": own_name_of(cand),
                          "href": (cand.get("href") or "")[:120], "clicked": False}
        result.allowed_controls.append(allowed_record)
        try:
            page.locator(f"[data-screener-id='{cand['id']}']").first.click(timeout=CLICK_TIMEOUT_MS)
            clicks += 1
            allowed_record["clicked"] = True
            result.clicked_controls.append({
                "tag": cand["tag"], "native": bool(cand.get("native")),
                "has_aria": bool(cand.get("has_aria")), "has_name": bool(cand.get("has_name")),
                "name": cand["name"][:60],
            })
            result.clicked.append(
                f"{cand['tag']}[score {cand.get('score', 0)}]:"
                f"{cand['name'][:60] or cand['role'] or '(unnamed)'}")
            page.wait_for_timeout(600)
        except Exception as exc:  # noqa: BLE001
            result.skipped_clicks.append(
                {"tag": cand["tag"], "name": cand["name"][:80],
                 "reason": f"not clickable ({type(exc).__name__})", "kind": "unclickable"})
            continue
        if normalise_request(page.url) != normalise_request(origin):
            result.navigated_away = page.url
            result.notes.append("navigation occurred during clicking; stopped")
            break


def run_pass(browser, url: str, throttle: HostThrottle, name: str,
             scroll: bool = False, hover: bool = False, click: bool = False) -> PassResult:
    result = PassResult(name=name)
    started = time.time()
    ctx = new_context(browser)
    try:
        page = load_page(ctx, url, throttle, result)
        if name == "baseline":
            page.wait_for_timeout(int(BASELINE_SETTLE_S * 1000))
        else:
            page.wait_for_timeout(1000)
        pre_snapshot(page, result)
        if scroll:
            do_scroll(page, result)
        if hover:
            do_hover(page, result)
        if click:
            # Against the settled URL, not the requested one: core.html redirects to
            # core, and comparing with the pre-redirect URL made the first click look
            # like a navigation and cut the pass short.
            do_click(page, result, result.settled_url or url)
        if scroll or hover or click:
            page.wait_for_timeout(int(POST_ACTION_SETTLE_S * 1000))
        if normalise_request(page.url) != normalise_request(result.settled_url or url):
            result.measurement_valid = False
            result.notes.append(
                f"measurement discarded: ended on {page.url}, not the page under test")
        snapshot(page, result)
        if name == "baseline":
            result.markup["inert_handlers"] = inert_click_handlers(page)
    finally:
        ctx.close()
    result.notes.append(f"pass took {time.time() - started:.1f}s")
    print(f"      {name}: {time.time() - started:.1f}s "
          f"(requests {len(result.requests_norm)}, nodes {result.node_count}, "
          f"text {result.inner_text_len})", flush=True)
    return result


# --- classification ---------------------------------------------------------

TEXT_NOISE_FLOOR = 40        # characters; below this a "reveal" is timestamps and counters
TEXT_TOLERANCE_PCT = 0.5     # stability: text may drift by under 0.5% ...
TEXT_TOLERANCE_CHARS = 200   # ... or by under 200 characters, whichever is more permissive


def channels(baseline: PassResult, after: PassResult) -> list[dict]:
    """Every reveal channel that applies, with the magnitude of each."""
    if not after.measurement_valid:
        return [{"channel": "invalid", "magnitude": 0,
                 "detail": "pass left the page under test; measurement discarded"}]
    all_new = sorted(after.requests_norm - baseline.requests_norm)
    new_requests = [u for u in all_new if not is_noise_request(u, after.response_meta.get(u))]
    noise_new = len(all_new) - len(new_requests)
    node_delta = after.node_count - baseline.node_count
    text_delta = after.inner_text_len - baseline.inner_text_len
    revealed = new_text_chars(baseline.inner_text, after.inner_text)

    found = []
    if new_requests:
        found.append({"channel": "network", "magnitude": len(new_requests),
                      "detail": f"{len(new_requests)} content URL(s) not requested at baseline"
                                + (f" ({noise_new} ad/analytics URL(s) excluded)" if noise_new else "")})
    if node_delta > 0:
        found.append({"channel": "dom-appended", "magnitude": node_delta,
                      "detail": f"DOM grew by {node_delta} node(s)"})
    # dom-hidden requires the node count to be unchanged: content already in the
    # DOM that CSS was hiding. new_text_chars rather than a length delta, so a
    # swap (carousel caption replaced by another) still counts as revealed.
    if node_delta == 0 and revealed > TEXT_NOISE_FLOOR:
        found.append({"channel": "dom-hidden", "magnitude": revealed,
                      "detail": f"{revealed} char(s) of text became visible, "
                                f"node count unchanged (innerText delta {text_delta:+d})"})
    if not found:
        found.append({"channel": "none", "magnitude": 0,
                      "detail": "no new content requests, no node growth, no new visible text"
                                + (f" ({noise_new} ad/analytics URL(s) excluded)" if noise_new else "")})
    return found


def channels_within(result: PassResult) -> list[dict]:
    """Reveal channels measured inside one page load: pre-interaction vs post.

    This is the primary signal. The cross-load comparison against the baseline pass
    is reported alongside it, but it cannot separate an interaction effect from a
    page that simply renders differently on the next load.
    """
    if not result.measurement_valid:
        return [{"channel": "invalid", "magnitude": 0,
                 "detail": "pass left the page under test; measurement discarded"}]
    all_new = sorted(result.requests_norm - result.pre_requests_norm)
    new_requests = []
    for u in all_new:
        rule = noise_rule(u, result.response_meta.get(u))
        if rule:
            result.noise_log.append({"url": u, "rule": rule})
        else:
            new_requests.append(u)
    noise_new = len(all_new) - len(new_requests)
    node_delta = result.node_count - result.pre_node_count
    text_delta = result.inner_text_len - result.pre_inner_text_len
    revealed = new_text_chars(result.pre_inner_text, result.inner_text)

    found = []
    if new_requests:
        found.append({"channel": "network", "magnitude": len(new_requests),
                      "detail": f"{len(new_requests)} content URL(s) requested only after interaction"
                                + (f" ({noise_new} ad/analytics excluded)" if noise_new else "")})
    if node_delta > 0:
        found.append({"channel": "dom-appended", "magnitude": node_delta,
                      "detail": f"DOM grew by {node_delta} node(s) during interaction"})
    if node_delta == 0 and revealed > TEXT_NOISE_FLOOR:
        found.append({"channel": "dom-hidden", "magnitude": revealed,
                      "detail": f"{revealed} char(s) became visible with node count unchanged "
                                f"(innerText delta {text_delta:+d})"})
    if not found:
        found.append({"channel": "none", "magnitude": 0,
                      "detail": "interaction changed nothing measurable"})
    return found


def mechanisms(baseline: PassResult, per_mech: dict[str, PassResult]) -> dict:
    """Attribute each delta to the single-mechanism pass that produced it."""
    out = {}
    for mech, result in per_mech.items():
        found = channels_within(result)
        names = [c["channel"] for c in found]
        if names and names[0] not in ("none", "invalid"):
            out[mech] = {
                "channels": names,
                "magnitudes": {c["channel"]: c["magnitude"] for c in found},
                "new_requests": len([u for u in (result.requests_norm - result.pre_requests_norm)
                                     if not is_noise_request(u, result.response_meta.get(u))]),
                "node_delta": result.node_count - result.pre_node_count,
                "new_text_chars": new_text_chars(result.pre_inner_text, result.inner_text),
            }
    return out


def markup_quality(baseline: PassResult, click_pass: PassResult | None = None) -> dict:
    m = baseline.markup or {}
    inert_info = m.get("inert_handlers", {}) or {}
    semantic = (m.get("aria_expanded", 0) + m.get("role_tab", 0)
                + m.get("role_tabpanel", 0) + m.get("details", 0)
                + m.get("native_controls_named", 0))
    inert = inert_info.get("inert", 0)
    if semantic and inert:
        verdict = "mixed"
    elif semantic:
        verdict = "semantic"
    elif inert:
        verdict = "inert"
    else:
        verdict = "none-detected"
    clicked = click_pass.clicked_controls if click_pass else []
    breakdown = {
        "clicked_total": len(clicked),
        "native_with_name": sum(1 for c in clicked if c["native"] and c["has_name"]),
        "native_without_name": sum(1 for c in clicked if c["native"] and not c["has_name"]),
        "nonnative_with_aria_or_name": sum(
            1 for c in clicked if not c["native"] and (c["has_aria"] or c["has_name"])),
        "nonnative_inert": sum(
            1 for c in clicked if not c["native"] and not c["has_aria"] and not c["has_name"]),
    }
    # Three tiers, not a binary. A <div role="button" aria-label="..."> is fully
    # legible to the accessibility tree even though it is not a native control, and
    # that middle tier turns out to be common.
    tiers = {
        "native_named": breakdown["native_with_name"],
        "div_with_name": breakdown["nonnative_with_aria_or_name"],
        "inert": breakdown["nonnative_inert"],
        "native_unnamed": breakdown["native_without_name"],
    }
    ranked = sorted(("native_named", "div_with_name", "inert"),
                    key=lambda t: -tiers[t])
    dominant = ranked[0] if tiers[ranked[0]] else None
    return {
        "verdict": verdict,
        "tiers": tiers,
        "dominant_tier": dominant,
        "aria_expanded": m.get("aria_expanded", 0),
        "native_controls_named": m.get("native_controls_named", 0),
        "native_controls_unnamed": m.get("native_controls_unnamed", 0),
        "controls_actually_clicked": breakdown,
        "role_tab": m.get("role_tab", 0),
        "role_tabpanel": m.get("role_tabpanel", 0),
        "details": m.get("details", 0),
        "semantic_total": semantic,
        "inert_div_span_click_handlers": inert,
        "div_span_with_click_and_some_semantics": inert_info.get("div_with_semantics", 0),
        "listener_scan": {k: inert_info.get(k) for k in ("method", "scanned", "truncated")},
    }


def lazy_attributes(served_html: str, rendered: dict) -> dict:
    counts = {}
    for attr in LAZY_ATTRS:
        n = len(re.findall(rf"\b{re.escape(attr)}\s*=", served_html, re.IGNORECASE))
        if n:
            counts[attr] = n
    n_loading = len(re.findall(r"""loading\s*=\s*["']lazy["']""", served_html, re.IGNORECASE))
    if n_loading:
        counts['loading="lazy"'] = n_loading
    n_srcset = len(re.findall(r"\bsrcset\s*=", served_html, re.IGNORECASE))
    if n_srcset:
        counts["srcset"] = n_srcset
    # Any other data-* attribute whose value looks like an image URL.
    other: dict[str, int] = {}
    for name, value in DATA_ATTR_RE.findall(served_html):
        lower = name.lower()
        if lower in counts or lower in LAZY_ATTRS:
            continue
        if IMAGE_URL_RE.search(value) or "/image" in value.lower():
            other[lower] = other.get(lower, 0) + 1
    counts.update(other)
    return {
        "served_html": counts,
        "rendered_dom": {k: v for k, v in (rendered or {}).items() if v},
        "attribute_names_found": sorted(counts),
        "autofetch_would_harvest": sorted(
            a for a in counts if a in ("data-src", "srcset")),
        "autofetch_would_miss": sorted(
            a for a in counts if a not in ("data-src", "srcset", 'loading="lazy"')),
    }


IMAGE_PATH_RE = re.compile(r"\.(?:jpe?g|png|gif|webp|avif|svg|bmp|ico)$", re.IGNORECASE)


def composition(page_url: str, urls: list[str], meta: dict) -> dict:
    """First-party / third-party / image split of a reveal.

    A magnitude is not interpretable without this: "network (190)" was 123 requests
    from an embedded newsletter widget, and "network (8)" was eight genuine lazy
    images from a third-party image host.
    """
    site = urllib.parse.urlsplit(page_url).netloc.lower()
    first = third = images = 0
    third_hosts: dict[str, int] = {}
    for url in urls:
        parts = urllib.parse.urlsplit(url)
        host = parts.netloc.lower()
        if host == site:
            first += 1
        else:
            third += 1
            third_hosts[host] = third_hosts.get(host, 0) + 1
        content_type = (meta.get(url) or {}).get("content_type", "") or ""
        if IMAGE_PATH_RE.search(parts.path) or content_type.startswith("image/"):
            images += 1
    dominant = max(third_hosts.items(), key=lambda kv: kv[1])[0] if third_hosts else None
    return {
        "first_party": first,
        "third_party": third,
        "images": images,
        "dominant_third_party_host": dominant,
        "dominant_third_party_count": third_hosts.get(dominant, 0) if dominant else 0,
    }


def access_report(baseline: PassResult, robots: dict, url: str) -> dict:
    html = baseline.served_html or ""
    lowered = html.lower()
    challenges = sorted({vendor for vendor, markers in BOT_MARKERS.items()
                         if any(m in lowered for m in markers)})
    thin = len(html.encode("utf-8", "ignore")) < THIN_BODY_BYTES
    login = [m for m in LOGIN_MARKERS if m in lowered]
    has_password_field = "type=\"password\"" in lowered or "type='password'" in lowered
    return {
        "http_status": baseline.status,
        "bot_challenge": bool(challenges) or (thin and baseline.inner_text_len < 200),
        "challenge_vendors": challenges,
        "thin_body": thin,
        "served_bytes": len(html.encode("utf-8", "ignore")),
        "login_wall": bool(login) or has_password_field,
        "login_markers": login + (["password field present"] if has_password_field else []),
        "robots": robots,
    }


def stability_report(first: PassResult, second: PassResult) -> dict:
    # Ads and analytics change every load by design; comparing them would mark every
    # commercial page unstable for a reason that has nothing to do with content.
    first_set = {u for u in first.requests_norm
                 if not is_noise_request(u, first.response_meta.get(u))}
    second_set = {u for u in second.requests_norm
                  if not is_noise_request(u, second.response_meta.get(u))}
    same_requests = first_set == second_set
    same_text = first.inner_text_len == second.inner_text_len

    # Content requests are the primary criterion; text is secondary and tolerant.
    # A page passes the text test if it is under 0.5% OR under 200 characters
    # absolute, whichever is more permissive - a percentage alone drifts with page
    # size. Raw deltas are always kept, never just the boolean.
    delta = second.inner_text_len - first.inner_text_len
    abs_delta = abs(delta)
    base = max(first.inner_text_len, 1)
    pct = abs_delta / base * 100
    text_within_tolerance = (pct < TEXT_TOLERANCE_PCT) or (abs_delta < TEXT_TOLERANCE_CHARS)
    only_first = sorted(first_set - second_set)[:10]
    only_second = sorted(second_set - first_set)[:10]
    return {
        "stable": bool(same_requests and text_within_tolerance),
        "same_request_set": same_requests,
        "same_inner_text_len": same_text,
        "text_within_tolerance": bool(text_within_tolerance),
        "text_delta_pct": round(pct, 4),
        "text_delta_abs": abs_delta,
        "tolerance": {"pct": TEXT_TOLERANCE_PCT, "chars": TEXT_TOLERANCE_CHARS,
                      "rule": "content request sets must match; text passes if under "
                              "either threshold"},
        "inner_text_len_first": first.inner_text_len,
        "inner_text_len_second": second.inner_text_len,
        "inner_text_len_delta": second.inner_text_len - first.inner_text_len,
        "content_requests_differing": len(only_first) + len(only_second),
        "requests_only_in_first": only_first,
        "requests_only_in_second": only_second,
        "usable": bool(same_requests and same_text),
    }


# --- per-URL driver ---------------------------------------------------------

# An explicit per-page override with a mandatory written reason. This exists so that
# a single page can be rescued in the open, instead of a global filter being tuned
# until that page passes - which would silently change the verdict for every future
# page screened.
STABILITY_OVERRIDES: dict[str, str] = {
    "http://kenwheeler.github.io/slick":
        "all 20 differing requests are ad-tech cookie syncs (360yield, pubmatic, "
        "casalemedia, adroll, dotomi, lijit, 3lift, contextweb, rfihub, adgrx, "
        "bfmio, inmobi, sonobi, id5-sync); zero content hosts differ; text delta "
        "+24 chars (0.17%), within tolerance",
}


def stability_override_for(url: str) -> str | None:
    reason = STABILITY_OVERRIDES.get(url)
    if reason is not None and not reason.strip():
        raise ValueError(f"stability override for {url} has an empty reason; "
                         "a written justification is mandatory")
    return reason


SEED_URLS = [
    "https://getbootstrap.com/docs/5.3/components/accordion/",
    "https://getbootstrap.com/docs/5.3/components/carousel/",
    "https://getbootstrap.com/docs/5.3/components/navs-tabs/",
    "http://kenwheeler.github.io/slick",
    "https://owlcarousel2.github.io/OwlCarousel2/demos/lazyLoad.html",
    # An individual demo, not /demos: that index embeds a Substack widget, which is
    # why it read unstable and why 123 of its 190 requests came from substackcdn.
    "https://swiperjs.com/demos/370-lazy-load-images/core.html",
    "https://splidejs.com",
]


GROUP_HEADING_RE = re.compile(r"^##\s*([A-Z])\.\s*(.+?)\s*$")


def parse_urls_markdown(path: Path) -> list[tuple[str, str, str]]:
    """Extract (url, group_letter, group_title) from a grouped markdown list.

    URLs live in fenced blocks under `## <Letter>. <title>` headings. The group is
    carried through into results.json so reveal_channel distributions can be compared
    against the predictions the grouping encodes.
    """
    group = title = ""
    in_fence = False
    out: list[tuple[str, str, str]] = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        heading = GROUP_HEADING_RE.match(line)
        if heading and not in_fence:
            group, title = heading.group(1), heading.group(2)
            continue
        if in_fence and stripped.startswith(("http://", "https://")):
            out.append((stripped, group, title))
    return out


def interleave_by_host(items: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """Round-robin across hosts so same-host URLs are never adjacent.

    The per-host limiter would serialise them anyway; spreading them means the wait is
    absorbed by work on other hosts instead of being spent idle.
    """
    from collections import defaultdict, deque
    buckets: dict[str, deque] = defaultdict(deque)
    for item in items:
        buckets[urllib.parse.urlsplit(item[0]).netloc.lower()].append(item)
    order: list[tuple[str, str, str]] = []
    while buckets:
        for host in list(buckets):
            order.append(buckets[host].popleft())
            if not buckets[host]:
                del buckets[host]
    return order


def screen_url(browser, url: str, throttle: HostThrottle, robots_cache: RobotsCache,
               skip_stability: bool = False) -> dict:
    started = time.time()
    robots = robots_cache.check(url)
    if not robots["allowed"]:
        # Disallowed paths are recorded and not screened. No page load at all.
        return {
            "url": url,
            "screened": False,
            "skip_reason": "robots.txt disallows this path for our User-Agent",
            "access": {"http_status": None, "bot_challenge": None, "login_wall": None,
                       "robots": robots},
            "reveal_channel": [], "mechanisms_present": {}, "markup_quality": {},
            "lazy_attributes": {}, "stability": {},
        }

    print(f"  [{url}] baseline ...", flush=True)
    baseline = run_pass(browser, url, throttle, "baseline")

    print(f"  [{url}] scroll / hover / click (attributed, separate loads) ...", flush=True)
    per_mech = {
        "scroll": run_pass(browser, url, throttle, "scroll", scroll=True),
        "hover": run_pass(browser, url, throttle, "hover", hover=True),
        "click": run_pass(browser, url, throttle, "click", click=True),
    }

    print(f"  [{url}] combined interacted pass ...", flush=True)
    combined = run_pass(browser, url, throttle, "interacted", scroll=True, hover=True, click=True)

    stability = {}
    if not skip_stability:
        elapsed = time.time() - started
        if elapsed < STABILITY_GAP_S:
            wait = STABILITY_GAP_S - elapsed
            print(f"  [{url}] waiting {wait:.0f}s for the 60s stability gap ...", flush=True)
            time.sleep(wait)
        print(f"  [{url}] stability re-load ...", flush=True)
        second = run_pass(browser, url, throttle, "baseline-2")
        stability = stability_report(baseline, second)
        override = stability_override_for(url)
        if override:
            stability["measured_stable"] = stability["stable"]
            stability["stable"] = True
            stability["usable"] = True
            stability["override_reason"] = override

    rendered_lazy = {}
    try:
        ctx = new_context(browser)
        tmp = PassResult(name="lazy-scan")
        page = load_page(ctx, url, throttle, tmp)
        rendered_lazy = page.evaluate(RENDERED_LAZY_JS, LAZY_ATTRS)
        ctx.close()
    except Exception as exc:  # noqa: BLE001
        rendered_lazy = {"error": type(exc).__name__}

    found = channels_within(combined)
    cross = channels(baseline, combined)
    within_new = sorted(
        u for u in (combined.requests_norm - combined.pre_requests_norm)
        if not is_noise_request(u, combined.response_meta.get(u)))
    return {
        "url": url,
        "screened": True,
        "reveal_channel": found,
        "reveal_channel_cross_load": {
            "channels": [c["channel"] for c in cross],
            "detail": [c["detail"] for c in cross],
            "note": "baseline load vs interacted load; includes load-to-load variance",
        },
        "reveal_channel_names": [c["channel"] for c in found],
        "composition": composition(url, within_new, combined.response_meta),
        "noise_filtered": combined.noise_log[:100],
        "noise_filtered_total": len(combined.noise_log),
        "mechanisms_present": mechanisms(baseline, per_mech),
        "markup_quality": markup_quality(baseline, per_mech.get("click")),
        "lazy_attributes": lazy_attributes(baseline.served_html, rendered_lazy),
        "stability": stability,
        "access": access_report(baseline, robots, url),
        "measurements": {
            name: {
                "requests": len(result.requests_norm),
                "inner_text_len": result.inner_text_len,
                "text_content_len": result.text_content_len,
                "hidden_text_chars": result.text_content_len - result.inner_text_len,
                "node_count": result.node_count,
                "notes": result.notes,
            }
            for name, result in [("baseline", baseline), ("interacted", combined),
                                 *[(k, v) for k, v in per_mech.items()]]
        },
        "interaction_log": {
            "clicked": combined.clicked,
            "skipped_clicks": combined.skipped_clicks,
            "safety_skips": combined.safety_skips,
            "allowed_controls": combined.allowed_controls,
            "form_skips": combined.form_skips,
            "hovered_elements": combined.hovered,
            "navigated_away": combined.navigated_away,
            "click_cap": MAX_CLICKS_PER_PAGE,
        },
        "new_requests_content": sorted(
            u for u in (combined.requests_norm - baseline.requests_norm)
            if not is_noise_request(u, combined.response_meta.get(u)))[:500],
        "new_requests_noise_excluded": sorted(
            u for u in (combined.requests_norm - baseline.requests_norm)
            if is_noise_request(u, combined.response_meta.get(u)))[:25],
        "new_requests_within_load": sorted(
            u for u in (combined.requests_norm - combined.pre_requests_norm)
            if not is_noise_request(u, combined.response_meta.get(u)))[:500],
        "blocked_ads": {
            "total": sum(len(p.blocked_requests) for p in
                         [baseline, combined, *per_mech.values()]),
            "baseline": [dict(b) for b in baseline.blocked_requests[:100]],
            "interacted": [dict(b) for b in combined.blocked_requests[:100]],
        } if BLOCKER is not None else {},
        "blocked_navigations": combined.blocked_navigations,
        "measurement_valid": combined.measurement_valid,
        "elapsed_s": round(time.time() - started, 1),
    }


# --- output -----------------------------------------------------------------

CHANNEL_ORDER = {"network": 0, "dom-appended": 1, "dom-hidden": 2, "none": 3}


def sort_key(row: dict):
    names = row.get("reveal_channel_names") or ["none"]
    best = min((CHANNEL_ORDER.get(n, 9) for n in names), default=9)
    if not row.get("screened"):
        return (10, row["url"])
    return (best, row["url"])


def write_markdown(rows: list[dict], path: Path) -> None:
    lines = [
        "# Page screening results",
        "",
        f"{len(rows)} URL(s). Sorted so network-channel pages come first: those are the ones",
        "with something for a resource-count metric to measure.",
        "",
        "| grp | url | reveal_channel(s) | composition | mechanisms | markup_quality | lazy_attributes | stable | access |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in sorted(rows, key=sort_key):
        url = row["url"]
        if not row.get("screened"):
            lines.append(f"| {row.get('group', '')} | {url} | — | — | — | — | — | — | "
                         f"**skipped**: {row['skip_reason']} |")
            continue
        chans = ", ".join(
            f"{c['channel']} ({c['magnitude']})" if c["channel"] != "none" else "none"
            for c in row["reveal_channel"])
        mechs = ", ".join(row["mechanisms_present"]) or "—"
        mq = row["markup_quality"]
        cb = mq.get("controls_actually_clicked", {})
        # All four categories: omitting nonnative-with-a-name printed 0/0/0 for a page
        # where a named <div> bullet was in fact clicked.
        mq_text = (f"{mq['verdict']} (sem {mq['semantic_total']}, "
                   f"inert {mq['inert_div_span_click_handlers']}; clicked "
                   f"{cb.get('clicked_total', 0)}: "
                   f"{cb.get('native_with_name', 0)} named / "
                   f"{cb.get('native_without_name', 0)} unnamed / "
                   f"{cb.get('nonnative_with_aria_or_name', 0)} div-named / "
                   f"{cb.get('nonnative_inert', 0)} inert)")
        lazy = row["lazy_attributes"]["attribute_names_found"]
        lazy_text = ", ".join(lazy) if lazy else "none"
        stab = row.get("stability") or {}
        if not stab:
            stable_text = "n/a"
        elif stab.get("stable"):
            stable_text = "yes"
        else:
            why = []
            if not stab.get("same_request_set"):
                why.append(f"{stab.get('content_requests_differing', 0)} content req differ")
            if not stab.get("text_within_tolerance"):
                why.append(f"text {stab.get('inner_text_len_delta', 0):+d} "
                           f"({stab.get('text_delta_pct', 0)}%)")
            stable_text = "**NO** (" + ", ".join(why) + ")"
        if stab and stab.get("stable") and stab.get("inner_text_len_delta"):
            stable_text = f"yes (text {stab['inner_text_len_delta']:+d})"
        if stab and stab.get("override_reason"):
            stable_text = f"stable (override: {stab['override_reason'][:90]}...)"
        acc = row["access"]
        bits = [str(acc["http_status"])]
        if acc["bot_challenge"]:
            bits.append("bot-challenge" + (f" ({','.join(acc['challenge_vendors'])})"
                                           if acc["challenge_vendors"] else ""))
        if acc["login_wall"]:
            bits.append("login-wall")
        bits.append("robots ok" if acc["robots"]["allowed"] else "**robots disallow**")
        comp = row.get("composition") or {}
        comp_text = (f"{comp.get('first_party', 0)}fp / {comp.get('third_party', 0)}tp / "
                     f"{comp.get('images', 0)}img"
                     + (f" ({comp['dominant_third_party_host']} x{comp['dominant_third_party_count']})"
                        if comp.get("dominant_third_party_host") else ""))
        lines.append(f"| {row.get('group', '')} | {url} | {chans} | {comp_text} | {mechs} | "
                     f"{mq_text} | {lazy_text} | {stable_text} | {', '.join(bits)} |")
    lines += ["", "## Notes", ""]
    for row in sorted(rows, key=sort_key):
        if not row.get("screened"):
            continue
        log = row["interaction_log"]
        lines.append(f"- **{row['url']}** — clicked {len(log['clicked'])}, "
                     f"skipped {len(log['skipped_clicks'])}, hovered {log['hovered_elements']}"
                     + (f", navigated away to {log['navigated_away']}" if log["navigated_away"] else "")
                     + f", {row.get('noise_filtered_total', 0)} request(s) filtered as noise"
                     + f", {row['elapsed_s']}s")
    path.write_text("\n".join(lines) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--urls", help="file with one URL per line; default is the seed list")
    ap.add_argument("--urls-md", help="grouped markdown list; URLs in fenced blocks under "
                                      "'## <Letter>. <title>' headings, group carried into results")
    ap.add_argument("--url", action="append", default=[], help="screen this URL (repeatable)")
    ap.add_argument("--out", default=str(PKG_ROOT / "out"))
    ap.add_argument("--headed", action="store_true", help="run Chromium headed")
    ap.add_argument("--skip-done", action="store_true",
                    help="skip URLs already screened successfully in the output results.json")
    ap.add_argument("--first-group", action="append", default=[],
                    help="screen these group letters before the rest (repeatable)")
    ap.add_argument("--merge", action="store_true",
                    help="merge into an existing results.json instead of replacing it; "
                         "re-screened URLs are updated in place, new ones appended")
    ap.add_argument("--acknowledge-skips", action="store_true",
                    help="continue past the safety-skip halt gate (read the skips first)")
    ap.add_argument("--block-ads", action="store_true",
                    help="block ads and trackers with EasyList + EasyPrivacy (adblock-rust), "
                         "as the exploration oracle does. OFF by default: every result "
                         "recorded so far was measured without it, and turning it on makes "
                         "a re-screened page incomparable with those")
    ap.add_argument("--skip-stability", action="store_true",
                    help="skip the 60s-apart re-load (faster; leaves stability unreported)")
    args = ap.parse_args()

    groups: dict[str, tuple[str, str]] = {}
    if args.urls_md:
        parsed_all = parse_urls_markdown(Path(args.urls_md))
        if args.first_group:
            wanted = {g.upper() for g in args.first_group}
            head = [x for x in parsed_all if x[1] in wanted]
            tail = [x for x in parsed_all if x[1] not in wanted]
            parsed = interleave_by_host(head) + interleave_by_host(tail)
            print(f"prioritising group(s) {', '.join(sorted(wanted))}: "
                  f"{len(head)} URL(s) first", flush=True)
        else:
            parsed = interleave_by_host(parsed_all)
        urls = [u for u, _, _ in parsed]
        groups = {u: (g, t) for u, g, t in parsed}
        by_group: dict[str, int] = {}
        for _, g, _ in parsed:
            by_group[g] = by_group.get(g, 0) + 1
        print(f"parsed {len(urls)} URL(s) from {args.urls_md}: "
              + ", ".join(f"{g}={n}" for g, n in sorted(by_group.items())), flush=True)
        print(f"interleaved across {len({urllib.parse.urlsplit(u).netloc for u in urls})} host(s)",
              flush=True)
    elif args.url:
        urls = args.url
    elif args.urls:
        urls = [ln.strip() for ln in Path(args.urls).read_text().splitlines()
                if ln.strip() and not ln.strip().startswith("#")]
    else:
        urls = SEED_URLS

    if CONTACT_PLACEHOLDER in CONTACT_URL or not CONTACT_URL.startswith("http"):
        # Hard refusal, not a warning: this is about to point at commerce sites, and a
        # banner is too easy to ignore. Every host we touch must be able to find out
        # who is requesting and why.
        print("REFUSING TO RUN: CONTACT_URL is not set to a real page.")
        print(f"  current value: {CONTACT_URL!r}")
        print("  Set it in tools/screen.py to a URL a site operator can load.")
        return 2

    if args.block_ads:
        global BLOCKER
        import blocking
        try:
            BLOCKER = blocking.Blocker()
        except blocking.BraveListsMissing as exc:
            print(exc)
            return 2
        print("ad/tracker blocking ON (Brave's own lists): "
              + ", ".join(f"{n} {i['version']} ({i['rules']} rules)"
                          for n, i in BLOCKER.list_info.items()), flush=True)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.skip_done:
        done_path = out_dir / "results.json"
        if done_path.exists():
            done = {r["url"] for r in json.loads(done_path.read_text()) if r.get("screened")}
            before = len(urls)
            urls = [u for u in urls if u not in done]
            print(f"--skip-done: {before - len(urls)} already screened, {len(urls)} remaining",
                  flush=True)
    throttle = HostThrottle()
    robots_cache = RobotsCache(PKG_ROOT / "cache" / "robots")

    from playwright.sync_api import sync_playwright

    rows = []
    seen_gate_keys: set[tuple[str, str]] = set()
    gate_log: list[dict] = []
    existing_path = out_dir / "results.json"
    if args.merge and existing_path.exists():
        rows = json.loads(existing_path.read_text())
        print(f"merging into {len(rows)} existing row(s) in {existing_path}", flush=True)

    def upsert(new_row: dict) -> None:
        """Replace the row for this URL if present, else append. Keeps one table
        consistent when a single failed URL is re-screened."""
        for i, existing in enumerate(rows):
            if existing.get("url") == new_row.get("url"):
                rows[i] = new_row
                return
        rows.append(new_row)

    with sync_playwright() as pw:
        for i, url in enumerate(urls, 1):
            print(f"[{i}/{len(urls)}] {url}", flush=True)
            row = None
            for attempt in (1, 2):
                # A fresh browser per URL: Chromium's network service can die
                # mid-run, after which every request in that instance fails with
                # ERR_INTERNET_DISCONNECTED regardless of host. Relaunching bounds
                # the blast radius to one URL instead of the rest of the list.
                browser = pw.chromium.launch(headless=not args.headed)
                try:
                    row = screen_url(browser, url, throttle, robots_cache,
                                     skip_stability=args.skip_stability)
                    break
                except Exception as exc:  # noqa: BLE001 - one bad page must not end the run
                    message = f"{type(exc).__name__}: {exc}"
                    transient = any(marker in message for marker in (
                        "ERR_INTERNET_DISCONNECTED", "ERR_NETWORK_CHANGED",
                        "ERR_CONNECTION_RESET", "ERR_CONNECTION_CLOSED",
                        "ERR_ADDRESS_INVALID", "ERR_NAME_NOT_RESOLVED",
                        "ERR_CONNECTION_TIMED_OUT", "ERR_EMPTY_RESPONSE",
                        "Target closed", "browser has been closed"))
                    print(f"  ERROR (attempt {attempt}): {message.splitlines()[0]}", flush=True)
                    if transient and attempt == 1:
                        # Back off before retrying: an immediate retry failed both
                        # times when the outage lasted longer than the relaunch.
                        print(f"  transient network failure; waiting {RETRY_BACKOFF_S:.0f}s "
                              "for connectivity, then relaunching browser", flush=True)
                        deadline = time.time() + RETRY_BACKOFF_S
                        while time.time() < deadline:
                            time.sleep(5)
                            try:
                                urllib.request.urlopen(
                                    urllib.request.Request(
                                        url, headers={"User-Agent": USER_AGENT}),
                                    timeout=10).read(1)
                                print("  connectivity restored; retrying", flush=True)
                                break
                            except Exception:  # noqa: BLE001 - still down, keep waiting
                                continue
                        continue
                    row = {
                        "url": url, "screened": False,
                        "skip_reason": f"screening error: {message.splitlines()[0]}",
                        "access": {"http_status": None, "robots": robots_cache.check(url)},
                        "reveal_channel": [], "reveal_channel_names": [],
                        "mechanisms_present": {}, "markup_quality": {},
                        "lazy_attributes": {}, "stability": {},
                    }
                    break   # without this, a failed attempt fell through and re-screened
                finally:
                    try:
                        browser.close()
                    except Exception:  # noqa: BLE001
                        pass
            if url in groups:
                row["group"], row["group_title"] = groups[url]
            upsert(row)
            (out_dir / "results.json").write_text(json.dumps(rows, indent=2, default=str))
            write_markdown(rows, out_dir / "results.md")

            # Manual gate: the denylist has never fired on a real page, so the first
            # time it does, stop and let a human read what was refused before any
            # further pages are touched.
            skips = (row.get("interaction_log") or {}).get("safety_skips") or []
            # Keyed on (term, group), not term alone: a "submit" on a docs page and a
            # "submit" on a commerce page are different situations, and commerce is
            # where a miss matters. Bounded by 9 terms x 9 groups.
            group_letter = row.get("group", "?")
            new_keys = sorted({(sk.get("term", "?"), group_letter) for sk in skips}
                              - seen_gate_keys)
            seen_gate_keys.update(new_keys)
            for term, grp in new_keys:
                gate_log.append({"term": term, "group": grp, "url": url,
                                 "skips": [sk for sk in skips if sk.get("term") == term]})
            if new_keys and not args.acknowledge_skips:
                print("\n" + "=" * 72, flush=True)
                print("HALTED: the click safety filter vetoed elements on this page.", flush=True)
                print(f"  new (term, group) combination(s): "
                      f"{', '.join(f'{t}@{g}' for t, g in new_keys)}", flush=True)
                print(f"  {url}", flush=True)
                print("=" * 72, flush=True)
                for skip in skips:
                    print(f"  [{skip['kind']}] <{skip['tag']}> {skip['name']!r}", flush=True)
                    print(f"      reason: {skip['reason']}", flush=True)
                    if skip.get("nearby"):
                        print(f"      nearby: {skip['nearby']!r}", flush=True)
                print("=" * 72, flush=True)
                print(f"{len(rows)} row(s) in the table. Results so far are written.",
                      flush=True)
                print("Read the skips above. To continue past this gate, re-run with "
                      "--acknowledge-skips.", flush=True)
                return 3

    if gate_log:
        (out_dir / "denylist-vetoes.json").write_text(json.dumps(gate_log, indent=2))
        print(f"\n{len(gate_log)} new (term, group) denylist combination(s) recorded in "
              f"{out_dir/'denylist-vetoes.json'}")

    print(f"\nwrote {out_dir/'results.json'} and {out_dir/'results.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
