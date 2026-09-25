#!/usr/bin/env python3
"""Phase 3 gate: validate a move manifest against the index before anything executes.

  gdrive-organizer validate --db private/index.sqlite --config private/config.json --manifest plan.jsonl

Manifest: JSON Lines, applied in order. Paths are relative to My Drive, as shown in the index.
  {"op": "mkdir", "dst": "Archive/Homelab"}
  {"op": "move",  "src": "old-backups/proxmox-2021", "dst": "Archive/Homelab/proxmox-2021"}
  {"op": "move",  "src": "Scan_001.pdf", "src_key": "<id from index>", "dst": "Reference/Vehicles/title.pdf"}
Optional per-op "override": ["recent", "sensitive", "not_owned"] makes an exception explicit and
auditable instead of a global flag.

Rules enforced (errors block execution):
  * nothing touches protected ground: the folder, anything inside it, any ANCESTOR of it
    (moving or renaming an ancestor moves the folder), any protected-looking name in dst
  * every src resolves to exactly one indexed item (Drive allows duplicate names; pass src_key)
  * sources are disjoint subtrees; no op reaches inside something moved earlier
  * dst parent exists (indexed dir or earlier mkdir); dst does not exist (case and Unicode
    insensitive); dst not inside src; no dot-folder destinations
  * items inside an atomic unit (git repo, compose project, bundle, node_modules...) move
    only as the whole unit
  * folders with files modified in the last recent_days are presumed live backup targets
  * sensitive-looking names, items not owned by you, items Drive says you cannot move
  * name validity: <=255 UTF-8 bytes, no slash/NUL/control chars/colon, no edge whitespace
  * Google pointer files (.gdoc etc) keep their extension
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
import time

from . import core as g

PROG = "gdrive-organizer validate"


def load_manifest(path: str):
    ops = []
    with open(path, "r", encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            op = json.loads(line)
            op["_line"] = n
            ops.append(op)
    return ops


def ancestors(pk: str):
    parts = pk.split("/")
    for i in range(len(parts) - 1, 0, -1):
        yield "/".join(parts[:i])


def validate(db, cfg, ops):
    guard = g.Guard(cfg)
    g.load_protected(db, guard)
    backend = g.get_meta(db, "backend")
    now = time.time()
    recent = now - cfg["recent_days"] * 86400

    rows = db.execute("SELECT * FROM items").fetchall()
    by_key = {r["key"]: r for r in rows}
    by_pk = collections.defaultdict(list)
    for r in rows:
        by_pk[r["pathkey"]].append(r["key"])
    stats = collections.defaultdict(lambda: [0, 0, 0.0])  # files, bytes, newest mtime
    for r in rows:
        if r["kind"] in ("file", "gdoc"):
            for a in ancestors(r["pathkey"]):
                s = stats[a]
                s[0] += 1
                s[1] += r["size"] or 0
                s[2] = max(s[2], r["mtime"] or 0)

    exists = set(by_pk) | set(guard.protected_pks)
    created, frozen = set(), set()
    src_pks = set()
    errors, warns, plan = [], [], []

    # pass 1: collect sources for the disjointness check
    for op in ops:
        if op.get("op") == "move" and "src" in op:
            src_pks.add(g.pkey(op["src"]))

    for i, op in enumerate(ops):
        tag = f"line {op['_line']}"
        kind = op.get("op")
        dst = g.nfc(op.get("dst", "")).strip("/")
        dpk = g.pkey(dst)
        over = set(op.get("override", []))
        e0 = len(errors)

        def err(msg):
            errors.append(f"{tag}: {msg}")

        if kind not in ("mkdir", "move"):
            err(f"unknown op {kind!r}")
            continue
        if not dst:
            err("empty dst")
            continue
        v = guard.path_violation(dst)
        if v:
            err(f"dst {v}")
        for p_ in g.name_problems(dst.rsplit("/", 1)[-1]):
            if p_.startswith("colon"):
                warns.append(f"{tag}: dst name has a {p_}")
            else:
                err(f"dst name: {p_}")
        if any(c.startswith(".") for c in dst.split("/")):
            err("dst goes into or creates a dot-folder")
        ppk = g.pkey(g.parent_of(dst))
        if ppk and ppk not in created:
            pk_items = [by_key[k] for k in by_pk.get(ppk, [])]
            dirs = [r for r in pk_items if r["kind"] == "dir"]
            if len(dirs) != 1:
                err("dst parent is not exactly one indexed folder or earlier mkdir "
                    f"(found {len(dirs)})")
            elif dirs[0]["under_atomic"]:
                err("dst parent is inside an atomic unit")
        if dpk in exists:
            err("dst already exists (case/Unicode-insensitive)")
        for a in [dpk] + list(ancestors(dpk)):
            if a in src_pks:
                err("dst is inside a source that this manifest moves (paths are pre-move)")
                break
            if a in frozen and a != dpk:
                err("dst is inside something moved earlier in this manifest")
                break

        entry = {"i": i, "op": kind, "dst": dst, "line": op["_line"]}
        if kind == "move":
            src = g.nfc(op.get("src", "")).strip("/")
            spk = g.pkey(src)
            item = None
            if op.get("src_key"):
                item = by_key.get(op["src_key"])
                if item is None or item["pathkey"] != spk:
                    err("src_key not found or does not match src path")
                    item = None
            else:
                keys = by_pk.get(spk, [])
                if len(keys) != 1:
                    err(f"src resolves to {len(keys)} items; pass src_key")
                else:
                    item = by_key[keys[0]]
            v = guard.path_violation(src)
            if v:
                err(f"src {v}")
            if is_inside(dpk, spk):
                err("dst is inside src")
            for a in ancestors(spk):
                if a in src_pks:
                    err("src lies inside another moved source (make sources disjoint)")
                    break
            if item is not None:
                if item["kind"] == "dot":
                    err("src is a dot entry")
                if item["under_atomic"] and not item["atomic_root"]:
                    err(f"src is inside atomic unit '{item['under_atomic']}'; move the unit")
                if item["sensitive"] and "sensitive" not in over:
                    err("src has a sensitive-looking name (override: sensitive)")
                if item["owned_by_me"] == 0 and "not_owned" not in over:
                    err("src not owned by you: Drive may create a shortcut instead or cut "
                        "collaborators' access (override: not_owned)")
                if item["can_move"] == 0:
                    err("Drive reports you cannot move this item")
                if item["kind"] in ("dir", "bundle", "heavy"):
                    nf, nb, newest = stats.get(spk, (0, 0, 0.0))
                    if newest > recent and "recent" not in over:
                        err(f"folder has files modified in last {cfg['recent_days']} days: "
                            "live backup target? (override: recent)")
                    entry.update(files=nf, bytes=nb)
                else:
                    entry.update(files=1, bytes=item["size"] or 0)
                    if (item["mtime"] or 0) > recent:
                        warns.append(f"{tag}: file modified recently")
                if backend == "fs" and item["kind"] == "gdoc" and g.ext_of(src) != g.ext_of(dst):
                    err("Google pointer file must keep its extension")
                elif item["kind"] == "file" and g.ext_of(src) != g.ext_of(dst):
                    warns.append(f"{tag}: extension changes {g.ext_of(src)} -> {g.ext_of(dst)}")
                if item["kind"] in ("symlink", "shortcut"):
                    warns.append(f"{tag}: moving a link/shortcut moves only the link")
                entry.update(src=src, src_key=item["key"], kind=item["kind"],
                             src_parent_key=item["parent_key"],
                             expect={"ino": item["ino"], "size": item["size"],
                                     "mtime": item["mtime"], "name": item["name"]})
            frozen.add(dpk)
        else:
            created.add(dpk)
        exists.add(dpk)
        if len(errors) == e0:
            plan.append(entry)
    return errors, warns, plan


def is_inside(child_pk: str, anc_pk: str) -> bool:
    return bool(anc_pk) and g.is_within(child_pk, anc_pk)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog=PROG)
    ap.add_argument("--db", required=True)
    ap.add_argument("--config")
    ap.add_argument("--manifest", required=True)
    a = ap.parse_args(argv)
    cfg = g.load_config(a.config)
    db = g.open_db(a.db)
    ops = load_manifest(a.manifest)
    errors, warns, plan = validate(db, cfg, ops)
    moves = [p for p in plan if p["op"] == "move"]
    print(f"ops={len(ops)} valid={len(plan)} errors={len(errors)} warnings={len(warns)}")
    print(f"mkdir={sum(1 for p in plan if p['op'] == 'mkdir')} move={len(moves)} "
          f"files_affected={sum(p.get('files', 0) for p in moves)} "
          f"bytes_affected={sum(p.get('bytes', 0) for p in moves)}")
    for e in errors[:100]:
        print("ERROR", e)
    for w in warns[:50]:
        print("WARN ", w)
    print(f"manifest sha256={g.sha256_file(a.manifest)}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
