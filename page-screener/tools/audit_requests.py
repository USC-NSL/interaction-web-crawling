#!/usr/bin/env python3
"""Audit a screened page's new-request list for double counting.

A demo gallery renders the same component many times. If the reveal count is
inflated by the same asset being counted once per instance, the count means
nothing. This groups the new requests by filename, by directory and by extension
so that pattern is visible rather than assumed.
"""

import argparse
import json
import sys
import urllib.parse
from collections import Counter
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--results", default=str(PKG_ROOT / "out" / "results.json"))
    ap.add_argument("--url", required=True)
    args = ap.parse_args()

    rows = {r["url"]: r for r in json.loads(Path(args.results).read_text())}
    row = rows.get(args.url)
    if row is None:
        print(f"no such url in results: {args.url}")
        return 2

    urls = row.get("new_requests_within_load") or row.get("new_requests_content") or []
    print(f"{args.url}")
    print(f"  new content requests recorded: {len(urls)}")
    print(f"  distinct URLs:                 {len(set(urls))}")

    names = [urllib.parse.urlsplit(u).path.rsplit("/", 1)[-1] for u in urls]
    dirs = [urllib.parse.urlsplit(u).path.rsplit("/", 1)[0] for u in urls]
    exts = [n.rsplit(".", 1)[-1].lower() if "." in n else "(none)" for n in names]

    print(f"  distinct filenames:            {len(set(names))}")
    dupes = {n: c for n, c in Counter(names).items() if c > 1}
    if dupes:
        print(f"  filenames appearing more than once ({len(dupes)}):")
        for name, count in sorted(dupes.items(), key=lambda kv: -kv[1])[:15]:
            print(f"      {count}x  {name}")
        print("      -> same asset under different paths: check for per-instance counting")
    else:
        print("  no filename appears twice: every new request is a distinct asset")

    print("  by extension:", dict(Counter(exts).most_common(10)))
    print("  top directories:")
    for directory, count in Counter(dirs).most_common(8):
        print(f"      {count:4}  {directory}")
    print("  sample:")
    for u in urls[:8]:
        print(f"      {u}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
