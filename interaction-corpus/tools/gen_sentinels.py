#!/usr/bin/env python3
"""Generate the sentinel PNG tree from manifest.json.

The manifest is the single source of truth: this script hardcodes no page id and
no sentinel URL. Every declared resource sentinel becomes a 1x1 transparent PNG
on disk at the path the URL implies. Content is irrelevant; only the URL matters.
"""

import argparse
import json
import struct
import sys
import zlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def onebyone_png() -> bytes:
    """A 1x1 fully transparent RGBA PNG, built without any image library."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)  # 1x1, 8-bit RGBA
    raw = b"\x00" + b"\x00\x00\x00\x00"  # one scanline: filter 0 + transparent px
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def load_manifest(path: Path) -> dict:
    with path.open() as fh:
        return json.load(fh)


def validate(manifest: dict, schema_path: Path) -> tuple[list[str], bool]:
    """Validate against the JSON schema. Returns (errors, was_actually_checked)."""
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", default=str(REPO_ROOT / "manifest.json"))
    ap.add_argument("--schema", default=str(REPO_ROOT / "schema" / "manifest.schema.json"))
    ap.add_argument("--root", default=str(REPO_ROOT), help="corpus root the URLs are relative to")
    ap.add_argument("--force", action="store_true", help="rewrite files that already exist")
    args = ap.parse_args()

    manifest = load_manifest(Path(args.manifest))
    errors, checked = validate(manifest, Path(args.schema))
    if errors:
        print("manifest FAILS schema validation:")
        for err in errors:
            print(f"  - {err}")
        return 2
    print("manifest: schema valid" if checked else "manifest: NOT schema-checked (jsonschema missing)")

    root = Path(args.root)
    png = onebyone_png()

    declared: list[str] = []
    for page in manifest["pages"]:
        declared.extend(page["resource_sentinels"])

    dupes = {u for u in declared if declared.count(u) > 1}
    if dupes:
        print("manifest declares duplicate sentinel URLs:")
        for url in sorted(dupes):
            print(f"  - {url}")
        return 2

    created = existing = 0
    wanted: set[Path] = set()
    for url in declared:
        dest = root / url.lstrip("/")
        wanted.add(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and not args.force:
            existing += 1
            continue
        dest.write_bytes(png)
        created += 1

    stale = sorted(p for p in (root / "sentinel").rglob("*.png") if p not in wanted)
    for path in stale:
        print(f"stale (declared nowhere in the manifest): /{path.relative_to(root)}")

    print(
        f"sentinels: {len(declared)} declared / {created} created / {existing} already present"
        + (f" / {len(stale)} stale" if stale else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
