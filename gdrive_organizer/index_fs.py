#!/usr/bin/env python3
"""Phase 1, local variant: metadata-only index of the Drive for desktop mount.

  gdrive-organizer index-fs --root "$HOME/Library/CloudStorage/GoogleDrive-<you>/My Drive" --db private/fs_index.sqlite

What it does NOT do:
  * open, read, hash or sniff any file (no hydration of file contents)
  * stat, list or descend into a protected entry (it is matched by name from the parent listing)
  * follow symlinks / Drive shortcuts, or descend into dot-folders, bundles or heavy dirs

What it does cost (fact, Apple TN3150): stat() and directory listing materialize dataless
FOLDERS (metadata only, network round trip each). First run on a cold tree is network bound.

PermissionError aborts the run. A silent skip would make the index look complete when it is not
(TCC denial for the terminal app, or the Claude Code sandbox, both surface as EPERM).
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import stat as st_mod
import sys
import time

from . import core as g

PROG = "gdrive-organizer index-fs"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog=PROG)
    ap.add_argument("--root", required=True, help='path to ".../My Drive"')
    ap.add_argument("--db", required=True)
    ap.add_argument("--config")
    ap.add_argument("--no-xattrs", action="store_true",
                    help="skip reading com.google.drivefs.item-id#S and FinderInfo")
    ap.add_argument("--allow-any-root", action="store_true",
                    help="allow a root outside ~/Library/CloudStorage (tests only)")
    ap.add_argument("--progress-every", type=int, default=2000)
    a = ap.parse_args(argv)

    cfg = g.load_config(a.config)
    guard = g.Guard(cfg)
    guard.warn_if_unprotected()
    # realpath: `apply` renames with RENAME_NOFOLLOW_ANY, which refuses any symlink in the path
    root = os.path.realpath(a.root)
    if "/Library/CloudStorage/" not in root and not a.allow_any_root:
        raise SystemExit("root does not resolve under ~/Library/CloudStorage (Mirror mode keeps "
                         "files elsewhere); pass the real folder with --allow-any-root")
    if guard.is_protected_name(os.path.basename(root)) or guard.text_hits_protected(root):
        raise SystemExit("root itself is or is inside the protected folder; refusing")
    if not os.path.isdir(root):
        raise SystemExit(f"not a directory: {root}")

    db = g.open_db(a.db, fresh=True)
    g.set_meta(db, "backend", "fs")
    g.set_meta(db, "root", root)
    g.set_meta(db, "started", time.time())
    counts = {"items": 0, "dirs": 0, "files": 0, "dataless": 0, "protected": 0, "dot": 0,
              "symlink": 0, "bundle_or_heavy": 0, "errors": 0}
    t0 = time.time()

    stack = [(root, "", None, 0)]
    while stack:
        abs_dir, rel_dir, parent_key, depth = stack.pop()
        try:
            it = os.scandir(abs_dir)
        except PermissionError as e:
            db.commit()
            raise SystemExit(f"FATAL EPERM listing a directory (TCC or sandbox?): {e}. "
                             "Index is incomplete; fix access and rerun.")
        except OSError as e:
            db.execute("INSERT INTO errors(path,err) VALUES(?,?)", (rel_dir, repr(e)))
            counts["errors"] += 1
            continue
        with it:
            for de in it:
                raw = de.name
                rel = f"{rel_dir}/{raw}" if rel_dir else raw
                # 1. protected check happens BEFORE any syscall on the entry
                if guard.is_protected_name(raw):
                    db.execute("INSERT INTO protected(path,pathkey,key,reason) VALUES(?,?,?,?)",
                               (rel, g.pkey(rel), None, "name match"))
                    counts["protected"] += 1
                    continue
                # 2. dot entries: record the name (for .git detection), never descend
                if raw.startswith("."):
                    g.insert_item(db, {"key": f"dot:{rel}", "path": rel, "pathkey": g.pkey(rel),
                                       "name": raw, "parent_key": parent_key, "depth": depth,
                                       "kind": "dot", "ext": g.ext_of(raw)})
                    counts["dot"] += 1
                    continue
                try:
                    s = de.stat(follow_symlinks=False)
                except PermissionError as e:
                    db.commit()
                    raise SystemExit(f"FATAL EPERM on stat (TCC or sandbox?): {e}")
                except OSError as e:
                    db.execute("INSERT INTO errors(path,err) VALUES(?,?)", (rel, repr(e)))
                    counts["errors"] += 1
                    continue
                flags = getattr(s, "st_flags", None)
                dataless = None if flags is None else int(bool(flags & g.SF_DATALESS))
                row = {"key": f"fs:{s.st_dev}:{s.st_ino}", "path": rel, "pathkey": g.pkey(rel),
                       "name": raw, "parent_key": parent_key, "depth": depth,
                       "ext": g.ext_of(raw), "size": s.st_size, "mtime": s.st_mtime,
                       "dataless": dataless, "ino": s.st_ino}
                descend = False
                if st_mod.S_ISLNK(s.st_mode):
                    row["kind"] = "symlink"
                    try:
                        tgt = os.readlink(de.path)
                    except OSError:
                        tgt = None
                    if tgt and guard.text_hits_protected(tgt):
                        db.execute(
                            "INSERT INTO protected(path,pathkey,key,reason) VALUES(?,?,?,?)",
                            (rel, g.pkey(rel), None, "link or shortcut to protected"))
                        counts["protected"] += 1
                        continue
                    row["link_target"] = tgt
                    counts["symlink"] += 1
                elif st_mod.S_ISDIR(s.st_mode):
                    if row["ext"] in g.BUNDLE_EXTS:
                        row["kind"] = "bundle"
                        counts["bundle_or_heavy"] += 1
                    elif raw in g.HEAVY_DIRS:
                        row["kind"] = "heavy"
                        counts["bundle_or_heavy"] += 1
                    else:
                        row["kind"] = "dir"
                        descend = True
                    counts["dirs"] += 1
                elif st_mod.S_ISREG(s.st_mode):
                    row["kind"] = "gdoc" if row["ext"] in g.GDOC_EXTS else "file"
                    counts["files"] += 1
                    if dataless:
                        counts["dataless"] += 1
                else:
                    row["kind"] = "other"
                if not a.no_xattrs and row["kind"] != "symlink":
                    iid = g.get_xattr(de.path, "com.google.drivefs.item-id#S")
                    if iid:
                        row["drive_id"] = iid.decode("utf-8", "replace").strip("\x00")
                        if guard.is_protected_id(row["drive_id"]):
                            db.execute(
                                "INSERT INTO protected(path,pathkey,key,reason) VALUES(?,?,?,?)",
                                (rel, g.pkey(rel), row["drive_id"], "drive id match"))
                            counts["protected"] += 1
                            continue
                    fi = g.get_xattr(de.path, "com.apple.FinderInfo")
                    row["finder_alias"] = int(bool(fi and fi[:4] == b"alis"))
                try:
                    g.insert_item(db, row)
                except sqlite3.IntegrityError:
                    row["key"] = f"fs:path:{rel}"  # inode reuse or hard link; fall back
                    g.insert_item(db, row)
                counts["items"] += 1
                if descend:
                    stack.append((de.path, rel, row["key"], depth + 1))
                if counts["items"] % a.progress_every == 0:
                    db.commit()
                    g.eprint(f"[index_fs] {counts['items']} items, {len(stack)} dirs queued, "
                             f"{time.time() - t0:.0f}s")

    n_atomic = g.mark_atomic(db)
    g.load_protected(db, guard)
    n_sens = g.mark_sensitive(db, guard)
    counts.update({"atomic_roots": n_atomic, "sensitive": n_sens,
                   "seconds": round(time.time() - t0, 1)})
    g.set_meta(db, "finished", time.time())
    g.set_meta(db, "counts", counts)
    db.commit()
    # aggregate counts only; no paths on stdout
    print({k: v for k, v in counts.items()})
    return 2 if counts["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
