# Interaction test corpus

Local corpus of self-contained HTML pages where content is revealed **only** by a
specific user interaction, and where "was it revealed?" is answerable by machine.
Built to the spec in `../SPEC-interaction-test-corpus.md`, which stays authoritative.

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
```

## Serve

```sh
.venv/bin/python tools/serve.py --port 8000 --reset
```

Serves the corpus root, so pages are at `/pages/<mechanism>/<name>.html` and assets at
`/sentinel/...`. Every request under `/sentinel/` is appended to `logs/requests.jsonl`
as one JSON object per line, including requests for sentinels that do not exist (those
get an empty `404` and are still logged — a missing sentinel is a bug worth seeing).
Everything is served `Cache-Control: no-store`, so repeat runs are never served from cache.

## Acceptance tests

With the server running:

```sh
.venv/bin/python tools/acceptance.py                 # during the build
.venv/bin/python tools/acceptance.py --strict        # final gate
.venv/bin/python tools/acceptance.py --only click-accordion-accessible
```

Prints a page × A1–A6 table and exits non-zero on any failure.

**MISSING vs FAIL.** A1 (uniqueness, assets on disk) runs over the entire manifest always
— it needs no HTML. A2–A6 need a built page: a manifest entry with no HTML file on disk is
reported `MISSING`, never as passing and never as not-applicable. The summary line always
states counts explicitly, e.g. `12 declared / 1 built / 11 MISSING`.

`--strict` exits non-zero if any declared page is `MISSING`. It is **off** during the build
(so a partially built corpus can still be green on what exists) and is the final gate before
the corpus is accepted. There is no other mechanism for tolerating a page that is present
but failing: a built page that fails any test fails the run, with or without `--strict`.

### Why hover pages start by hovering the h1

A repeated `hover` on the element the pointer is already over moves nothing, so no
`mouseenter` fires and A6's repeat passes attempt nothing whether or not the guard is
present. Every hover page therefore begins `required_actions` with a hover on the `h1`:
each repeat pass leaves the control and re-enters it, which makes the guard observable on
the JS pages. Before this, the two `hover-dropdown-*` pages derived `guard_observable:
false`; with the hover-away action both derive `true`.

### CSS-fetched sentinels (`sentinel_fetch: "css"`)

On `hover-css-menu` and `hover-css-bgimage` no script requests a sentinel: the browser's
style engine does, when a `:hover` rule first renders a `background-image`. The page still
assembles the URL with `assetUrl(idx)`, and installs the rule at load through the CSSOM
(`sheet.insertRule`), so no `url()` is in the served HTML and A4 holds. Browsertrix
`autofetch` reads stylesheet `url()`s only inside `@media` rules, plus text added to a
`<style>` element, so a plain rule inserted this way is not harvested either.

These pages have no `fetchSentinel` and no already-fetched guard, and the A6 wrappers
never see the browser's request. The manifest marks them `"sentinel_fetch": "css"`, and
A6 then asserts two things per sentinel instead of the attempt counts: **zero** attempts by
script (a script fetch means the page is no longer a CSS-only reveal), and **exactly one**
network request across the session. `guard-removal` does not apply. The `script-fetch-css`
mutation, which requests the sentinel from a `mouseover` listener as well, is what exercises
A6 on these pages, and `--derive` records `guard_observable: false` for them.

### Why a page keeps its trigger after it is spent

Several pages could naturally remove or disable their trigger once used — an exhausted
"Load more", a consumed "Continue reading" strip. They deliberately do not: A6 re-runs
`required_actions` twice on the same page load, and a trigger that has been removed, hidden,
or marked `aria-disabled` makes that re-run time out, which reports as an A6 failure rather
than as the no-op it should be. Exhausted triggers therefore stay in the DOM and stay
clickable, announcing their state through their label and a live status instead.

## Sentinel assets

```sh
.venv/bin/python tools/gen_sentinels.py
```

Reads `manifest.json`, validates it against `schema/manifest.schema.json`, and writes a 1×1
transparent PNG for every declared `resource_sentinels` entry. Reports any PNG under
`sentinel/` that the manifest no longer declares.

## Mutation testing — required, not optional

```sh
.venv/bin/python tools/mutations.py                       # whole corpus (slow, ~20 min)
.venv/bin/python tools/mutations.py --page click-accordion-accessible
.venv/bin/python tools/mutations.py --mutation guard-removal --derive
```

A passing acceptance suite proves nothing unless each test can also *fail*. A6 shipped
vacuous twice during this build — once because Chromium dedupes a repeated image request
even under `no-store`, once because repeating a toggle action lands on the hidden state —
and both times a green column hid it. `mutations.py` is what stops the next one.

For each (page, mutation) it copies the corpus to a temp tree, applies a deliberate defect
to the copy, serves it on its own port with its own log, runs `acceptance.py`, and asserts
that **exactly** the expected tests fail. A mutation that trips the wrong test is as much a
bug as one that trips nothing. Every page must carry a mutation for each of A3
(fires-on-load), A4 (static-leak), A2 (dead-handler) and A6 (guard-removal, or
script-fetch-css on a CSS-fetched page); the tool fails
if a page has no applicable mutation for one of them.

`--derive` is how `guard_observable` gets its value: run the guard-removal mutation and
record what was observed. Never set that field by reasoning about the page.

### Probing a page by hand: `.click()` fires `mouseover` first

Playwright's `.click()` moves the pointer to the element before pressing, so it dispatches
`mouseover` on the way in. Any probe that uses `.click()` to prove a hover page has *no*
click path will see the hover fire and report a false failure — this already produced one
during the build, making `hover-dropdown-inert` look click-operable when it is not. To test
that a click path is genuinely absent, dispatch the event without moving the pointer:

```python
page.evaluate("document.querySelector('.nav-trigger').click()")   # no mouseover
page.evaluate("document.querySelector('.nav-trigger').focus()")   # no mouseover
```

and use `.hover()` only when a real pointer movement is what you mean to test. The same
applies to `.tap()` and `.dblclick()`.

## Adding a page

In this order:

1. **Manifest entry first.** Add the page to `manifest.json`: id, path, mechanism,
   submechanism, variant, its sentinel URLs, and its `required_actions`. Set
   `guard_observable` to `null` — it is derived, not chosen. Nothing else in the repo
   hardcodes a page id or a sentinel URL: both tools read them from here.
2. **Regenerate sentinels:** `tools/gen_sentinels.py`.
3. **Write the page** at the declared path, following the sentinel contract below and the
   two shared idioms: assemble the URL in `assetUrl(idx)` ending in `.join('/')`, and route
   every fetch through `fetchSentinel(idx)` whose first statement is the already-fetched
   guard. The generic mutations depend on both. A page whose sentinels the browser
   fetches from CSS has no `fetchSentinel`; see "CSS-fetched sentinels" above. Hidden
   text-sentinel containers carry `class="sentinel-text"`.
4. **Run the acceptance tests** and get all six green.
5. **Derive the flag:** `tools/mutations.py --page <id> --mutation guard-removal --derive`.
6. **Run the full mutation matrix for the page** and get it clean before starting the next
   one.

## The sentinel contract — do not break this

A sentinel is a uniquely-named asset the browser fetches **if and only if** the required
interaction occurs, which makes recall a set comparison over requested URLs rather than a
diff of page text. For that to hold, the sentinel URL must not be statically discoverable:
it must not appear as a literal string anywhere in the served HTML, so that a crawler which
regexes the source, or which harvests `src` / `href` / `data-src` / `srcset` /
`<link rel=preload>` / `<link rel=prefetch>`, finds nothing. Browsertrix's `autofetch`
specifically harvests `data-src` and `srcset` without interacting with the page, so a
lazy-loading page written the normal way silently becomes a page that requires no
interaction. The rule is therefore: **store only an opaque index in the DOM
(`data-idx="01"`) and assemble the URL in JS at interaction time** from page-level `MECH`
and `VARIANT` constants — `['', 'sentinel', MECH, VARIANT, idx + '.png'].join('/')` — never
writing a path or any multi-component fragment of one into the markup. Never add query
strings; cache-busting params break URL-set comparison downstream. A3 (zero recall without
interaction) and A4 (no static leakage) exist to enforce this: if a page fails them the page
is wrong, and the fix is never to relax the test or widen `expect_static_discoverable`.

A4's threat model is a harvester that turns strings in the HTML into fetchable URLs. It does
not defend against a reader that interprets the JS and reassembles the URL semantically —
see "Threat model and its limit" in §2 of the spec for why that is acceptable.
