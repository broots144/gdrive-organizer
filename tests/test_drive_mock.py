"""Exercise index_drive / validate / apply(drive) / peek_drive against an in-memory fake Drive.

Asserts: the protected folder's ID never appears in any list query or get/update call; a
shortcut to it is quarantined; duplicate names force src_key; moves are ID based and undo
restores parents and names exactly.
"""
import copy
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from gdrive_organizer import core as g  # noqa: E402
from gdrive_organizer import drive_api as gdrive  # noqa: E402
from gdrive_organizer import validate  # noqa: E402

PROTECTED = "ACME_V_EXAMPLECORP_LEGAL"

F = g.FOLDER_MIME
OLD = "2021-03-01T00:00:00.000Z"
NEW = "2099-01-01T00:00:00.000Z"
ITEMS = {
    "ROOT": {"name": "My Drive", "mimeType": F, "parents": []},
    "LEGALID": {"name": PROTECTED, "mimeType": F, "parents": ["ROOT"]},
    "L1": {"name": "complaint.pdf", "mimeType": "application/pdf", "parents": ["LEGALID"]},
    "LSUB": {"name": "exhibits", "mimeType": F, "parents": ["LEGALID"]},
    "L2": {"name": "ex1.pdf", "mimeType": "application/pdf", "parents": ["LSUB"]},
    "SC": {"name": "case files", "mimeType": g.SHORTCUT_MIME, "parents": ["ROOT"],
           "shortcutDetails": {"targetId": "LEGALID", "targetMimeType": F}},
    "HL": {"name": "homelab", "mimeType": F, "parents": ["ROOT"]},
    "PX": {"name": "proxmox-2021", "mimeType": F, "parents": ["HL"]},
    "PX1": {"name": "vzdump-100.tar.gz", "mimeType": "application/gzip", "parents": ["PX"],
            "size": "5000", "md5Checksum": "aaa", "modifiedTime": OLD},
    "PX2": {"name": "vzdump-100 copy.tar.gz", "mimeType": "application/gzip", "parents": ["PX"],
            "size": "5000", "md5Checksum": "aaa", "modifiedTime": OLD},
    "S1": {"name": "Scan_001.pdf", "mimeType": "application/pdf", "parents": ["ROOT"],
           "size": "10", "modifiedTime": OLD},
    "S2": {"name": "Scan_001.pdf", "mimeType": "application/pdf", "parents": ["ROOT"],
           "size": "12", "modifiedTime": OLD},
    "GD": {"name": "untitled document", "mimeType": "application/vnd.google-apps.document",
           "parents": ["ROOT"], "modifiedTime": OLD},
    "NO": {"name": "shared-with-me.xlsx", "mimeType": "application/octet-stream",
           "parents": ["ROOT"], "size": "3", "modifiedTime": OLD, "ownedByMe": False},
    "LIVE": {"name": "rclone-target", "mimeType": F, "parents": ["HL"]},
    "LV1": {"name": "today.bak", "mimeType": "application/octet-stream", "parents": ["LIVE"],
            "size": "1", "modifiedTime": NEW},
    "SLASH": {"name": "a/b notes.txt", "mimeType": "text/plain", "parents": ["ROOT"],
              "size": "20", "modifiedTime": OLD},
}
CALLS = []
TIMEOUT_AFTER_APPLY = set()  # file IDs whose next update applies, then times out


class Req:
    def __init__(self, fn):
        self.fn, self.headers = fn, {}

    def execute(self):
        return self.fn(self.headers)


class Files:
    def __init__(self, st):
        self.st = st

    def _meta(self, fid):
        d = self.st[fid]
        out = {"id": fid, "name": d["name"], "mimeType": d["mimeType"],
               "parents": list(d["parents"]), "trashed": d.get("trashed", False),
               "ownedByMe": d.get("ownedByMe", True),
               "capabilities": {"canMoveItemWithinDrive": True}}
        for k in ("size", "md5Checksum", "modifiedTime", "shortcutDetails"):
            if k in d:
                out[k] = d[k]
        return out

    def list(self, q=None, pageToken=None, **kw):
        CALLS.append(("list", q))
        m = re.match(r"'([^']+)' in parents", q)
        pid = m.group(1)
        nm = re.search(r"name = '([^']+)'", q)

        def run(h):
            kids = [self._meta(k) for k, d in self.st.items()
                    if pid in d["parents"] and not d.get("trashed")
                    and (not nm or d["name"] == nm.group(1))]
            return {"files": kids}
        return Req(run)

    def get(self, fileId, fields=None):
        CALLS.append(("get", fileId))
        if fileId == "root":
            return Req(lambda h: {"id": "ROOT", "name": "My Drive"})
        return Req(lambda h: self._meta(fileId))

    def update(self, fileId, body=None, addParents=None, removeParents=None, fields=None):
        CALLS.append(("update", fileId))

        def run(h):
            d = self.st[fileId]
            if removeParents:
                d["parents"].remove(removeParents)
            if addParents:
                d["parents"].append(addParents)
            for k, v in (body or {}).items():
                d[k] = v
            if fileId in TIMEOUT_AFTER_APPLY:
                TIMEOUT_AFTER_APPLY.discard(fileId)
                raise TimeoutError("The read operation timed out")
            return self._meta(fileId)
        return Req(run)

    def create(self, body, fields=None):
        CALLS.append(("create", body["name"]))

        def run(h):
            nid = f"NEW{len(self.st)}"
            self.st[nid] = {"name": body["name"], "mimeType": body["mimeType"],
                            "parents": list(body["parents"])}
            return {"id": nid}
        return Req(run)

    def export(self, fileId, mimeType):
        CALLS.append(("export", fileId))
        return Req(lambda h: b"Meeting notes about the homelab rebuild plan")

    def get_media(self, fileId):
        CALLS.append(("media", fileId))
        return Req(lambda h: b"hello world, plain text body")


class Svc:
    def __init__(self, st):
        self._f = Files(st)

    def files(self):
        return self._f


STATE = copy.deepcopy(ITEMS)
gdrive.service = lambda *a, **k: Svc(STATE)


def run(mod, argv):
    import importlib
    m = importlib.import_module(f"gdrive_organizer.{mod}")
    try:
        return m.main(argv)
    except SystemExit as e:
        return e.code


def main():
    tmp = tempfile.mkdtemp()
    os.chdir(tmp)
    cfg = dict(g.CONFIG_DEFAULT, protected_ids=["LEGALID"], protected_names=[PROTECTED])
    json.dump(cfg, open("cfg.json", "w"))
    assert run("index_drive", ["--db", "d.sqlite", "--config", "cfg.json",
                               "--client-secret", "x"]) == 0
    # boundary: the protected ID must never be queried, fetched or updated
    bad = [c for c in CALLS if "LEGALID" in str(c[1]) or c[1] in ("L1", "L2", "LSUB")]
    assert not bad, bad
    import sqlite3
    db = sqlite3.connect("d.sqlite")
    prot = sorted(r[0] for r in db.execute("select reason from protected"))
    assert prot == ["id or name match", "shortcut to protected"], prot
    keys = {r[0] for r in db.execute("select key from items")}
    assert not keys & {"LEGALID", "L1", "L2", "LSUB", "SC"}, keys
    print("index_drive boundary OK; items:", len(keys))

    with open("plan.jsonl", "w") as fh:
        for op in [
            {"op": "mkdir", "dst": "Archive"},
            {"op": "move", "src": "homelab/proxmox-2021", "dst": "Archive/proxmox-2021"},
            {"op": "move", "src": "Scan_001.pdf", "src_key": "S2", "dst": "Archive/scan-b.pdf"},
            {"op": "move", "src": "untitled document", "dst": "Archive/homelab plan"},
        ]:
            fh.write(json.dumps(op) + "\n")
    with open("bad.jsonl", "w") as fh:
        for op in [
            {"op": "move", "src": "Scan_001.pdf", "dst": "x.pdf"},
            {"op": "move", "src": "shared-with-me.xlsx", "dst": "y.xlsx"},
            {"op": "move", "src": "homelab/rclone-target", "dst": "z"},
            {"op": "move", "src": "case files", "dst": "w"},
        ]:
            fh.write(json.dumps(op) + "\n")
    errs, warns, plan = validate.validate(g.open_db("d.sqlite"), cfg,
                                         validate.load_manifest("bad.jsonl"))
    joined = "\n".join(errs)
    for needle in ("resolves to 2 items", "not owned by you", "modified in last",
                   "resolves to 0 items"):
        assert needle in joined, (needle, joined)
    print("validator negative cases OK")
    errs, warns, plan = validate.validate(g.open_db("d.sqlite"), cfg,
                                         validate.load_manifest("plan.jsonl"))
    assert not errs, errs
    sha = g.sha256_file("plan.jsonl")
    before = copy.deepcopy(STATE)
    rc = run("apply", ["--backend", "drive", "--db", "d.sqlite", "--config", "cfg.json",
                       "--manifest", "plan.jsonl", "--execute", "--confirm-sha", sha,
                       "--pause", "0"])
    assert rc == 0, rc
    arch = [k for k, d in STATE.items() if d["name"] == "Archive"][0]
    assert STATE["PX"]["parents"] == [arch] and STATE["S2"]["name"] == "scan-b.pdf"
    assert STATE["S1"]["parents"] == ["ROOT"] and STATE["GD"]["name"] == "homelab plan"
    print("apply(drive) OK")
    rc = run("apply", ["--backend", "drive", "--db", "d.sqlite", "--config", "cfg.json",
                       "--manifest", "plan.jsonl", "--execute", "--confirm-sha", sha,
                       "--undo", "--pause", "0"])
    assert rc == 0, rc
    for k, d in before.items():
        assert STATE[k]["parents"] == d["parents"] and STATE[k]["name"] == d["name"], k
    assert STATE[arch].get("trashed") is True
    print("undo(drive) OK: all parents and names restored; created folder trashed")
    # a move that Drive applies but whose response times out: the retry must reconcile, not
    # re-run it (which would halt on drift), and the reconciled record must still undo
    with open("plan2.jsonl", "w") as fh:
        for op in [
            {"op": "mkdir", "dst": "Archive2"},
            {"op": "move", "src": "homelab/proxmox-2021", "dst": "Archive2/proxmox-2021"},
            {"op": "move", "src": "untitled document", "dst": "Archive2/homelab plan"},
        ]:
            fh.write(json.dumps(op) + "\n")
    sha2 = g.sha256_file("plan2.jsonl")
    base = ["--backend", "drive", "--db", "d.sqlite", "--config", "cfg.json",
            "--manifest", "plan2.jsonl", "--execute", "--confirm-sha", sha2, "--pause", "0"]
    before2 = copy.deepcopy(STATE)
    TIMEOUT_AFTER_APPLY.add("PX")
    rc = run("apply", base)
    assert rc != 0 and "TimeoutError" in str(rc), rc
    arch2 = [k for k, d in STATE.items() if d["name"] == "Archive2"][0]
    assert STATE["PX"]["parents"] == [arch2], "the timed-out move did happen"
    rc = run("apply", base)
    assert "failed earlier" in str(rc), rc  # needs an explicit --retry-failed
    rc = run("apply", base + ["--retry-failed"])
    assert rc == 0, rc
    assert STATE["PX"]["parents"] == [arch2] and STATE["GD"]["name"] == "homelab plan"
    rc = run("apply", base + ["--undo"])
    assert rc == 0, rc
    for k, d in before2.items():
        assert STATE[k]["parents"] == d["parents"] and STATE[k]["name"] == d["name"], k
    assert STATE[arch2].get("trashed") is True
    print("timeout after apply OK: retry reconciled, undo restored everything")

    bad = [c for c in CALLS if "LEGALID" in str(c[1]) or c[1] in ("L1", "L2", "LSUB", "SC")]
    assert not bad, bad

    rc = run("peek", ["--db", "d.sqlite", "--config", "cfg.json", "--client-secret", "x",
                            "--limit", "50"])
    assert rc == 0
    snips = list(sqlite3.connect("d.sqlite").execute("select key,method,flagged from snippets"))
    print("peek snippets:", snips)
    assert all(k not in ("L1", "L2") for k, _, _ in snips)
    db2 = sqlite3.connect("d.sqlite")
    db2.execute("DELETE FROM snippets")
    db2.commit()
    rc = run("peek", ["--db", "d.sqlite", "--config", "cfg.json", "--client-secret", "x",
                      "--max-depth", "0", "--include-sensitive"])
    assert rc == 0
    depth = dict(db2.execute("select key, depth from items"))
    scoped = [k for k, in db2.execute("select key from snippets")]
    assert scoped and all(depth[k] == 0 for k in scoped), scoped
    assert all(k not in ("L1", "L2") for k in scoped)
    print("peek --max-depth 0 --include-sensitive OK:", sorted(scoped))
    db2.execute("DELETE FROM snippets")
    db2.commit()
    with open("keys.txt", "w") as fh:
        fh.write("# ambiguous names only\nGD\nL1\n")  # L1 is protected: never indexed
    rc = run("peek", ["--db", "d.sqlite", "--config", "cfg.json", "--client-secret", "x",
                      "--only-keys", "keys.txt"])
    assert rc == 0
    only = [k for k, in db2.execute("select key from snippets")]
    assert only == ["GD"], only
    print("peek --only-keys OK")
    print("ALL DRIVE MOCK TESTS PASSED")


if __name__ == "__main__":
    main()
