"""scripts/rules_to_plan.py on a synthetic index: the manifest it writes must validate with zero
errors, empty the dump folder, merge case-insensitive collisions, route identical colliding files
to duplicates, mark sensitive sources with an override, never rename files, and leave protected,
not-owned and recently modified items where they are.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from gdrive_organizer import core as g  # noqa: E402
from gdrive_organizer import validate  # noqa: E402
import rules_to_plan  # noqa: E402

PROTECTED = "ACME_V_EXAMPLECORP_LEGAL"
OLD = time.time() - 5 * 365 * 86400
NEW = time.time() - 86400

RULES = '''
DUMP = "old dump"
DUMP_CHILD = ("i.parent_key = (SELECT key FROM items WHERE depth=0 AND kind='dir' "
              "AND pathkey='old dump')")
LIVE_TOPS = ["finance", "media", "family"]
RULES = [
    dict(id="work", where=DUMP_CHILD + " AND i.name REGEXP '(?i)initech'",
         dst="archive/work/{lh}"),
    dict(id="dups", where=DUMP_CHILD + " AND i.kind='file' AND i.md5 IN (SELECT md5 FROM items "
         "WHERE md5 IS NOT NULL AND substr(pathkey,1,9)<>'old dump/')",
         dst="archive/duplicates/{orig}"),
    dict(id="finance", where="(i.depth=0 AND i.kind='dir' AND i.pathkey IN ('taxes','live')) "
         "OR (" + DUMP_CHILD + " AND lower(i.name)='taxes')", dst="finance/{lh}"),
    dict(id="family", where="(" + DUMP_CHILD + " AND lower(i.name)='legal notes') "
         "OR (i.depth=0 AND i.pathkey='shared stuff')", dst="family/{lh}"),
    dict(id="media", where="i.depth=0 AND i.pathkey='photos'", dst="media/{lh}"),
    dict(id="unsorted", where=DUMP_CHILD, dst="archive/unsorted/{orig}"),
]
'''


def build(db, cfg):
    n = [0]

    def add(key, path, kind="file", parent=None, size=0, md5=None, mtime=OLD, owned=1):
        n[0] += 1
        name = path.rsplit("/", 1)[-1]
        g.insert_item(db, {
            "key": key, "path": path, "pathkey": g.pkey(path), "name": name,
            "parent_key": parent, "depth": path.count("/"), "kind": kind,
            "ext": g.ext_of(name) if kind == "file" else "", "size": size, "mtime": mtime,
            "md5": md5, "owned_by_me": owned, "can_move": 1,
        })

    add("D", "old dump", "dir")
    add("DW", "old dump/Initech Files", "dir", "D")
    add("DW1", "old dump/Initech Files/tps.doc", parent="DW", size=10, md5="w1")
    add("DT", "old dump/Taxes", "dir", "D")
    add("DT1", "old dump/Taxes/2012 receipt.pdf", parent="DT", size=20, md5="t0")
    add("DC", "old dump/copy.pdf", parent="D", size=30, md5="x")
    add("DM", "old dump/Mystery Box", "dir", "D")
    add("DM1", "old dump/Mystery Box/thing.bin", parent="DM", size=40, md5="m1")
    add("DL", "old dump/legal notes", "dir", "D")
    add("DL1", "old dump/legal notes/n.txt", parent="DL", size=5, md5="l1")
    add("DS", "old dump/loose note.txt", parent="D", size=6, md5="u1")
    # two top-level folders whose names differ only in case, with overlapping content
    add("T1", "Taxes", "dir")
    add("T1Y", "Taxes/2020", "dir", "T1")
    add("T1A", "Taxes/2020/receipt-a.pdf", parent="T1Y", size=50, md5="a")
    add("T1S", "Taxes/2020/same.pdf", parent="T1Y", size=60, md5="s")
    add("T2", "taxes", "dir")
    add("T2Y", "taxes/2020", "dir", "T2")
    add("T2B", "taxes/2020/receipt-b.pdf", parent="T2Y", size=70, md5="b")
    add("T2S", "taxes/2020/same.pdf", parent="T2Y", size=60, md5="s")
    add("P", "Photos", "dir")
    add("P1", "Photos/orig.pdf", parent="P", size=30, md5="x")
    add("L", "live", "dir")
    add("L1", "live/today.bak", parent="L", size=1, mtime=NEW)
    add("N", "shared stuff", "dir", owned=0)
    db.execute("INSERT INTO protected(path, pathkey, key, reason) VALUES(?,?,?,?)",
               (PROTECTED, g.pkey(PROTECTED), "PROT", "id or name match"))
    g.set_meta(db, "backend", "drive")
    g.mark_atomic(db)
    g.mark_sensitive(db, g.Guard(cfg))
    db.commit()


def main():
    tmp = tempfile.mkdtemp()
    cfg_path, db_path = os.path.join(tmp, "cfg.json"), os.path.join(tmp, "idx.sqlite")
    cfg = dict(g.CONFIG_DEFAULT, protected_names=[PROTECTED])
    json.dump(cfg, open(cfg_path, "w"))
    rules_path, out = os.path.join(tmp, "rules.py"), os.path.join(tmp, "plan.jsonl")
    open(rules_path, "w").write(RULES)

    db = g.open_db(db_path)
    build(db, cfg)
    db.close()

    with contextlib.redirect_stdout(io.StringIO()) as buf:
        rc = rules_to_plan.main(["--db", db_path, "--config", cfg_path, "--rules", rules_path,
                                 "--out", out])
    assert rc == 0, rc
    summary = buf.getvalue()
    assert "items remaining in dump: 0" in summary, summary

    db = g.open_db(db_path)
    ops = validate.load_manifest(out)
    errors, warns, plan = validate.validate(db, g.load_config(cfg_path), ops)
    assert not errors, errors
    assert all(o["op"] in ("mkdir", "move") for o in ops)
    moves = {o["src_key"]: o for o in ops if o["op"] == "move"}
    dsts = {k: o["dst"] for k, o in moves.items()}
    mkdirs = {o["dst"] for o in ops if o["op"] == "mkdir"}

    assert dsts["DW"] == "archive/work/initech-files", dsts
    assert dsts["DC"] == "archive/duplicates/old-dump/copy.pdf", dsts
    assert dsts["DM"] == "archive/unsorted/old-dump/mystery-box", dsts
    assert dsts["DS"] == "archive/unsorted/old-dump/loose note.txt", dsts  # files never renamed
    assert dsts["DT"] == "finance/taxes/from-old-dump", dsts  # dump child nests, dump empties
    assert dsts["DL"] == "family/legal-notes" and moves["DL"]["override"] == ["sensitive"]
    assert dsts["P"] == "media/photos", dsts
    # case-insensitive collision: both taxes folders merge into one mkdir'd target
    assert {"finance/taxes", "finance/taxes/2020"} <= mkdirs, mkdirs
    assert dsts["T1A"] == "finance/taxes/2020/receipt-a.pdf", dsts
    assert dsts["T2B"] == "finance/taxes/2020/receipt-b.pdf", dsts
    same = sorted(dsts[k] for k in ("T1S", "T2S"))
    assert same == ["archive/duplicates/taxes/2020/same.pdf", "finance/taxes/2020/same.pdf"], same
    assert not {"T1", "T2", "T1Y", "T2Y"} & set(dsts), "merged shells must stay in place"
    # left in place
    assert not {"L", "L1", "N"} & set(dsts), dsts
    assert not any(PROTECTED.casefold() in (o.get("src", "") + o["dst"]).casefold() for o in ops)
    print(f"rules_to_plan: ok ({len(ops)} ops, {len(moves)} moves, 0 validation errors)")


if __name__ == "__main__":
    main()
