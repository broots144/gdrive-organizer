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


def check_journal(recs, sha, plan, ops):
    """The journal is not covered by --confirm-sha, so treat it as untrusted input: it must
    start with this manifest's header, and each record must match the reviewed op it claims to
    be. `ops` is the whole sha-confirmed manifest: undo may run against a re-indexed Drive where
    an op no longer validates (so it is not in `plan`), but it must still be an op the owner
    reviewed. Values a record adds (Drive IDs, old names) are verified again before use."""
    if not recs:
        return
    if recs[0].get("state") != "header":
        raise SystemExit("journal has no manifest header; refusing to use it")
    by_i = {e["i"]: e for e in plan}
    for r in recs:
        if r.get("state") == "header":
            if r.get("manifest_sha") != sha:
                raise SystemExit("journal belongs to a different manifest; refusing to use it")
            continue
        i = r.get("i")
        e = by_i.get(i)
        if e is None:
            if not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < len(ops):
                raise SystemExit(f"journal record for op {i!r} is not in this manifest; "
                                 "refusing to use this journal")
            op = ops[i]
            e = {"op": op.get("op"), "dst": g.nfc(op.get("dst", "")).strip("/"),
                 "src": g.nfc(op["src"]).strip("/") if op.get("src") else None,
                 "src_key": op.get("src_key")}
            if r.get("state") in ("intent", "done") and any(
                    (r.get(k) or None) != (e.get(k) or None) for k in ("op", "dst", "src", "src_key")):
                raise SystemExit(f"journal record for op {i} does not match the manifest; "
                                 "refusing to use this journal")
            continue
        if r.get("state") in ("intent", "done") and any(
                r.get(k) != e.get(k) for k in ("op", "dst", "src", "src_key")):
            raise SystemExit(f"journal record for op {r.get('i')} does not match the manifest; "
                             "refusing to use this journal")


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

    def adopt_created(self, rec):
        pass

    def undo(self, rec, op=None):
        if rec["op"] == "mkdir":
            os.rmdir(self.ab(rec["dst"]))  # only succeeds if empty
        else:
            if not op or g.pkey(rec.get("src_raw") or "") != g.pkey(op.get("src") or ""):
                raise RuntimeError("journal src_raw is not the reviewed src path")
            if self.raw.get(rec["src_key"]) != rec["src_raw"]:
                raise RuntimeError("journal src_raw does not match the index")
            g.rename_excl(self.ab(rec["dst"]), self.ab(rec["src_raw"]))


# ------------------------------------------------------------------ drive backend

class DriveBackend:
    def __init__(self, db, guard, client_secret, token):
        from . import drive_api as gdrive
        self.gd = gdrive
        self.svc = gdrive.service("write", client_secret, token)
        self.root_id = g.get_meta(db, "root_id")
        # The index is not covered by --confirm-sha, so every Drive ID taken from it is checked
        # against Drive itself: the root, and (below) each folder's live path.
        live_root = self.gd.call(self.svc.files().get(fileId="root", fields="id"))["id"]
        if not self.root_id or live_root != self.root_id:
            raise SystemExit("the index's root_id is not this account's My Drive; re-index")
        self.guard = guard
        self.dirs = {}
        for r in db.execute("SELECT key, pathkey FROM items WHERE kind='dir'"):
            self.dirs.setdefault(r["pathkey"], []).append(r["key"])
        self.created = {}  # pathkey -> new folder id, rebuilt from the journal

    def live_path_ok(self, fid, rel):
        """True if Drive shows `fid` at the reviewed path `rel` (relative to My Drive): walk its
        parents up to the root and compare each name. Binds IDs that came from the index or the
        journal, neither covered by --confirm-sha, to the paths the owner reviewed."""
        parts = [p for p in g.nfc(rel).strip("/").split("/") if p]
        cur = fid
        for comp in reversed(parts):
            if not cur or cur == self.root_id or self.guard.is_protected_id(cur):
                return False
            m = self.gd.call(self.svc.files().get(fileId=cur, fields="id,name,parents,trashed"))
            if m.get("trashed") or g.pkey(m.get("name", "").replace("/", ":")) != g.pkey(comp):
                return False
            ps = m.get("parents") or []
            if len(ps) != 1:
                return False
            cur = ps[0]
        return cur == self.root_id

    def folder_id(self, rel_parent):
        pk = g.pkey(rel_parent)
        if pk == "":
            return self.root_id
        if pk in self.created:
            fid = self.created[pk]
        else:
            ids = self.dirs.get(pk, [])
            if len(ids) != 1:
                raise RuntimeError(f"cannot resolve folder id for dst parent ({len(ids)} matches)")
            fid = ids[0]
        if self.guard.is_protected_id(fid):
            raise RuntimeError("dst parent resolves to a protected folder")
        if not self.live_path_ok(fid, rel_parent):
            raise RuntimeError("folder id for a planned path is not that folder in Drive")
        return fid

    def check_src(self, e, parent):
        """The item's live parent must be the reviewed src folder, not only the index's ID."""
        if not self.live_path_ok(parent, g.parent_of(e["src"])):
            raise RuntimeError("drift: src is not at the reviewed path in Drive")

    def _check_created(self, dst, new_id):
        """A folder ID taken from the journal must be the folder this manifest created: a live
        folder with the planned name, directly under the planned parent."""
        if not new_id or self.guard.is_protected_id(new_id):
            raise RuntimeError("journal names an invalid or protected folder id")
        pid = self.folder_id(g.parent_of(dst))
        cur = self.gd.call(self.svc.files().get(fileId=new_id,
                                                fields="id,name,mimeType,parents,trashed"))
        if (cur.get("mimeType") != g.FOLDER_MIME or cur.get("trashed")
                or cur.get("name") != dst.rsplit("/", 1)[-1] or cur.get("parents") != [pid]):
            raise RuntimeError("journal folder id is not the folder this manifest created")

    def adopt_created(self, rec):
        self._check_created(rec["dst"], rec.get("new_id"))
        self.created[g.pkey(rec["dst"])] = rec["new_id"]

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
        if g.nfc(cur["name"].replace("/", ":")) != e["src"].rsplit("/", 1)[-1]:
            raise RuntimeError("drift: src name is not the reviewed name")
        self.check_src(e, old_pid)
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
        """Move one exact duplicate, or one empty folder, to Drive's trash after live checks."""
        fid = e["src_key"]
        if e["kind"] == "dir":
            cur = self.gd.call(self.svc.files().get(fileId=fid, fields="id,parents,trashed"))
            if cur.get("trashed"):
                raise RuntimeError("drift: folder is already trashed")
            if cur.get("parents") != [e["src_parent_key"]]:
                raise RuntimeError("drift: folder parent changed since indexing")
            self.check_src(e, e["src_parent_key"])
            kids = self.gd.call(self.svc.files().list(
                q=f"'{fid}' in parents and trashed = false", fields="files(id)", pageSize=1))
            if kids.get("files"):
                raise RuntimeError("folder is not empty in Drive; leaving it")
            self.gd.call(self.svc.files().update(fileId=fid, body={"trashed": True}, fields="id"))
            return {"trashed": True}
        fields = "id,name,parents,trashed,md5Checksum,size"
        cur = self.gd.call(self.svc.files().get(fileId=fid, fields=fields))
        keep = self.gd.call(self.svc.files().get(fileId=e["keep_key"], fields=fields))
        if cur.get("trashed"):
            raise RuntimeError("drift: src is already trashed")
        if cur.get("parents") != [e["src_parent_key"]]:
            raise RuntimeError("drift: src parent changed since indexing")
        self.check_src(e, e["src_parent_key"])
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

    def undo(self, rec, e=None, op=None):
        """Reverse one journal record, only if Drive still shows exactly what the record says
        was done. `e` is the reviewed plan entry, when the manifest still validates; `op` is the
        sha-confirmed manifest op, which binds the record whether or not it still validates."""
        for fid in (rec.get("src_key"), rec.get("new_id"), rec.get("old_parent"),
                    rec.get("new_parent")):
            if self.guard.is_protected_id(fid):
                raise RuntimeError("journal record names a protected id")
        if rec["op"] == "trash":
            cur = self.gd.call(self.svc.files().get(fileId=rec["src_key"],
                                                    fields="id,parents,trashed"))
            if not cur.get("trashed"):
                raise RuntimeError("drift: item is no longer in the trash")
            if e is not None and cur.get("parents") != [e["src_parent_key"]]:
                raise RuntimeError("drift: trashed item is not in its original folder")
            self.gd.call(self.svc.files().update(fileId=rec["src_key"], body={"trashed": False},
                                                 fields="id"))
            return
        if rec["op"] == "mkdir":
            self.created.pop(g.pkey(rec["dst"]), None)
            self._check_created(rec["dst"], rec.get("new_id"))
            kids = self.gd.call(self.svc.files().list(
                q=f"'{rec['new_id']}' in parents and trashed = false", fields="files(id)",
                pageSize=1))
            if kids.get("files"):
                raise RuntimeError("created folder is not empty; leaving it")
            self.gd.call(self.svc.files().update(fileId=rec["new_id"], body={"trashed": True}))
            return
        new_name = rec["dst"].rsplit("/", 1)[-1]
        # After a re-index a completed move no longer validates (e is None), so bind the values
        # the record adds to the reviewed manifest op: back to the reviewed src folder (checked
        # live in Drive) under the reviewed name, from the planned destination folder.
        if not op or not op.get("src"):
            raise RuntimeError("journal record has no reviewed manifest op")
        src = g.nfc(op["src"]).strip("/")
        if g.nfc((rec.get("old_name") or "").replace("/", ":")) != src.rsplit("/", 1)[-1]:
            raise RuntimeError("journal record does not match the reviewed move (name)")
        if not rec.get("old_parent") or not self.live_path_ok(rec["old_parent"], g.parent_of(src)):
            raise RuntimeError("journal record does not match the reviewed move (folder)")
        if rec.get("new_parent") != self.folder_id(g.parent_of(rec["dst"])):
            raise RuntimeError("journal record does not match the reviewed destination")
        if e is not None:
            if rec.get("old_parent") != e["src_parent_key"] or \
                    (rec.get("old_name") or "").replace("/", ":") != e["expect"]["name"]:
                raise RuntimeError("journal record does not match the reviewed move")
            if rec.get("new_parent") != self.folder_id(g.parent_of(rec["dst"])):
                raise RuntimeError("journal record does not match the reviewed destination")
        cur = self.gd.call(self.svc.files().get(fileId=rec["src_key"],
                                                fields="id,name,parents,trashed"))
        if cur.get("trashed") or cur.get("parents") != [rec["new_parent"]] or \
                cur.get("name") != new_name:
            raise RuntimeError("drift: item is not where this run put it")
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
    ap.add_argument("--token", help="save the full-access token here for reuse; by default "
                    "it stays in memory and each run asks you to sign in")
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
    ops = v.load_manifest(a.manifest)
    errors, warns, plan = v.validate(db, cfg, ops)
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

    if a.backend == "drive" and not a.token and os.path.exists("private/token_write.json"):
        print("NOTE: private/token_write.json is no longer used; it holds full access to your "
              "Drive. Delete it (see docs/oauth-setup.md).", flush=True)
    be = (FsBackend(db, guard, a.root, a.allow_any_root) if a.backend == "fs"
          else DriveBackend(db, guard, a.client_secret, a.token))

    recs = journal.read()
    check_journal(recs, sha, plan, ops)

    if a.undo:
        return run_undo(be, recs, jpath, a, plan, ops)

    if os.path.exists(jpath + ".undo.jsonl"):
        raise SystemExit("this journal has been undone; use a new --journal path for a fresh run")
    if not recs:
        journal.append({"i": -1, "state": "header", "manifest_sha": sha})
    for r in recs:  # rebuild created-folder ids, each checked against Drive
        if r.get("state") == "done" and r.get("op") == "mkdir":
            try:
                be.adopt_created(r)
            except RuntimeError as ex:
                raise SystemExit(f"journal refused at op {r['i']}: {ex}")
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


def run_undo(be, checked, jpath, a, plan, ops):
    """`checked` is the journal as check_journal accepted it; it is not read again."""
    upath = jpath + ".undo.jsonl"
    uj = Journal(upath)
    undone = {r["i"] for r in uj.read() if r.get("state") == "undone"}
    recs = [r for r in checked if r.get("state") == "done"]
    by_i = {e["i"]: e for e in plan}
    if isinstance(be, DriveBackend):
        # created folders resolve destination parents; each is verified again before its undo
        for r in recs:
            if r["op"] == "mkdir" and r.get("new_id") and r["i"] not in undone:
                be.created[g.pkey(r["dst"])] = r["new_id"]
    n = 0
    for rec in reversed(recs):
        if rec["i"] in undone:
            continue
        try:
            if isinstance(be, DriveBackend):
                be.undo(rec, by_i.get(rec["i"]), ops[rec["i"]])
            else:
                be.undo(rec, ops[rec["i"]])
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
