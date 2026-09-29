#!/usr/bin/env python3
"""Copy Brave's own filter lists out of a Browsertrix crawl profile.

The exploration oracle blocks ads and trackers so that its pool is comparable with a
Browsertrix crawl. Comparable is not the same as similar: Browsertrix runs Brave with
shields on, and Brave's adblock component carries uBlock filters, EasyList,
EasyPrivacy, the EasyList cookie list and Brave's own lists. An explorer loading
EasyList+EasyPrivacy from easylist.to would block a strict subset, leaving the pool
holding requests the crawl never had a chance to make.

So the explorer uses Brave's files, not its own download: same engine (adblock-rust),
byte-identical lists. This tool copies them out of a crawl profile, records each
component's id and version, and hashes every file, so a result can always be traced
back to exactly what it was filtered against.

Only network filtering is replicated. Brave's cosmetic filtering, farbling and
storage partitioning change how a page looks and what it can read, not which requests
it makes, so they are deliberately not imitated.

    .venv/bin/python tools/sync_brave_lists.py                 # newest crawl profile
    .venv/bin/python tools/sync_brave_lists.py --profile <dir> --dest <dir>
"""

import argparse
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CRAWLS = PKG_ROOT.parent / "crawl-eval" / "runs"

# The five Brave components that carry network filter lists, by extension id. Named
# here rather than discovered, so that a profile which is missing one is an error
# instead of a quietly smaller blocklist.
COMPONENTS = {
    "iodkpdagapdfkphljnddpjlldadblomo": "Brave Default Adblock Filters",
    "kihnoaefogbkmblfimmibknnmkllbhlf": "Brave Default Privacy Filters",
    "cdbbhgbmjhfnhnmgeddbliobbofkgdhe": "Cookie notice blocker",
    "adcocjohghhfpidemphmcmlmhnfgikei": "Brave First Party Adblock Filters",
    "bfpgedeaaibpoidldhjcknekahbikncb": "Mobile app promo blocker",
}


def newest_profile(crawls_root: Path) -> Path | None:
    profiles = sorted(crawls_root.glob("*/crawls/collections/*/profile"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    return profiles[0] if profiles else None


def list_title(path: Path) -> str:
    """The list's own '! Title:' header, for the record."""
    with path.open(errors="replace") as fh:
        for _ in range(8):
            line = fh.readline()
            if not line:
                break
            if line.startswith("! Title:"):
                return line.split(":", 1)[1].strip()
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", help="a Browsertrix crawl profile directory "
                                      "(default: the newest under crawl-eval/runs)")
    ap.add_argument("--crawls", default=str(DEFAULT_CRAWLS))
    ap.add_argument("--dest", default=str(PKG_ROOT / "cache" / "brave-lists"))
    args = ap.parse_args()

    profile = Path(args.profile) if args.profile else newest_profile(Path(args.crawls))
    if profile is None or not profile.is_dir():
        print(f"no crawl profile found under {args.crawls}; pass --profile explicitly")
        return 2

    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    entries = []
    for component_id, name in COMPONENTS.items():
        matches = sorted((profile / component_id).glob("*/list.txt"))
        if not matches:
            print(f"MISSING: {name} ({component_id}) has no list.txt in {profile}")
            print("  Brave's components are downloaded per crawl; use a profile from a "
                  "crawl that ran with network access.")
            return 3
        source = matches[-1]
        version = source.parent.name
        target_dir = dest / component_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "list.txt"
        shutil.copyfile(source, target)
        data = target.read_bytes()
        entries.append({
            "component_id": component_id,
            "component_name": name,
            "version": version,
            "list_title": list_title(target),
            "bytes": len(data),
            "lines": data.count(b"\n"),
            "sha256": hashlib.sha256(data).hexdigest(),
            "source": str(source),
            "source_mtime": time.strftime("%Y-%m-%d %H:%M:%S",
                                          time.localtime(source.stat().st_mtime)),
            "path": str(target.relative_to(dest)),
        })
        print(f"  {name:<38} {version:<12} {len(data):>9} B  {entries[-1]['lines']:>7} lines")

    manifest = {
        "copied_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source_profile": str(profile),
        "engine": "adblock-rust (Brave) via python-adblock",
        "note": "Byte-identical copies of the network filter lists Brave loaded during "
                "the Browsertrix crawls. Network filtering only: Brave's cosmetic "
                "filtering, farbling and storage partitioning are not replicated, "
                "because they do not change which requests a page makes.",
        "lists": entries,
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2))
    total = sum(e["bytes"] for e in entries)
    print(f"\nwrote {len(entries)} list(s), {total / 1e6:.1f} MB, and manifest.json to {dest}")
    print("This directory is deliberately not in git; re-run this tool to recreate it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
