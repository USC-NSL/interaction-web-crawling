#!/usr/bin/env python3
"""Ad and tracker blocking, shared by the screener and the exploration oracle.

Brave's adblock-rust engine (via the `adblock` package) loaded with **Brave's own
filter lists**, copied byte-for-byte out of a Browsertrix crawl profile by
`tools/sync_brave_lists.py`. Same engine, same lists, so a pool measured by the
explorer and a crawl measured by Browsertrix are filtered against the same rules.

That is why this does not download EasyList itself. Browsertrix runs Brave with
shields on, and Brave's components carry uBlock filters, EasyList, EasyPrivacy, the
EasyList cookie list and Brave's own lists; an explorer loading EasyList+EasyPrivacy
from easylist.to blocks a strict subset of that, which leaves the pool holding
requests the crawl never had a chance to make and biases every config's recall
downward.

**Network filtering only.** Brave's cosmetic filtering, farbling and storage
partitioning are not replicated: they change how a page looks and what it can read,
not which requests it makes.

One engine per list rather than one engine over all five: adblock-rust reports that a
network rule matched but not the rule's text, so knowing *which list* caught a request
is the only attribution available, and it is worth the extra lookups.
"""

import json
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent

# Config: where the Brave lists live. About 9 MB, deliberately not in git; recreate
# with tools/sync_brave_lists.py. Override per run with --brave-lists.
LISTS_DIR = PKG_ROOT / "cache" / "brave-lists"

# Playwright resource types -> adblock-rust request types. Anything unmapped is
# "other", which is what adblock-rust itself falls back to.
RESOURCE_TYPES = {
    "document": "document",
    "stylesheet": "stylesheet",
    "image": "image",
    "media": "media",
    "font": "font",
    "script": "script",
    "texttrack": "media",
    "xhr": "xmlhttprequest",
    "fetch": "xmlhttprequest",
    "prefetch": "other",
    "eventsource": "other",
    "websocket": "websocket",
    "manifest": "other",
    "signedexchange": "other",
    "ping": "ping",
    "cspviolationreport": "csp_report",
    "preflight": "other",
    "other": "other",
}

MISSING_HELP = """\
REFUSING TO RUN: the Brave filter lists are not available at
  {path}

They are copied out of a Browsertrix crawl profile and kept out of git (~9 MB).
Recreate them with:

  .venv/bin/python tools/sync_brave_lists.py            # newest crawl profile
  .venv/bin/python tools/sync_brave_lists.py --profile <crawl profile dir>

Blocking must not silently fall back to a different blocklist: the pool would then
be filtered against rules the crawl never used, and recall numbers from before and
after the change would not be comparable.
{detail}"""


class BraveListsMissing(RuntimeError):
    """The configured list directory is absent, incomplete or unreadable."""


class Blocker:
    """Brave's network filter lists, queried per request."""

    def __init__(self, lists_dir: Path | str | None = None):
        import adblock  # imported here so the module can be inspected without the dep

        self.lists_dir = Path(lists_dir) if lists_dir else LISTS_DIR
        manifest_path = self.lists_dir / "manifest.json"
        if not manifest_path.is_file():
            raise BraveListsMissing(MISSING_HELP.format(
                path=self.lists_dir,
                detail=f"\n(no manifest.json at {manifest_path})"))
        try:
            self.manifest = json.loads(manifest_path.read_text())
            entries = self.manifest["lists"]
        except Exception as exc:  # noqa: BLE001
            raise BraveListsMissing(MISSING_HELP.format(
                path=self.lists_dir, detail=f"\n(unreadable manifest: {exc})")) from exc
        if not entries:
            raise BraveListsMissing(MISSING_HELP.format(
                path=self.lists_dir, detail="\n(manifest lists no filter lists)"))

        self.engines: dict[str, "adblock.Engine"] = {}
        self.list_info: dict[str, dict] = {}
        for entry in entries:
            path = self.lists_dir / entry["path"]
            if not path.is_file():
                raise BraveListsMissing(MISSING_HELP.format(
                    path=self.lists_dir,
                    detail=f"\n(manifest names {entry['path']}, which is not there)"))
            text = path.read_text(errors="replace")
            filters = adblock.FilterSet()
            filters.add_filter_list(text)
            name = entry["component_name"]
            self.engines[name] = adblock.Engine(filter_set=filters)
            self.list_info[name] = {
                "component_id": entry["component_id"],
                "version": entry["version"],
                "list_title": entry.get("list_title", ""),
                "bytes": entry["bytes"],
                "lines": entry["lines"],
                "sha256": entry["sha256"],
                "rules": sum(1 for ln in text.splitlines()
                             if ln.strip() and not ln.startswith("!")),
            }

    def match(self, url: str, source_url: str, resource_type: str) -> dict | None:
        """The list and rule that block this request, or None."""
        kind = RESOURCE_TYPES.get((resource_type or "other").lower(), "other")
        for name, engine in self.engines.items():
            try:
                result = engine.check_network_urls(url=url, source_url=source_url,
                                                   request_type=kind)
            except Exception:  # noqa: BLE001 - never let the blocker break a page
                continue
            if result.matched:
                return {"list": name, "component_id": self.list_info[name]["component_id"],
                        "version": self.list_info[name]["version"],
                        "rule": result.filter or "", "request_type": kind,
                        "important": bool(getattr(result, "important", False))}
        return None

    def describe(self) -> dict:
        """What every result was filtered against, recorded with the result."""
        return {
            "engine": "adblock-rust (Brave) via python-adblock",
            "source": "Brave's own component lists, copied from a Browsertrix crawl "
                      "profile by tools/sync_brave_lists.py",
            "lists_dir": str(self.lists_dir),
            "copied_at": self.manifest.get("copied_at"),
            "source_profile": self.manifest.get("source_profile"),
            "scope": "network filtering only; Brave's cosmetic filtering, farbling and "
                     "storage partitioning are not replicated (they do not change which "
                     "requests a page makes)",
            "rule_text": "python-adblock 0.6.0 reports the matching list and that a "
                         "network rule matched, but not the rule text",
            "lists": self.list_info,
        }


def install(page, blocker: Blocker, on_block, page_url_getter=None) -> None:
    """Route every request through the blocker; abort what it matches.

    Must be registered so that it runs before any other route handler: a blocked
    request must never reach a navigation guard or the network. Non-matching requests
    fall through to whatever else is registered.

    on_block(url, resource_type, match_info) records the block. The main frame's own
    document is never blocked - the page under test is the measurement, not an ad.
    """
    def handler(route, request):
        try:
            if request.frame == page.main_frame and request.resource_type == "document":
                route.fallback()
                return
            source = (page_url_getter() if page_url_getter else page.url) or request.url
            hit = blocker.match(request.url, source, request.resource_type)
            if hit:
                on_block(request.url, request.resource_type, hit)
                route.abort("blockedbyclient")
                return
        except Exception:  # noqa: BLE001 - a blocker fault must not break the page
            pass
        route.fallback()

    page.route("**/*", handler)
