#!/usr/bin/env python3
"""Acceptance tests A1-A6 for the interaction corpus (spec §8).

Reads manifest.json for everything: no page id, selector or sentinel URL is
hardcoded here. Runs against a server already started by tools/serve.py, and
uses that server's request log as ground truth for "was the sentinel fetched?".

A1 runs over the entire manifest and needs no HTML. A2-A6 need a built page; a
manifest entry with no HTML file on disk is reported MISSING, never as passing
and never as not-applicable. --strict makes MISSING a failure.
"""

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS = ["A1", "A2", "A3", "A4", "A5", "A6"]

PASS, FAIL, MISSING = "PASS", "FAIL", "MISSING"

# Settle time after an action, in ms. Pages must reveal within 200ms (§4).
ACTION_SETTLE_MS = 300
A2_WAIT_S = 1.0
A6_REPEAT_PASSES = 2
A3_WAIT_S = 10.0


# --------------------------------------------------------------------------- #
# manifest                                                                      #
# --------------------------------------------------------------------------- #

def load_manifest(path: Path) -> dict:
    with path.open() as fh:
        return json.load(fh)


def validate_schema(manifest: dict, schema_path: Path) -> tuple[list[str], bool]:
    try:
        import jsonschema
    except ImportError:
        return [], False
    with schema_path.open() as fh:
        schema = json.load(fh)
    validator = jsonschema.Draft202012Validator(schema)
    return (
        [
            f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
            for e in validator.iter_errors(manifest)
        ],
        True,
    )


# --------------------------------------------------------------------------- #
# server request log                                                            #
# --------------------------------------------------------------------------- #

class RequestLogReader:
    """Reads tools/serve.py's JSONL log, sliced by byte offset.

    Tests run sequentially, so "requests made during this phase" is exactly the
    lines appended between two marks.
    """

    def __init__(self, path: Path):
        self.path = path

    def mark(self) -> int:
        return self.path.stat().st_size if self.path.exists() else 0

    def since(self, offset: int) -> list[dict]:
        if not self.path.exists():
            return []
        with self.path.open("rb") as fh:
            fh.seek(offset)
            blob = fh.read()
        records = []
        for line in blob.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return records

    @staticmethod
    def paths(records: list[dict]) -> list[str]:
        return [r["path"] for r in records]


# --------------------------------------------------------------------------- #
# A1 - uniqueness and on-disk existence                                         #
# --------------------------------------------------------------------------- #

def run_a1(manifest: dict, root: Path) -> dict[str, tuple[str, list[str]]]:
    """Per-page A1 result. Runs over the entire manifest, HTML or not."""
    seen_resource: dict[str, list[str]] = {}
    seen_text: dict[str, list[str]] = {}
    for page in manifest["pages"]:
        for url in page["resource_sentinels"]:
            seen_resource.setdefault(url, []).append(page["id"])
        for tok in page["text_sentinels"]:
            seen_text.setdefault(tok, []).append(page["id"])

    results: dict[str, tuple[str, list[str]]] = {}
    for page in manifest["pages"]:
        problems: list[str] = []
        for url in page["resource_sentinels"]:
            owners = seen_resource[url]
            if len(owners) > 1:
                problems.append(f"resource sentinel {url} also declared by {', '.join(o for o in owners if o != page['id'])}")
            if page["resource_sentinels"].count(url) > 1:
                problems.append(f"resource sentinel {url} declared twice on this page")
            dest = root / url.lstrip("/")
            if not dest.is_file():
                problems.append(f"missing on disk: {url}")
        for tok in page["text_sentinels"]:
            owners = seen_text[tok]
            if len(owners) > 1:
                problems.append(f"text sentinel {tok} also declared by {', '.join(o for o in owners if o != page['id'])}")
        results[page["id"]] = (PASS if not problems else FAIL, problems)
    return results


# --------------------------------------------------------------------------- #
# A4 - static leakage (spec §8, amended)                                        #
# --------------------------------------------------------------------------- #

def leak_candidates(sentinel_path: str) -> set[str]:
    """Every string in the HTML that a harvester could resolve into this URL.

    Split on '/', drop empties, take every contiguous run of two or more
    components joined by '/', with and without a leading '/', plus the terminal
    filename alone. A single bare component is deliberately not a candidate.
    """
    comps = [c for c in sentinel_path.split("/") if c]
    out: set[str] = set()
    for i in range(len(comps)):
        for j in range(i + 2, len(comps) + 1):
            joined = "/".join(comps[i:j])
            out.add(joined)
            out.add("/" + joined)
    if comps:
        out.add(comps[-1])
    return out


LINK_TAG_RE = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
REL_RE = re.compile(r"""\brel\s*=\s*("([^"]*)"|'([^']*)'|([^\s>]+))""", re.IGNORECASE)
HREF_RE = re.compile(r"""\bhref\s*=\s*("([^"]*)"|'([^']*)'|([^\s>]+))""", re.IGNORECASE)


def _attr(match: re.Match | None) -> str:
    if not match:
        return ""
    return next((g for g in match.groups()[1:] if g is not None), "")


def run_a4(html: str, page: dict) -> tuple[str, list[str]]:
    problems: list[str] = []
    for url in page["resource_sentinels"]:
        for cand in sorted(leak_candidates(url)):
            if cand in html:
                problems.append(f"HTML contains fetchable fragment {cand!r} of {url}")
    for tag in LINK_TAG_RE.findall(html):
        rel = _attr(REL_RE.search(tag)).lower()
        href = _attr(HREF_RE.search(tag))
        if any(kind in rel.split() for kind in ("preload", "prefetch")) and "sentinel" in href.lower():
            problems.append(f"declares {rel} link to sentinel: {tag}")
    return (PASS if not problems else FAIL, problems)


# --------------------------------------------------------------------------- #
# A6 instrumentation                                                            #
# --------------------------------------------------------------------------- #

# Counts *attempted* fetches at the browser-API boundary. It must sit here and not
# inside a page-level helper: a counter in the same helper that holds the
# already-fetched guard is only reached once the guard has allowed the fetch, so a
# correctly guarded page would be indistinguishable from an unguarded one. The
# wrappers are additive - record, then call through.
#
# Injected with add_init_script only, so nothing is injected during real crawls.
ATTEMPT_COUNTER_JS = """
(() => {
  const attempts = [];
  Object.defineProperty(window, '__sentinelAttempts', {
    get() { return attempts; }, configurable: false, enumerable: false,
  });
  const record = (u) => { try { if (u != null) attempts.push(String(u)); } catch (e) {} };

  const imgSrc = Object.getOwnPropertyDescriptor(HTMLImageElement.prototype, 'src');
  if (imgSrc && imgSrc.set) {
    Object.defineProperty(HTMLImageElement.prototype, 'src', {
      configurable: true,
      enumerable: imgSrc.enumerable,
      get: imgSrc.get,
      set(value) { record(value); return imgSrc.set.call(this, value); },
    });
  }

  const origFetch = window.fetch;
  if (origFetch) {
    window.fetch = function (input, init) {
      record(typeof input === 'string' ? input : (input && input.url));
      return origFetch.apply(this, arguments);
    };
  }

  const origOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url) {
    record(url);
    return origOpen.apply(this, arguments);
  };
})();
"""


def collect_attempts(pw_page) -> list[str]:
    """Attempted sentinel URLs across every frame, normalised to path-only."""
    raw: list[str] = []
    for frame in pw_page.frames:
        try:
            raw.extend(frame.evaluate("() => (window.__sentinelAttempts || []).slice()"))
        except Exception:  # noqa: BLE001 - a frame may have gone away
            continue
    out = []
    for url in raw:
        try:
            out.append(urlsplit(url).path)
        except ValueError:
            out.append(url)
    return out


def count_by(items: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        counts[item] = counts.get(item, 0) + 1
    return counts


# --------------------------------------------------------------------------- #
# browser-driven phases                                                         #
# --------------------------------------------------------------------------- #

def perform_actions(pw_page, actions: list[dict]) -> None:
    for action in actions:
        kind = action["type"]
        if kind == "none":
            continue
        if kind == "wait":
            # a page whose controls work only after hydration: the wait is part of
            # the required interaction, not a settle after it
            pw_page.wait_for_timeout(action["ms"])
            continue
        for _ in range(action.get("repeat", 1)):
            scope = pw_page
            if action.get("frame"):
                scope = pw_page.frame_locator(action["frame"])
            if kind == "click":
                scope.locator(action["selector"]).first.click()
            elif kind == "hover":
                scope.locator(action["selector"]).first.hover()
            elif kind == "scroll_to":
                scope.locator(action["selector"]).first.scroll_into_view_if_needed()
            elif kind == "scroll_by":
                pw_page.mouse.wheel(0, action["pixels"])
            elif kind == "select":
                scope.locator(action["selector"]).first.select_option(action["value"])
            else:
                raise ValueError(f"unknown action type: {kind}")
            pw_page.wait_for_timeout(ACTION_SETTLE_MS)


def visible_text_sentinels(pw_page, tokens: list[str]) -> list[str]:
    if not tokens:
        return []
    text = pw_page.evaluate("() => document.body.innerText")
    return [t for t in tokens if t in text]


def text_sentinel_counts(pw_page, tokens: list[str]) -> dict[str, int]:
    """Occurrences of each token in the rendered text, for A6's text assertion."""
    if not tokens:
        return {}
    text = pw_page.evaluate("() => document.body.innerText")
    return {t: text.count(t) for t in tokens}


def run_dynamic(browser, base_url: str, page: dict, log: RequestLogReader) -> dict:
    """Runs A2, A3, A5 and A6 for one built page. Returns {test: (status, problems)}."""
    url = f"{base_url.rstrip('/')}/{page['path']}"
    declared = set(page["resource_sentinels"])
    tokens = page["text_sentinels"]
    out: dict[str, tuple[str, list[str]]] = {}

    # -- A3: load, wait, touch nothing ---------------------------------------
    ctx = browser.new_context(viewport={"width": 1280, "height": 720})
    pw_page = ctx.new_page()
    mark = log.mark()
    pw_page.goto(url, wait_until="load")
    pw_page.wait_for_timeout(int(A3_WAIT_S * 1000))
    idle_paths = RequestLogReader.paths(log.since(mark))
    idle_text = visible_text_sentinels(pw_page, tokens)
    ctx.close()

    # A3 applies to every page, including expect_static_discoverable ones. That flag
    # describes what an attribute-harvesting crawler would find; this harness drives
    # plain Chromium with no autofetch, so a page whose URL sits in a data-src
    # attribute must still request nothing when nobody interacts. The flag exempts a
    # page from A4 only.
    problems = []
    if idle_paths:
        problems += [f"fetched without interaction: {p}" for p in sorted(set(idle_paths))]
    if idle_text:
        problems += [f"text sentinel visible without interaction: {t}" for t in idle_text]
    out["A3"] = (PASS if not problems else FAIL, problems)

    # -- A2 + A5 + A6: one load, actions performed twice ----------------------
    ctx = browser.new_context(viewport={"width": 1280, "height": 720})
    ctx.add_init_script(ATTEMPT_COUNTER_JS)
    pw_page = ctx.new_page()
    observed: list[str] = []
    pw_page.on("request", lambda req: observed.append(req.url))

    mark = log.mark()
    pw_page.goto(url, wait_until="load")
    try:
        perform_actions(pw_page, page["required_actions"])
        action_error = None
    except Exception as exc:  # noqa: BLE001 - reported as a test failure
        action_error = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
    pw_page.wait_for_timeout(int(A2_WAIT_S * 1000))
    first_paths = RequestLogReader.paths(log.since(mark))
    first_attempts = count_by(collect_attempts(pw_page))
    shown = set(visible_text_sentinels(pw_page, tokens))

    problems = []
    if action_error:
        problems.append(f"required_actions raised {action_error}")
    for missing in sorted(declared - set(first_paths)):
        problems.append(f"declared sentinel never requested: {missing}")
    for missing in sorted(t for t in tokens if t not in shown):
        problems.append(f"declared text sentinel not in body.innerText: {missing}")
    recall_num = len(declared & set(first_paths)) + len(shown)
    recall_den = len(declared) + len(tokens)
    out["A2"] = (PASS if not problems else FAIL, problems)
    out["_recall"] = (f"{recall_num}/{recall_den}", [])

    # A5: every request the browser made must be same-origin.
    origin = base_url.rstrip("/")
    foreign = sorted({u for u in observed if not u.startswith(origin + "/") and u != origin})
    out["A5"] = (PASS if not foreign else FAIL, [f"external request: {u}" for u in foreign])

    # A6: repeat the same actions on the same page load. Two passes, not one: one
    # pass leaves a toggle in the hidden state, so nothing is attempted whether or
    # not the guard is present.
    mark = log.mark()
    repeat_error = None
    for _ in range(A6_REPEAT_PASSES):
        try:
            perform_actions(pw_page, page["required_actions"])
        except Exception as exc:  # noqa: BLE001
            repeat_error = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
            break
    pw_page.wait_for_timeout(int(A2_WAIT_S * 1000))
    second_paths = RequestLogReader.paths(log.since(mark))
    total_attempts = count_by(collect_attempts(pw_page))
    text_counts = text_sentinel_counts(pw_page, tokens)
    ctx.close()

    problems = []
    if repeat_error:
        problems.append(f"repeated required_actions raised {repeat_error}")
    for url in sorted(declared):
        total = total_attempts.get(url, 0)
        before = first_attempts.get(url, 0)
        # Absolute: exactly one attempt across the whole session. A delta check is
        # blind to duplicates inside a single pass - IntersectionObserver fires on
        # every intersection change, mouseover repeatedly across one hover.
        if total != 1:
            problems.append(
                f"absolute: {url} attempted {total}x across the session, expected exactly 1"
                + ("; page JS is missing an already-fetched guard" if total > 1 else "")
            )
        # Delta: the repeat passes must add nothing.
        if total > before:
            problems.append(
                f"delta: {url} re-attempted on repeat ({before} -> {total} attempts)"
            )
    # Absolute, text: duplication is the one corruption a URL-set metric cannot see.
    # An unguarded template clone appends twice and inflates unique-text measurements
    # downstream while the WARC looks identical.
    for token in sorted(tokens):
        count = text_counts.get(token, 0)
        if count != 1:
            problems.append(
                f"absolute text: {token} appears {count}x in body.innerText, expected exactly 1"
                + ("; the reveal is not guarded against repeating" if count > 1 else "")
            )
    # Network: free, and it catches instrumentation that stopped recording.
    for dup in sorted(set(second_paths)):
        problems.append(f"network: {dup} re-requested on repeat (x{second_paths.count(dup)})")
    out["A6"] = (PASS if not problems else FAIL, problems)

    return out


# --------------------------------------------------------------------------- #
# reporting                                                                     #
# --------------------------------------------------------------------------- #

def fetch_html(base_url: str, rel_path: str) -> str:
    with urllib.request.urlopen(f"{base_url.rstrip('/')}/{rel_path}", timeout=10) as resp:
        return resp.read().decode("utf-8", "replace")


def server_is_up(base_url: str) -> bool:
    try:
        urllib.request.urlopen(base_url, timeout=5).read()
        return True
    except urllib.error.HTTPError:
        return True
    except OSError:
        return False


def print_table(rows: list[tuple[str, dict]], id_width: int) -> None:
    header = "page".ljust(id_width) + "".join(t.center(9) for t in TESTS) + "  recall"
    print(header)
    print("-" * len(header))
    for page_id, res in rows:
        line = page_id.ljust(id_width)
        for test in TESTS:
            status = res.get(test, (MISSING, []))[0]
            line += status.center(9)
        line += "  " + res.get("_recall", ("-", []))[0]
        print(line)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", default=str(REPO_ROOT / "manifest.json"))
    ap.add_argument("--schema", default=str(REPO_ROOT / "schema" / "manifest.schema.json"))
    ap.add_argument("--root", default=str(REPO_ROOT))
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--log", default=str(REPO_ROOT / "logs" / "requests.jsonl"))
    ap.add_argument("--only", action="append", default=[], help="restrict to page id(s)")
    ap.add_argument("--strict", action="store_true", help="exit non-zero if any declared page is MISSING")
    ap.add_argument("--json", dest="json_out", default=None, help="also write results as JSON (used by tools/mutations.py)")
    args = ap.parse_args()

    root = Path(args.root)
    manifest = load_manifest(Path(args.manifest))
    schema_errors, checked = validate_schema(manifest, Path(args.schema))
    if schema_errors:
        print("manifest FAILS schema validation:")
        for err in schema_errors:
            print(f"  - {err}")
        return 2
    print("manifest: schema valid" if checked else "manifest: NOT schema-checked (jsonschema missing)")

    pages = manifest["pages"]
    if args.only:
        pages = [p for p in pages if p["id"] in set(args.only)]
        if not pages:
            print(f"no manifest page matches --only {args.only}")
            return 2

    if not server_is_up(args.base_url):
        print(f"no server at {args.base_url} — start tools/serve.py first")
        return 2

    a1 = run_a1(manifest, root)
    log = RequestLogReader(Path(args.log))

    results: dict[str, dict] = {p["id"]: {"A1": a1[p["id"]]} for p in pages}
    built = [p for p in pages if (root / p["path"]).is_file()]
    missing = [p for p in pages if not (root / p["path"]).is_file()]

    if built:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            for page in built:
                print(f"running {page['id']} ...", flush=True)
                html = fetch_html(args.base_url, page["path"])
                if page["expect_static_discoverable"]:
                    results[page["id"]]["A4"] = (PASS, ["skipped: expect_static_discoverable"])
                else:
                    results[page["id"]]["A4"] = run_a4(html, page)
                results[page["id"]].update(run_dynamic(browser, args.base_url, page, log))
            browser.close()

    print()
    id_width = max([len(p["id"]) for p in pages] + [len("page")]) + 2
    print_table([(p["id"], results[p["id"]]) for p in pages], id_width)

    if args.json_out:
        payload = {
            "pages": {
                pid: {
                    "status": {t: res.get(t, (MISSING, []))[0] for t in TESTS},
                    "problems": {t: res.get(t, (MISSING, []))[1] for t in TESTS},
                    "recall": res.get("_recall", ("-", []))[0],
                }
                for pid, res in results.items()
            },
            "declared": len(pages),
            "built": len(built),
            "missing": [p["id"] for p in missing],
        }
        Path(args.json_out).write_text(json.dumps(payload, indent=2))

    failures = [
        (pid, test, res[test][1])
        for pid, res in results.items()
        for test in TESTS
        if res.get(test, (MISSING, []))[0] == FAIL
    ]
    if failures:
        print("\nfailures:")
        for pid, test, problems in failures:
            print(f"  {pid} {test}:")
            for problem in problems or ["(no detail)"]:
                print(f"      - {problem}")

    print(
        f"\n{len(pages)} declared / {len(built)} built / {len(missing)} MISSING"
        + (f" / {len(failures)} failing check(s)" if failures else " / 0 failing checks")
    )
    if missing:
        print("MISSING pages (no HTML file yet): " + ", ".join(p["id"] for p in missing))
        if args.strict:
            print("--strict: MISSING pages count as failure")

    if failures:
        return 1
    if missing and args.strict:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
