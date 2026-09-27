#!/usr/bin/env python3
"""Phase 2 (replacement): content peeks for ambiguous files through the Drive API, not the mount.

  gdrive-organizer peek --db private/index.sqlite --config private/config.json \
      --client-secret private/client_secret.json --token token_read.json --limit 500

Why not the mount: Google does not document partial-content support for its File Provider
("you need to fully download files before you can examine them"), and `fileproviderctl evict`
was removed in macOS 14.4, so "read 1.5 KB then evict" downloads whole files and cannot clean up.
Here nothing touches local disk except this SQLite file:
  * Google Docs/Sheets/Slides: files.export as text/plain or text/csv, truncated
  * text-like files: HTTP Range request for the first 4 KB only
  * PDF / DOCX up to --max-bytes: downloaded into memory, first page / first paragraphs
  * images and scans: skipped (Drive already OCRs them for search; see README for scoped
    fullText queries)

Candidates: not sensitive, not inside an atomic unit, and either a generic name (config
generic_name_regex) or a loose document at depth 0-1. --max-depth N narrows that to items at
depth <= N (0 = the top level of My Drive) and then takes every document there, not only
generic names.
Any snippet matching no_snippet_regex is discarded and only flagged. Snippets stay in SQLite;
feed an LLM (or a local model) only unflagged snippets.
--only-keys FILE peeks exactly the Drive IDs listed in FILE (one per line), for when names
already explain most files and only a few need a look; sensitivity rules still apply.
--include-sensitive also peeks sensitive-named items and keeps the text of matching snippets
(still marked flagged=1). Use it only when you have decided that content may be shown to your
assistant. Protected folders are never affected: they are not in the index at all.
"""
from __future__ import annotations

import argparse
import io
import re
import signal
import sys
import threading
import zipfile
from contextlib import contextmanager

from . import core as g
from . import drive_api as gdrive

PROG = "gdrive-organizer peek"

DOC_EXTS = {".pdf", ".docx", ".doc", ".txt", ".md", ".csv", ".rtf", ".odt", ".xlsx", ".json",
            ".yaml", ".yml", ".conf", ".cfg", ".ini", ".log", ".xml", ".html", ".sh", ".py"}
TEXTY = ("text/", "application/json", "application/xml", "application/x-yaml",
         "application/x-sh", "application/javascript")
EXPORT = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.presentation": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
}


def candidates(db, cfg, limit, max_depth=None, include_sensitive=False, only_keys=None):
    gen = re.compile(cfg["generic_name_regex"])
    sens = "" if include_sensitive else "AND sensitive=0 "
    rows = db.execute(
        "SELECT key,name,depth,kind,ext,size,mime FROM items WHERE kind IN ('file','gdoc') "
        f"{sens}AND under_atomic IS NULL AND key NOT IN (SELECT key FROM snippets)"
    ).fetchall()
    out = []
    for r in rows:
        doc = r["ext"] in DOC_EXTS or r["kind"] == "gdoc"
        if only_keys is not None:
            keep = r["key"] in only_keys
        elif max_depth is not None:
            keep = r["depth"] <= max_depth and (doc or gen.match(r["name"]))
        else:
            keep = gen.match(r["name"]) or (r["depth"] <= 1 and doc)
        if keep:
            out.append(r)
        if limit and len(out) >= limit:
            break
    return out


# Files peeked here can come from anyone who shares a folder with you, so the parsers get
# bounded input: at most max_bytes downloaded whatever the index said, a DOCX (a zip) must
# not expand past ZIP_MAX_TOTAL or compress suspiciously well (a zip bomb), and each parse
# gets PARSE_SECONDS. A refusal is recorded as that file's error and the run continues.
PARSE_SECONDS = 20
ZIP_MAX_TOTAL = 64 * 1024 * 1024
ZIP_MAX_RATIO = 200


class PeekRefused(Exception):
    pass


def _download_capped(svc, key, max_bytes):
    req = svc.files().get_media(fileId=key)
    req.headers["Range"] = f"bytes=0-{max_bytes}"
    data = gdrive.call(req)
    if len(data) > max_bytes:
        raise PeekRefused(f"file is larger than {max_bytes} bytes")
    return data


def _check_zip(data):
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        total = 0
        for info in zf.infolist():
            total += info.file_size
            if total > ZIP_MAX_TOTAL:
                raise PeekRefused("document expands beyond the safety limit")
            if info.compress_size and info.file_size / info.compress_size > ZIP_MAX_RATIO:
                raise PeekRefused("document is compressed suspiciously well (zip bomb?)")


@contextmanager
def _time_limit(seconds):
    if not hasattr(signal, "SIGALRM") or threading.current_thread() is not threading.main_thread():
        yield
        return

    def _expired(*_):
        raise PeekRefused(f"parsing took longer than {seconds}s")

    previous = signal.signal(signal.SIGALRM, _expired)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def peek(svc, r, max_bytes, chars):
    mime = r["mime"] or ""
    if mime in EXPORT:
        data = gdrive.call(svc.files().export(fileId=r["key"], mimeType=EXPORT[mime]))
        return "export", data.decode("utf-8", "replace")[:chars]
    if mime.startswith(TEXTY) or r["ext"] in {".txt", ".md", ".csv", ".json", ".yaml", ".yml",
                                               ".conf", ".cfg", ".ini", ".log", ".xml", ".sh", ".py"}:
        req = svc.files().get_media(fileId=r["key"])
        req.headers["Range"] = "bytes=0-4095"
        return "range4k", gdrive.call(req).decode("utf-8", "replace")[:chars]
    size = r["size"] or 0
    if r["ext"] == ".pdf" and 0 < size <= max_bytes:
        from pypdf import PdfReader
        data = _download_capped(svc, r["key"], max_bytes)
        with _time_limit(PARSE_SECONDS):
            rd = PdfReader(io.BytesIO(data))
            return "pdf_p1", (rd.pages[0].extract_text() or "")[:chars] if rd.pages else ""
    if r["ext"] == ".docx" and 0 < size <= max_bytes:
        import docx
        data = _download_capped(svc, r["key"], max_bytes)
        _check_zip(data)
        with _time_limit(PARSE_SECONDS):
            d = docx.Document(io.BytesIO(data))
            return "docx", "\n".join(p.text for p in d.paragraphs)[:chars]
    return "skip", None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog=PROG)
    ap.add_argument("--db", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--client-secret", required=True)
    ap.add_argument("--token", default="private/token_read.json")
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--max-bytes", type=int, default=15 * 1024 * 1024)
    ap.add_argument("--chars", type=int, default=1500)
    ap.add_argument("--max-depth", type=int, help="only items at depth <= N (0 = My Drive top level)")
    ap.add_argument("--only-keys", help="file of Drive IDs (one per line) to peek, nothing else")
    ap.add_argument("--include-sensitive", action="store_true",
                    help="also peek sensitive-named items and keep flagged snippet text")
    a = ap.parse_args(argv)
    cfg = g.load_config(a.config)
    guard = g.Guard(cfg)
    db = g.open_db(a.db)
    if g.get_meta(db, "backend") != "drive":
        raise SystemExit("peek_drive needs an index built by `index-drive` (Drive IDs)")
    g.load_protected(db, guard)
    svc = gdrive.service("read", a.client_secret, a.token)
    only = None
    if a.only_keys:
        with open(a.only_keys, encoding="utf-8") as fh:
            only = {ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")}
    cands = candidates(db, cfg, a.limit, a.max_depth, a.include_sensitive, only)
    stats = {"candidates": len(cands), "ok": 0, "flagged": 0, "skipped": 0, "errors": 0}
    for r in cands:
        method, text, flagged, err = "skip", None, 0, None
        try:
            method, text = peek(svc, r, a.max_bytes, a.chars)
        except Exception as ex:  # keep going; record per-file failure
            err = repr(ex)[:300]
            stats["errors"] += 1
        if err:
            method = "error"
        elif text and guard.no_snippet and guard.no_snippet.search(text):
            flagged = 1
            if not a.include_sensitive:
                text = None
            stats["flagged"] += 1
        elif method == "skip":
            stats["skipped"] += 1
        else:
            stats["ok"] += 1
        db.execute("INSERT OR REPLACE INTO snippets(key,method,text,flagged,error) VALUES(?,?,?,?,?)",
                   (r["key"], method, text, flagged, err))
        db.commit()
    print(stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
