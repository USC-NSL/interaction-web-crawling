# Page screener

Classifies real web pages by **what a user interaction reveals on them**, so that pages can
be chosen for a later crawl-comparison study on the basis of what they actually gate rather
than on what they look like.

Separate from `../interaction-corpus/`, which is the synthetic ground-truth corpus. This
tool touches nothing in there.

## Run

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium

.venv/bin/python -u tools/screen.py                       # the seed list
.venv/bin/python -u tools/screen.py --url https://example.com/page
.venv/bin/python -u tools/screen.py --urls urls.txt --out out
.venv/bin/python -u tools/screen.py --headed              # watch it work
.venv/bin/python -u tools/screen.py --skip-stability      # faster, leaves stability unreported
```

Use `-u` (unbuffered): a run takes minutes per URL and you want to see progress.

Writes `out/results.json` (full detail) and `out/results.md` (the table), rewriting both
after every URL so a long run is never lost.

## Passes

Six page loads per URL, each in a fresh browser context:

| pass | what it does | what it feeds |
|---|---|---|
| `baseline` | load, networkidle, wait 10s, touch nothing | the reference for every delta |
| `scroll` | scroll to bottom in 400px steps, 300ms pauses, back to top | `mechanisms_present` |
| `hover` | hover up to 12 nav/card candidates for 500ms each | `mechanisms_present` |
| `click` | click up to 15 safe controls once each | `mechanisms_present` |
| `interacted` | scroll, then hover, then click, in that order | `reveal_channel` |
| `baseline-2` | a second untouched load, ≥60s after the first | `stability` |

The three single-mechanism passes exist because a combined pass cannot attribute a delta to
the action that caused it. Attribution matters more than speed here, so they run on separate
loads rather than in sequence on one.

## Markup quality

Counts, page-wide, the things that make a control discoverable from the accessibility tree:
`aria-expanded`, `role="tab"`, `role="tabpanel"`, `<details>`, **and native controls
carrying an accessible name** (`button`, `summary`, `a[href]`, `input[type=button|submit]`,
`[role=button|tab]`). Separately it counts click handlers on `<div>`/`<span>` that have no
ARIA and no accessible name, using CDP `DOMDebugger.getEventListeners` rather than guessing
from `cursor: pointer`.

That last inclusion matters. Counting only ARIA attributes scored Slick — whose arrows are
`<button class="slick-prev" aria-label="Previous">Previous</button>` — as having no controls
at all, while the same page produced twelve network reveals by clicking them. Any JS-heavy
page built from plain named buttons would have read `none-detected`, and the
accessible-versus-inert hypothesis would have been untestable exactly where it is most
interesting.

`controls_actually_clicked` breaks down the controls the click pass actually operated
(`native_with_name` / `native_without_name` / `nonnative_inert`), which is the direct
evidence for the hypothesis: were the controls that revealed content discoverable without
looking at pixels?

That field is permanent, and it has already earned it. Owlcarousel reads `mixed` page-wide,
but the eleven controls that actually revealed its images were native buttons whose label
sits on an inner `<span>` — so every control that did the revealing was **unnamed**. The
page-wide counts alone would have hidden the exact distinction the accessible-versus-inert
hypothesis rests on.

## Reveal channels

- `network` — the interacted pass requested URLs the baseline did not
- `dom-appended` — DOM node count grew
- `dom-hidden` — node count unchanged, but text became visible (CSS was hiding it)
- `none` — nothing changed

All that apply are emitted, each with its magnitude. Two measurement choices worth knowing:

**Requests are compared on origin+path, not the full URL.** Real pages append cache-busting
and analytics parameters, so a raw URL-set comparison reports every beacon as a new request.
The raw URLs are still kept in the JSON.

**Noise is identified structurally, not by a host list alone.** Three rules, in order of
confidence: a known ad/analytics **host**; a cookie-sync or pixel **path** shape (`/sync`,
`/cm/`, `/match`, `/pixel`, `/usersync`); or a **response** that is a 3xx redirect or a body
under 1KB. Cookie syncs are redirects or tiny; a real lazy image is kilobytes. The rules
layer — `ad.360yield.com/server_match` slips past the path rule but is caught at 500 bytes.
Every filtered request is logged in `noise_filtered` with the rule that filtered it, so the
filter stays auditable.

The criterion is deliberately **not** first-party-only: owlcarousel's genuine lazy images
come from `placehold.it`, a third-party host, and a first-party rule would discard the
cleanest known-answer page in the set.

**`composition` makes a magnitude interpretable.** Every page carries
`{first_party, third_party, images, dominant_third_party_host}`. Without it, `network (190)`
looked like a huge reveal when 123 of those requests were an embedded newsletter widget, and
`network (8)` looked small when all eight were genuine lazy images.

**`dom-hidden` uses new text characters, not a length delta.** A carousel that swaps one
caption for another of similar length reveals real content while the length delta reads
about zero. The measure is the number of characters on lines present after the interaction
that were not present before; `TEXT_NOISE_FLOOR` (40 chars) keeps clocks and view counters
from registering as reveals. The signed length delta is reported alongside it.

## Safety and etiquette

Non-negotiable, and enforced in code rather than by convention:

- **One page in flight at a time**, and at least 5s between page loads to the same host
  (`MIN_HOST_INTERVAL`). Screening is sequential, which is what makes the one-per-host rule
  automatic.
- **A descriptive research User-Agent** identifying this as academic research and stating the
  rate limit. **Set `CONTACT_URL` in `tools/screen.py` before any real-page run** — it ships as
  the placeholder `https://example.edu/REPLACE-ME-before-running`, and the tool prints a
  warning banner while it stays that way. It must be a URL a site operator can read to find
  out who is requesting and why, not an email address.
- **robots.txt is fetched once per host, cached to `cache/robots/`, and honoured.** A
  disallowed path is recorded with its rules and never loaded.
- **Clicks are filtered by a denylist** covering buy / checkout / subscribe / unsubscribe /
  delete / post / submit / sign in / add to cart and neighbours, matched against both the
  element's accessible name and its surrounding text. Anything inside a `<form>` is skipped,
  as is any link that would navigate off the page under test or open a new tab. Every skip is
  logged with the reason. When unsure, it skips.
- **Clicks are capped at 15 per page** (and 40 attempts, since the click cap counts only
  successes), and clicking stops early if a navigation happens.
- **The safety filter halts the run the first time it vetoes anything.** The denylist has
  never fired on a real page — the validation set is documentation and demo sites with
  nothing transactional on them — so rather than assume it behaves, the run stops on the
  first page with a non-empty safety-skip log, prints each vetoed element with its name,
  surrounding text and reason, and exits 3. Read them, then re-run with
  `--acknowledge-skips` to continue. Skips are split by kind: `safety-denylist` and
  `safety-form` trip the gate; `unsuitable` (navigates away, opens a tab) and `unclickable`
  (element never became actionable) do not.
- **Nothing is ever authenticated.** Login walls are detected and reported, never bypassed.
- Popups are closed on sight and dialogs dismissed; downloads are refused.

## Stability

A page is usable only if two untouched loads 60s apart agree, because the two crawl
conditions in the later study will be hours apart and any drift between them is
uninterpretable. The test is deliberately asymmetric:

- **Content request sets must match exactly.** This is the primary criterion. Ad and
  analytics hosts are excluded first — they change every load by design, and comparing them
  marked every commercial page unstable for reasons unrelated to content.
- **Text may drift** by under 0.5% **or** under 200 characters absolute, whichever is more
  permissive. A percentage alone drifts with page size: 0.5% is 100 characters on a small
  page and 2000 on a large one.

`results.json` always keeps the raw numbers — `inner_text_len_delta`, `text_delta_pct`,
`text_delta_abs`, `content_requests_differing` — never only the boolean.

### Overriding a stability verdict

`STABILITY_OVERRIDES` in `tools/screen.py` maps a URL to a **mandatory written reason**; an
empty reason raises. An override sets `stable` true while preserving the measured verdict in
`measured_stable`, and `results.md` shows `stable (override: <reason>)` rather than a bare
`yes`.

Use this instead of widening a filter. Slick fails the request criterion on 20 differing
cookie-sync requests with zero content hosts differing — a global filter tuned until slick
passed would silently change the verdict for every page screened afterwards, whereas an
override states the exception in the open and leaves the measurement intact.

## Reading the output

`results.md` is sorted so `network`-channel pages come first — those are the ones with
something for a resource-count metric to measure. `lazy_attributes` reports the attribute
names actually found in the **served** HTML, and separately what the rendered DOM has, since
a JS-built page can differ completely. `autofetch_would_harvest` versus
`autofetch_would_miss` is the field that matters downstream: Browsertrix autofetch takes
`data-src` and `srcset` only, so a page using `data-lazy` is a different test case from one
using `data-src` even though the user action is identical.

---

# Exploration oracle (`tools/explore.py`)

A second instrument in this package, sharing the screener's machinery but answering a
different question. The screener asks *what kind of thing does an interaction reveal on
this page*. The explorer asks *how much is there to reveal at all*, because recall needs a
denominator:

```
baseline  = content requests with no interaction
pool      = (explorer ∪ every behavior config) − baseline − noise
recall(X) = |X's new requests ∩ pool| / |pool|
```

It is an evaluation instrument, never part of a production crawl, so it is thorough rather
than fast: roughly a minute per run, two runs per page. It is also deliberately
**independent of Browsertrix** — no browsertrix-behaviors code or logic is reused, because
an oracle that shares the tested system's blind spots cannot measure them.

What it *does* reuse, by importing `screen.py`: page loading, response bookkeeping, the
noise rules, the click denylist and structural safety rules, the navigation guard, robots
and per-host rate limiting.

```sh
.venv/bin/python -u tools/explore.py --url https://example.com/page
.venv/bin/python -u tools/explore_acceptance.py          # the corpus gate; run this first
```

Writes `explore/out/<page_id>.json` per page and `explore/out/summary.md`.

## Strategy

Per page, in one browser session, twice:

| step | what it does |
|---|---|
| baseline | load, networkidle, **30s idle** (periodic detection), 10s settle; record requests |
| scroll | main document, then every scrollable element and same-origin iframe, 400px steps |
| hover | every visible native control or element with a hover listener, real pointer, 500ms |
| click | every visible safe candidate, repeatedly, until a click reveals nothing novel |
| re-discover | repeat discovery after each pass; clicking reveals more controls |

**Clicking repeats until nothing novel appears**, capped at 10 per element. Novel means a
content request URL *or* a visible text fragment not seen earlier in the session. That
definition is what stops a wrapping carousel: it changes the DOM on every click forever, but
once it wraps to slides already seen, both tests fail and the element is done. A non-novel
click ends that element only — never the page.

**Caps per run:** 300 actions, 10 minutes, 5 discovery passes, 50 viewport heights per
scroll container. Which cap ended exploration, if any, is recorded. One action is one hover,
one click, or one full sweep of one scroll container; scroll *steps* are attributed
individually but do not count, so one infinite-scroll page cannot spend the whole budget
before hover and click ever run.

**Nondeterminism is measured, not assumed.** The full exploration runs twice. Only URLs
found in *both* runs go in the pool (`stable_new_content`); the rest are reported as
`unstable_new_content`.

## Candidate discovery: three sources

| source | what it finds | why it is needed |
|---|---|---|
| `native` | `button`, `summary`, `a[href]`, `[role=button]`, `[role=tab]` | the accessible case |
| `cdp-listener` | any element with a click/hover listener, via `DOMDebugger.getEventListeners` | the inert `<div>` with a real handler |
| `cursor-pointer` | visible elements whose computed cursor is `pointer` | React and friends attach every handler at the document root, so CDP finds nothing on the element that is actually clickable |

`cursor:pointer` is inherited, so an element counts only when its parent is not also
`pointer` — otherwise every inner `<span>` and icon of a clickable tile becomes a separate
candidate. Sources overlap, so the per-page `click_sources` block reports `only_source`:
the elements a source found that no other source did. On the corpus that is 0 for every
source (its inert controls carry real listeners); on a root-delegated page it is where every
candidate comes from.

## What is excluded from the pool, and why

Four different reasons, kept separate in the output because they mean different things:

- **`blocked_ads`** — ads and trackers, aborted at the route layer by adblock-rust
  (Brave's engine) loaded with **Brave's own five filter lists**, copied byte-for-byte out
  of a Browsertrix crawl profile by `tools/sync_brave_lists.py`: uBlock filters (which
  bundles EasyList), EasyPrivacy, the EasyList cookie list, Brave's first-party filters and
  Fanboy's mobile notifications. Same engine, same rules the crawler had. Downloading
  EasyList+EasyPrivacy directly, as this first did, blocks a strict *subset* of what Brave
  blocks, which leaves the pool holding requests a crawl never had the chance to make and
  biases every config's recall downward. Network filtering only: Brave's cosmetic filtering,
  farbling and storage partitioning change how a page looks and what it can read, not which
  requests it makes. Never fetched, so never in the pool; installed *before* the first
  navigation, so the baseline is filtered on the same terms as everything after it. Every
  result records the component ids, versions and SHA-256s it was filtered against.
  `python-adblock` reports the matching list, but not the rule text.
- **`periodic_patterns`** — before baseline, the explorer sits idle for 30s and records
  everything the page fetches on its own: heartbeats, polling, session pings, rotating ad
  slots. URLs are normalised to scheme+host+path, so a poll whose query changes every time
  still matches. Any later request matching a pattern is excluded from attribution and from
  every novelty check. A timer is never a reveal.
- **`noise_filtered` / `size_filtered`** — the screener's rules, which by default now run in
  **advisory** mode: they report what they would still filter *after* blocking, rather than
  filtering it. That number is the measure of what the blocker missed. `--noise-mode enforce`
  restores the screener's behavior of removing them. The size rule (`body under 1KB`) is
  reported separately because it also catches small *content* — the screener has dropped a
  347-byte `page-data.json` and a 1013-byte loader SVG that way — and each entry carries its
  size and content-type so the catches are reviewable.
- **navigation side effects** — a click that navigates is never a reveal. Main-frame
  navigations after load are answered with 204 No Content, which leaves the page and its JS
  state intact where an abort can commit an error page; this also stops same-URL reloads,
  which the screener's guard permits. History-API route changes are reverted.

`--noise-exempt PREFIX` with a mandatory `--noise-exempt-reason` is the same idea as
`STABILITY_OVERRIDES`: an exception stated in the open rather than a rule widened for
everyone. The corpus acceptance uses it for one reason — every sentinel is a deliberately
68-byte 1×1 PNG, which the size rule classifies as a tracking pixel by design.

## Attribution

Every new request records the action that first caused it —
`{action: scroll|hover|click, target, iteration, pass, sources}` — because the downstream
comparison is about *which interaction types* a behavior config reaches, not just how many
resources it gets.

## Acceptance

`tools/explore_acceptance.py` serves the corpus with its own `serve.py` and request log,
runs the explorer over all 14 pages, and requires: all 28 resource sentinels in the stable
set, zero in either baseline, and the explorer's `/sentinel/` request set equal to the
server's. Two independent channels, as in `crawl-eval`. Text sentinels are reported but not
gated: they reveal text, not resources. Nothing in the corpus is ever modified to make this
pass.

## Ad blocking: where the lists come from

```sh
.venv/bin/python tools/sync_brave_lists.py        # newest crawl profile under crawl-eval/runs
```

Copies the five Brave components into `cache/brave-lists/` (about 9 MB) with a manifest
recording each component id, version, list title, size and SHA-256. That directory is
**not in git** — it is recreatable from any crawl profile, and every result already records
what it was filtered against. `blocking.LISTS_DIR` is the configured path; `--brave-lists`
overrides it per run. If it is missing, both tools refuse to run rather than fall back to a
different blocklist, because a pool filtered against rules the crawl never used is not
comparable with one that was.

Verified when the lists were adopted: the Browsertrix crawl profiles carry these components
fully populated (5.2 MB of adblock filters, 1.7 MB of EasyPrivacy, and more), and the
profile's `Default/Preferences` holds per-site shields metadata for the crawled host — so
Brave's shields were live during the crawls, not merely installed.

`tools/screen.py` gained `--block-ads`, which installs the same blocker. It is **off by
default**: every result in `out/results.json` was measured without it, and turning it on
makes a re-screened page incomparable with those. The explorer blocks by default, because
its pool is compared against Browsertrix crawls.
