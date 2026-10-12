#!/usr/bin/env python3
"""Exploration oracle: find as many interaction-gated resources on a page as we can.

This is an evaluation instrument, never part of a production crawl. It exists to
build the denominator for behavior-config recall on real pages, where the true set
of interaction-gated resources is unknown:

    baseline  = content requests with no interaction
    pool      = (explorer U every behavior config) - baseline - noise
    recall(X) = |X's new requests & pool| / |pool|

so it is deliberately thorough rather than fast. It is also deliberately
INDEPENDENT of Browsertrix: nothing here is taken from browsertrix-behaviors, because
an oracle that shares the tested system's blind spots cannot measure them.

What it does reuse is the screener (screen.py): page loading, response bookkeeping,
the noise rules, the click denylist and structural safety rules, the navigation
guard, robots.txt and per-host rate limiting. None of the screener's behavior or
output is changed; this file only imports from it.

Per page, in one browser session (and the whole thing twice, for stability):

  1. baseline  load, networkidle + 10s, record content requests
  2. scroll    the main page, then every scrollable element and same-origin iframe,
               in small steps until the height stops growing or the budget is hit
  3. hover     every visible native control or element with a hover listener (CDP),
               with real pointer input so CSS :hover applies
  4. click     every visible safe candidate, repeatedly, until a click produces no
               novel content (or 10 clicks). Candidates come from three sources:
               native controls, CDP listener marks, and cursor:pointer - the last
               because a framework that delegates every handler to the document root
               (React and friends) leaves DOMDebugger nothing to find on the element
               that is actually clickable
  5. repeat    discovery after each pass; clicking reveals more controls

Every request is attributed to the action that first caused it.
"""

import argparse
import json
import re
import sys
import time
import urllib.parse
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import blocking  # noqa: E402
import screen  # noqa: E402  - for screen.BLOCKER, the load-time blocker hook
from screen import (  # noqa: E402
    CLICK_TIMEOUT_MS, CONTACT_PLACEHOLDER, CONTACT_URL, HOVER_MS, NAV_TIMEOUT_MS,
    NETWORKIDLE_TIMEOUT_MS, SAFETY_DENY, SCROLL_PAUSE_MS, SCROLL_STEP_PX, USER_AGENT,
    VIEWPORT, HostThrottle, PassResult, RobotsCache, click_is_safe, load_page,
    noise_rule, normalise_request, own_name_of, parse_urls_markdown,
)

PKG_ROOT = Path(__file__).resolve().parent.parent

# --- exploration parameters -------------------------------------------------

PERIODIC_WINDOW_S = 30.0          # idle observation before baseline; see Explorer.run
BASELINE_SETTLE_S = 10.0          # after networkidle, as the screener's baseline does
SCROLL_BUDGET_VIEWPORTS = 50      # per scroll container; infinite scroll has no end
SCROLL_NO_GROWTH_STEPS = 3        # at the end, stop once height is flat this many steps
MAX_CLICKS_PER_ELEMENT = 10
MAX_ACTIONS = 300                 # hovers + clicks + scroll sweeps (see ACTION_ACCOUNTING)
MAX_SECONDS = 600.0               # wall clock per exploration run
MAX_DISCOVERY_PASSES = 5
LISTENER_SCAN_LIMIT = 20000       # elements whose listeners are read per discovery pass
SETTLE_MIN_MS = 600               # after a click, before looking for novelty
SETTLE_QUIET_MS = 500             # ... then until no request has been in flight this long
SETTLE_MAX_MS = 5000
RESTORE_LOSS_RATIO = 2.0          # see click_element: undoing a collapsing click
TEXT_FRAGMENT_LOG_LIMIT = 2000
# Per-phase budgets: shares of --max-actions and --max-seconds, so no phase can spend
# another's. Phase time is the wall time spent inside that phase's actions; the idle
# window, baseline and discovery are not charged. When exploration is otherwise done
# and click is capped, the scroll and hover budget left unused moves to click.
PHASES = ("scroll", "hover", "click")
PHASE_SHARES = {"scroll": (0.10, 0.20), "hover": (0.30, 0.25), "click": (0.60, 0.55)}
BACKSTOP_FACTOR = 2.0             # run-level wall clock, a safety net beyond the phases
OPEN_STUB_JS = """
(() => {
  window.open = function (url) {
    try { window.__exploreBlockedOpen && window.__exploreBlockedOpen(String(url)); } catch (e) {}
    return null;
  };
})();
"""
RAW_URLS_PER_KEY = 5              # raw URLs kept per origin+path key; --all-raw-urls lifts it
LABEL_MAX_WORDS = 4               # the screener's single-word denylist: names this short only

# The request guard is the primary defense; these cover what it cannot see. Phrase
# rules are specific and rarely innocent, so they apply at any name length (a product
# title inside "add ... to cart" must not switch them off). Short rules are generic
# words common in harmless text, so they apply only to names of LABEL_MAX_WORDS or
# fewer, like the screener's single-word list.
PHRASE_DENY = re.compile(
    r"\b(add\b.{0,80}?\bto\s+(cart|bag|basket|wish\s?list|favou?rites?)|buy|check\s?out|"
    r"wish\s?list|favou?rites?|rate\s+this|rate|ratings?|vote|not\s+helpful|helpful)\b",
    re.IGNORECASE | re.DOTALL)
SHORT_DENY = re.compile(r"\b(report|like|save|follow|comment|reply)\b", re.IGNORECASE)


def deny_rule(prefix: str, matched: str) -> str:
    """A stable rule id for counting: 'add ... to cart' whatever the product."""
    text = re.sub(r"\s+", " ", matched.lower()).strip()
    if text.startswith("add ") and " to " in text:
        text = "add ... to " + text.rsplit(" to ", 1)[1]
    return f"{prefix}:{text}"
CONSENT_WORDS = re.compile(r"\b(cookies?|consent)\b", re.IGNORECASE)
# A link to another document with a click listener is clicked only if it points at an
# image or video (a lightbox). Other listener links are mostly client-side router links.
MEDIA_PATH = re.compile(r"\.(jpe?g|png|gif|webp|avif|svg|bmp|mp4|webm|mov|m4v|ogv)$", re.IGNORECASE)
# Clicks are dispatched to the element, as RevealClick does them, so an overlay or a
# sticky header cannot swallow them. Before dispatching, a hit test at the element's
# centre (after scrolling it into view, as a person would) records whether a real
# pointer would have reached it, and if not, which element was on top.
CLICK_STATE_JS = """(el) => !el.isConnected || !el.checkVisibility() ? 'not visible'
  : el.matches('[disabled], [aria-disabled="true"]') ? 'disabled' : ''"""
DISPATCH_CLICK_JS = """(el) => {
  let r = el.getBoundingClientRect();
  if (r.bottom < 0 || r.right < 0 || r.top > innerHeight || r.left > innerWidth) {
    el.scrollIntoView({block: 'center', inline: 'center'});
    r = el.getBoundingClientRect();
  }
  const x = r.left + r.width / 2, y = r.top + r.height / 2;
  const hit = document.elementFromPoint(x, y);
  const describe = (e) => e.tagName.toLowerCase() + (e.id ? '#' + e.id : '')
    + [...e.classList].slice(0, 2).map((c) => '.' + c).join('');
  let interceptor = null;
  if (!hit) interceptor = '(off-screen or zero size)';
  else if (hit !== el && !el.contains(hit)) interceptor = describe(hit);
  const init = {bubbles: true, cancelable: true, clientX: x, clientY: y};
  for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
    el.dispatchEvent(type.startsWith('pointer')
      ? new PointerEvent(type, {...init, pointerType: 'mouse'}) : new MouseEvent(type, init));
  }
  return {reachable: !interceptor, interceptor};
}"""
# Puts a click's path change back: traverse to the entry the click left if it pushed
# one (the page's router follows the popstate), else put the URL back with
# replaceState, which cannot restore the router's own state.
RESTORE_ROUTE_JS = """async ([url, key]) => {
  const nav = window.navigation;
  if (nav && key && nav.currentEntry && nav.currentEntry.key !== key) {
    const ok = await Promise.race([
      new Promise((resolve) => {
        window.addEventListener('popstate', () => resolve(true), {once: true});
        nav.traverseTo(key).finished.catch(() => resolve(false));
      }),
      new Promise((resolve) => setTimeout(() => resolve(false), 2000)),
    ]);
    if (ok) return 'traverse';
  }
  history.replaceState(window.__exploreState ?? null, '', url);
  return 'replaceState';
}"""
CONSENT_ACTIONS = re.compile(r"\b(accept|agree|allow|reject)\b", re.IGNORECASE)

# Request guard: after load, only GET and HEAD leave the page, plus these reads.
# Algolia's query endpoints are POST by design (DocSearch on the Astro docs, the
# Gymshark product search in the 44-page data); a query changes nothing.
WRITE_ALLOWANCES = [
    (re.compile(r"(^|\.)algolia(net)?\.(net|com|io)$", re.I),
     re.compile(r"^/1/indexes/[^/]+/(query|queries)$"), "Algolia search query"),
    # Google's gRPC-web APIs read via POST; the method name states the operation.
    # Only Get/List/Search/BatchGet: Init, Set, Update, Create, Delete stay blocked.
    (re.compile(r"(^|\.)googleapis\.com$", re.I),
     re.compile(r"^/\$rpc/[^/]+/(Get|List|Search|BatchGet)[A-Za-z0-9_]*$"),
     "Google gRPC-web read (Get/List/Search/BatchGet)"),
]

# --- --match-crawler: mirror Browsertrix 1.14.3's browser (src/util/browser.ts) ---
# Brave Origin 149 under Xvfb, GEOMETRY=1360x1020x16, puppeteer defaultViewport of the
# full screen (recording mode, no height offset), deviceScaleFactor 1 (puppeteer
# default), its Linux UA plus the crawl-eval suffix, and the sec-ch-ua headers it sets.
CRAWLER_VIEWPORT = {"width": 1360, "height": 1020}
CRAWLER_UA_SUFFIX = (f"USC-CURVE-AcademicResearch-CrawlEval/1.0 (+{CONTACT_URL}; academic "
                     "research; evaluating crawler click behaviors; one page at a time, "
                     ">=60s per host)")
CRAWLER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/149.0.0.0 Safari/537.36 " + CRAWLER_UA_SUFFIX)
CRAWLER_HEADERS = {
    "sec-ch-ua": '"Not(A:Brand";v="8", "Chromium";v="149", "Brave";v="149"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Linux"',
}
# defaultArgs plus the chromeArgs additions, minus what is about the container rather
# than the page: --remote-debugging-*, --no-sandbox, --display, --user-agent (set on
# the context instead). --disable-lazy-loading is the one that matters most: it turns
# off native loading=lazy for images and iframes.
CRAWLER_ARGS = [
    "--disable-field-trial-config", "--disable-background-networking",
    "--enable-features=NetworkService,NetworkServiceInProcess",
    "--disable-background-timer-throttling", "--disable-backgrounding-occluded-windows",
    "--disable-back-forward-cache", "--disable-breakpad",
    "--disable-client-side-phishing-detection",
    "--disable-component-extensions-with-background-pages", "--disable-component-update",
    "--no-default-browser-check", "--disable-default-apps", "--disable-dev-shm-usage",
    "--disable-extensions",
    "--disable-features=ImprovedCookieControls,LazyFrameLoading,GlobalMediaControls,"
    "DestroyProfileOnBrowserClose,MediaRouter,DialMediaRouteProvider,AcceptCHFrame,"
    "AutoExpandDetailsElement,CertificateTransparencyComponentUpdater,"
    "AvoidUnnecessaryBeforeUnloadCheckSync,Translate,OptimizationGuideModelDownloading,"
    "OptimizationHintsFetching,OptimizationTargetPrediction,OptimizationHints",
    "--allow-pre-commit-input", "--disable-hang-monitor", "--disable-ipc-flooding-protection",
    "--disable-popup-blocking", "--disable-prompt-on-repost", "--disable-renderer-backgrounding",
    "--disable-sync", "--disable-lazy-loading", "--force-color-profile=srgb",
    "--metrics-recording-only", "--no-first-run", "--enable-automation",
    "--password-store=basic", "--use-mock-keychain", "--no-service-autorun",
    "--export-tagged-pdf", "--apps-keep-chrome-alive-in-tests",
    "--disable-background-media-suspend", "--autoplay-policy=no-user-gesture-required",
    "--disable-site-isolation-trials",
]
RUNS = 2

ACTION_ACCOUNTING = (
    "one action per hover, per click (restore clicks included), and per scroll sweep "
    "of one container; individual scroll steps are logged as iterations of their sweep "
    "and do not count toward the cap, so one infinite-scroll page cannot spend the "
    "whole budget before hover and click run. Budgets are per phase (PHASE_SHARES of "
    "max_actions and max_seconds); unused scroll and hover budget moves to click once "
    "exploration is otherwise done")

CLICK_LISTENER_TYPES = {"click", "mousedown", "mouseup", "pointerdown", "pointerup",
                        "dblclick", "auxclick"}
HOVER_LISTENER_TYPES = {"mouseover", "mouseenter", "pointerover", "pointerenter"}

# Native controls, per the explorer's spec. a[href] is included here and narrowed to
# same-page links by the screener's click_is_safe, which logs every away-link it drops.
NATIVE_CLICK_SEL = "button, summary, a[href], [role='button'], [role='tab']"
# Hover is harmless, so the native set is wider: anything a user can point at and
# expect a response from.
NATIVE_HOVER_SEL = ("a[href], button, summary, select, textarea, "
                    "input:not([type='hidden']), [role='button'], [role='tab'], "
                    "[role='menuitem'], [role='link'], [tabindex]:not([tabindex='-1'])")

# Delegation roots: a click listener on <body> says nothing about where to click.
DELEGATION_ROOT_TAGS = {"HTML", "BODY", "HEAD"}
SKIP_SCAN_TAGS = {"SCRIPT", "STYLE", "META", "LINK", "HEAD", "TITLE", "NOSCRIPT",
                  "TEMPLATE", "BR", "WBR", "BASE"}

NON_NETWORK_SCHEMES = ("data:", "blob:", "about:", "chrome:", "chrome-extension:",
                       "javascript:")


# --- in-page scripts ----------------------------------------------------------

# An element's signature, so a re-rendered control is known again: its path from the
# nearest ancestor with a stable id (ids that look generated per render are skipped),
# as tag:nth-of-type steps, plus role, name and attributes that stay the same across
# renders. The same recipe as RevealClick's.
SIGNATURE_JS = r"""
  const GENERATED_ID = /\d{3,}|:|[0-9a-f]{8}|^(ember|react|rc|mui|radix|headlessui|ng)[-_]/i;
  const sigName = (el) => (
    el.getAttribute('aria-label') || el.getAttribute('title') ||
    el.getAttribute('alt') || (el.tagName === 'INPUT' ? (el.value || '') : '') ||
    (el.innerText || el.textContent || '')
  ).replace(/\s+/g, ' ').trim().slice(0, 100);
  const signature = (el) => {
    const path = [];
    for (let e = el; e && e.nodeType === 1 && e !== document.body; e = e.parentElement) {
      if (e.id && !GENERATED_ID.test(e.id)) { path.unshift('#' + e.id); break; }
      const tag = e.tagName;
      const same = e.parentElement
        ? Array.prototype.filter.call(e.parentElement.children, (c) => c.tagName === tag) : [e];
      path.unshift(tag.toLowerCase() + ':' + (same.indexOf(e) + 1));
    }
    return [path.join('>'), el.getAttribute('role') || '', sigName(el),
            ...['type', 'href', 'name', 'title', 'data-testid'].map((a) => el.getAttribute(a) || '')
           ].join('|');
  };
"""

# The visible element that now carries a signature (a control rebuilt after a click).
FIND_BY_SIGNATURE_JS = r"""
([sig, tag]) => {
""" + SIGNATURE_JS + r"""
  for (const el of document.querySelectorAll(tag)) {
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height || !el.checkVisibility() || signature(el) !== sig) continue;
    if (!el.hasAttribute('data-explore-id')) {
      window.__exploreSeq += 1;
      el.setAttribute('data-explore-id', window.__explorePrefix + '-' + window.__exploreSeq);
    }
    return el.getAttribute('data-explore-id');
  }
  return null;
}
"""

# Candidate discovery. Walks the document and every open shadow root, keeps visible
# elements from any of the three sources (native control, CDP listener mark,
# cursor:pointer), and gives each a stable id that survives later passes.
DISCOVER_JS = r"""
({prefix, clickSel, hoverSel, delegationRoots, pointer}) => {
  if (!window.__explorePrefix) { window.__explorePrefix = prefix; window.__exploreSeq = 0; }
  const P = window.__explorePrefix;
""" + SIGNATURE_JS + r"""
  const acc = (el) => (
    el.getAttribute('aria-label') || el.getAttribute('title') ||
    el.getAttribute('alt') || (el.tagName === 'INPUT' ? (el.value || '') : '') ||
    (el.innerText || el.textContent || '')
  ).trim().slice(0, 200);
  const nearby = (el) => {
    const p = el.closest('li, td, th, figure, article, section, div');
    return ((p && (p.innerText || '')) || '').trim().slice(0, 200);
  };
  const classes = (e) => {
    const c = e.className;
    return ((c && c.baseVal !== undefined ? c.baseVal : c) || '').toString()
      .trim().split(/\s+/).filter(Boolean).slice(0, 2);
  };
  // inside a cookie/consent banner: an ancestor (or the element) whose id, class,
  // role or aria-label says so
  const CONSENT_RE = /cookie|consent|gdpr|onetrust|cookiebot|didomi|\bcmp\b|privacy-banner/i;
  const inConsent = (el) => {
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
      const cls = typeof n.className === 'string' ? n.className : '';
      if (CONSENT_RE.test([n.id, cls, n.getAttribute('role') || '',
                           n.getAttribute('aria-label') || ''].join(' '))) {
        return true;
      }
    }
    return false;
  };
  const cssPath = (el) => {
    const parts = [];
    let e = el;
    for (let d = 0; e && e.nodeType === 1 && d < 4; d++) {
      let s = e.tagName.toLowerCase();
      if (e.id) { parts.unshift(s + '#' + e.id); break; }
      const cls = classes(e);
      if (cls.length) s += '.' + cls.join('.');
      const p = e.parentElement;
      if (p) {
        const same = Array.prototype.filter.call(p.children, (c) => c.tagName === e.tagName);
        if (same.length > 1) s += ':nth-of-type(' + (same.indexOf(e) + 1) + ')';
      }
      parts.unshift(s);
      e = e.parentElement;
    }
    return parts.join(' > ');
  };
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    if (el.checkVisibility && !el.checkVisibility({visibilityProperty: true})) return false;
    const st = getComputedStyle(el);
    return st.visibility !== 'hidden' && st.display !== 'none' && st.pointerEvents !== 'none';
  };
  const out = [];
  const visit = (root) => {
    const els = root.querySelectorAll('*');
    for (const el of els) {
      if (el.shadowRoot) visit(el.shadowRoot);
      if (delegationRoots.indexOf(el.tagName) !== -1) continue;
      const lst = (el.getAttribute('data-explore-lst') || '').split(' ').filter(Boolean);
      const nativeClick = el.matches(clickSel);
      const nativeHover = el.matches(hoverSel);
      const lstClick = lst.indexOf('click') !== -1;
      const lstHover = lst.indexOf('hover') !== -1;
      // Third source: cursor:pointer. React and friends attach every handler at the
      // root, so DOMDebugger sees nothing on the element that is actually clickable,
      // and a delegated handler cannot be traced back to its target. The pointer
      // cursor is the one signal such a page still gives. It is inherited, so an
      // element only counts when its parent is NOT also a pointer: that keeps the
      // outermost element of each pointer subtree and drops the inner spans and
      // icons that would otherwise each become a candidate.
      const cursorPointer = pointer && getComputedStyle(el).cursor === 'pointer' &&
        !(el.parentElement && getComputedStyle(el.parentElement).cursor === 'pointer');
      if (!(nativeClick || nativeHover || lstClick || lstHover || cursorPointer)) continue;
      if (!visible(el)) continue;
      if (!el.hasAttribute('data-explore-id')) {
        window.__exploreSeq += 1;
        el.setAttribute('data-explore-id', P + '-' + window.__exploreSeq);
      }
      const anchor = el.closest('a');
      // a link to another document: hovering it mostly triggers prefetch
      const linksAway = !!(anchor && anchor.href && /^https?:/.test(anchor.href) &&
                           anchor.href.split('#')[0] !== location.href.split('#')[0]);
      out.push({
        id: el.getAttribute('data-explore-id'),
        sig: signature(el),
        tag: el.tagName.toLowerCase(),
        selector: cssPath(el),
        name: acc(el),
        nearby: nearby(el),
        in_form: !!el.closest('form'),
        in_consent: inConsent(el),
        links_away: linksAway,
        is_submit: (el.getAttribute('type') || '').toLowerCase() === 'submit',
        in_method_post: !!el.closest('[method="post" i]'),
        href: anchor ? (anchor.getAttribute('href') || '') : '',
        target: anchor ? (anchor.getAttribute('target') || '') : '',
        role: el.getAttribute('role') || '',
        click_ok: nativeClick || lstClick || cursorPointer,
        hover_ok: (nativeHover || lstHover) && !linksAway,
        via: [nativeClick ? 'native-click' : null, nativeHover ? 'native-hover' : null,
              lstClick ? 'cdp-listener-click' : null, lstHover ? 'cdp-listener-hover' : null,
              cursorPointer ? 'cursor-pointer' : null].filter(Boolean),
        // Which of the three discovery sources found this element. The later
        // analysis needs to know how much each source contributes on its own.
        sources: [(nativeClick || nativeHover) ? 'native' : null,
                  (lstClick || lstHover) ? 'cdp-listener' : null,
                  cursorPointer ? 'cursor-pointer' : null].filter(Boolean),
      });
    }
  };
  visit(document);
  return out;
}
"""

# Scroll containers other than the document itself, either axis.
SCROLLABLES_JS = r"""
({prefix}) => {
  if (!window.__explorePrefix) { window.__explorePrefix = prefix; window.__exploreSeq = 0; }
  const P = window.__explorePrefix;
  const out = [];
  const scroller = document.scrollingElement || document.documentElement;
  const visit = (root) => {
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) visit(el.shadowRoot);
      if (el === scroller || el === document.body || el === document.documentElement) continue;
      const st = getComputedStyle(el);
      const oy = /(auto|scroll|overlay)/.test(st.overflowY) && el.scrollHeight > el.clientHeight + 8;
      const ox = /(auto|scroll|overlay)/.test(st.overflowX) && el.scrollWidth > el.clientWidth + 8;
      if (!oy && !ox) continue;
      const r = el.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0 || st.visibility === 'hidden' || st.display === 'none') continue;
      if (!el.hasAttribute('data-explore-scroll')) {
        window.__exploreSeq += 1;
        el.setAttribute('data-explore-scroll', P + '-s' + window.__exploreSeq);
      }
      const cls = ((el.className && el.className.baseVal !== undefined ? el.className.baseVal
                    : el.className) || '').toString().trim().split(/\s+/).filter(Boolean).slice(0, 2);
      out.push({
        id: el.getAttribute('data-explore-scroll'),
        label: el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') +
               (cls.length ? '.' + cls.join('.') : ''),
        y: oy, x: ox,
      });
    }
  };
  visit(document);
  return out;
}
"""

# One scroll step on a container (or the document when id is null). Returns the
# geometry needed to decide whether the container is exhausted.
SCROLL_STEP_JS = r"""
({id, axis, step}) => {
  const el = id ? document.querySelector('[data-explore-scroll="' + id + '"]')
                : (document.scrollingElement || document.documentElement);
  if (!el) return null;
  const y = axis === 'y';
  const before = y ? el.scrollTop : el.scrollLeft;
  if (id) { el.scrollBy(y ? 0 : step, y ? step : 0); }
  else { window.scrollBy(0, step); }
  const pos = y ? el.scrollTop : el.scrollLeft;
  const size = y ? el.scrollHeight : el.scrollWidth;
  const client = id ? (y ? el.clientHeight : el.clientWidth) : window.innerHeight;
  return {moved: pos - before, pos: pos, size: size, client: client,
          at_end: pos + client >= size - 2};
}
"""

SCROLL_GEOM_JS = r"""
({id, axis}) => {
  const el = id ? document.querySelector('[data-explore-scroll="' + id + '"]')
                : (document.scrollingElement || document.documentElement);
  if (!el) return null;
  const y = axis === 'y';
  return {size: y ? el.scrollHeight : el.scrollWidth,
          client: id ? (y ? el.clientHeight : el.clientWidth) : window.innerHeight};
}
"""

VISIBLE_TEXT_JS = "() => document.body ? document.body.innerText : ''"


# --- helpers ------------------------------------------------------------------

def is_network_url(url: str) -> bool:
    return not url.lower().startswith(NON_NETWORK_SCHEMES)


def same_origin(a: str, b: str) -> bool:
    pa, pb = urllib.parse.urlsplit(a), urllib.parse.urlsplit(b)
    return (pa.scheme, pa.netloc) == (pb.scheme, pb.netloc)


def page_id_for(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    slug = re.sub(r"[^A-Za-z0-9]+", "-", f"{parts.netloc}{parts.path}").strip("-").lower()
    return slug[:120] or "page"


class NoiseFilter:
    """The screener's noise rules, plus explicit, reasoned exemptions.

    An exemption is a URL prefix with a mandatory written reason, in the same spirit
    as the screener's STABILITY_OVERRIDES: an exception stated in the open rather
    than a rule widened for everyone. The rule that WOULD have fired is still
    recorded, so an exempted URL stays auditable.
    """

    def __init__(self, exemptions: list[tuple[str, str]], mode: str = "advisory"):
        self.mode = mode
        for prefix, reason in exemptions:
            if not reason.strip():
                raise ValueError(f"noise exemption {prefix!r} has an empty reason; "
                                 "a written justification is mandatory")
        self.exemptions = exemptions

    def classify(self, url: str, meta: dict | None) -> tuple[str | None, str, str | None]:
        """(effective_rule, category, rule_that_would_have_fired_if_exempted).

        Category splits the screener's rules without changing any of them. The
        response-size rule is the one that also catches small *content* - the
        screener dropped a 347-byte page-data.json and a 1013-byte loader SVG that
        way - so what it removes is reported separately, for review, rather than
        being mixed in with cookie syncs and ad hosts. noise_rule tests host, then
        path, then response, and returns the first match, so a URL categorised
        "size" was removed by the size rule alone.
        """
        rule = noise_rule(url, meta)
        if rule is None:
            return None, "content", None
        category = "size" if rule.startswith("response: body") else "noise"
        for prefix, _reason in self.exemptions:
            if url.startswith(prefix):
                return None, "content", rule
        return rule, category, None


# --- one exploration run ------------------------------------------------------

class Explorer:
    """One full exploration of one page in one browser context."""

    def __init__(self, browser, url: str, throttle: HostThrottle, opts, noise: NoiseFilter,
                 run_no: int):
        self.browser = browser
        self.url = url
        self.throttle = throttle
        self.opts = opts
        self.noise = noise
        self.run_no = run_no
        self.result = PassResult(name=f"explore-run-{run_no}")
        self.started = time.time()
        self.backstop = self.started + BACKSTOP_FACTOR * opts.max_seconds
        actions = {p: int(opts.max_actions * a) for p, (a, _s) in PHASE_SHARES.items()}
        actions["click"] = opts.max_actions - actions["scroll"] - actions["hover"]
        self.budget = {p: {"actions": actions[p], "seconds": opts.max_seconds * PHASE_SHARES[p][1]}
                       for p in PHASES}
        self.spent = {p: {"actions": 0, "seconds": 0.0} for p in PHASES}
        self.phase_caps: dict[str, str | None] = {p: None for p in PHASES}
        self.phase: str | None = None
        self.phase_t0 = 0.0
        self.donation: dict | None = None
        # norm url -> {first_action, raw, resource_type, t}
        self.requests: dict[str, dict] = {}
        self.inflight: set = set()
        self.last_net_activity = time.time()
        self.actions: list[dict] = []
        self.current = 0                       # action id; 0 is the baseline
        self.action_count = 0                  # toward MAX_ACTIONS
        self.cap_hit: str | None = None
        self.seen_text: set[str] = set()
        self.novel_text_log: list[str] = []
        self.blocked_navigations: list[dict] = []
        self.popups: list[dict] = []
        self.soft_navigations: list[dict] = []
        self.vetoes: list[dict] = []           # every refused click, with its kind
        self.safety_skips: list[dict] = []     # the subset that trips the halt gate
        self.unclickable: list[dict] = []
        self.skipped_clicks: list[dict] = []   # not visible or disabled: never clicked
        self.scroll_containers: list[dict] = []
        self.listener_scans: list[dict] = []
        self.discovery_passes: list[dict] = []
        self.processed_hover: set[str] = set()
        self.processed_click: set[str] = set()
        # per signature: clicks so far and whether the last one revealed something
        self.sig_state: dict[str, dict] = {}
        self.processed_scroll: set[str] = set()
        self.click_candidates_seen: dict[str, list] = {}
        # Requests the page makes on its own, on a timer, while nobody touches it:
        # heartbeats, polling, session pings, rotating ad slots. Normalised to
        # scheme+host+path, so a poll whose query string changes every time still
        # matches. Recorded during an idle window before baseline, and excluded from
        # attribution and novelty for the rest of the session.
        self.periodic_patterns: set[str] = set()
        self.raw_types: dict[str, str] = {}    # raw URL -> resource type, first seen
        self.blocked_writes: list[dict] = []   # non-GET/HEAD requests the guard aborted
        self.blocked_opens: list[dict] = []    # window.open calls the stub refused
        self.allowed_writes: list[dict] = []   # ... and the ones an allowance let through
        # raw URL -> CDP initiator, CDP resource type, and Sec-Purpose/Purpose header
        self.request_meta: dict[str, dict] = {}
        self._rid_url: dict[str, str] = {}
        self.periodic_window_hits: set[str] = set()
        self.recording_periodic = False
        self.periodic_excluded: dict[str, int] = {}
        self.blocked_seen: set[str] = set()
        self._blocked_cache: set[str] = set()
        self._blocked_len = -1
        self.main_scrolled_to: int = 0
        self.nav_excluded_actions: set[int] = set()
        self.route_change_actions: set[int] = set()  # clicks that changed the path
        self.state_url_changes: list[dict] = []        # clicks that changed query or fragment
        self.baseline_window: tuple[float, float] = (0.0, 0.0)
        self.settled_url = url
        self.target_norm = normalise_request(url)
        self.guard_on = False
        # The write guard starts with the first interaction phase: the idle window and
        # baseline run unguarded, measured the way a no-behavior crawl is.
        self.write_guard_on = False
        self.prefix_seq = 0

    # ---- request bookkeeping --------------------------------------------------

    def _on_request(self, request) -> None:
        self.inflight.add(request)
        self.last_net_activity = time.time()
        url = request.url
        if not is_network_url(url):
            return
        norm = normalise_request(url)
        if self.recording_periodic:
            self.periodic_window_hits.add(norm)
        elif norm in self.periodic_patterns:
            # Never a reveal: the page was making this request before anyone touched
            # it. Excluded from attribution and from every novelty check.
            self.periodic_excluded[norm] = self.periodic_excluded.get(norm, 0) + 1
            return
        rec = self.requests.get(norm)
        if rec is None:
            self.requests[norm] = {"first_action": self.current, "raw": [url],
                                   "resource_type": request.resource_type,
                                   "t": round(time.time(), 3)}
        elif url not in rec["raw"] and (self.opts.all_raw_urls
                                        or len(rec["raw"]) < RAW_URLS_PER_KEY):
            rec["raw"].append(url)
        self.raw_types.setdefault(url, request.resource_type)

    def _attach_network_cdp(self, page) -> None:
        """Initiator and purpose per request, which page request events do not carry."""
        try:
            cdp = page.context.new_cdp_session(page)
            cdp.on("Network.requestWillBeSent", self._on_cdp_request)
            cdp.on("Network.requestWillBeSentExtraInfo", self._on_cdp_extra)
            cdp.send("Network.enable")
            self._net_cdp = cdp
        except Exception:  # noqa: BLE001 - instrumentation must never break a page
            pass

    @staticmethod
    def _purpose(headers: dict) -> str | None:
        low = {k.lower(): v for k, v in (headers or {}).items()}
        return low.get("sec-purpose") or low.get("purpose")

    def _on_cdp_request(self, ev: dict) -> None:
        req = ev.get("request") or {}
        url = req.get("url", "")
        if not url.startswith("http"):
            return
        init = ev.get("initiator") or {}
        frames = ((init.get("stack") or {}).get("callFrames") or [])
        init_url = init.get("url") or next((f["url"] for f in frames if f.get("url")), None)
        rec = self.request_meta.setdefault(url, {"initiator_type": init.get("type"),
                                                 "initiator_url": init_url,
                                                 "cdp_type": ev.get("type"), "purpose": None})
        rec["purpose"] = rec["purpose"] or self._purpose(req.get("headers"))
        self._rid_url[ev.get("requestId", "")] = url

    def _on_cdp_extra(self, ev: dict) -> None:
        url = self._rid_url.get(ev.get("requestId", ""))
        if url and url in self.request_meta:
            rec = self.request_meta[url]
            rec["purpose"] = rec["purpose"] or self._purpose(ev.get("headers"))

    def _on_request_done(self, request) -> None:
        self.inflight.discard(request)
        self.last_net_activity = time.time()

    @staticmethod
    def graphql_kind(body: str | None) -> str | None:
        """'query' only when every operation in the body is a query; else None."""
        if not body:
            return None
        try:
            data = json.loads(body)
        except ValueError:
            return None
        kinds = set()
        for op in data if isinstance(data, list) else [data]:
            query = op.get("query") if isinstance(op, dict) else None
            if not isinstance(query, str):
                return None                     # persisted query or not GraphQL: unsure
            text = re.sub(r"#[^\n]*", "", query).strip()
            if re.search(r"(^|[}\s])(mutation|subscription)\b", text):
                return None
            if not (text.startswith("{") or re.match(r"query\b", text)):
                return None
            kinds.add("query")
        return "query" if kinds == {"query"} else None

    def write_allowance(self, request) -> str | None:
        parts = urllib.parse.urlsplit(request.url)
        for host_re, path_re, why in WRITE_ALLOWANCES:
            if host_re.search(parts.netloc) and path_re.search(parts.path):
                return why
        try:
            body = request.post_data
        except Exception:  # noqa: BLE001 - binary body
            body = None
        if self.graphql_kind(body) == "query":
            return "GraphQL query (no mutation or subscription)"
        return None

    def _guard(self, route, request) -> None:
        """Stricter than the screener's guard, layered on top of it.

        The screener aborts main-frame navigations to a *different* URL. Here every
        main-frame document request after the initial load is answered with 204 No
        Content, which Chromium treats as "stay on the current document": the page
        and all its JS state survive, where an abort can commit an error page. That
        also stops same-URL reloads (href="?x=1", a form GET to itself), which the
        screener's guard lets through and which would silently reset the page.
        Everything else falls back to the screener's handler.
        """
        try:
            if self.write_guard_on and request.method not in ("GET", "HEAD"):
                why = self.write_allowance(request)
                act = self.actions[self.current - 1] if self.current else {}
                record = {"method": request.method, "url": request.url,
                          "resource_type": request.resource_type, "action_id": self.current,
                          "action": act.get("action", "baseline"), "target": act.get("target")}
                if why:
                    self.allowed_writes.append({**record, "allowance": why})
                else:
                    try:
                        buf = request.post_data_buffer or b""
                    except Exception:  # noqa: BLE001
                        buf = b""
                    record["body"] = buf[:200].decode("utf-8", "replace")
                    self.blocked_writes.append(record)
                    if request.is_navigation_request() and request.frame == self.page.main_frame:
                        # a form POST: abort would commit an error page; 204 keeps the page
                        route.fulfill(status=204, body="")
                    else:
                        route.abort()
                    return
            if (self.guard_on and request.is_navigation_request()
                    and request.frame == self.page.main_frame):
                self.blocked_navigations.append({"url": request.url, "action": self.current})
                self.nav_excluded_actions.add(self.current)
                route.fulfill(status=204, body="")
                return
        except Exception:  # noqa: BLE001 - never let the guard break the page
            pass
        route.fallback()

    def stub_window_open(self) -> None:
        """From the first interaction on, window.open opens nothing and is logged.

        Closing popups on sight is too late: by then the popup has loaded. Installed
        in every current frame, and as an init script for documents created later.
        """
        try:
            self.page.expose_function(
                "__exploreBlockedOpen",
                lambda url: self.blocked_opens.append({"url": url, "action": self.current}))
            self.page.add_init_script(OPEN_STUB_JS)
        except Exception:  # noqa: BLE001 - instrumentation must never break a page
            pass
        for frame in self.page.frames:
            try:
                frame.evaluate(OPEN_STUB_JS)
            except Exception:  # noqa: BLE001 - detached or cross-process frame
                pass

    def _on_popup(self, popup) -> None:
        # screen.load_page already closes every popup; this records who caused it.
        try:
            url = popup.url
        except Exception:  # noqa: BLE001
            url = ""
        self.popups.append({"url": url, "action": self.current})
        self.nav_excluded_actions.add(self.current)

    # ---- action bookkeeping ---------------------------------------------------

    def enter(self, phase: str) -> None:
        self.phase, self.phase_t0 = phase, time.time()

    def leave(self) -> None:
        if self.phase:
            self.spent[self.phase]["seconds"] += time.time() - self.phase_t0
        self.phase = None

    def phase_seconds(self, phase: str) -> float:
        extra = time.time() - self.phase_t0 if self.phase == phase else 0.0
        return self.spent[phase]["seconds"] + extra

    def caps_reached(self) -> bool:
        """The current phase's own budget, or the run-level backstop."""
        if self.cap_hit:
            return True
        if time.time() >= self.backstop:
            self.cap_hit = "wall_clock_backstop"
            return True
        p = self.phase
        if p is None:
            return False
        if not self.phase_caps[p]:
            if self.spent[p]["actions"] >= self.budget[p]["actions"]:
                self.phase_caps[p] = "actions"
            elif self.phase_seconds(p) >= self.budget[p]["seconds"]:
                self.phase_caps[p] = "wall_clock"
        return self.phase_caps[p] is not None

    def begin(self, action: str, target: str, iteration: int, counts: bool = True,
              **extra) -> dict:
        record = {"id": len(self.actions) + 1, "action": action, "target": target,
                  "iteration": iteration, "t_start": round(time.time() - self.started, 2),
                  **extra}
        self.actions.append(record)
        self.current = record["id"]
        if counts:
            self.action_count += 1
            if self.phase:
                self.spent[self.phase]["actions"] += 1
        return record

    def settle(self, min_ms: int = SETTLE_MIN_MS, max_ms: int = SETTLE_MAX_MS) -> None:
        """Wait at least min_ms, then until the network has been quiet for a moment."""
        self.page.wait_for_timeout(min_ms)
        end = time.time() + (max_ms - min_ms) / 1000
        while time.time() < end:
            quiet = (time.time() - self.last_net_activity) * 1000
            if not self.inflight and quiet >= SETTLE_QUIET_MS:
                return
            self.page.wait_for_timeout(100)

    def visible_lines(self) -> set[str]:
        """Visible text fragments (innerText lines) of the page and same-origin frames."""
        lines: set[str] = set()
        for frame in self._frames():
            try:
                text = frame.evaluate(VISIBLE_TEXT_JS)
            except Exception:  # noqa: BLE001 - frame detached mid-call
                continue
            lines.update(ln.strip() for ln in text.splitlines() if ln.strip())
        return lines

    def absorb_text(self) -> set[str]:
        """Add current visible text to the session seen-set; return what was novel."""
        lines = self.visible_lines()
        novel = lines - self.seen_text
        self.seen_text |= lines
        for ln in sorted(novel):
            if len(self.novel_text_log) < TEXT_FRAGMENT_LOG_LIMIT:
                self.novel_text_log.append(ln)
        return novel

    def content_rule(self, norm: str) -> str | None:
        rule, _, _ = self.noise.classify(norm, self.result.response_meta.get(norm))
        return rule

    def blocked_norms(self) -> set[str]:
        """Normalised URLs the ad/tracker blocker aborted, recomputed as they arrive."""
        entries = self.result.blocked_requests
        if len(entries) != self._blocked_len:
            self._blocked_cache = {normalise_request(b["url"]) for b in entries}
            self._blocked_len = len(entries)
        return self._blocked_cache

    def novel_content_requests(self, action_id: int) -> list[str]:
        """Content requests first seen during this action (so, by construction, novel).

        A request the blocker aborted was never fetched, so it can never be a reveal:
        without this a click that fires a blocked tracker would read as novel and keep
        the element being clicked ten times over.
        """
        blocked = self.blocked_norms()
        return sorted(u for u, r in self.requests.items()
                      if r["first_action"] == action_id and u not in blocked
                      and self.content_rule(u) is None)

    def _frames(self):
        """The main frame and every same-origin child frame."""
        out = [self.page.main_frame]
        for frame in self.page.frames:
            if frame is self.page.main_frame:
                continue
            furl = frame.url or ""
            if furl in ("about:blank", "about:srcdoc") or same_origin(furl, self.settled_url):
                out.append(frame)
        return out

    def check_navigation(self, act: dict, before: tuple[str, str | None] | None = None) -> bool:
        """True if this action tried to leave the page. Reverts if it managed to.

        A click that changed the path (or the origin) is a route change, not a reveal:
        what it fetched stays out of the pool. A query or fragment change (?page=2, a
        hash tab, an anchor) is in-page state: it is kept, and its fetches count.
        before is (url, history entry key) from just before a click.
        """
        navigated = act["id"] in self.nav_excluded_actions
        try:
            now = self.page.url
        except Exception:  # noqa: BLE001
            now = ""
        if normalise_request(now) != self.target_norm:
            # A history-API route change: no document request to block. Treated as a
            # navigation (a different "page" of an SPA is not a reveal on this one).
            navigated = True
            self.nav_excluded_actions.add(act["id"])
            entry = {"from_action": act["id"], "url": now,
                     "kind": "route change" if before else "soft navigation", "reverted_by": None}
            self.soft_navigations.append(entry)
            if before:
                self.route_change_actions.add(act["id"])
                try:
                    entry["reverted_by"] = self.page.evaluate(RESTORE_ROUTE_JS, list(before))
                    self.settle()
                except Exception:  # noqa: BLE001
                    pass
            if normalise_request(self.page.url) != self.target_norm:
                entry["reverted_by"] = self.revert()
        elif before and now != before[0]:
            self.state_url_changes.append({"from_action": act["id"], "url": now})
        if navigated:
            act["navigated"] = True
        return navigated

    def route_before(self) -> tuple[str, str | None]:
        """The URL and history entry key before a click, keeping history.state for
        a replaceState restore."""
        try:
            key = self.page.evaluate("""() => {
              window.__exploreState = history.state;
              return window.navigation && navigation.currentEntry
                ? navigation.currentEntry.key : null;
            }""")
        except Exception:  # noqa: BLE001
            key = None
        return self.page.url, key

    def revert(self) -> str:
        rev = self.begin("revert", self.settled_url, 1, counts=False)
        self.nav_excluded_actions.add(rev["id"])
        try:
            self.page.go_back(timeout=5000)
            self.settle()
        except Exception:  # noqa: BLE001
            pass
        if normalise_request(self.page.url) == self.target_norm:
            return "history.back"
        self.guard_on = False
        try:
            self.throttle.wait(self.settled_url)
            self.page.goto(self.settled_url, wait_until="load")
            try:
                self.page.wait_for_load_state("networkidle", timeout=NETWORKIDLE_TIMEOUT_MS)
            except Exception:  # noqa: BLE001
                pass
        finally:
            self.guard_on = True
        return "reload"

    def donate_to_click(self) -> None:
        """Exploration is otherwise done: give click what scroll and hover left unused."""
        if not self.phase_caps["click"] or self.cap_hit:
            return
        gift = {k: sum(max(0, self.budget[p][k] - self.spent[p][k]) for p in ("scroll", "hover"))
                for k in ("actions", "seconds")}
        if gift["actions"] < 1 or gift["seconds"] <= 0:
            return
        for p in ("scroll", "hover"):
            for k in ("actions", "seconds"):
                self.budget[p][k] = min(self.budget[p][k], self.spent[p][k])
        for k in ("actions", "seconds"):
            self.budget["click"][k] += gift[k]
        self.phase_caps["click"] = None
        self.donation = {**gift, "click_passes": 0}
        for pass_no in range(1, self.opts.max_passes + 1):
            self.mark_listener_elements()
            new_click = [(f, c) for f, c in self.discover()
                         if c["click_ok"] and c["id"] not in self.processed_click]
            if not new_click:
                break
            self.donation["click_passes"] += 1
            self.enter("click")
            for frame, cand in new_click:
                if self.caps_reached():
                    break
                self.processed_click.add(cand["id"])
                if self.veto(cand):
                    continue
                self.click_element(frame, cand, pass_no)
            self.leave()
            if self.phase_caps["click"] or self.cap_hit:
                break

    # ---- CDP listener discovery -----------------------------------------------

    def mark_listener_elements(self) -> dict:
        """Mark every element carrying a click-ish or hover-ish listener.

        Same mechanism as the screener's inert_click_handlers (DOMDebugger.
        getEventListeners over CDP), widened from div/span to every element, to the
        hover event types, and to same-process iframes and shadow roots (pierce).
        Marks are data-explore-lst attributes that DISCOVER_JS then reads.
        """
        stats = {"method": "cdp", "scanned": 0, "click": 0, "hover": 0, "truncated": False}
        try:
            cdp = self.page.context.new_cdp_session(self.page)
        except Exception as exc:  # noqa: BLE001
            stats["method"] = f"unavailable ({type(exc).__name__})"
            return stats
        try:
            cdp.send("DOM.enable")
            doc = cdp.send("DOM.getDocument", {"depth": -1, "pierce": True})
            nodes: list[tuple[int, str]] = []
            stack = [doc["root"]]
            while stack:
                node = stack.pop()
                if node.get("nodeType") == 1 and node.get("nodeName", "").upper() not in SKIP_SCAN_TAGS:
                    nodes.append((node["nodeId"], node.get("nodeName", "").upper()))
                stack.extend(reversed(node.get("children") or []))
                stack.extend(node.get("shadowRoots") or [])
                if node.get("contentDocument"):
                    stack.append(node["contentDocument"])
            stats["truncated"] = len(nodes) > LISTENER_SCAN_LIMIT
            for node_id, tag in nodes[:LISTENER_SCAN_LIMIT]:
                if tag in DELEGATION_ROOT_TAGS:
                    continue
                stats["scanned"] += 1
                try:
                    obj = cdp.send("DOM.resolveNode", {"nodeId": node_id,
                                                       "objectGroup": "explore"})["object"]
                    listeners = cdp.send("DOMDebugger.getEventListeners",
                                         {"objectId": obj["objectId"]}).get("listeners", [])
                except Exception:  # noqa: BLE001 - node went away
                    continue
                types = {lst.get("type") for lst in listeners}
                marks = []
                if types & CLICK_LISTENER_TYPES:
                    marks.append("click")
                    stats["click"] += 1
                if types & HOVER_LISTENER_TYPES:
                    marks.append("hover")
                    stats["hover"] += 1
                if marks:
                    try:
                        cdp.send("DOM.setAttributeValue", {"nodeId": node_id,
                                                           "name": "data-explore-lst",
                                                           "value": " ".join(marks)})
                    except Exception:  # noqa: BLE001
                        pass
            try:
                cdp.send("Runtime.releaseObjectGroup", {"objectGroup": "explore"})
            except Exception:  # noqa: BLE001
                pass
        except Exception as exc:  # noqa: BLE001
            stats["method"] = f"failed ({type(exc).__name__}: {str(exc)[:80]})"
        finally:
            try:
                cdp.detach()
            except Exception:  # noqa: BLE001
                pass
        return stats

    def discover(self) -> list[tuple[object, dict]]:
        """(frame, candidate) for every visible candidate in every same-origin frame."""
        out = []
        for frame in self._frames():
            self.prefix_seq += 1
            try:
                cands = frame.evaluate(DISCOVER_JS, {
                    "prefix": f"r{self.run_no}f{self.prefix_seq}",
                    "clickSel": NATIVE_CLICK_SEL, "hoverSel": NATIVE_HOVER_SEL,
                    "delegationRoots": sorted(DELEGATION_ROOT_TAGS),
                    "pointer": bool(self.opts.cursor_pointer)})
            except Exception:  # noqa: BLE001 - frame detached
                continue
            for cand in cands:
                cand["frame"] = "main" if frame is self.page.main_frame else frame.url
                out.append((frame, cand))
        return out

    # ---- scroll ---------------------------------------------------------------

    def scroll_container(self, frame, cid: str | None, label: str, axis: str) -> None:
        """Step through one container until it is exhausted or its budget is spent."""
        geom = frame.evaluate(SCROLL_GEOM_JS, {"id": cid, "axis": axis})
        if not geom:
            return
        client = max(int(geom["client"]), 1)
        # Never step further than most of the container's own viewport, or content in
        # a short container would be jumped over without ever intersecting.
        step = SCROLL_STEP_PX if cid is None else max(40, min(SCROLL_STEP_PX, int(client * 0.8)))
        budget_px = self.opts.scroll_budget * client
        act = self.begin("scroll", label, 0, axis=axis, frame="main"
                         if frame is self.page.main_frame else frame.url)
        record = {"target": label, "axis": axis, "frame": act["frame"], "action_id": act["id"],
                  "step_px": step, "budget_px": budget_px, "start_size": geom["size"],
                  "steps": 0, "scrolled_px": 0, "budget_hit": False, "ended": None}
        self.scroll_containers.append(record)
        last_size = geom["size"]
        flat = stuck = 0
        n = 0
        while True:
            if record["scrolled_px"] >= budget_px:
                record["budget_hit"] = True
                record["ended"] = "budget"
                break
            if self.caps_reached():
                record["ended"] = "wall_clock"
                break
            n += 1
            # Each step is its own attribution unit, logged under the sweep's action.
            step_act = self.begin("scroll", label, n, counts=False, axis=axis,
                                  frame=act["frame"], sweep=act["id"])
            try:
                res = frame.evaluate(SCROLL_STEP_JS, {"id": cid, "axis": axis, "step": step})
            except Exception:  # noqa: BLE001
                res = None
            if res is None:
                record["ended"] = "container gone"
                break
            self.page.wait_for_timeout(SCROLL_PAUSE_MS)
            record["steps"] = n
            record["scrolled_px"] += max(0, int(res["moved"]))
            grew = res["size"] > last_size
            last_size = max(last_size, res["size"])
            stuck = stuck + 1 if res["moved"] <= 0 else 0
            if res["at_end"] or res["moved"] <= 0:
                flat = 0 if grew else flat + 1
            else:
                flat = 0
            step_act["size"] = res["size"]
            if flat >= SCROLL_NO_GROWTH_STEPS:
                record["ended"] = "end reached, no growth for 3 steps"
                break
            if stuck >= SCROLL_NO_GROWTH_STEPS and not grew:
                record["ended"] = "cannot scroll further"
                break
        # Let anything the last steps triggered land before the next action begins.
        self.settle(min_ms=SCROLL_PAUSE_MS, max_ms=3000)
        record["end_size"] = last_size
        act["steps"] = record["steps"]
        act["budget_hit"] = record["budget_hit"]
        self.check_navigation(act)
        self.absorb_text()

    def scroll_pass(self) -> None:
        # The document first. Re-swept in later passes only if it has grown.
        try:
            size = self.page.evaluate(
                "(document.scrollingElement || document.documentElement).scrollHeight")
        except Exception:  # noqa: BLE001
            size = 0
        if size > self.main_scrolled_to and not self.caps_reached():
            self.scroll_container(self.page.main_frame, None, "window", "y")
            self.main_scrolled_to = self.page.evaluate(
                "(document.scrollingElement || document.documentElement).scrollHeight")
        # Same-origin iframes: their documents.
        for frame in self._frames()[1:]:
            key = f"frame:{frame.url}"
            if key in self.processed_scroll or self.caps_reached():
                continue
            self.processed_scroll.add(key)
            try:
                self.scroll_container(frame, None, f"iframe {frame.url}", "y")
            except Exception:  # noqa: BLE001 - frame detached
                continue
        # Every scrollable element, both axes, in every same-origin frame.
        for frame in self._frames():
            self.prefix_seq += 1
            try:
                containers = frame.evaluate(SCROLLABLES_JS,
                                            {"prefix": f"r{self.run_no}f{self.prefix_seq}"})
            except Exception:  # noqa: BLE001
                continue
            for c in containers:
                for axis in ("y", "x"):
                    if not c[axis]:
                        continue
                    key = f"{c['id']}:{axis}"
                    if key in self.processed_scroll or self.caps_reached():
                        continue
                    self.processed_scroll.add(key)
                    try:
                        self.scroll_container(frame, c["id"], c["label"], axis)
                    except Exception:  # noqa: BLE001
                        continue
        try:
            self.page.evaluate("window.scrollTo(0, 0)")
            self.page.wait_for_timeout(SCROLL_PAUSE_MS)
        except Exception:  # noqa: BLE001
            pass

    # ---- hover ----------------------------------------------------------------

    def hover(self, frame, cand: dict, pass_no: int) -> None:
        act = self.begin("hover", cand["selector"], 1, element=cand["id"], name=cand["name"][:80],
                         via=cand["via"], sources=cand["sources"], frame=cand["frame"], **{"pass": pass_no})
        try:
            # Real pointer input, not dispatchEvent, so CSS :hover applies.
            frame.locator(f"[data-explore-id='{cand['id']}']").first.hover(timeout=CLICK_TIMEOUT_MS)
            self.page.wait_for_timeout(HOVER_MS)
        except Exception as exc:  # noqa: BLE001 - covered, moved, detached
            act["outcome"] = f"not hoverable ({type(exc).__name__})"
            return
        self.settle(min_ms=0, max_ms=3000)
        self.check_navigation(act)
        act["novel_text"] = len(self.absorb_text())
        act["new_requests"] = self.novel_content_requests(act["id"])
        act["outcome"] = "hovered"

    # ---- click ----------------------------------------------------------------

    def click_element(self, frame, cand: dict, pass_no: int) -> None:
        """Click repeatedly until a click produces no novel content, or 10 clicks on
        this element's signature, counted across re-renders.

        Novel = a content request URL, or a visible text fragment, not seen earlier in
        this session. A wrapping carousel changes the DOM on every click forever; it
        stops once it wraps back to slides already seen. A click that navigates is
        never a reveal, and ends this element.
        """
        locator = frame.locator(f"[data-explore-id='{cand['id']}']").first
        # recorded only once a click happens, so a node that was already gone does not
        # mark its signature as seen
        seen = self.sig_state.get(cand["sig"], {"clicks": 0, "novel": False})
        n = 0
        while seen["clicks"] < self.opts.max_clicks_per_element:
            n += 1
            if self.caps_reached():
                return
            try:
                state = locator.evaluate(CLICK_STATE_JS, timeout=CLICK_TIMEOUT_MS)
            except Exception as exc:  # noqa: BLE001
                state = f"gone ({type(exc).__name__})"
            if state.startswith("gone") and seen["novel"] and n > 1:
                # a control rebuilt after a click that revealed something (a load-more
                # replaced after each batch): carry on with the new node
                try:
                    new_id = frame.evaluate(FIND_BY_SIGNATURE_JS, [cand["sig"], cand["tag"]])
                except Exception:  # noqa: BLE001
                    new_id = None
                if new_id and new_id not in self.processed_click:
                    self.processed_click.add(new_id)
                    locator = frame.locator(f"[data-explore-id='{new_id}']").first
                    n -= 1
                    continue
            if state:
                if n == 1:
                    self.skipped_clicks.append({"target": cand["selector"], "name": own_name_of(cand),
                                                "reason": state})
                return
            before = self.visible_lines()
            route = self.route_before()
            act = self.begin("click", cand["selector"], n, element=cand["id"],
                             name=cand["name"][:80], via=cand["via"], sources=cand["sources"], frame=cand["frame"],
                             **{"pass": pass_no})
            try:
                act.update(locator.evaluate(DISPATCH_CLICK_JS, timeout=CLICK_TIMEOUT_MS))
            except Exception as exc:  # noqa: BLE001
                act["outcome"] = f"not clickable ({type(exc).__name__})"
                if n == 1:
                    self.unclickable.append({"target": cand["selector"], "name": own_name_of(cand),
                                             "reason": act["outcome"]})
                return
            self.sig_state[cand["sig"]] = seen
            seen["clicks"] += 1
            seen["novel"] = False
            self.settle()
            if self.check_navigation(act, route):
                act["outcome"] = ("route change; not a reveal" if act["id"] in self.route_change_actions
                                  else "navigated (blocked or reverted); not a reveal")
                self.absorb_text()
                return
            after = self.visible_lines()
            novel_text = after - self.seen_text
            self.absorb_text()
            novel_req = self.novel_content_requests(act["id"])
            act["novel_text"] = len(novel_text)
            act["new_requests"] = novel_req
            if novel_text or novel_req:
                act["outcome"] = "novel"
                seen["novel"] = True
                continue
            act["outcome"] = "not novel; element done"
            # A toggle's second click collapses what its first click revealed, which
            # would hide any controls inside the panel from the next discovery pass.
            # If this click mostly removed visible text, click once more to restore
            # the revealed state. Logged as its own (counted) action.
            lost = sum(len(x) for x in before - after)
            gained = sum(len(x) for x in after - before)
            if lost > 0 and lost > RESTORE_LOSS_RATIO * gained and not self.caps_reached():
                route = self.route_before()
                rest = self.begin("click", cand["selector"], n + 1, element=cand["id"],
                                  name=cand["name"][:80], via=cand["via"], sources=cand["sources"], frame=cand["frame"],
                                  restore=True, **{"pass": pass_no})
                try:
                    rest.update(locator.evaluate(DISPATCH_CLICK_JS, timeout=CLICK_TIMEOUT_MS))
                    self.settle()
                    if not self.check_navigation(rest, route):
                        restored = self.visible_lines()
                        rest["outcome"] = ("restored" if (before - after) <= restored
                                           else "restore attempted, state differs")
                        rest["novel_text"] = len(self.absorb_text())
                        rest["new_requests"] = self.novel_content_requests(rest["id"])
                    else:
                        rest["outcome"] = "navigated (blocked or reverted); not a reveal"
                except Exception as exc:  # noqa: BLE001
                    rest["outcome"] = f"restore failed ({type(exc).__name__})"
            return

    def veto(self, cand: dict) -> bool:
        """Apply the safety rules. True if the click is refused."""
        name = (cand.get("name") or "").strip()
        phrase = PHRASE_DENY.search(name)
        short = (SHORT_DENY.search(name) if len(name.split()) <= LABEL_MAX_WORDS else None)
        rule = None
        if phrase:
            safe, kind, rule = False, "safety-denylist", deny_rule("phrase", phrase.group(0))
            reason = f"phrase rule matched {phrase.group(0)!r} in this control's own name"
        elif cand.get("in_consent"):
            safe, kind, rule = False, "safety-denylist", "consent:banner"
            reason = "inside a cookie/consent banner"
        elif CONSENT_ACTIONS.search(name) and CONSENT_WORDS.search(name):
            safe, kind, rule = False, "safety-denylist", "consent:name"
            reason = "consent control (accept/agree/allow/reject with cookie/consent)"
        elif short:
            safe, kind, rule = False, "safety-denylist", deny_rule("short", short.group(0))
            reason = f"short-name rule matched {short.group(0)!r} in this control's own name"
        else:
            safe, reason, kind = click_is_safe(cand, self.settled_url,
                                               label_max_words=LABEL_MAX_WORDS)
            # A link to an image or video with its own click listener is a lightbox,
            # not a plain navigation. Click it: the navigation guard answers any
            # main-frame navigation with 204. The scheme and path rules ran before
            # this check, so they still apply.
            if (kind == "unsuitable" and reason == "link navigates away from the page under test"
                    and "cdp-listener-click" in (cand.get("via") or [])
                    and MEDIA_PATH.search(urllib.parse.urlsplit(cand.get("href") or "").path)
                    and (cand.get("target") or "").lower() != "_blank"):
                safe, reason, kind = True, "", "ok"
            if not safe:
                word = SAFETY_DENY.search(name) if kind == "safety-denylist" else None
                rule = deny_rule("word", word.group(0)) if word else kind
        if safe:
            return False
        record = {"target": cand["selector"], "tag": cand["tag"], "name": own_name_of(cand),
                  "nearby": cand["nearby"][:120], "href": (cand.get("href") or "")[:160],
                  "sources": cand.get("sources", []), "reason": reason, "kind": kind,
                  "rule": rule}
        if kind == "safety-denylist":
            term = PHRASE_DENY.search(name) or SHORT_DENY.search(name) \
                or SAFETY_DENY.search(own_name_of(cand))
            record["term"] = term.group(0).lower()[:40] if term else reason[:40]
            self.safety_skips.append(record)
        self.vetoes.append(record)
        return True

    # ---- driver ---------------------------------------------------------------

    def run(self) -> dict:
        match = self.opts.match_crawler
        ctx = self.browser.new_context(
            viewport=CRAWLER_VIEWPORT if match else VIEWPORT,
            user_agent=CRAWLER_UA if match else USER_AGENT,
            device_scale_factor=1 if match else None,
            extra_http_headers=CRAWLER_HEADERS if match else None,
            accept_downloads=False,
            java_script_enabled=True,
            # Requests a service worker makes for the page are invisible to page
            # request events; an oracle cannot afford a blind spot there.
            service_workers="block",
        )
        ctx.set_default_timeout(CLICK_TIMEOUT_MS)
        ctx.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        try:
            # 1. Baseline. The screener's loader: goto, networkidle, its own guard.
            page = load_page(ctx, self.url, self.throttle, self.result,
                             on_page=self._attach_network_cdp)
            self.page = page
            self.settled_url = self.result.settled_url or self.url
            self.target_norm = normalise_request(self.settled_url)
            # Everything requested during the load is baseline (action 0).
            for raw in self.result.requests_raw:
                if is_network_url(raw):
                    norm = normalise_request(raw)
                    rec = self.requests.setdefault(norm, {"first_action": 0, "raw": [],
                                                          "resource_type": "", "t": 0})
                    if raw not in rec["raw"] and (self.opts.all_raw_urls
                                                  or len(rec["raw"]) < RAW_URLS_PER_KEY):
                        rec["raw"].append(raw)
            self.raw_types.update(self.result.request_types)
            # Registered straight after load_page returns: the sync API dispatches
            # events only inside blocking calls, so there is no gap.
            page.on("request", self._on_request)
            page.on("requestfinished", self._on_request_done)
            page.on("requestfailed", self._on_request_done)
            ctx.on("page", self._on_popup)
            page.route("**/*", self._guard)
            self.guard_on = True
            # 1a. Periodic-fetch detection: sit idle and watch what the page fetches
            # with nobody touching it. Anything seen here is a timer, not a reveal,
            # and is excluded for the rest of the session. Done before the baseline
            # measurement so the baseline is taken on a page whose timers are known.
            self.recording_periodic = True
            page.wait_for_timeout(int(self.opts.periodic_window * 1000))
            self.recording_periodic = False
            self.periodic_patterns = set(self.periodic_window_hits)
            page.wait_for_timeout(int(BASELINE_SETTLE_S * 1000))
            self.baseline_window = (self.started, time.time())
            self.absorb_text()
            self.novel_text_log.clear()        # baseline text is not "novel"
            baseline_norm = sorted(u for u, r in self.requests.items() if r["first_action"] == 0)

            # 2-5. Scroll, hover, click; re-discover until nothing new.
            listener_stats = self.mark_listener_elements()
            self.listener_scans.append(listener_stats)
            self.write_guard_on = True
            self.stub_window_open()
            passes_cap_hit = False
            for pass_no in range(1, self.opts.max_passes + 2):
                if pass_no > 1:
                    listener_stats = self.mark_listener_elements()
                    self.listener_scans.append(listener_stats)
                cands = self.discover()
                new_hover = [(f, c) for f, c in cands
                             if c["hover_ok"] and c["id"] not in self.processed_hover]
                new_click = [(f, c) for f, c in cands
                             if c["click_ok"] and c["id"] not in self.processed_click]
                for _f, c in cands:
                    if c["click_ok"]:
                        self.click_candidates_seen.setdefault(c["id"], c["sources"])
                if pass_no > self.opts.max_passes:
                    # One discovery beyond the last pass, only to know whether the pass
                    # cap is what ended exploration.
                    passes_cap_hit = bool(new_hover or new_click)
                    break
                info = {"pass": pass_no, "candidates": len(cands), "new_hover": len(new_hover),
                        "new_click": len(new_click)}
                self.discovery_passes.append(info)
                scroll_before = len(self.scroll_containers)
                # scroll_pass checks the cap only where there is something left to scroll,
                # so a phase that used its budget exactly is not reported as capped
                self.enter("scroll")
                self.scroll_pass()
                self.leave()
                info["scroll_sweeps"] = len(self.scroll_containers) - scroll_before
                if pass_no > 1 and not new_hover and not new_click and not info["scroll_sweeps"]:
                    info["result"] = "nothing new; done"
                    break
                self.enter("hover")
                for frame, cand in new_hover:
                    if self.caps_reached():
                        break
                    self.processed_hover.add(cand["id"])
                    self.hover(frame, cand, pass_no)
                self.leave()
                self.enter("click")
                for frame, cand in new_click:
                    if self.caps_reached():
                        break
                    self.processed_click.add(cand["id"])
                    seen = self.sig_state.get(cand["sig"])
                    if seen and not (seen["novel"] and seen["clicks"] < self.opts.max_clicks_per_element):
                        # the same control under a new node, and clicking it stopped revealing
                        self.skipped_clicks.append({"target": cand["selector"], "name": own_name_of(cand),
                                                    "reason": "seen"})
                        continue
                    if self.veto(cand):
                        continue
                    self.click_element(frame, cand, pass_no)
                self.leave()
                if self.cap_hit or all(self.phase_caps.values()):
                    break
            self.donate_to_click()
            # Anything still loading lands under the last action.
            self.settle(min_ms=2000, max_ms=8000)
            self.blocked_seen = self.blocked_norms()
            capped = [f"{p}:{c}" for p, c in self.phase_caps.items() if c]
            ended_by = (self.cap_hit or ",".join(capped)
                        or ("discovery_passes" if passes_cap_hit else None))
            return self.report(baseline_norm, ended_by)
        finally:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass

    def size_record(self, url: str, rule: str) -> dict:
        m = self.result.response_meta.get(url) or {}
        return {"url": url, "rule": rule, "size": m.get("size"),
                "content_type": m.get("content_type", ""), "status": m.get("status")}

    def click_source_stats(self) -> dict:
        """What each discovery source contributed: candidates, clicks, reveals.

        Sources overlap (a <button> with a click listener and a pointer cursor is all
        three), so `only_source` counts the elements a source found that no other
        source did. That is the number that says whether cursor:pointer earns its
        place on a given page.
        """
        stats: dict[str, dict] = {}
        elements: dict[str, dict] = {}
        for act in self.actions:
            if act["action"] != "click" or not act.get("element"):
                continue
            el = elements.setdefault(act["element"], {
                "sources": act.get("sources") or [], "clicks": 0, "novel": 0})
            el["clicks"] += 1
            if act.get("new_requests") or act.get("novel_text"):
                el["novel"] += 1
        blank = {"candidates": 0, "only_source": 0, "elements_clicked": 0, "clicks": 0,
                 "elements_revealing": 0}
        for cand in self.click_candidates_seen.values():
            for src in cand or ["unknown"]:
                row = stats.setdefault(src, dict(blank))
                row["candidates"] += 1
                if len(cand) == 1:
                    row["only_source"] += 1
        for el in elements.values():
            for src in el["sources"] or ["unknown"]:
                row = stats.setdefault(src, dict(blank))
                row["elements_clicked"] += 1
                row["clicks"] += el["clicks"]
                if el["novel"]:
                    row["elements_revealing"] += 1
        return stats

    def click_reachability(self) -> dict:
        """How many dispatched clicks a real pointer would not have reached, and what
        was on top of the element instead."""
        clicks = [a for a in self.actions if a["action"] == "click" and "reachable" in a]
        blocked = [a for a in clicks if not a["reachable"]]
        return {"clicks": len(clicks), "unreachable": len(blocked),
                "by_interceptor": dict(Counter(a["interceptor"] for a in blocked).most_common(10))}

    def report(self, baseline_norm: list[str], ended_by: str | None) -> dict:
        meta = self.result.response_meta
        by_id = {a["id"]: a for a in self.actions}
        baseline_content, baseline_noise, baseline_size = [], [], []
        for u in baseline_norm:
            rule, category, _ = self.noise.classify(u, meta.get(u))
            if not rule:
                baseline_content.append(u)
            elif category == "size":
                baseline_size.append(self.size_record(u, rule))
            else:
                baseline_noise.append({"url": u, "rule": rule})
        new_content: dict[str, dict] = {}
        noise_filtered: list[dict] = []
        size_filtered: list[dict] = []
        excluded_nav: list[dict] = []
        time_on_page: list[dict] = []
        exempted: list[dict] = []
        for u, rec in sorted(self.requests.items()):
            aid = rec["first_action"]
            if aid == 0:
                continue
            act = by_id.get(aid, {})
            attribution = {"action": act.get("action"), "target": act.get("target"),
                           "iteration": act.get("iteration"), "action_id": aid}
            if "pass" in act:
                attribution["pass"] = act["pass"]
            if act.get("sources"):
                attribution["sources"] = act["sources"]
            if act.get("sweep"):
                attribution["sweep"] = act["sweep"]
            if act.get("action") == "click" and act.get("outcome", "").startswith("not clickable"):
                # The click never happened: whatever arrived while it was attempted
                # is the page's own doing over time, not a reveal.
                time_on_page.append({"url": u, **attribution})
                continue
            if aid in self.nav_excluded_actions:
                excluded_nav.append({"url": u, **attribution,
                                     "reason": ("route change" if aid in self.route_change_actions
                                                else "navigation")})
                continue
            if u in self.blocked_seen:
                # Never fetched: the blocker aborted it. Out of the pool, and listed
                # under blocked_ads with the list and rule that matched.
                continue
            rule, category, would_have = self.noise.classify(u, meta.get(u))
            if rule and self.noise.mode == "enforce":
                (size_filtered if category == "size" else noise_filtered).append(
                    {**self.size_record(u, rule), **attribution} if category == "size"
                    else {"url": u, "rule": rule, **attribution})
                continue
            if rule:
                # Advisory mode: the noise rules no longer decide pool membership,
                # the blocker does. What they would still have filtered is recorded
                # instead, because that is the measure of what the blocker missed.
                (size_filtered if category == "size" else noise_filtered).append(
                    {**self.size_record(u, rule), "advisory": True, **attribution})
            if would_have:
                exempted.append({"url": u, "rule_exempted": would_have})
            new_content[u] = {"attribution": attribution, "raw": rec["raw"],
                              "resource_type": rec["resource_type"],
                              "response": meta.get(u)}
        # Map each block onto the action that was running when it happened.
        starts = [(a["t_start"], a["id"]) for a in self.actions]
        blocked = []
        for b in self.result.blocked_requests:
            offset = b.get("t", 0) - self.started
            aid = 0
            for t_start, action_id in starts:
                if t_start <= offset:
                    aid = action_id
            act = by_id.get(aid, {})
            blocked.append({k: v for k, v in b.items() if k != "t"}
                           | {"action": act.get("action", "baseline"),
                              "target": act.get("target"), "action_id": aid})
        counted = [a for a in self.actions if a["action"] in ("hover", "click")
                   or (a["action"] == "scroll" and a["iteration"] == 0)]
        return {
            "run": self.run_no,
            "settled_url": self.settled_url,
            "http_status": self.result.status,
            "baseline_window": [round(self.baseline_window[0], 3), round(self.baseline_window[1], 3)],
            "baseline_content": baseline_content,
            # Every baseline request as requested (query kept), before the noise rules,
            # with its status: lets a caller compare at full-URL granularity against an
            # instrument that applies no noise rules. raw is capped at 5 per path.
            "baseline_raw": sorted({raw for u in baseline_norm for raw in self.requests[u]["raw"]}),
            "baseline_status": {u: (meta.get(u) or {}).get("status") for u in baseline_norm},
            "raw_types": dict(sorted(self.raw_types.items())),
            "request_meta": dict(sorted(self.request_meta.items())),
            "blocked_writes": self.blocked_writes,
            "blocked_opens": self.blocked_opens,
            "allowed_writes": self.allowed_writes,
            "baseline_noise": baseline_noise,
            "new_content": new_content,
            "baseline_size_filtered": baseline_size,
            "noise_mode": self.noise.mode,
            "noise_filtered": noise_filtered,
            "size_filtered": size_filtered,
            "noise_exempted": exempted,
            "excluded_navigation_side_effects": excluded_nav,
            "time_on_page": time_on_page,
            "all_requests_raw": sorted({raw for r in self.requests.values() for raw in r["raw"]}),
            "caps": {"ended_by": ended_by, "actions_used": self.action_count,
                     "max_actions": self.opts.max_actions, "max_seconds": self.opts.max_seconds,
                     "max_passes": self.opts.max_passes,
                     "phase_caps": self.phase_caps,
                     "phase_budgets": self.budget,
                     "phase_spent": {p: {"actions": v["actions"], "seconds": round(v["seconds"], 1)}
                                     for p, v in self.spent.items()},
                     "donation_to_click": self.donation},
            "blocked_ads": blocked,
            "blocked_ads_total": len(self.result.blocked_requests),
            "periodic_patterns": sorted(self.periodic_patterns),
            "periodic_excluded": {"requests": sum(self.periodic_excluded.values()),
                                  "by_url": dict(sorted(self.periodic_excluded.items()))},
            "click_sources": self.click_source_stats(),
            "scroll_containers": self.scroll_containers,
            "scroll_budget_hits": [c["target"] + f" ({c['axis']})"
                                   for c in self.scroll_containers if c["budget_hit"]],
            "discovery_passes": self.discovery_passes,
            "listener_scans": self.listener_scans,
            "safety_vetoes": self.vetoes,
            "safety_skips_gate": self.safety_skips,
            "unclickable": self.unclickable,
            "skipped_clicks": self.skipped_clicks,
            "click_reachability": self.click_reachability(),
            "blocked_navigations": self.blocked_navigations,
            "popups_closed": self.popups,
            "soft_navigations": self.soft_navigations,
            "state_url_changes": self.state_url_changes,
            "action_counts": {k: sum(1 for a in counted if a["action"] == k)
                              for k in ("scroll", "hover", "click")},
            "actions": self.actions,
            "novel_text_fragments": self.novel_text_log,
            "notes": self.result.notes,
            "elapsed_s": round(time.time() - self.started, 1),
        }


# --- per page -----------------------------------------------------------------

def explore_page(pw, url: str, page_id: str, throttle: HostThrottle, robots_cache: RobotsCache,
                 opts, noise: NoiseFilter) -> dict:
    started = time.time()
    robots = robots_cache.check(url)
    base = {"page_id": page_id, "url": url, "robots": robots,
            "parameters": {"scroll_budget_viewports": opts.scroll_budget,
                           "max_actions": opts.max_actions, "max_seconds_per_run": opts.max_seconds,
                           "max_clicks_per_element": opts.max_clicks_per_element,
                           "max_discovery_passes": opts.max_passes, "runs": opts.runs,
                           "periodic_window_s": opts.periodic_window,
                           "noise_mode": opts.noise_mode,
                           "block_ads": bool(opts.block_ads),
                           "action_accounting": ACTION_ACCOUNTING,
                           "noise_exemptions": [{"prefix": p, "reason": r}
                                                for p, r in noise.exemptions]}}
    if not robots["allowed"]:
        return {**base, "explored": False,
                "skip_reason": "robots.txt disallows this path for our User-Agent"}
    runs = []
    for run_no in range(1, opts.runs + 1):
        print(f"    run {run_no}/{opts.runs} ...", flush=True)
        # A fresh browser per run, as the screener does per URL: a dead network
        # service must not take the second run down with the first.
        if opts.match_crawler:
            # full Chromium (new headless) rather than the headless shell; the crawler
            # runs headful under Xvfb, which a desktop run cannot reproduce
            browser = pw.chromium.launch(headless=not opts.headed, channel="chromium",
                                         args=CRAWLER_ARGS,
                                         ignore_default_args=["--enable-automation",
                                                              "--hide-scrollbars"])
        else:
            browser = pw.chromium.launch(headless=not opts.headed)
        try:
            runs.append(Explorer(browser, url, throttle, opts, noise, run_no).run())
        except Exception as exc:  # noqa: BLE001 - one bad page must not end the batch
            runs.append({"run": run_no, "error": f"{type(exc).__name__}: {str(exc)[:300]}"})
        finally:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass
        r = runs[-1]
        if "error" in r:
            print(f"      ERROR: {r['error']}", flush=True)
        else:
            print(f"      baseline {len(r['baseline_content'])}, new {len(r['new_content'])}, "
                  f"actions {r['caps']['actions_used']} {r['action_counts']}, "
                  f"ended_by {r['caps']['ended_by']}, {r['elapsed_s']}s", flush=True)

    ok = [r for r in runs if "error" not in r]
    if len(ok) < len(runs) or not ok:
        return {**base, "explored": False, "skip_reason": "exploration run failed",
                "runs": runs, "elapsed_s": round(time.time() - started, 1)}

    # Anything that loaded without interaction in EITHER run is not interaction-gated.
    baseline_union = set().union(*(set(r["baseline_content"]) for r in ok))
    news = [{u: v for u, v in r["new_content"].items() if u not in baseline_union} for r in ok]
    stable_urls = set(news[0]).intersection(*news[1:]) if len(news) > 1 else set(news[0])
    all_new = set().union(*news)
    stable = {u: {f"run{i + 1}": news[i][u]["attribution"] for i in range(len(news))}
              | {"response": news[0][u]["response"]}
              for u in sorted(stable_urls)}
    unstable = {u: {"runs": [i + 1 for i in range(len(news)) if u in news[i]],
                    "attribution": next(news[i][u]["attribution"]
                                        for i in range(len(news)) if u in news[i])}
                for u in sorted(all_new - stable_urls)}
    noise = {}
    for r in ok:
        for entry in r["noise_filtered"]:
            noise.setdefault(entry["url"], {"rule": entry["rule"], "runs": []})["runs"].append(r["run"])
    # Removed by the response-size rule alone: reviewable, and out of the pool. The
    # screener has dropped real content this way (a 347-byte page-data.json, a
    # 1013-byte loader SVG), so these are listed with size and content-type rather
    # than counted in with the cookie syncs.
    size_filtered = {}
    for r in ok:
        for entry in r["size_filtered"]:
            row = size_filtered.setdefault(entry["url"], {
                "rule": entry["rule"], "size": entry["size"],
                "content_type": entry["content_type"], "status": entry["status"],
                "attribution": {k: entry.get(k) for k in ("action", "target", "iteration")},
                "runs": []})
            row["runs"].append(r["run"])
    # Union over runs: a source's contribution is about what it can find at all.
    sources: dict[str, dict] = {}
    for r in ok:
        for src, vals in r["click_sources"].items():
            row = sources.setdefault(src, {})
            for k, v in vals.items():
                row[k] = max(row.get(k, 0), v)
    # Ads and trackers the blocker aborted: never fetched, so never in the pool.
    blocked_ads: dict[str, dict] = {}
    for r in ok:
        for b in r["blocked_ads"]:
            row = blocked_ads.setdefault(b["url"], {
                "list": b["list"], "rule": b["rule"], "resource_type": b["resource_type"],
                "action": b.get("action"), "runs": []})
            if r["run"] not in row["runs"]:
                row["runs"].append(r["run"])
    periodic = sorted(set().union(*(set(r["periodic_patterns"]) for r in ok)))
    by_action = {}
    for u, v in stable.items():
        kind = v["run1"]["action"]
        by_action[kind] = by_action.get(kind, 0) + 1
    ended = sorted({r["caps"]["ended_by"] for r in ok if r["caps"]["ended_by"]})
    return {
        **base,
        "explored": True,
        "baseline": sorted(baseline_union),
        "stable_new_content": stable,
        "unstable_new_content": unstable,
        "noise_filtered": [{"url": u, **v} for u, v in sorted(noise.items())],
        "size_filtered": [{"url": u, **v} for u, v in sorted(size_filtered.items())],
        "blocked_ads": [{"url": u, **v} for u, v in sorted(blocked_ads.items())],
        "blocked_ads_requests": sum(r["blocked_ads_total"] for r in ok),
        "periodic_patterns": periodic,
        "periodic_excluded_requests": sum(r["periodic_excluded"]["requests"] for r in ok),
        "click_sources": sources,
        "stable_by_action": by_action,
        "caps_hit": ended,
        "scroll_budget_hits": {f"run{r['run']}": r["scroll_budget_hits"] for r in ok},
        "safety_vetoes": {f"run{r['run']}": r["safety_vetoes"] for r in ok},
        "safety_skips_gate": [sk for r in ok for sk in r["safety_skips_gate"]],
        "blocked_navigations": {f"run{r['run']}": r["blocked_navigations"] for r in ok},
        "runs": runs,
        "elapsed_s": round(time.time() - started, 1),
    }


# --- output -------------------------------------------------------------------

def write_summary(rows: list[dict], path: Path) -> None:
    lines = [
        "# Exploration oracle summary",
        "",
        "`stable` = new content URLs (not in either baseline, noise-filtered) found in both "
        "runs; this is what the pool uses. `unstable` = found in one run only.",
        "",
        "`size` counts requests the response-size rule (under 1KB) accounts for on its "
        "own: reviewable small content, not cookie syncs. `cursor-only` counts click "
        "candidates that ONLY the cursor:pointer source found, and how many were clicked.",
        "",
        "`blocked` = ads/trackers aborted by EasyList+EasyPrivacy (never fetched, never "
        "in the pool). `noise`/`size` = what the screener's rules would still filter on "
        "top of that, which is the measure of what the blocker missed; in the default "
        "advisory mode they are reported, not removed. `periodic` = timer-driven request "
        "patterns seen while the page sat idle, and the requests excluded because of them.",
        "",
        "| page | baseline | stable new | by action (scroll/hover/click) | unstable | "
        "blocked | noise adv | size adv | periodic pat/excl | cursor-only cand/clicked | "
        "actions r1/r2 | caps hit | scroll budget hits | vetoes (denylist) | navs blocked | "
        "elapsed |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        if not row.get("explored"):
            lines.append(f"| {row['page_id']} |" + " — |" * 14 +
                         f" **skipped**: {row.get('skip_reason')} |")
            continue
        ok = [r for r in row["runs"] if "error" not in r]
        ba = row["stable_by_action"]
        budget = sum(len(v) for v in row["scroll_budget_hits"].values())
        vetoes = sum(len(v) for v in row["safety_vetoes"].values())
        navs = sum(len(v) for v in row["blocked_navigations"].values())
        cp = row.get("click_sources", {}).get("cursor-pointer", {})
        lines.append(
            f"| {row['page_id']} | {len(row['baseline'])} | {len(row['stable_new_content'])} | "
            f"{ba.get('scroll', 0)}/{ba.get('hover', 0)}/{ba.get('click', 0)} | "
            f"{len(row['unstable_new_content'])} | "
            f"{len(row['blocked_ads'])} ({row['blocked_ads_requests']}) | "
            f"{len(row['noise_filtered'])} | {len(row['size_filtered'])} | "
            f"{len(row['periodic_patterns'])}/{row['periodic_excluded_requests']} | "
            f"{cp.get('only_source', 0)}/{cp.get('elements_clicked', 0)} | "
            f"{'/'.join(str(r['caps']['actions_used']) for r in ok)} | "
            f"{', '.join(row['caps_hit']) or 'none'} | {budget} | "
            f"{vetoes} ({len(row['safety_skips_gate'])}) | {navs} | {row['elapsed_s']}s |")
    path.write_text("\n".join(lines) + "\n")


def load_targets(args) -> list[tuple[str, str, str]]:
    """(page_id, url, group)."""
    if args.corpus_manifest:
        manifest = json.loads(Path(args.corpus_manifest).read_text())
        base = args.corpus_base.rstrip("/")
        out = [(p["id"], f"{base}/{p['path']}", "corpus") for p in manifest["pages"]]
        if args.only:
            out = [t for t in out if t[0] in set(args.only)]
        return out
    if args.urls_md:
        return [(page_id_for(u), u, g) for u, g, _ in parse_urls_markdown(Path(args.urls_md))]
    if args.urls:
        urls = [ln.strip() for ln in Path(args.urls).read_text().splitlines()
                if ln.strip() and not ln.strip().startswith("#")]
        return [(page_id_for(u), u, "?") for u in urls]
    return [(page_id_for(u), u, "?") for u in args.url]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", action="append", default=[], help="explore this URL (repeatable)")
    ap.add_argument("--urls", help="file with one URL per line")
    ap.add_argument("--urls-md", help="grouped markdown list, as the screener reads it")
    ap.add_argument("--corpus-manifest", help="interaction-corpus manifest.json; page ids from it")
    ap.add_argument("--corpus-base", default="http://127.0.0.1:8000",
                    help="where serve.py is serving the corpus")
    ap.add_argument("--only", action="append", default=[], help="corpus page id (repeatable)")
    ap.add_argument("--out", default=str(PKG_ROOT / "explore" / "out"))
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--runs", type=int, default=RUNS)
    ap.add_argument("--scroll-budget", type=float, default=SCROLL_BUDGET_VIEWPORTS,
                    help="viewport heights per scroll container (default 50)")
    ap.add_argument("--max-actions", type=int, default=MAX_ACTIONS)
    ap.add_argument("--max-seconds", type=float, default=MAX_SECONDS)
    ap.add_argument("--max-clicks-per-element", type=int, default=MAX_CLICKS_PER_ELEMENT)
    ap.add_argument("--max-passes", type=int, default=MAX_DISCOVERY_PASSES)
    ap.add_argument("--periodic-window", type=float, default=PERIODIC_WINDOW_S,
                    help="seconds to sit idle before baseline, recording the page's own "
                         "timer-driven requests (default 30)")
    ap.add_argument("--match-crawler", action="store_true",
                    help="mirror Browsertrix 1.14.3's browser: 1360x1020 viewport, scale 1, "
                         "its user agent and sec-ch-ua headers, its Chromium launch flags")
    ap.add_argument("--all-raw-urls", action="store_true",
                    help=f"keep every raw URL per origin+path key, not just {RAW_URLS_PER_KEY}")
    ap.add_argument("--min-host-interval", type=float, default=screen.MIN_HOST_INTERVAL,
                    help="seconds between page loads to one host, including reloads "
                         "(default: the screener's, 5)")
    ap.add_argument("--no-block-ads", dest="block_ads", action="store_false",
                    help="do not block ads and trackers (default: block, with Brave's own "
                         "filter lists, so the pool matches what a Browsertrix crawl - "
                         "which runs Brave with shields on - was able to request)")
    ap.add_argument("--brave-lists", default=None,
                    help=f"directory holding Brave's filter lists and their manifest "
                         f"(default: {blocking.LISTS_DIR}); populate it with "
                         f"tools/sync_brave_lists.py")
    ap.add_argument("--noise-mode", choices=("advisory", "enforce"), default="advisory",
                    help="advisory (default): the blocker decides what is out of the pool, "
                         "and the screener's noise rules only report what they would still "
                         "have filtered - which is the measure of what the blocker missed. "
                         "enforce: the noise rules also remove, as the screener does")
    ap.add_argument("--no-cursor-pointer", dest="cursor_pointer", action="store_false",
                    help="drop the cursor:pointer candidate source (native controls and "
                         "CDP listeners only)")
    ap.add_argument("--noise-exempt", action="append", default=[], metavar="URL_PREFIX",
                    help="exempt URLs with this prefix from the noise rules (repeatable; "
                         "each needs a matching --noise-exempt-reason)")
    ap.add_argument("--noise-exempt-reason", action="append", default=[], metavar="TEXT")
    ap.add_argument("--acknowledge-skips", action="store_true",
                    help="continue past the safety-veto halt gate (read the vetoes first)")
    args = ap.parse_args()

    if len(args.noise_exempt) != len(args.noise_exempt_reason):
        print("every --noise-exempt needs its own --noise-exempt-reason")
        return 2
    noise = NoiseFilter(list(zip(args.noise_exempt, args.noise_exempt_reason)),
                        mode=args.noise_mode)

    targets = load_targets(args)
    if not targets:
        print("nothing to explore: give --url, --urls, --urls-md or --corpus-manifest")
        return 2
    if CONTACT_PLACEHOLDER in CONTACT_URL or not CONTACT_URL.startswith("http"):
        print("REFUSING TO RUN: CONTACT_URL in tools/screen.py is not set to a real page.")
        return 2

    blocker_info = {"enabled": False}
    if args.block_ads:
        # Set on the screener module: its load_page installs the route before the
        # first navigation, so the baseline load is blocked on the same terms as
        # everything after it.
        try:
            screen.BLOCKER = blocking.Blocker(args.brave_lists)
        except blocking.BraveListsMissing as exc:
            print(exc)
            return 2
        blocker_info = {"enabled": True, **screen.BLOCKER.describe()}
        print("ad/tracker blocking ON (Brave's own lists, copied from "
              f"{screen.BLOCKER.manifest.get('source_profile', '?')}):", flush=True)
        for name, info in screen.BLOCKER.list_info.items():
            print(f"    {name} {info['version']}: {info['list_title']} "
                  f"({info['rules']} rules)", flush=True)
    else:
        print("ad/tracker blocking OFF (--no-block-ads)", flush=True)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    throttle = HostThrottle(args.min_host_interval)
    robots_cache = RobotsCache(PKG_ROOT / "cache" / "robots")
    seen_gate_keys: set[tuple[str, str]] = set()
    rows: list[dict] = []

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        for i, (page_id, url, group) in enumerate(targets, 1):
            print(f"[{i}/{len(targets)}] {page_id}  {url}", flush=True)
            row = explore_page(pw, url, page_id, throttle, robots_cache, args, noise)
            row["blocking"] = blocker_info
            row["group"] = group
            rows.append(row)
            (out_dir / f"{page_id}.json").write_text(json.dumps(row, indent=2, default=str))
            write_summary(rows, out_dir / "summary.md")

            # The screener's gate, unchanged in spirit: halt on the first new
            # (term, group) denylist veto, so a human reads what was refused.
            skips = row.get("safety_skips_gate") or []
            new_keys = sorted({(sk.get("term", "?"), group) for sk in skips} - seen_gate_keys)
            seen_gate_keys.update(new_keys)
            if new_keys and not args.acknowledge_skips:
                print("\n" + "=" * 72)
                print("HALTED: the click safety filter vetoed elements on this page.")
                print(f"  new (term, group): {', '.join(f'{t}@{g}' for t, g in new_keys)}")
                for sk in skips:
                    print(f"  [{sk['kind']}] <{sk['tag']}> {sk['name']!r}  reason: {sk['reason']}")
                    if sk.get("nearby"):
                        print(f"      nearby: {sk['nearby']!r}")
                print("=" * 72)
                print("Results so far are written. Re-run with --acknowledge-skips to continue.")
                return 3
    print(f"\nwrote {len(rows)} page file(s) and {out_dir / 'summary.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())