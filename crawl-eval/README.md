# crawl-eval

Browsertrix behavior-isolation crawls over `../interaction-corpus/`. One crawl per
condition, each with exactly one behavior enabled (or none), never cumulative.

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
docker pull webrecorder/browsertrix-crawler:latest          # tested: 1.14.3

.venv/bin/python -u tools/run_crawls.py --rerun              # all five conditions
.venv/bin/python tools/warc_recall.py runs/c*/               # recall + cross-check
.venv/bin/python tools/report.py                             # table + minimal subsets
```

`run_crawls.py` starts a fresh `serve.py` per condition with its own request log, so
`warc_recall.py` can require the WARC's `/sentinel/` path set to equal the set the server
saw. If they disagree it exits non-zero and prints the delta; `report.py` refuses to build a
table from a run whose cross-check failed.

## Browsertrix facts this depends on (verified in 1.14.3 source, not assumed)

- **Disabling behaviors:** `--behaviors ""`. Works because arg coercion splits on commas and
  drops empty strings, leaving `[]`, and an empty list sets `behaviorOpts` to `""`. Each run
  checks the crawl log: condition 1 must log zero `Run Behaviors Started` lines.
- **Unknown behavior names are warned about and ignored**, so a typo silently runs a
  no-behavior crawl. Valid names: `autoplay autofetch autoscroll autoclick siteSpecific`.
- **Bare `--text` means `--text to-pages`.** That text is snapshotted **before behaviors run**
  and reads every DOM text node **without checking visibility**. It cannot reflect a reveal,
  and it recovers `display:none` text with no interaction. The runs add `final-to-warc`, a
  post-behavior snapshot written as a `urn:textFinal` resource record — skipped when the text
  is unchanged, and never written when no behavior ran.
- **Byte-identical payloads become `revisit` records.** Every sentinel PNG is the same bytes,
  so later ones are written as revisits carrying the original HTTP headers. Recall reads the
  status from `response` and `revisit` records alike; a response-only counter under-reports.
- **`--clickSelector` defaults to `"a"`.** Stock autoclick clicks anchors only.
- **Autoscroll gates itself:** it runs only if `window`, `document` or `body` has a `scroll`
  event listener (or the page has ≥2 frames), then only if a trial scroll changes
  `scrollHeight` or starts a fetch. IntersectionObserver-based lazy loading registers no scroll
  listener, so autoscroll skips it.
- **Autofetch harvests any `data-*` attribute** whose value starts with `http`, `/`, `./` or
  `../`, in addition to `srcset`, `data-src`, `data-srcset` and `loading=lazy`.
