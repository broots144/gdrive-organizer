#!/usr/bin/env python3
"""Aggregate report over an index. This is the ONLY thing an LLM should read.

  gdrive-organizer report --db private/index.sqlite --config private/config.json > report.txt

Sizing (estimate): 50,000 raw paths at ~100 bytes is ~5 MB, over a million tokens. This report
is a few hundred lines. Sensitive-looking names are counted here and written only to a local
quarantine file for you to read yourself; protected entries are counted, never named.
"""
from __future__ import annotations

import argparse
import collections
import os
import time

from . import core as g

PROG = "gdrive-organizer report"


def human(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog=PROG)
    ap.add_argument("--db", required=True)
    ap.add_argument("--config")
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--quarantine-out", default="private/quarantine.txt")
    a = ap.parse_args(argv)
    cfg = g.load_config(a.config)
    guard = g.Guard(cfg)
    db = g.open_db(a.db)

    def show(path: str) -> str:
        return "[sensitive-looking name, see quarantine file]" if guard.is_sensitive_path(path) else path
    now = time.time()
    recent = now - cfg["recent_days"] * 86400

    rows = db.execute(
        "SELECT key,path,pathkey,name,depth,kind,ext,size,mtime,dataless,md5,owned_by_me,"
        "viewed_by_me,atomic_root,under_atomic,sensitive FROM items").fetchall()
    files = [r for r in rows if r["kind"] in ("file", "gdoc")]
    out = []
    p = out.append

    p(f"# backend={g.get_meta(db, 'backend')} counts={g.get_meta(db, 'counts')}")
    p(f"protected entries (not named): {db.execute('SELECT COUNT(*) FROM protected').fetchone()[0]}")
    p(f"index errors: {db.execute('SELECT COUNT(*) FROM errors').fetchone()[0]}")
    kinds = collections.Counter(r["kind"] for r in rows)
    p(f"kinds: {dict(kinds)}")
    tot = sum(r["size"] or 0 for r in files)
    p(f"files: {len(files)}  bytes: {human(tot)}")
    dl = [r for r in files if r["dataless"] == 1]
    if any(r["dataless"] is not None for r in files):
        p(f"dataless (cloud-only) files: {len(dl)}  local: {len(files) - len(dl)}")

    p("\n## extensions by bytes")
    by_ext = collections.defaultdict(lambda: [0, 0])
    for r in files:
        by_ext[r["ext"] or "(none)"][0] += 1
        by_ext[r["ext"] or "(none)"][1] += r["size"] or 0
    for ext, (c, b) in sorted(by_ext.items(), key=lambda kv: -kv[1][1])[:30]:
        p(f"{ext:>12} {c:>8} files {human(b):>9}")

    # recursive rollups per directory, attributed to every ancestor up to depth 3
    roll = collections.defaultdict(lambda: [0, 0, 0.0])  # files, bytes, newest mtime
    for r in files:
        parts = r["path"].split("/")[:-1]
        for d in range(1, min(len(parts), 3) + 1):
            k = "/".join(parts[:d])
            v = roll[k]
            v[0] += 1
            v[1] += r["size"] or 0
            v[2] = max(v[2], r["mtime"] or 0)
    p(f"\n## top {a.top} folders (depth<=3) by recursive file count")
    for k, (c, b, m) in sorted(roll.items(), key=lambda kv: -kv[1][0])[:a.top]:
        live = "  LIVE?" if m > recent else ""
        p(f"{c:>8} files {human(b):>9}  newest={time.strftime('%Y-%m-%d', time.localtime(m))}  {show(k)}{live}")
    p(f"\n## top {a.top} folders (depth<=3) by bytes")
    for k, (c, b, m) in sorted(roll.items(), key=lambda kv: -kv[1][1])[:a.top]:
        p(f"{human(b):>9} {c:>8} files  {show(k)}")

    loose = [r for r in files if r["depth"] <= 1 and not r["under_atomic"]]
    p(f"\n## loose files at depth 0-1: {len(loose)}")
    lx = collections.Counter(r["ext"] or "(none)" for r in loose)
    p("   by ext: " + ", ".join(f"{e}:{c}" for e, c in lx.most_common(15)))

    atom = [r for r in rows if r["atomic_root"]]
    in_atom = sum(1 for r in files if r["under_atomic"])
    p(f"\n## atomic units (projects, bundles, heavy dirs): {len(atom)} roots, "
      f"{in_atom} files inside them (move only as whole units)")

    md5 = collections.defaultdict(list)
    for r in files:
        if r["md5"]:
            md5[(r["md5"], r["size"])].append(r)
    groups = [v for v in md5.values() if len(v) > 1]
    wasted = sum((len(v) - 1) * (v[0]["size"] or 0) for v in groups)
    if md5:
        p(f"\n## exact duplicates (md5+size, server-side): {len(groups)} groups, "
          f"{sum(len(v) for v in groups)} files, reclaimable ~{human(wasted)}")

    if any(r["owned_by_me"] is not None for r in files):
        p(f"not owned by me: {sum(1 for r in files if r['owned_by_me'] == 0)} files")
    if any(r["viewed_by_me"] is not None for r in files):
        yrs = now - 2 * 365 * 86400
        p(f"never viewed or not viewed in 2y: "
          f"{sum(1 for r in files if not r['viewed_by_me'] or r['viewed_by_me'] < yrs)} files")

    p(f"\n## top {a.top} largest files (sensitive-looking names excluded)")
    for r in sorted((r for r in files if not r["sensitive"]), key=lambda r: -(r["size"] or 0))[:a.top]:
        p(f"{human(r['size']):>9}  {r['path']}")

    sens = [r for r in rows if r["sensitive"]]
    p(f"\n## sensitive-looking names: {len(sens)} (listed only in {a.quarantine_out}, not here)")
    with open(a.quarantine_out, "w", encoding="utf-8") as fh:
        for r in sorted(sens, key=lambda r: r["path"]):
            fh.write(f"{r['kind']}\t{r['path']}\n")
    os.chmod(a.quarantine_out, 0o600)

    print("\n".join(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
