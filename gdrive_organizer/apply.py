#!/usr/bin/env python3
"""Phase 4: execute (or undo) a validated manifest. Dry run unless --execute.

  # dry run
  gdrive-organizer apply --backend drive --db private/index.sqlite --config private/config.json --manifest plan.jsonl
  # execute (sha comes from `validate`; the manifest is frozen once you approve it)
  gdrive-organizer apply ... --execute --confirm-sha <sha256> --batch 50 --pause 60
  # undo everything this journal recorded, newest first
  gdrive-organizer apply ... --undo --execute --confirm-sha <sha256>

trash (exact duplicates only, see validate) goes to Drive's trash, never a permanent delete; undo
un-trashes it. Drive empties its trash after 30 days.

Honest semantics: there is no cross-item atomicity. Each op is individually atomic (local
rename(2), or one Drive files.update), and the journal is write-ahead (intent, fsync, act, done,
fsync), so a crash leaves at most one op in doubt, which the next run reconciles by inspection.

Backends:
  drive  (recommended) server-side files.update(addParents/removeParents/name). Keyed by Drive ID,
         so undo does not depend on paths. Drive never overwrites on move (it allows duplicate
         names), so a clobber is impossible; the validator still blocks duplicates.
         Drive for desktop then pulls the changes down as metadata.
  fs     local rename on the mount via renamex_np(RENAME_EXCL | RENAME_NOFOLLOW_ANY). Never
         replaces an existing name. Drive for desktop then pushes each rename to the cloud
         asynchronously; batch and pause so its queue drains, and check it shows "up to date".

Run this yourself in a terminal (tmux/screen). Do not let an agent run --execute.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import core as g
from . import validate as v

PROG = "gdrive-organizer apply"


class Journal:
    def __init__(self, path: str):
        self.path = path

    def read(self):
        if not os.path.exists(self.path):
            return []
        with open(self.path, "r", encoding="utf-8") as fh:
            return [json.loads(x) for x in fh if x.strip()]

    def append(self, rec: dict) -> None:
        rec = dict(rec, ts=time.time())
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())


def last_state(records):
    st = {}
    for r in records:
        st[r["i"]] = r
    return st


# ------------------------------------------------------------------ fs backend

class FsBackend:
    def __init__(self, db, guard, root_arg, allow_any_root):
        self.root = g.get_meta(db, "root")
        if root_arg and os.path.realpath(root_arg) != os.path.realpath(self.root):
            raise SystemExit("--root does not match the root recorded in the index")
        if "/Library/CloudStorage/" not in self.root and not allow_any_root:
            raise SystemExit("index root is not under ~/Library/CloudStorage")
        self.guard = guard
        self.raw = {r["key"]: r["path"] for r in db.execute("SELECT key, path FROM items")}

    def ab(self, rel):
        return os.path.join(self.root, rel)

    def mkdir(self, e):
        os.mkdir(self.ab(e["dst"]))
        return {}

    def _check_src(self, e):
        s = os.lstat(self.ab(self.raw[e["src_key"]]))
        x = e["expect"]
        if x["ino"] is not None and s.st_ino != x["ino"]:
            raise RuntimeError("drift: src inode changed since indexing")
        if e["kind"] in ("file", "gdoc"):
            if x["size"] is not None and s.st_size != x["size"]:
                raise RuntimeError("drift: src size changed since indexing")
            if x["mtime"] is not None and abs(s.st_mtime - x["mtime"]) > 1.0:
                raise RuntimeError("drift: src mtime changed since indexing")
        return s

    def move(self, e):
        s = self._check_src(e)
        if not os.path.isdir(os.path.dirname(self.ab(e["dst"]))):
            raise RuntimeError("dst parent missing")
        g.rename_excl(self.ab(self.raw[e["src_key"]]), self.ab(e["dst"]))
        if os.lstat(self.ab(e["dst"])).st_ino != s.st_ino:
            raise RuntimeError("post-check: dst inode mismatch")
        return {"src_raw": self.raw[e["src_key"]]}

    def trash(self, e):
        raise RuntimeError("trash is supported by the drive backend only")

    def reconcile(self, e, rec):
        """An op without done (crash, or an error after acting): decide from the filesystem
        whether it happened. Returns (verdict, undo info for a done record)."""
        if e["op"] == "mkdir":
            return ("done", {}) if os.path.isdir(self.ab(e["dst"])) else ("redo", {})
        src_ok = os.path.lexists(self.ab(self.raw[e["src_key"]]))
        dst_ok = os.path.lexists(self.ab(e["dst"]))
        if not src_ok and dst_ok:
            return "done", {"src_raw": self.raw[e["src_key"]]}
        if src_ok and not dst_ok:
            return "redo", {}
        return "halt", {}

    def undo(self, rec):
        if rec["op"] == "mkdir":
            os.rmdir(self.ab(rec["dst"]))  # only succeeds if empty
        else:
            g.rename_excl(self.ab(rec["dst"]), self.ab(rec["src_raw"]))


# ------------------------------------------------------------------ drive backend

class DriveBackend:
    def __init__(self, db, guard, client_secret, token):
        from . import drive_api as gdrive
        self.gd = gdrive
        self.svc = gdrive.service("write", client_secret, token)
        self.root_id = g.get_meta(db, "root_id")
        self.guard = guard
        self.dirs = {}
        for r in db.execute("SELECT key, pathkey FROM items WHERE kind='dir'"):
            self.dirs.setdefault(r["pathkey"], []).append(r["key"])
        self.created = {}  # pathkey -> new folder id, rebuilt from the journal

    def folder_id(self, rel_parent):
        pk = g.pkey(rel_parent)
        if pk == "":
            return self.root_id
        if pk in self.created:
            return self.created[pk]
        ids = self.dirs.get(pk, [])
        if len(ids) != 1:
            raise RuntimeError(f"cannot resolve folder id for dst parent ({len(ids)} matches)")
        return ids[0]

    def mkdir(self, e):
        pid = self.folder_id(g.parent_of(e["dst"]))
        name = e["dst"].rsplit("/", 1)[-1]
        f = self.gd.call(self.svc.files().create(
            body={"name": name, "mimeType": g.FOLDER_MIME, "parents": [pid]}, fields="id"))
        self.created[g.pkey(e["dst"])] = f["id"]
        return {"new_id": f["id"], "parent": pid}

    def move(self, e):
        fid = e["src_key"]
        cur = self.gd.call(self.svc.files().get(fileId=fid, fields="id,name,parents,trashed"))
        if cur.get("trashed"):
            raise RuntimeError("drift: src is trashed")
        if cur["name"].replace("/", ":") != e["expect"]["name"]:
            raise RuntimeError("drift: src name changed since indexing")
        old_pid = e["src_parent_key"]
        if cur.get("parents") != [old_pid]:
            raise RuntimeError("drift: src parent changed since indexing")
        new_pid = self.folder_id(g.parent_of(e["dst"]))
        new_name = e["dst"].rsplit("/", 1)[-1]
        kw = {"fileId": fid, "fields": "id,name,parents"}
        if new_pid != old_pid:
            kw.update(addParents=new_pid, removeParents=old_pid)
        body = {"name": new_name} if new_name != e["expect"]["name"] else {}
        res = self.gd.call(self.svc.files().update(body=body, **kw))
        if res.get("parents") != [new_pid]:
            raise RuntimeError("post-check: parents not as expected")
        return {"old_parent": old_pid, "new_parent": new_pid,
                "old_name": cur["name"], "new_name": res["name"]}

    def trash(self, e):
        """Move one exact duplicate to Drive's trash after re-checking both copies live."""
        fid = e["src_key"]
        fields = "id,name,parents,trashed,md5Checksum,size"
        cur = self.gd.call(self.svc.files().get(fileId=fid, fields=fields))
        keep = self.gd.call(self.svc.files().get(fileId=e["keep_key"], fields=fields))
        if cur.get("trashed"):
            raise RuntimeError("drift: src is already trashed")
        if cur.get("parents") != [e["src_parent_key"]]:
            raise RuntimeError("drift: src parent changed since indexing")
        if not cur.get("md5Checksum") or cur.get("md5Checksum") != e["expect"]["md5"]:
            raise RuntimeError("drift: src content changed since indexing")
        if keep.get("trashed") or keep.get("md5Checksum") != cur.get("md5Checksum") or \
                str(keep.get("size")) != str(cur.get("size")):
            raise RuntimeError("the copy to keep is gone, trashed or no longer identical")
        self.gd.call(self.svc.files().update(fileId=fid, body={"trashed": True}, fields="id"))
        return {"trashed": True}

    def reconcile(self, e, rec):
        """An op without done (crash, or a timeout after Drive applied it): decide from Drive
        whether it happened. Returns (verdict, undo info for a done record)."""
        if e["op"] == "mkdir":
            pid = self.folder_id(g.parent_of(e["dst"]))
            name = e["dst"].rsplit("/", 1)[-1].replace("'", "\\'")
            r = self.gd.call(self.svc.files().list(
                q=f"'{pid}' in parents and name = '{name}' and mimeType = '{g.FOLDER_MIME}' "
                  "and trashed = false", fields="files(id)", pageSize=10))
            found = r.get("files", [])
            if len(found) == 1:
                self.created[g.pkey(e["dst"])] = found[0]["id"]
                return "done", {"new_id": found[0]["id"], "parent": pid}
            return ("redo", {}) if not found else ("halt", {})
        if e["op"] == "trash":
            cur = self.gd.call(self.svc.files().get(fileId=e["src_key"],
                                                    fields="parents,trashed"))
            if cur.get("trashed"):
                return "done", {"trashed": True}
            return ("redo", {}) if cur.get("parents") == [e["src_parent_key"]] else ("halt", {})
        cur = self.gd.call(self.svc.files().get(fileId=e["src_key"], fields="name,parents"))
        new_pid = self.folder_id(g.parent_of(e["dst"]))
        new_name = e["dst"].rsplit("/", 1)[-1]
        if cur.get("parents") == [new_pid] and cur["name"] == new_name:
            # Unrenamed: the current name is the original. Renamed: the index name is the best
            # record of it (a '/' in the original shows as ':' there).
            old_name = cur["name"] if new_name == e["expect"]["name"] else e["expect"]["name"]
            return "done", {"old_parent": e["src_parent_key"], "new_parent": new_pid,
                            "old_name": old_name, "new_name": cur["name"]}
        if cur.get("parents") == [e["src_parent_key"]]:
            return "redo", {}
        return "halt", {}

    def undo(self, rec):
        if rec["op"] == "trash":
            self.gd.call(self.svc.files().update(fileId=rec["src_key"], body={"trashed": False},
                                                 fields="id"))
            return
        if rec["op"] == "mkdir":
            kids = self.gd.call(self.svc.files().list(
                q=f"'{rec['new_id']}' in parents and trashed = false", fields="files(id)",
                pageSize=1))
            if kids.get("files"):
                raise RuntimeError("created folder is not empty; leaving it")
            self.gd.call(self.svc.files().update(fileId=rec["new_id"], body={"trashed": True}))
        else:
            kw = {"fileId": rec["src_key"], "fields": "id"}
            if rec["new_parent"] != rec["old_parent"]:
                kw.update(addParents=rec["old_parent"], removeParents=rec["new_parent"])
            self.gd.call(self.svc.files().update(body={"name": rec["old_name"]}, **kw))


# ------------------------------------------------------------------ main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog=PROG)
    ap.add_argument("--backend", choices=["fs", "drive"], required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--config")
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--journal")
    ap.add_argument("--root", help="fs backend: must match the indexed root")
    ap.add_argument("--allow-any-root", action="store_true", help="tests only")
    ap.add_argument("--client-secret", default="private/client_secret.json")
    ap.add_argument("--token", default="private/token_write.json")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--confirm-sha")
    ap.add_argument("--batch", type=int, default=50)
    ap.add_argument("--pause", type=float, default=60.0, help="seconds between batches")
    ap.add_argument("--max-ops", type=int, default=0, help="stop after N ops this run (0 = all)")
    ap.add_argument("--undo", action="store_true")
    ap.add_argument("--retry-failed", action="store_true")
    a = ap.parse_args(argv)

    cfg = g.load_config(a.config)
    db = g.open_db(a.db)
    if g.get_meta(db, "backend") != a.backend:
        raise SystemExit("index backend does not match --backend")
    sha = g.sha256_file(a.manifest)
    if a.execute and a.confirm_sha != sha:
        raise SystemExit(f"--confirm-sha must equal the manifest sha256 ({sha}); "
                         "the manifest changed or was not reviewed")
    guard = g.Guard(cfg)
    g.load_protected(db, guard)
    errors, warns, plan = v.validate(db, cfg, v.load_manifest(a.manifest))
    if errors and not a.undo:
        for e in errors[:50]:
            print("ERROR", e)
        raise SystemExit(f"{len(errors)} validation errors; nothing executed")

    jpath = a.journal or a.manifest + ".journal.jsonl"
    journal = Journal(jpath)

    if not a.execute:
        print(f"DRY RUN backend={a.backend} ops={len(plan)} warnings={len(warns)} journal={jpath}")
        for e in plan[:25]:
            print(f"  {e['op']:5} {e.get('src', ''):<60} -> {e['dst']}")
        if len(plan) > 25:
            print(f"  ... {len(plan) - 25} more")
        return 0

    be = (FsBackend(db, guard, a.root, a.allow_any_root) if a.backend == "fs"
          else DriveBackend(db, guard, a.client_secret, a.token))

    if a.undo:
        return run_undo(be, journal, jpath, a)

    if os.path.exists(jpath + ".undo.jsonl"):
        raise SystemExit("this journal has been undone; use a new --journal path for a fresh run")
    recs = journal.read()
    for r in recs:  # rebuild created-folder ids for the drive backend
        if r.get("state") == "done" and r.get("op") == "mkdir" and r.get("new_id"):
            getattr(be, "created", {})[g.pkey(r["dst"])] = r["new_id"]
    state = last_state(recs)
    n_this_run = 0
    for e in plan:
        prev = state.get(e["i"])
        if prev and prev["state"] == "done":
            continue
        if prev and prev["state"] == "failed" and not a.retry_failed:
            raise SystemExit(f"op {e['i']} (manifest line {e['line']}) failed earlier: "
                             f"{prev.get('err')}. Inspect, then rerun with --retry-failed.")
        if prev and prev["state"] in ("intent", "failed"):
            # A failed op may still have happened (e.g. a timeout after Drive applied it), so
            # check reality before acting again.
            verdict, extra = be.reconcile(e, prev)
            if verdict == "done":
                journal.append(dict({"i": e["i"], "state": "done", "op": e["op"], "dst": e["dst"],
                                     "src": e.get("src"), "src_key": e.get("src_key"),
                                     "reconciled": True}, **extra))
                print(f"op {e['i']}: already applied; recorded as done", flush=True)
                continue
            if verdict == "halt":
                raise SystemExit(f"op {e['i']} is in an unknown state; inspect by hand")
        # defense in depth: re-check protected ground immediately before acting
        for pth in (e.get("src"), e["dst"]):
            if pth and guard.path_violation(pth):
                raise SystemExit(f"op {e['i']} touches protected ground; aborting")
        journal.append({"i": e["i"], "state": "intent", "op": e["op"], "dst": e["dst"],
                        "src": e.get("src"), "src_key": e.get("src_key")})
        try:
            extra = {"mkdir": be.mkdir, "move": be.move, "trash": be.trash}[e["op"]](e)
        except Exception as ex:
            journal.append({"i": e["i"], "state": "failed", "err": repr(ex)})
            raise SystemExit(f"op {e['i']} failed: {ex!r}. Journal: {jpath}")
        journal.append(dict({"i": e["i"], "state": "done", "op": e["op"], "dst": e["dst"],
                             "src": e.get("src"), "src_key": e.get("src_key")}, **extra))
        n_this_run += 1
        if a.max_ops and n_this_run >= a.max_ops:
            print(f"stopped after --max-ops {a.max_ops}")
            return 0
        if n_this_run % a.batch == 0:
            print(f"{n_this_run} ops done; pausing {a.pause:.0f}s for sync to drain", flush=True)
            time.sleep(a.pause)
    print(f"complete: {n_this_run} ops this run. Journal: {jpath}")
    return 0


def run_undo(be, journal, jpath, a):
    upath = jpath + ".undo.jsonl"
    uj = Journal(upath)
    undone = {r["i"] for r in uj.read() if r.get("state") == "undone"}
    recs = [r for r in journal.read() if r.get("state") == "done"]
    n = 0
    for rec in reversed(recs):
        if rec["i"] in undone:
            continue
        try:
            be.undo(rec)
        except Exception as ex:
            uj.append({"i": rec["i"], "state": "failed", "err": repr(ex)})
            raise SystemExit(f"undo of op {rec['i']} failed: {ex!r}. Undo journal: {upath}")
        uj.append({"i": rec["i"], "state": "undone"})
        n += 1
        if n % a.batch == 0:
            time.sleep(a.pause)
    print(f"undone {n} ops. Undo journal: {upath}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
