#!/usr/bin/env python3
"""Unit tests for the request-guard allowances, run against both implementations.

The explorer's Explorer.write_allowance is called directly. RevealClick's
isAllowedWrite is called on the live behavior instance, in a page that has loaded
the built bundle, so the test exercises the shipped code rather than a copy of
its regex. Run with page-screener's venv (it has Playwright):

    ../page-screener/.venv/bin/python tools/test_write_allowances.py [--bundle PATH]

Exits non-zero on any disagreement with the expected verdict.
"""

import argparse
import sys
from pathlib import Path
from types import SimpleNamespace

PKG_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG_ROOT.parent / "page-screener" / "tools"))
from explore import Explorer  # noqa: E402

BUNDLE = PKG_ROOT.parent.parent / "browsertrix-behaviors" / "dist" / "behaviors.js"

RPC = "https://maps.googleapis.com/$rpc/google.internal.maps.mapsjs.v1.MapsJsInternalService/"
CASES = [
    # (url, allowed?)
    (RPC + "GetViewportInfo", True),
    ("https://developerprofiles-pa.googleapis.com/$rpc/google.internal.developerprofiles.v1"
     ".content.ContentService/ListReleaseNotesHistory", True),
    (RPC + "SearchPlaces", True),
    (RPC + "BatchGetItems", True),
    (RPC + "UpdateViewport", False),
    (RPC + "CreateSession", False),
    (RPC + "DeleteItem", False),
    (RPC + "SetPreference", False),
    (RPC + "InitMapsJwt", False),
    (RPC + "getViewportInfo", False),                       # method names are PascalCase
    ("https://maps.googleapis.com/v1/GetViewportInfo", False),  # not a $rpc path
    ("https://example.com/$rpc/svc.Service/GetThing", False),   # not googleapis.com
    ("https://googleapis.com.example.com/$rpc/svc.Service/GetThing", False),
    ("https://abc-dsn.algolia.net/1/indexes/*/queries", True),  # existing allowance
    ("https://abc-dsn.algolia.net/1/indexes/products/browse", False),
]
# Body cases, all to a URL no host allowance covers: only a GraphQL query reads.
API = "https://shop.example.com/graphql"
BODY_CASES = [
    # (body, allowed?)
    ('{"query":"query Reviews { reviews { id } }","operationName":"Reviews"}', True),
    ('{"query":"{ products { id } }"}', True),
    ('[{"query":"query A { a }"},{"query":"query B { b }"}]', True),
    ('{"query":"mutation AddToCart { addToCart(id: 1) { id } }"}', False),
    ('[{"query":"query A { a }"},{"query":"mutation B { b }"}]', False),
    ('{"query":"subscription S { s }"}', False),
    ('{"operationName":"Reviews","extensions":{"persistedQuery":{"sha256Hash":"x"}}}', False),
    ("[]", False),                                           # no operation at all
    ('{"filter":{},"sort":[],"page":{"size":20}}', False),   # allowance 2, rejected
    ("event=summary", False),
]


def explorer_allows(url: str, body: str = "[]") -> bool:
    fake_self = SimpleNamespace(graphql_kind=Explorer.graphql_kind)
    request = SimpleNamespace(url=url, post_data=body)
    return Explorer.write_allowance(fake_self, request) is not None


def revealclick_allows(cases: list[tuple[str, str]], bundle: Path) -> list[bool]:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto("about:blank")
        page.add_script_tag(path=str(bundle))
        result = page.evaluate(
            """(cases) => {
                self.__bx_behaviors.init({autoclick: true, autofetch: false, autoplay: false,
                                          autoscroll: false, siteSpecific: false});
                const rc = self.__bx_behaviors.behaviors.find(
                    (b) => typeof b.isAllowedWrite === 'function');
                if (!rc) throw new Error('RevealClick not in the bundle');
                return cases.map(([u, body]) => rc.isAllowedWrite(u, body));
            }""", cases)
        browser.close()
        return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bundle", type=Path, default=BUNDLE)
    args = ap.parse_args()
    cases = [(u, "[]", ok) for u, ok in CASES] + [(API, b, ok) for b, ok in BODY_CASES]
    rc = revealclick_allows([(u, b) for u, b, _ in cases], args.bundle)
    failures = 0
    for (url, body, expected), got_rc in zip(cases, rc):
        got_ex = explorer_allows(url, body)
        ok = got_ex == expected and got_rc == expected
        failures += not ok
        verdict = "allow" if expected else "block"
        print(f"{'ok  ' if ok else 'FAIL'} expect {verdict:5}  explorer={got_ex!s:5} "
              f"revealclick={got_rc!s:5}  {url[-50:]} {body[:45] if body != '[]' or url == API else ''}")
    print(f"\n{len(cases) - failures}/{len(cases)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
