#!/usr/bin/env python3
"""Per-page sentinel recall and timing for one Browsertrix crawl of the corpus.

Resource sentinels: WARC response/revisit records for /sentinel/ URLs, status 200
only. Every sentinel PNG is byte-identical, so a duplicate-payload revisit record
must count exactly like a response - the HTTP status is read from both.

Text sentinels: from the `--text` extraction in pages.jsonl, not from WARC response
bodies (the hidden text is in the served HTML either way). Note what that text is:
Browsertrix snapshots it BEFORE behaviors run, and reads every text node without
checking visibility. A supplementary column reads the post-behavior urn:textFinal
resource records, which exist only when some behavior was enabled.

Cross-check: the set of /sentinel/ paths in the WARC must equal the set the server
logged for the same run. If they disagree the run is not trustworthy, so this
exits non-zero and prints the delta instead of reporting numbers.
"""

import argparse
import json
import re
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path

from warcio.archiveiterator import ArchiveIterator

PKG_ROOT = Path(__file__).resolve().parent.parent
CORPUS = PKG_ROOT.parent / "interaction-corpus"
TEXT_RE = re.compile(r"SENTINEL-TEXT-[A-Z0-9-]+")


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def page_for_url(url: str, pages: list[dict]) -> dict | None:
    path = urllib.parse.urlsplit(url).path.lstrip("/")
    for page in pages:
        if path == page["path"]:
            return page
    return None


def analyse(run_dir: Path) -> dict:
    name = run_dir.name
    coll = run_dir / "crawls" / "collections" / name
    pages = json.loads((CORPUS / "manifest.json").read_text())["pages"]

    # ---- WARC ----------------------------------------------------------------
    warc_sentinels: dict[str, dict] = {}     # path -> {statuses, record_types}
    final_text: dict[str, str] = {}          # page url -> post-behavior text
    record_types: dict[str, int] = {}
    for warc in sorted((coll / "archive").glob("*.warc.gz")):
        with open(warc, "rb") as fh:
            for rec in ArchiveIterator(fh):
                record_types[rec.rec_type] = record_types.get(rec.rec_type, 0) + 1
                target = rec.rec_headers.get_header("WARC-Target-URI") or ""
                if rec.rec_type in ("response", "revisit"):
                    path = urllib.parse.urlsplit(target).path
                    if path.startswith("/sentinel/"):
                        status = None
                        if rec.http_headers is not None:
                            try:
                                status = int(rec.http_headers.get_statuscode())
                            except (TypeError, ValueError):
                                status = None
                        entry = warc_sentinels.setdefault(path, {"statuses": [], "record_types": []})
                        entry["statuses"].append(status)
                        entry["record_types"].append(rec.rec_type)
                elif rec.rec_type == "resource" and target.startswith("urn:textFinal:"):
                    final_text[target[len("urn:textFinal:"):]] = (
                        rec.content_stream().read().decode("utf-8", "replace"))

    # ---- server log: the independent witness ----------------------------------
    server_paths: set[str] = set()
    log_path = run_dir / "requests.jsonl"
    for line in log_path.read_text().splitlines() if log_path.exists() else []:
        if line.strip():
            server_paths.add(json.loads(line)["path"])

    warc_paths = set(warc_sentinels)
    cross_check = {
        "agree": warc_paths == server_paths,
        "only_in_warc": sorted(warc_paths - server_paths),
        "only_in_server_log": sorted(server_paths - warc_paths),
        "warc_count": len(warc_paths),
        "server_count": len(server_paths),
    }

    # ---- pages.jsonl text, and per-page timing ---------------------------------
    initial_text: dict[str, str] = {}
    for line in (coll / "pages" / "pages.jsonl").read_text().splitlines():
        obj = json.loads(line)
        if "url" in obj:
            initial_text[obj["url"]] = obj.get("text") or ""

    starts: dict[str, datetime] = {}
    finishes: dict[str, datetime] = {}
    load_states: dict[str, int] = {}
    behaviors_logged: dict[str, int] = {}
    for log in sorted((coll / "logs").glob("*.log")):
        for line in log.read_text().splitlines():
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            details = obj.get("details")
            page_url = details.get("page") if isinstance(details, dict) else None
            if not page_url:
                continue
            if obj.get("message") == "Awaiting page load" and page_url not in starts:
                starts[page_url] = parse_ts(obj["timestamp"])
            elif obj.get("message") == "Page Finished":
                finishes[page_url] = parse_ts(obj["timestamp"])
                load_states[page_url] = obj["details"].get("loadState")
            if str(obj.get("message", "")).startswith("Run Behaviors Started"):
                behaviors_logged[page_url] = behaviors_logged.get(page_url, 0) + 1

    # ---- per page --------------------------------------------------------------
    rows = []
    for page in pages:
        url = next((u for u in initial_text if page_for_url(u, [page])), None)
        declared_res = page["resource_sentinels"]
        declared_txt = page["text_sentinels"]
        res_hits = [s for s in declared_res
                    if 200 in (warc_sentinels.get(s, {}).get("statuses") or [])]
        txt_initial = TEXT_RE.findall(initial_text.get(url, "")) if url else []
        txt_hits = [t for t in declared_txt if t in txt_initial]
        final = final_text.get(url) if url else None
        final_source = "textFinal"
        if final is None and url and behaviors_logged.get(url, 0) > 0:
            # Browsertrix skips writing urn:textFinal when the post-behavior text is
            # identical to the initial extraction ("extracted text unchanged"), so an
            # absent record after behaviors ran means "same as pages.jsonl".
            final = initial_text.get(url, "")
            final_source = "unchanged-from-initial"
        elif final is None:
            final_source = "no-behaviors-ran"
        txt_final_hits = ([t for t in declared_txt if t in TEXT_RE.findall(final)]
                          if final is not None else None)
        total = len(declared_res) + len(declared_txt)
        hits = len(res_hits) + len(txt_hits)
        elapsed = None
        if url in starts and url in finishes:
            elapsed = round((finishes[url] - starts[url]).total_seconds(), 2)
        rows.append({
            "id": page["id"],
            "url": url,
            "declared": total,
            "hits": hits,
            "recall": round(hits / total, 3) if total else None,
            "resource_hits": res_hits,
            "text_hits_pages_jsonl": txt_hits,
            "text_hits_final": txt_final_hits,
            "text_final_source": final_source,
            "time_s": elapsed,
            "load_state": load_states.get(url),
            "behavior_runs_logged": behaviors_logged.get(url, 0),
        })

    return {
        "condition": name,
        "cross_check": cross_check,
        "record_types": record_types,
        "sentinel_records": {p: v for p, v in sorted(warc_sentinels.items())},
        "total_declared": sum(r["declared"] for r in rows),
        "total_hits": sum(r["hits"] for r in rows),
        "pages": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run_dir", nargs="+")
    args = ap.parse_args()
    failed = False
    for rd in args.run_dir:
        result = analyse(Path(rd))
        (Path(rd) / "recall.json").write_text(json.dumps(result, indent=2, default=str))
        cc = result["cross_check"]
        if not cc["agree"]:
            failed = True
            print(f"\n!!! CROSS-CHECK FAILED for {result['condition']}: WARC and server log disagree")
            print(f"    WARC sentinel paths:   {cc['warc_count']}")
            print(f"    server sentinel paths: {cc['server_count']}")
            print(f"    only in WARC:          {cc['only_in_warc']}")
            print(f"    only in server log:    {cc['only_in_server_log']}")
        else:
            print(f"{result['condition']}: cross-check OK ({cc['warc_count']} sentinel path(s)); "
                  f"recall {result['total_hits']}/{result['total_declared']}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
