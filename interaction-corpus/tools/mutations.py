#!/usr/bin/env python3
"""Mutation testing of the acceptance suite (spec §11).

A passing acceptance suite proves nothing unless each test can also fail. A6
shipped vacuous twice - once because Chromium deduped the repeat image request,
once because repeating a toggle action landed on the hidden state - and a green
column hid it. This tool asserts mechanically that each test detects the defect
it exists to detect.

For each (page, mutation) it copies the corpus to a temporary tree, applies the
transform to that copy's HTML, serves the copy on its own port with its own
request log, runs acceptance.py against it, and asserts that EXACTLY the expected
tests fail - no more and no fewer. A mutation that trips the wrong test is as much
a bug as one that trips nothing.

guard_observable (§7) is a claim under test, not a suppression: the guard-removal
mutation runs on every page regardless of the flag, and the flag is then checked
against what was observed.
"""

import argparse
import json
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable

OK, MISMATCH, NA, ERROR, NOOP = "OK", "MISMATCH", "n/a", "ERROR", "NOOP"


# --------------------------------------------------------------------------- #
# mutations                                                                     #
# --------------------------------------------------------------------------- #

@dataclass
class Mutation:
    name: str
    description: str
    # (html, page) -> mutated html, or None when the mutation cannot apply.
    transform: Callable[[str, dict], str | None]
    # (page) -> set of test ids expected to fail.
    expected: Callable[[dict], set[str]]


def _first_sentinel(page: dict) -> str | None:
    return page["resource_sentinels"][0] if page["resource_sentinels"] else None


def _opaque_fetch_js(url: str) -> str:
    """A fetch of `url` that leaks no A4 candidate: components stay separate
    literals and the filename is split, so no multi-component fragment and no
    'NN.png' appears in the source."""
    comps = [c for c in url.split("/") if c]
    stem = comps[-1].removesuffix(".png")
    parts = ", ".join(repr(c) for c in comps[:-1])
    return (
        "<script>(function(){var p=['', %s, %r + '.png'];"
        "var i=new Image();i.src=p.join('/');})();</script>" % (parts, stem)
    )


def m_fires_on_load(html: str, page: dict) -> str | None:
    url = _first_sentinel(page)
    if not url:
        return None
    return html.replace("</body>", _opaque_fetch_js(url) + "\n</body>")


def m_static_leak_comment(html: str, page: dict) -> str | None:
    url = _first_sentinel(page)
    if not url:
        return None
    if page.get("expect_static_discoverable"):
        # The sentinel path is already in the served HTML by construction, so there
        # is no leak to introduce and A4 is not asserted on this page.
        return None
    return html.replace("</head>", f"<!-- asset: {url} -->\n</head>")


def m_static_leak_preload(html: str, page: dict) -> str | None:
    url = _first_sentinel(page)
    if not url:
        return None
    if page.get("expect_static_discoverable"):
        # The sentinel path is already in the served HTML by construction, so there
        # is no leak to introduce and A4 is not asserted on this page.
        return None
    return html.replace("</head>", f'<link rel="preload" as="image" href="{url}">\n</head>')


def m_dead_handler_url(html: str, page: dict) -> str | None:
    """The handler still runs, but the URL it produces never reaches the sentinel.

    Two idioms: pages that assemble the URL per §2 end in .join('/'); the
    attribute-name pair reads it from a data-* attribute instead and has no join to
    break, so the dataset key is redirected at an attribute that does not exist.
    """
    if not page["resource_sentinels"]:
        return None
    if ".join('/')" in html:
        return html.replace(".join('/')", ".join('_')", 1)
    mutated = re.sub(r"\.dataset\.([a-zA-Z]+)", r".dataset.absent\1", html, count=1)
    return mutated if mutated != html else None


def m_dead_handler_event(html: str, page: dict) -> str | None:
    """Text-sentinel pages have no assetUrl to break; kill the listener instead."""
    if page["resource_sentinels"] or not page["text_sentinels"]:
        return None
    mutated = re.sub(r"addEventListener\('(\w+)'", r"addEventListener('__dead-\1'", html, count=1)
    return mutated if mutated != html else None


def m_guard_removal(html: str, page: dict) -> str | None:
    """Disable the already-fetched guard (§11 shared idiom)."""
    if "if (fetched.has(idx)) return null;" not in html:
        return None
    return html.replace("if (fetched.has(idx)) return null;", "if (false) return null;")


def m_text_guard_removal(html: str, page: dict) -> str | None:
    """Disable the guard that stops a text reveal from being applied twice.

    Only meaningful where the reveal appends content (a <template> clone). A
    display:none reveal that toggles a class is idempotent by construction and has
    no guard to remove, so this returns None there.
    """
    if not page["text_sentinels"]:
        return None
    for guarded, unguarded in (
        ("if (!expanded && !cloned) {", "if (!expanded) {"),
        ("if (!cloned) {", "if (true) {"),
    ):
        if guarded in html:
            return html.replace(guarded, unguarded, 1)
    return None


def m_text_visible_on_load(html: str, page: dict) -> str | None:
    if not page["text_sentinels"]:
        return None
    inject = (
        "<style>.sentinel-text{display:block !important;}</style>"
        "<script>document.querySelectorAll('template').forEach(function(t){"
        "document.body.appendChild(t.content.cloneNode(true));});</script>"
    )
    return html.replace("</body>", inject + "\n</body>")


def m_external_dep(html: str, page: dict) -> str | None:
    return html.replace(
        "</head>",
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter">\n</head>',
    )


def _text_visible_expected(page: dict) -> set[str]:
    """A3 always; A6 too where the page's own reveal appends.

    The mutation clones every <template> at load. On a page whose interaction also
    clones, the token then renders twice and A6's "exactly 1" text assertion fires.
    On a display:none page the mutation only unhides text that is already there
    once, so the count stays 1 and A6 is unaffected.
    """
    source = (REPO_ROOT / page["path"]).read_text()
    return {"A3", "A6"} if m_text_guard_removal(source, page) is not None else {"A3"}


def _guard_expected(page: dict) -> set[str]:
    # guard_observable is a claim under test: true => A6 must fail under mutation,
    # false => A6 must pass. null on a built page is an error, handled by the caller.
    return {"A6"} if page.get("guard_observable") else set()


MUTATIONS: list[Mutation] = [
    # A6 as well as A3: the load-time fetch and the interaction-time fetch are two
    # attempts on the same URL, which A6's absolute "exactly 1" assertion catches.
    Mutation("fires-on-load", "sentinel fetched at load, no interaction",
             m_fires_on_load, lambda p: {"A3", "A6"}),
    Mutation("static-leak-comment", "sentinel path literal in an HTML comment",
             m_static_leak_comment, lambda p: {"A4"}),
    Mutation("static-leak-preload", "sentinel path in <link rel=preload>",
             m_static_leak_preload, lambda p: {"A3", "A4"}),
    # A6 as well as A2: zero attempts on the declared URL also violates "exactly 1".
    Mutation("dead-handler-url", "assembled URL never reaches the sentinel path",
             m_dead_handler_url, lambda p: {"A2", "A6"}),
    # A6 as well as A2: with the listener dead the token renders 0 times, which
    # violates A6's "exactly 1" text assertion just as 0 fetches violates the
    # resource one. Same entanglement as dead-handler-url.
    Mutation("dead-handler-event", "reveal listener never fires",
             m_dead_handler_event, lambda p: {"A2", "A6"}),
    Mutation("text-visible-on-load", "text sentinel rendered without interaction",
             m_text_visible_on_load, _text_visible_expected),
    Mutation("guard-removal", "already-fetched guard disabled",
             m_guard_removal, _guard_expected),
    Mutation("text-guard-removal", "text reveal applied twice (template cloned again)",
             m_text_guard_removal, _guard_expected),
    Mutation("external-dep", "page loads an off-origin stylesheet",
             m_external_dep, lambda p: {"A5"}),
]

REQUIRED_COVERAGE = {  # spec §11: every mechanism carries at least these
    "A3": ("fires-on-load", "text-visible-on-load"),
    "A4": ("static-leak-comment", "static-leak-preload"),
    "A2": ("dead-handler-url", "dead-handler-event"),
    "A6": ("guard-removal", "text-guard-removal"),
}


# --------------------------------------------------------------------------- #
# harness plumbing                                                              #
# --------------------------------------------------------------------------- #

def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def build_temp_corpus(tmp: Path) -> None:
    shutil.copy2(REPO_ROOT / "manifest.json", tmp / "manifest.json")
    shutil.copytree(REPO_ROOT / "schema", tmp / "schema")
    shutil.copytree(REPO_ROOT / "pages", tmp / "pages")
    shutil.copytree(REPO_ROOT / "sentinel", tmp / "sentinel")
    (tmp / "logs").mkdir(exist_ok=True)


def wait_for_server(base_url: str, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(base_url, timeout=2).read()
            return True
        except urllib.error.HTTPError:
            return True
        except OSError:
            time.sleep(0.15)
    return False


def run_acceptance(tmp: Path, page_id: str, attempts: int = 3) -> dict:
    """Serve the temp corpus and run acceptance against it.

    The port is chosen, released and re-bound by a separate process, so an
    ephemeral-port race is possible; retry rather than lose the run.
    """
    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            return _run_acceptance_once(tmp, page_id, free_port())
        except RuntimeError as exc:
            last_error = str(exc)
            if attempt < attempts:
                time.sleep(1.0)
    raise RuntimeError(f"{last_error} (after {attempts} attempts)")


def _run_acceptance_once(tmp: Path, page_id: str, port: int) -> dict:
    base_url = f"http://127.0.0.1:{port}"
    log = tmp / "logs" / "requests.jsonl"
    out = tmp / "result.json"
    out.unlink(missing_ok=True)
    server = subprocess.Popen(
        [PYTHON, str(REPO_ROOT / "tools" / "serve.py"),
         "--port", str(port), "--root", str(tmp), "--log", str(log), "--reset"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    try:
        if not wait_for_server(base_url):
            server.terminate()
            stderr = (server.stderr.read() or "").strip().splitlines()
            detail = stderr[-1] if stderr else "no stderr"
            raise RuntimeError(f"temp server never came up on {base_url}: {detail}")
        subprocess.run(
            [PYTHON, str(REPO_ROOT / "tools" / "acceptance.py"),
             "--manifest", str(tmp / "manifest.json"), "--root", str(tmp),
             "--base-url", base_url, "--log", str(log),
             "--only", page_id, "--json", str(out)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=False,
        )
        if not out.exists():
            raise RuntimeError("acceptance.py produced no JSON result")
        return json.loads(out.read_text())["pages"][page_id]
    finally:
        server.terminate()
        server.wait(timeout=10)


def failing_tests(result: dict) -> set[str]:
    return {t for t, status in result["status"].items() if status == "FAIL"}


# --------------------------------------------------------------------------- #
# main                                                                          #
# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", default=str(REPO_ROOT / "manifest.json"))
    ap.add_argument("--page", action="append", default=[], help="restrict to page id(s)")
    ap.add_argument("--mutation", action="append", default=[], help="restrict to mutation name(s)")
    ap.add_argument("--derive", action="store_true",
                    help="write observed guard_observable (and its evidence) back to the manifest")
    args = ap.parse_args()

    manifest = json.loads(Path(args.manifest).read_text())
    pages = [p for p in manifest["pages"] if (REPO_ROOT / p["path"]).is_file()]
    if args.page:
        pages = [p for p in pages if p["id"] in set(args.page)]
    mutations = [m for m in MUTATIONS if not args.mutation or m.name in set(args.mutation)]
    if not pages:
        print("no built page matches the filter")
        return 2

    results: dict[str, dict[str, str]] = {}
    problems: list[str] = []
    exemptions: list[str] = []
    derived: dict[str, tuple[bool, str]] = {}

    for page in pages:
        page_id = page["id"]
        results[page_id] = {}
        source = (REPO_ROOT / page["path"]).read_text()
        for mut in mutations:
            mutated = mut.transform(source, page)

            # A transform that returns the source unchanged has silently failed to
            # match. Without this check it is indistinguishable from a page that
            # survived the mutation, which is the same defect class as a leaked loop
            # variable judging every page against the wrong source: the apparatus
            # reporting success for work it never did.
            if mutated is not None and mutated == source:
                results[page_id][mut.name] = NOOP
                problems.append(
                    f"{page_id} / {mut.name} ({mut.description}): transform matched nothing and "
                    "left the source unchanged, so the mutation was never actually applied"
                )
                print(f"  {page_id} / {mut.name}: {NOOP} (transform matched nothing)", flush=True)
                continue

            if mutated is None:
                results[page_id][mut.name] = NA
                if mut.name == "guard-removal" and args.derive and not page["resource_sentinels"]:
                    # No resource sentinels means no fetch guard to remove. A text page
                    # may still be observable through text-guard-removal, so only record
                    # false when neither kind of guard exists. Never left null.
                    if m_text_guard_removal(source, page) is None:
                        derived[page_id] = (
                            False,
                            "no resource sentinels and the text reveal only toggles a class, "
                            "so no guard of either kind exists to observe",
                        )
                        print(f"  {page_id} / {mut.name}: n/a - derived guard_observable=false "
                              "(no guard of either kind)", flush=True)
                continue
            try:
                with tempfile.TemporaryDirectory(prefix="mutation-") as tmpdir:
                    tmp = Path(tmpdir)
                    build_temp_corpus(tmp)
                    (tmp / page["path"]).write_text(mutated)
                    observed = failing_tests(run_acceptance(tmp, page_id))
            except (RuntimeError, json.JSONDecodeError, KeyError) as exc:
                # Contain the failure: one bad run must not discard the matrix, and
                # it must never be mistaken for a mutation that behaved correctly.
                results[page_id][mut.name] = ERROR
                problems.append(f"{page_id} / {mut.name}: harness error: {exc}")
                print(f"  {page_id} / {mut.name}: {ERROR} ({exc})", flush=True)
                continue

            if mut.name == "text-guard-removal" and args.derive:
                evidence = ("A6 FAILS under text-guard-removal" if "A6" in observed
                            else "A6 PASSES under text-guard-removal")
                derived[page_id] = ("A6" in observed, evidence)
                results[page_id][mut.name] = "derived: " + ("true" if "A6" in observed else "false")
                print(f"  {page_id} / {mut.name}: derived guard_observable="
                      f"{'true' if 'A6' in observed else 'false'}", flush=True)
                continue

            if mut.name == "guard-removal" and args.derive:
                evidence = "A6 FAILS under guard-removal" if "A6" in observed else "A6 PASSES under guard-removal"
                derived[page_id] = ("A6" in observed, evidence)
                results[page_id][mut.name] = "derived: " + ("true" if "A6" in observed else "false")
                print(f"  {page_id} / {mut.name}: derived guard_observable="
                      f"{'true' if 'A6' in observed else 'false'}", flush=True)
                continue

            if mut.name in ("guard-removal", "text-guard-removal") and page.get("guard_observable") is None:
                results[page_id][mut.name] = MISMATCH
                problems.append(
                    f"{page_id}: guard_observable is null on a built page; run --derive"
                )
                continue

            expected = mut.expected(page)
            if observed == expected:
                results[page_id][mut.name] = OK
            else:
                results[page_id][mut.name] = MISMATCH
                detail = (
                    f"{page_id} / {mut.name} ({mut.description}): "
                    f"expected {sorted(expected) or 'no failures'}, observed {sorted(observed) or 'no failures'}"
                )
                if mut.name in ("guard-removal", "text-guard-removal"):
                    detail += (
                        f" - manifest claims guard_observable={page['guard_observable']}, "
                        "which the mutation contradicts"
                    )
                problems.append(detail)
            print(f"  {page_id} / {mut.name}: {results[page_id][mut.name]}", flush=True)

    if args.derive and derived:
        raw = json.loads(Path(args.manifest).read_text())
        for entry in raw["pages"]:
            if entry["id"] in derived:
                observable, evidence = derived[entry["id"]]
                entry["guard_observable"] = observable
                note = entry.get("notes", "").rstrip()
                note = re.sub(r"\s*guard_observable derived empirically:.*$", "", note)
                entry["notes"] = f"{note} guard_observable derived empirically: {evidence}.".strip()
        Path(args.manifest).write_text(json.dumps(raw, indent=2) + "\n")
        print(f"\nmanifest updated: guard_observable derived for {len(derived)} page(s)")

    # coverage: every built page must carry the four required mutations
    for page in pages:
        page_source = (REPO_ROOT / page["path"]).read_text()
        for test, names in REQUIRED_COVERAGE.items():
            if args.mutation and not any(n in set(args.mutation) for n in names):
                continue
            applied = [n for n in names if results[page["id"]].get(n) not in (None, NA, NOOP, ERROR)]
            if applied:
                continue
            if test == "A4" and page.get("expect_static_discoverable"):
                exemptions.append(
                    f"{page['id']}: A4 not exercised - page declares "
                    "expect_static_discoverable, so the sentinel path is in the served "
                    "HTML by construction and there is no leak to introduce"
                )
                continue
            if test == "A4" and not page["resource_sentinels"]:
                # A4 asserts that no sentinel path leaks into the HTML. A page that
                # declares no resource sentinels has no path that could leak, so there
                # is nothing to assert and nothing to mutate. Its text sentinels are
                # deliberately present in the source (§2 allows the display:none
                # variant), so a "text leak" check would fail by construction.
                exemptions.append(
                    f"{page['id']}: A4 not exercised - page declares no resource sentinels, "
                    "so there is no sentinel path that could leak into the HTML"
                )
                continue
            if (test == "A6" and not page["resource_sentinels"]
                    and m_text_guard_removal(page_source, page) is None):
                # A6 asserts both resource-fetch and text idempotence, but this page has
                # neither a fetch guard nor an appending text reveal: repeating its action
                # cannot duplicate anything. Announced, never silently skipped.
                exemptions.append(
                    f"{page['id']}: A6 not exercised - page declares no resource sentinels and "
                    "its text reveal only toggles a class, so there is no guard of either kind "
                    "to remove; idempotence is structural"
                )
                continue
            problems.append(
                f"{page['id']}: no applicable mutation exercises {test} "
                f"(candidates: {', '.join(names)})"
            )

    print()
    names = [m.name for m in mutations]
    width = max([len(p["id"]) for p in pages] + [4]) + 2
    colw = max(len(n) for n in names) + 2
    print("page".ljust(width) + "".join(n.center(colw) for n in names))
    print("-" * (width + colw * len(names)))
    for page in pages:
        print(page["id"].ljust(width)
              + "".join(results[page["id"]].get(n, NA).center(colw) for n in names))

    if exemptions:
        print("\nnot exercised (stated, not skipped):")
        for exemption in exemptions:
            print(f"  - {exemption}")

    if problems:
        print("\nproblems:")
        for problem in problems:
            print(f"  - {problem}")
        print(f"\n{len(problems)} problem(s)")
        return 1
    print(f"\nall mutations behaved as declared across {len(pages)} page(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
