# Interaction Corpus and Explorer

Test pages and an evaluation oracle for measuring how well crawler behaviors
capture content that only appears after user interaction.

## Interaction corpus

`interaction-corpus/pages/` — 14 HTML pages, each gating content behind exactly
one interaction (click, hover, or scroll).

The gated content is a **sentinel resource**: a uniquely named asset
(e.g. `/sentinel/click-carousel/accessible/03.png`) that the page requests only
when the correct interaction occurs. Its presence in a crawl record proves the
interaction happened, so recall is a set comparison rather than a judgment call.
Sentinel URLs are assembled in JavaScript at interaction time so they cannot be
found by scraping the markup.

Most mechanisms appear twice: once with correct semantics (real `<button>`,
`aria-expanded`, accessible name) and once visually identical but built from
unlabelled `<div>` elements.

| Page | Interaction | Markup | Sentinels |
|---|---|---|---|
| click-accordion-accessible | click, expand panel | semantic | 1 |
| click-accordion-inert | click, `<div onclick>` | inert | 1 |
| click-carousel-accessible | click "next" ×3 | semantic | 3 |
| click-carousel-inert | click, unlabelled arrows | inert | 3 |
| click-loadmore-accessible | click "load more" ×2 | semantic | 2 |
| click-loadmore-inert | click, styled `<span>` | inert | 2 |
| click-continuereading-accessible | click, `<template>` clone | semantic | 2 text |
| click-continuereading-inert | click, `display:none` toggle | inert | 2 text |
| hover-dropdown-accessible | hover, also focus-operable | semantic | 2 |
| hover-dropdown-inert | hover only | inert | 2 |
| scroll-lazyimg-js | scroll, IntersectionObserver | — | 3 |
| scroll-infinite | scroll, 3 appended batches | — | 3 |
| click-lazyimg-datasrc | click, `data-src` attribute | — | 3 |
| click-lazyimg-datalazy | click, `data-lazy` attribute | — | 3 |

28 resource sentinels and 4 text sentinels. Declared sentinels per page are in
`interaction-corpus/manifest.json`.

### Serving the pages

```bash
cd interaction-corpus
python tools/serve.py --port 8000 --reset
```

Pages are at `http://localhost:8000/pages/...`. Every sentinel request is logged
to `logs/requests.jsonl`, so which interactions fired can be checked directly:

```bash
grep sentinel logs/requests.jsonl
```

## Explorer

`page-screener/tools/explore.py` — a Playwright-based exhaustive interaction
oracle. Used to estimate the full set of interaction-gated resources on pages
where no sentinels exist.

Per page it scrolls every scroll container, hovers every candidate element using
real pointer input (so CSS `:hover` triggers), and clicks every candidate
repeatedly until a click stops producing novel content. It re-discovers
candidates after each reveal, since opening a panel can expose new controls.
Every fetched resource is attributed to the action that caused it.

Click candidates come from native controls, elements with click listeners
(via CDP), and elements with computed `cursor: pointer` — the last catches
frameworks such as React that attach handlers at the root.

Ads and trackers are blocked with EasyList and EasyPrivacy. After load, the page
sits idle for 30 seconds to learn periodically fetched URLs, which are then
excluded from attribution.

It is deliberately independent of Browsertrix, so it does not share the blind
spots of the behaviors it is used to evaluate.

### Acceptance

Validated against the corpus: all 28 resource sentinels recovered, none in the
no-interaction baseline, and the explorer's own request records agree with the
server log.

```bash
pip install -r page-screener/requirements.txt
playwright install chromium
python page-screener/tools/explore_acceptance.py
```

The acceptance script starts its own server on port 8010.
