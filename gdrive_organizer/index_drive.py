#!/usr/bin/env python3
"""Phase 1, API variant (recommended): metadata-only index of My Drive via the Drive API.

  gdrive-organizer index-drive --db private/index.sqlite --config private/config.json \
      --client-secret private/client_secret.json 

Why this instead of walking the mount:
  * zero FileProvider involvement: no dataless folder materialization, no TCC prompts
  * returns what the mount cannot: md5Checksum (exact dupes without downloading), ownedByMe,
    shortcut targets, Google-native mime types, viewedByMeTime (real staleness signal),
    canMoveItemWithinDrive
  * scope is drive.metadata.readonly: this token cannot read content or change anything

Boundary: breadth-first, one files.list per folder ("'<id>' in parents"). The protected folder is
recognized in its parent's listing (by ID from config, or by name) and is never queried, so none
of its children's metadata is ever requested. Do NOT replace this with a global files.list or
`rclone lsjson -R` without `--disable ListR`: both fetch everything and filter afterwards.

Cost (fact, Drive API limits page): files.list = 100 quota units; per-user limit 325,000/min.
~5,000 folders = 500,000 units, roughly 2 minutes of quota.
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import deque
from datetime import datetime

from . import core as g
from . import drive_api as gdrive

PROG = "gdrive-organizer index-drive"

FIELDS = ("nextPageToken, files(id,name,mimeType,size,md5Checksum,modifiedTime,viewedByMeTime,"
          "ownedByMe,shortcutDetails(targetId,targetMimeType),"
          "capabilities(canMoveItemWithinDrive,canRename))")


def ts(s):
    """RFC 3339 from Drive ('2024-01-02T03:04:05.678Z') to epoch seconds."""
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog=PROG)
    ap.add_argument("--db", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--client-secret", required=True)
    ap.add_argument("--token", default="private/token_meta.json")
    a = ap.parse_args(argv)

    cfg = g.load_config(a.config)
    guard = g.Guard(cfg)
    guard.warn_if_unprotected()
    if (guard.names or guard.rx) and not guard.ids:
        g.eprint("WARNING: protected_ids is empty. Exclusion falls back to name matching only. "
                 "Paste the folder ID from its Drive URL into private/config.json.")
    svc = gdrive.service("meta", a.client_secret, a.token)
    root = gdrive.call(svc.files().get(fileId="root", fields="id,name"))
    if guard.is_protected_id(root["id"]):
        raise SystemExit("root is protected; refusing")

    db = g.open_db(a.db, fresh=True)
    g.set_meta(db, "backend", "drive")
    g.set_meta(db, "root_id", root["id"])
    g.set_meta(db, "started", time.time())
    counts = {"items": 0, "folders_listed": 0, "protected": 0, "shortcuts": 0, "gdocs": 0}
    t0 = time.time()

    q = deque([(root["id"], "", 0)])
    while q:
        fid, rel_dir, depth = q.popleft()
        token = None
        while True:
            resp = gdrive.call(svc.files().list(
                q=f"'{fid}' in parents and trashed = false", spaces="drive", corpora="user",
                pageSize=1000, fields=FIELDS, pageToken=token))
            for f in resp.get("files", []):
                name = f["name"]
                shown = name.replace("/", ":")  # Drive allows '/' in names; keep paths parseable
                rel = f"{rel_dir}/{shown}" if rel_dir else shown
                mime = f.get("mimeType", "")
                sc = f.get("shortcutDetails") or {}
                if guard.is_protected_id(f["id"]) or guard.is_protected_name(name):
                    db.execute("INSERT INTO protected(path,pathkey,key,reason) VALUES(?,?,?,?)",
                               (rel, g.pkey(rel), f["id"], "id or name match"))
                    counts["protected"] += 1
                    continue
                if mime == SHORTCUT and (guard.is_protected_id(sc.get("targetId"))):
                    db.execute("INSERT INTO protected(path,pathkey,key,reason) VALUES(?,?,?,?)",
                               (rel, g.pkey(rel), f["id"], "shortcut to protected"))
                    counts["protected"] += 1
                    continue
                ext = g.ext_of(shown)
                descend = False
                if mime == g.FOLDER_MIME:
                    if name.startswith("."):
                        kind = "dot"
                    elif ext in g.BUNDLE_EXTS:
                        kind = "bundle"
                    elif name in g.HEAVY_DIRS:
                        kind = "heavy"
                    else:
                        kind, descend = "dir", True
                elif mime == SHORTCUT:
                    kind = "shortcut"
                    counts["shortcuts"] += 1
                elif mime.startswith(g.GAPPS_PREFIX):
                    kind = "gdoc"
                    counts["gdocs"] += 1
                else:
                    kind = "dot" if name.startswith(".") else "file"
                caps = f.get("capabilities") or {}
                g.insert_item(db, {
                    "key": f["id"], "path": rel, "pathkey": g.pkey(rel), "name": shown,
                    "parent_key": fid, "depth": depth, "kind": kind, "ext": ext,
                    "size": int(f["size"]) if f.get("size") else None,
                    "mtime": ts(f.get("modifiedTime")), "drive_id": f["id"], "mime": mime,
                    "md5": f.get("md5Checksum"), "owned_by_me": int(bool(f.get("ownedByMe"))),
                    "viewed_by_me": ts(f.get("viewedByMeTime")),
                    "can_move": int(bool(caps.get("canMoveItemWithinDrive", True))),
                    "link_target": sc.get("targetId"),
                })
                counts["items"] += 1
                if descend:
                    q.append((f["id"], rel, depth + 1))
            token = resp.get("nextPageToken")
            if not token:
                break
        counts["folders_listed"] += 1
        if counts["folders_listed"] % 200 == 0:
            db.commit()
            g.eprint(f"[index_drive] {counts['items']} items, {counts['folders_listed']} folders, "
                     f"{len(q)} queued, {time.time() - t0:.0f}s")

    counts["atomic_roots"] = g.mark_atomic(db)
    g.load_protected(db, guard)
    counts["sensitive"] = g.mark_sensitive(db, guard)
    counts["seconds"] = round(time.time() - t0, 1)
    g.set_meta(db, "finished", time.time())
    g.set_meta(db, "counts", counts)
    db.commit()
    print(counts)
    return 0


SHORTCUT = g.SHORTCUT_MIME

if __name__ == "__main__":
    sys.exit(main())
