"""Unit tests for paths the end-to-end tests do not reach: empty-dirs, drive_api retry and OAuth
plumbing, the CLI dispatcher, journal and check_journal edge cases, both apply backends' drift
refusals, reconcile and undo checks, and validate's error branches.

Everything is synthetic: a temp dir, a hand-built index, an in-memory fake Drive, and the Google
OAuth classes replaced by fakes. Nothing here opens a browser, talks to the network or reads a
real Drive.

  python3 tests/test_units.py
"""
import contextlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time

import _pkg  # noqa: F401  (loads the package without the repo root on sys.path)
from gdrive_organizer import apply as ap  # noqa: E402
from gdrive_organizer import cli  # noqa: E402
from gdrive_organizer import core as g  # noqa: E402
from gdrive_organizer import dedupe  # noqa: E402
from gdrive_organizer import drive_api as gdrive  # noqa: E402
from gdrive_organizer import empty_dirs  # noqa: E402
from gdrive_organizer import plan as planmod  # noqa: E402
from gdrive_organizer import validate as v  # noqa: E402

OLD = time.time() - 400 * 86400
NEW = time.time() - 60
PROTECTED = "EXAMPLE_PROTECTED_FOLDER"
CFG = dict(g.CONFIG_DEFAULT, protected_names=[PROTECTED], protected_ids=["PROTID"])
TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


@contextlib.contextmanager
def quiet():
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        yield out, err


def raises(exc, fn, *a, match=None, **kw):
    try:
        fn(*a, **kw)
    except exc as ex:
        if match is not None:
            assert match in str(ex), (match, str(ex))
        return ex
    raise AssertionError(f"{getattr(fn, '__name__', fn)} did not raise {exc.__name__}")


def run_main(mod, argv):
    with quiet() as (out, err):
        try:
            rc = mod.main(argv)
        except SystemExit as e:
            rc = e.code
    return rc, out.getvalue(), err.getvalue()


def item(key, path, kind="file", parent=None, size=5, mtime=OLD, md5=None, owned=1, can_move=1,
         **extra):
    name = path.rsplit("/", 1)[-1]
    return dict(key=key, path=path, pathkey=g.pkey(path), name=name, parent_key=parent,
                depth=path.count("/"), kind=kind, ext=g.ext_of(name),
                size=None if kind == "dir" else size, mtime=mtime, md5=md5, owned_by_me=owned,
                can_move=can_move, **extra)


def mkdb(path, rows, backend="drive", protected=(), finished=True):
    db = g.open_db(path)
    g.set_meta(db, "backend", backend)
    g.set_meta(db, "root_id", "ROOT")
    for r in rows:
        extra = {k: r.pop(k) for k in ("atomic_root", "under_atomic", "sensitive") if k in r}
        g.insert_item(db, r)
        for k, val in extra.items():
            db.execute(f"UPDATE items SET {k}=? WHERE key=?", (val, r["key"]))
    for p in protected:
        db.execute("INSERT INTO protected(path, pathkey, key, reason) VALUES(?,?,?,?)",
                   (p, g.pkey(p), "PROTID", "id or name match"))
    if finished:
        g.set_meta(db, "finished", time.time())
    db.commit()
    return db


def write_jsonl(path, recs):
    with open(path, "w", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")


def tmpdir():
    d = os.path.realpath(tempfile.mkdtemp(prefix="gdo-unit-"))
    return d


# ------------------------------------------------------------------ cli


@test
def cli_dispatch():
    rc, out, _ = run_main(cli, [])
    assert rc == 0 and "usage: gdrive-organizer <command>" in out and "empty-dirs" in out
    rc, out, _ = run_main(cli, ["--version"])
    assert rc == 0 and out.strip() == _pkg.load().__version__
    rc, _, err = run_main(cli, ["frobnicate"])
    assert rc == 2 and "unknown command: frobnicate" in err
    rc, _, err = run_main(cli, ["validate", "--help"])
    assert rc == 0  # argparse exits 0 on -h from the dispatched module
    print("cli: usage, --version, unknown command (rc 2), dispatch to a subcommand")


# ------------------------------------------------------------------ core


@test
def core_helpers():
    assert g.name_problems("ok name.txt") == []
    assert "empty or dot component" in g.name_problems("..")
    assert "slash or NUL in name" in g.name_problems("a\x00b")
    assert "name longer than 255 bytes" in g.name_problems("é" * 200)  # 400 UTF-8 bytes
    assert "leading or trailing whitespace" in g.name_problems(" x")
    assert "control character" in g.name_problems("a\tb")
    assert any(p.startswith("colon") for p in g.name_problems("a:b"))
    assert g.ext_of(".bashrc") == "" and g.ext_of("dir/A.TXT") == ".txt" and g.ext_of("x") == ""
    assert g.is_within("a/b", "") and g.is_within("a/b", "a") and not g.is_within("ab", "a")
    assert list(g.iter_chunks([1, 2, 3, 4, 5], 2)) == [[1, 2], [3, 4], [5]]
    guard = g.Guard(CFG)
    assert guard.text_hits_protected(f"notes about {PROTECTED.lower()} stuff")
    assert not guard.text_hits_protected("homelab notes")
    assert guard.is_protected_id("PROTID") and not guard.is_protected_id(None)
    with quiet() as (_, err):
        g.Guard(g.CONFIG_DEFAULT).warn_if_unprotected()
    assert "no protected folders configured" in err.getvalue()
    d = tmpdir()
    p = os.path.join(d, "x.sqlite")
    g.open_db(p).close()
    raises(SystemExit, g.open_db, p, fresh=True, match="refusing to overwrite")
    # non-clobbering rename: EEXIST, and both files intact
    a, b = os.path.join(d, "a"), os.path.join(d, "b")
    open(a, "w").write("A")
    open(b, "w").write("B")
    ex = raises(OSError, g.rename_excl, a, b)
    assert ex.errno == 17 and open(a).read() == "A" and open(b).read() == "B", ex
    assert g.get_xattr(a, "user.none-such") is None
    shutil.rmtree(d)
    print("core: name problems, ext_of, guard text checks, fresh-db refusal, rename_excl EEXIST")


# ------------------------------------------------------------------ drive_api


class Flaky:
    def __init__(self, method, errors):
        self.method, self.errors, self.n = method, list(errors), 0

    def execute(self):
        self.n += 1
        if self.errors:
            raise self.errors.pop(0)
        return {"ok": True}


def herr(status, reason=None, raw=None):
    from googleapiclient.errors import HttpError
    import httplib2
    body = raw if raw is not None else (
        json.dumps({"error": {"errors": [{"reason": reason}]}}) if reason else "{}").encode()
    return HttpError(httplib2.Response({"status": status}), body)


@test
def drive_call_status_handling():
    from googleapiclient.errors import HttpError
    real_sleep, gdrive.time.sleep = gdrive.time.sleep, lambda s: None
    try:
        # retryable on reads and writes alike: 429, 5xx, 403 rate/backend reasons
        w = Flaky("PATCH", [herr(429), herr(502), herr(403, "backendError"),
                            herr(403, "internalError"), herr(504)])
        assert gdrive.call(w) == {"ok": True} and w.n == 6, w.n
        # never retried: 404, 400, a 403 that is a real permission refusal, unparseable body
        for e in (herr(404), herr(400), herr(403, "insufficientFilePermissions"),
                  herr(403, raw=b"<html>not json</html>")):
            r = Flaky("GET", [e])
            raises(HttpError, gdrive.call, r)
            assert r.n == 1, (e, r.n)
        # gives up after `tries` and re-raises the last error
        r = Flaky("GET", [herr(503)] * 5)
        raises(HttpError, gdrive.call, r, tries=3)
        assert r.n == 3, r.n
        # idempotent=True opts a non-GET into transport retries; idempotent=False opts a GET out
        r = Flaky("POST", [ConnectionResetError(54, "reset")])
        assert gdrive.call(r, idempotent=True) == {"ok": True} and r.n == 2
        r = Flaky("GET", [ConnectionResetError(54, "reset")])
        raises(ConnectionResetError, gdrive.call, r, idempotent=False)
        assert r.n == 1
        # a request object with no .method is treated as non-idempotent
        class NoMethod(Flaky):
            pass
        r = NoMethod("x", [TimeoutError("t")])
        del r.method
        raises(TimeoutError, gdrive.call, r)
        assert r.n == 1
        # httplib2 transport errors count as transport errors
        import httplib2
        r = Flaky("GET", [httplib2.ServerNotFoundError("dns")])
        assert gdrive.call(r) == {"ok": True} and r.n == 2
    finally:
        gdrive.time.sleep = real_sleep
    assert gdrive._reason(herr(403, raw=b"\xff\xfe")) == ""
    assert gdrive._reason(herr(403, "rateLimitExceeded")) == "rateLimitExceeded"
    print("drive_api.call: retry set, non-retry set, give-up, idempotent override, httplib2")


class FakeCreds:
    instances = []

    def __init__(self, valid=True, expired=False, refresh_token="r", refresh_raises=None,
                 payload="tok"):
        self.valid, self.expired, self.refresh_token = valid, expired, refresh_token
        self.refresh_raises, self.payload, self.refreshed = refresh_raises, payload, 0
        FakeCreds.instances.append(self)

    def refresh(self, request):
        self.refreshed += 1
        if self.refresh_raises:
            raise self.refresh_raises
        self.valid = True

    def to_json(self):
        return json.dumps({"fake": self.payload})


@test
def drive_service_oauth_plumbing():
    """service(): token reuse, refresh, failed refresh falls back to sign-in, in-memory tokens
    are never written, written tokens are 0600. Every Google class here is a fake."""
    import google.auth.transport.requests as gtr
    import google.oauth2.credentials as goc
    import google_auth_oauthlib.flow as gflow
    import googleapiclient.discovery as gdisc
    saved = (gtr.Request, goc.Credentials, gflow.InstalledAppFlow, gdisc.build)
    log = {"flows": 0, "stored": None, "built": [], "modes": []}

    class Creds:
        @staticmethod
        def from_authorized_user_file(path, scopes):
            log["loaded"] = (path, scopes)
            return log["stored"]

    class Flow:
        @staticmethod
        def from_client_secrets_file(path, scopes):
            log["flow_scopes"] = scopes
            return Flow()

        def run_local_server(self, port):
            assert port == 0
            log["flows"] += 1
            return WatchedCreds(payload="fresh")

    class WatchedCreds(FakeCreds):
        def to_json(self):
            # serialized while the token file is already open: record its mode at that moment,
            # so a create-then-chmod window shows up even though the final mode is 0600
            if log.get("tok") and os.path.exists(log["tok"]):
                log["modes"].append(os.stat(log["tok"]).st_mode & 0o777)
            return super().to_json()

    def build(api, ver, credentials, cache_discovery):
        log["built"].append((api, ver, credentials, cache_discovery))
        return "SVC"

    # The fakes only apply while service() imports the Google classes inside the function; a
    # module-level import would bypass them and start a real browser sign-in.
    for name in ("Request", "Credentials", "InstalledAppFlow", "build"):
        assert not hasattr(gdrive, name), f"drive_api imports {name} at module level"
    gtr.Request, goc.Credentials = (lambda: "REQ"), Creds
    gflow.InstalledAppFlow, gdisc.build = Flow, build
    d = tmpdir()
    cs = os.path.join(d, "cs.json")  # never created; only the fake flow sees it
    cwd, mask = os.getcwd(), os.umask(0)
    try:
        # no token path: signs in, keeps the token in memory, writes nothing (cwd is the temp
        # dir, so a stray relative write would land here and be caught)
        os.chdir(d)
        assert gdrive.service("write", cs, None) == "SVC"
        assert log["flows"] == 1 and log["flow_scopes"] == [gdrive.SCOPES["write"]]
        assert os.listdir(d) == []
        os.chdir(cwd)
        # token path that does not exist yet: signs in and writes it in a new folder, 0600
        # from creation even with umask 0
        tok = log["tok"] = os.path.join(d, "nested", "token_meta.json")
        gdrive.service("meta", cs, tok)
        assert log["flows"] == 2 and json.load(open(tok)) == {"fake": "fresh"}
        assert os.stat(tok).st_mode & 0o777 == 0o600, oct(os.stat(tok).st_mode)
        # an existing, too-open token file is tightened when it is rewritten
        os.chmod(tok, 0o644)
        gdrive.service("meta", cs, tok)
        assert log["flows"] == 3 and os.stat(tok).st_mode & 0o777 == 0o600
        assert log["modes"] == [0o600, 0o600], [oct(m) for m in log["modes"]]
        # a valid stored token: no sign-in, no rewrite
        log["stored"] = FakeCreds(valid=True)
        os.chmod(tok, 0o600)
        before = os.stat(tok).st_mtime_ns
        gdrive.service("meta", cs, tok)
        assert log["flows"] == 3 and os.stat(tok).st_mtime_ns == before
        assert log["loaded"] == (tok, [gdrive.SCOPES["meta"]])
        # expired with a refresh token: refreshed in place, no sign-in
        log["stored"] = FakeCreds(valid=False, expired=True)
        gdrive.service("meta", cs, tok)
        assert log["stored"].refreshed == 1 and log["flows"] == 3
        # refresh fails (invalid_grant after 7 days in Testing): sign in again, rewrite token
        log["stored"] = FakeCreds(valid=False, expired=True, refresh_raises=ValueError("grant"))
        with quiet() as (out, _):
            gdrive.service("meta", cs, tok)
        assert "could not be refreshed (ValueError)" in out.getvalue()
        assert "grant" not in out.getvalue()  # the exception text is not echoed
        assert log["flows"] == 4 and json.load(open(tok)) == {"fake": "fresh"}
        assert set(log["modes"]) == {0o600}
        # invalid and not refreshable (no refresh token): sign in
        log["stored"] = FakeCreds(valid=False, expired=True, refresh_token=None)
        gdrive.service("meta", cs, tok)
        assert log["flows"] == 5 and log["stored"].refreshed == 0
        assert all(b[3] is False for b in log["built"])
    finally:
        os.chdir(cwd)
        os.umask(mask)
        gtr.Request, goc.Credentials, gflow.InstalledAppFlow, gdisc.build = saved
        shutil.rmtree(d)
    print("drive_api.service: memory-only token, 0600 token file, reuse, refresh, re-sign-in")


# ------------------------------------------------------------------ empty-dirs


def empty_dirs_index(d):
    rows = [
        item("E", "Old", "dir", "ROOT"),
        item("E1", "Old/a", "dir", "E"),
        item("E2", "Old/a/b", "dir", "E1"),
        item("E3", "Old/c", "dir", "E"),
        item("F", "Full", "dir", "ROOT"),
        item("F1", "Full/sub", "dir", "F"),
        item("FF", "Full/sub/x.txt", "file", "F1", md5="m1"),
        item("DT", "Dotted", "dir", "ROOT"),
        item("DTF", "Dotted/.keep", "dot", "DT"),
        item("SCP", "Links", "dir", "ROOT"),
        item("SC", "Links/to-x", "shortcut", "SCP"),
        item("T", "Taxonomy", "dir", "ROOT"),
        item("T1", "Taxonomy/Reference", "dir", "T"),
        item("N", "Camera Uploads", "dir", "ROOT"),
        item("N1", "Camera Uploads/2024", "dir", "N"),
        item("P", "Shared", "dir", "ROOT", owned=0),
        item("L", "Locked", "dir", "ROOT", can_move=0),
        item("R", "repo", "dir", "ROOT", atomic_root=1),
        item("R1", "repo/build", "dir", "R", under_atomic="repo"),
        item("H", "Holder", "dir", "ROOT"),  # only child is the protected folder
        item("Q", "Mixed", "dir", "ROOT"),
        item("Q1", "Mixed/empty", "dir", "Q"),
        item("Q2", "Mixed/theirs", "dir", "Q", owned=0),
    ]
    return mkdb(os.path.join(d, "idx.sqlite"), rows, protected=[f"Holder/{PROTECTED}"])


@test
def empty_dirs_selection():
    d = tmpdir()
    empty_dirs_index(d).close()
    json.dump(CFG, open(os.path.join(d, "cfg.json"), "w"))
    with open(os.path.join(d, "pol.py"), "w") as fh:
        fh.write("NEVER_TRASH = ['Camera Uploads']\nKEEP_EMPTY = ['taxonomy/reference']\n")
    out = os.path.join(d, "out.jsonl")
    argv = ["--db", os.path.join(d, "idx.sqlite"), "--config", os.path.join(d, "cfg.json"),
            "--policy", os.path.join(d, "pol.py"), "--out", out]
    rc, stdout, _ = run_main(empty_dirs, argv)
    assert rc == 0, stdout
    ops = [json.loads(x) for x in open(out)]
    assert all(o["op"] == "trash" and set(o) == {"op", "src", "src_key"} for o in ops), ops
    got = [o["src"] for o in ops]
    # innermost first, then by path; Mixed/empty goes, Mixed stays (a kept folder below it)
    assert got == ["Old/a/b", "Mixed/empty", "Old/a", "Old/c", "Old"], got
    assert "empty folders to trash: 5" in stdout, stdout
    for reason, n in (("never_trash_area", 2), ("keep_empty", 1), ("protected", 1),
                      ("atomic", 2), ("not_owned_or_locked", 3), ("has_kept_subfolder", 2)):
        assert f"'{reason}': {n}" in stdout, (reason, stdout)
    assert PROTECTED not in stdout and PROTECTED not in open(out).read()
    # the output must pass validate as-is (validate wants innermost first, no keep_key)
    db = g.open_db(os.path.join(d, "idx.sqlite"))
    errors, _, plan = v.validate(db, CFG, v.load_manifest(out))
    assert not errors and len(plan) == 5, errors
    shutil.rmtree(d)
    print("empty-dirs: innermost first, policy/protected/atomic/owner skips, output validates")


# ------------------------------------------------------------------ dedupe


@test
def dedupe_keep_policy():
    d = tmpdir()
    rows = [
        item("K", "Reference/a.pdf", parent="RF", md5="m", size=9),
        item("X1", "Downloads/a.pdf", parent="DL", md5="m", size=9),
        item("X2", "Inbox/a (1).pdf", parent="IB", md5="m", size=9),
        item("NT", "Camera/a.pdf", parent="CA", md5="m", size=9),
        item("AT", "repo/a.pdf", parent="RP", md5="m", size=9, under_atomic="repo"),
        item("NO", "shared/a.pdf", parent="SH", md5="m", size=9, owned=0),
        item("RC", "new/a.pdf", parent="NW", md5="m", size=9, mtime=NEW),
        item("SN", "Inbox/medical a.pdf", parent="IB", md5="m", size=9, sensitive=1),
        item("Z1", "z/empty1", parent="Z", md5="e", size=0),
        item("Z2", "z/empty2", parent="Z", md5="e", size=0),
        item("U", "unique.bin", parent="ROOT", md5="u", size=3),
    ]
    mkdb(os.path.join(d, "idx.sqlite"), rows).close()
    json.dump(CFG, open(os.path.join(d, "cfg.json"), "w"))
    with open(os.path.join(d, "pol.py"), "w") as fh:
        # Downloads would win on path length; KEEP_ORDER puts Reference first, LAST demotes Inbox
        fh.write("KEEP_ORDER = ['Reference']\nLAST = ['Inbox']\nNEVER_TRASH = ['Camera']\n")
    out = os.path.join(d, "out.jsonl")
    rc, stdout, _ = run_main(dedupe, ["--db", os.path.join(d, "idx.sqlite"), "--config",
                                      os.path.join(d, "cfg.json"), "--policy",
                                      os.path.join(d, "pol.py"), "--out", out])
    assert rc == 0, stdout
    ops = {o["src_key"]: o for o in map(json.loads, open(out))}
    assert set(ops) == {"X1", "X2", "SN"}, ops
    assert all(o["keep_key"] == "K" for o in ops.values())
    assert ops["SN"]["override"] == ["sensitive"] and "override" not in ops["X1"]
    for reason in ("never_trash_area", "inside_atomic", "not_owned", "recent"):
        assert f"'{reason}': 1" in stdout, (reason, stdout)
    assert "duplicate groups: 1" in stdout, stdout  # zero-byte files are never grouped
    errors, _, _ = v.validate(g.open_db(os.path.join(d, "idx.sqlite")), CFG,
                              v.load_manifest(out))
    assert not errors, errors
    assert dedupe.human(10) == "10.0B" and dedupe.human(2048) == "2.0KB"
    assert dedupe.human(5 * 1024 ** 4) == "5.0TB"
    shutil.rmtree(d)
    print("dedupe: KEEP_ORDER/LAST ranking, skip reasons, sensitive override, output validates")


# ------------------------------------------------------------------ validate


def validate_index(d):
    rows = [
        item("A", "Archive", "dir", "ROOT"),
        item("R", "repo", "dir", "ROOT", atomic_root=1),
        item("RS", "repo/src", "dir", "R", under_atomic="repo"),
        item("RF", "repo/src/main.py", "file", "RS", under_atomic="repo"),
        item("F1", "one.txt", "file", "ROOT", md5="m", size=4),
        item("F2", "two.txt", "file", "ROOT", md5="m", size=4),
        item("F3", "three.txt", "file", "ROOT", md5="other", size=4),
        item("F0", "zero.txt", "file", "ROOT", md5="z", size=0),
        item("F0b", "zero-b.txt", "file", "ROOT", md5="z", size=0),
        item("NM", "nomd5.bin", "file", "ROOT", md5=None, size=4),
        item("SNS", "medical bill.pdf", "file", "ROOT", md5="m", size=4, sensitive=1),
        item("NO", "theirs.txt", "file", "ROOT", md5="m", size=4, owned=0),
        item("RC", "fresh.txt", "file", "ROOT", md5="m", size=4, mtime=NEW),
        item("SC", "link", "shortcut", "ROOT"),
        item("D", "Dir", "dir", "ROOT"),
        item("D1", "Dir/inner", "dir", "D"),
        item("DF", "Dir/inner/f.txt", "file", "D1", md5="m", size=4),
        item("E", "Empty", "dir", "ROOT"),
        item("E1", "Empty/sub", "dir", "E"),
        item("EL", "Locked", "dir", "ROOT", can_move=0),
        item("EO", "NotMine", "dir", "ROOT", owned=0),
        item("DOT", ".hidden", "dot", "ROOT"),
        item("GD", "plan.gdoc", "gdoc", "ROOT"),
        item("CM", "nomove.txt", "file", "ROOT", can_move=0),
    ]
    return mkdb(os.path.join(d, "idx.sqlite"), rows)


def errs_for(db, ops, backend="drive"):
    for n, op in enumerate(ops, 1):
        op["_line"] = n
    with db:
        g.set_meta(db, "backend", backend)
    errors, warns, plan = v.validate(db, CFG, ops)
    return "\n".join(errors), "\n".join(warns), plan


@test
def validate_move_branches():
    d = tmpdir()
    db = validate_index(d)
    cases = [
        ({"op": "copy", "dst": "x"}, "unknown op 'copy'"),
        ({"op": "mkdir", "dst": "/"}, "empty dst"),
        ({"op": "mkdir", "dst": "Archive/.cache"}, "dot-folder"),
        ({"op": "mkdir", "dst": "Archive/ edge"}, "leading or trailing whitespace"),
        ({"op": "mkdir", "dst": "repo/src/new"}, "inside an atomic unit"),
        ({"op": "mkdir", "dst": "Nope/new"}, "not exactly one indexed folder"),
        ({"op": "mkdir", "dst": "ARCHIVE"}, "already exists"),
        ({"op": "mkdir", "dst": f"Archive/{PROTECTED}"}, "dst path component matches protected"),
        ({"op": "move", "src": "one.txt", "src_key": "F2", "dst": "Archive/one.txt"},
         "src_key not found or does not match"),
        ({"op": "move", "src": "Archive", "dst": "Archive/inner"}, "dst is inside src"),
        ({"op": "move", "src": ".hidden", "dst": "Archive/hidden"}, "src is a dot entry"),
        ({"op": "move", "src": "repo/src", "dst": "Archive/src"}, "inside atomic unit 'repo'"),
        ({"op": "move", "src": "medical bill.pdf", "dst": "Archive/bill.pdf"}, "sensitive-looking"),
        ({"op": "move", "src": "nomove.txt", "dst": "Archive/nomove.txt"}, "cannot move"),
    ]
    for op, needle in cases:
        e, _, _ = errs_for(db, [dict(op)])
        assert needle in e, (op, needle, e)
    # gdoc pointer files keep their extension on the fs backend
    e, _, _ = errs_for(db, [{"op": "move", "src": "plan.gdoc", "dst": "Archive/plan.txt"}], "fs")
    assert "Google pointer file must keep its extension" in e, e
    # overrides make the exception explicit and per op
    e, w, plan = errs_for(db, [{"op": "move", "src": "medical bill.pdf", "dst": "Archive/b.pdf",
                                "override": ["sensitive"]},
                               {"op": "move", "src": "theirs.txt", "dst": "Archive/t.txt",
                                "override": ["not_owned"]},
                               {"op": "move", "src": "fresh.txt", "dst": "Archive/fresh.md"},
                               {"op": "move", "src": "link", "dst": "Archive/link"},
                               {"op": "mkdir", "dst": "Archive/a:b"}])
    assert not e, e
    for needle in ("file modified recently", "extension changes .txt -> .md",
                   "moves only the link", "colon"):
        assert needle in w, (needle, w)
    assert len(plan) == 5
    # sources must be disjoint, and nothing may land inside an earlier move's destination
    e, _, _ = errs_for(db, [{"op": "move", "src": "Dir", "dst": "Archive/Dir"},
                            {"op": "move", "src": "Dir/inner", "dst": "Archive/inner"}])
    assert "src lies inside another moved source" in e, e
    e, _, _ = errs_for(db, [{"op": "move", "src": "Empty", "dst": "Archive/Empty"},
                            {"op": "mkdir", "dst": "Archive/Empty/x"}])
    assert "inside something moved earlier" in e, e
    e, _, _ = errs_for(db, [{"op": "mkdir", "dst": "Dir/inner/new"},
                            {"op": "move", "src": "Dir", "dst": "Archive/Dir"}])
    assert "dst is inside a source that this manifest moves" in e, e
    shutil.rmtree(d)
    print("validate: move/mkdir error branches, per-op overrides, warnings, disjoint sources")


@test
def validate_trash_branches():
    d = tmpdir()
    db = validate_index(d)
    t = lambda src, key, keep=None, **kw: dict({"op": "trash", "src": src, "src_key": key,  # noqa
                                                "keep_key": keep}, **kw)
    cases = [
        (t("one.txt", None, "F2"), "trash needs src and a matching src_key"),
        (t("one.txt", "F1"), "keep_key not found"),
        (t("one.txt", "F1", "F1"), "keep_key is the src itself"),
        (t("one.txt", "F1", "F3"), "not byte-identical"),
        (t("nomd5.bin", "NM", "F1"), "not byte-identical"),
        (t("zero.txt", "F0", "F0b"), "empty files are not deduplicated"),
        (t("one.txt", "F1", "SC"), "keep_key is not a file"),
        (t("link", "SC", "F1"), "never shortcuts or bundles"),
        (t("repo/src/main.py", "RF", "F1"), "inside atomic unit"),
        (t("medical bill.pdf", "SNS", "F1"), "sensitive-looking"),
        (t("theirs.txt", "NO", "F1"), "not yours to do"),
        (t("fresh.txt", "RC", "F1"), "modified recently"),
        (t("Dir", "D"), "1 non-folder item(s) below it"),
        (t("Empty", "E"), "1 folder(s) below it are not trashed earlier"),
        (t("Empty/sub", "E1", "F1"), "a folder trash needs none"),
        (t("Locked", "EL"), "cannot change this folder"),
        (t("NotMine", "EO"), "folder not owned by you"),
        (t("repo", "R"), "atomic unit"),
    ]
    for op, needle in cases:
        e, _, _ = errs_for(db, [dict(op)])
        assert needle in e, (op, needle, e)
    e, _, _ = errs_for(db, [t("one.txt", "F1", "F2"), t("two.txt", "F2", "F1")])
    assert "keep_key is itself trashed by this manifest" in e, e
    e, _, _ = errs_for(db, [{"op": "move", "src": "two.txt", "dst": "Archive/two.txt"},
                            t("one.txt", "F1", "F2")])
    assert "keep_key is moved by this manifest" in e, e
    e, _, _ = errs_for(db, [{"op": "move", "src": "Dir", "dst": "Archive/Dir"},
                            t("Dir/inner/f.txt", "DF", "F1")])
    assert "src lies inside another source" in e, e
    # an empty folder chain, innermost first, is valid; outermost first is not
    e, _, plan = errs_for(db, [t("Empty/sub", "E1"), t("Empty", "E")])
    assert not e and [p["kind"] for p in plan] == ["dir", "dir"], e
    e, _, _ = errs_for(db, [t("Empty", "E"), t("Empty/sub", "E1")])
    assert "not trashed earlier" in e, e
    # the CLI summary counts trash ops and folder trash separately
    write_jsonl(os.path.join(d, "m.jsonl"), [t("Empty/sub", "E1"), t("Empty", "E"),
                                             t("one.txt", "F1", "F2")])
    rc, out, _ = run_main(v, ["--db", os.path.join(d, "idx.sqlite"), "--manifest",
                              os.path.join(d, "m.jsonl")])
    assert rc == 0 and "trash=3 (folders=2) bytes_trashed=4" in out, out
    shutil.rmtree(d)
    print("validate: every trash refusal, keep_key survival, folder trash ordering, CLI summary")


@test
def validate_manifest_comments():
    d = tmpdir()
    p = os.path.join(d, "m.jsonl")
    with open(p, "w") as fh:
        fh.write('# reviewed 2026-01-01\n\n{"op": "mkdir", "dst": "A"}\n  \n{"op": "mkdir", "dst": "B"}\n')
    ops = v.load_manifest(p)
    assert [(o["dst"], o["_line"]) for o in ops] == [("A", 3), ("B", 5)], ops
    shutil.rmtree(d)
    print("validate.load_manifest: skips comments and blanks, keeps real line numbers")


# ------------------------------------------------------------------ journal


@test
def journal_edges():
    d = tmpdir()
    p = os.path.join(d, "j.jsonl")
    j = ap.Journal(p)
    assert j.read() == []
    # a bad line in the middle is refused (not a crash artifact)
    with open(p, "w") as fh:
        fh.write('{"i": -1, "state": "header"}\nnot json\n{"i": 0, "state": "intent"}\n')
    raises(SystemExit, j.read, match="line 2 is not valid JSON")
    # a torn last line is dropped, and the next append truncates it away first
    with open(p, "w") as fh:
        fh.write('{"i": -1, "state": "header"}\n{"i": 0, "sta')
    with quiet() as (_, err):
        recs = j.read()
    assert recs == [{"i": -1, "state": "header"}] and "torn line" in err.getvalue()
    j.append({"i": 0, "state": "intent"})
    lines = open(p).read().splitlines()
    assert len(lines) == 2 and json.loads(lines[1])["state"] == "intent", lines
    # a whole but unterminated last record is kept, and the next append starts a new line
    with open(p, "w") as fh:
        fh.write('{"i": -1, "state": "header"}\n{"i": 0, "state": "done"}')
    assert len(j.read()) == 2
    j.append({"i": 1, "state": "intent"})
    assert [json.loads(x)["i"] for x in open(p).read().splitlines()] == [-1, 0, 1]
    assert ap.last_state([{"i": 0, "state": "intent"}, {"i": 0, "state": "done"}])[0]["state"] \
        == "done"
    shutil.rmtree(d)
    print("journal: mid-file garbage refused, torn tail truncated, unterminated tail kept")


@test
def check_journal_refusals():
    plan = [{"i": 0, "op": "mkdir", "dst": "A", "src": None, "src_key": None}]
    ops = [{"op": "mkdir", "dst": "A"}, {"op": "move", "src": "x", "src_key": "K", "dst": "A/x"}]
    hdr = {"i": -1, "state": "header", "manifest_sha": "S"}
    ap.check_journal([], "S", plan, ops)  # empty journal: fine
    ap.check_journal([hdr, {"i": 0, "state": "done", "op": "mkdir", "dst": "A", "src": None,
                            "src_key": None}], "S", plan, ops)
    raises(SystemExit, ap.check_journal, [{"i": 0, "state": "done"}], "S", plan, ops,
           match="no manifest header")
    raises(SystemExit, ap.check_journal, [dict(hdr, manifest_sha="T")], "S", plan, ops,
           match="different manifest")
    for bad_i in (5, -1, True, "0", None):
        raises(SystemExit, ap.check_journal, [hdr, {"i": bad_i, "state": "done"}], "S", plan,
               ops, match="not in this manifest")
    raises(SystemExit, ap.check_journal,
           [hdr, {"i": 0, "state": "done", "op": "mkdir", "dst": "B", "src": None,
                  "src_key": None}], "S", plan, ops, match="does not match the manifest")
    # op 1 no longer validates (not in plan) but is still a reviewed manifest op
    ok = {"i": 1, "state": "done", "op": "move", "dst": "A/x", "src": "x", "src_key": "K"}
    ap.check_journal([hdr, ok], "S", plan, ops)
    raises(SystemExit, ap.check_journal, [hdr, dict(ok, src_key="OTHER")], "S", plan, ops,
           match="does not match the manifest")
    # a failed record carries no op fields and is accepted for an in-range op
    ap.check_journal([hdr, {"i": 1, "state": "failed", "err": "x"}], "S", plan, ops)
    print("check_journal: header, sha, out-of-range and bool indexes, field mismatch")


# ------------------------------------------------------------------ fs backend


def fs_setup():
    d = tmpdir()
    root = os.path.join(d, "My Drive")
    os.makedirs(os.path.join(root, "Archive"))
    os.makedirs(os.path.join(root, "proj"))
    for n in ("a.txt", "b.txt", "c.txt"):
        with open(os.path.join(root, n), "w") as fh:
            fh.write(n)
        os.utime(os.path.join(root, n), (OLD, OLD))
    json.dump(CFG, open(os.path.join(d, "cfg.json"), "w"))
    rc, out, err = run_main(_index_fs(), ["--root", root, "--db", os.path.join(d, "fs.sqlite"),
                                          "--config", os.path.join(d, "cfg.json"),
                                          "--allow-any-root", "--no-xattrs"])
    assert rc == 0, (out, err)
    return d, root


def _index_fs():
    from gdrive_organizer import index_fs
    return index_fs


def fs_entry(db, src, dst):
    ops = [{"op": "move", "src": src, "dst": dst, "_line": 1}]
    errors, _, plan = v.validate(db, CFG, ops)
    assert not errors, errors
    return plan[0]


@test
def fs_backend_checks():
    d, root = fs_setup()
    db = g.open_db(os.path.join(d, "fs.sqlite"))
    guard = g.Guard(CFG)
    raises(SystemExit, ap.FsBackend, db, guard, "/somewhere/else", True, match="does not match")
    raises(SystemExit, ap.FsBackend, db, guard, None, False, match="not under ~/Library")
    be = ap.FsBackend(db, guard, root, True)
    e = fs_entry(db, "a.txt", "Archive/a.txt")
    raises(RuntimeError, be.trash, e, match="drive backend only")
    # drift: size, mtime, and inode (a different file now at that path)
    p = os.path.join(root, "a.txt")
    with open(p, "a") as fh:
        fh.write("more")
    os.utime(p, (OLD, OLD))
    raises(RuntimeError, be.move, e, match="size changed")
    with open(p, "w") as fh:
        fh.write("a.txt")
    os.utime(p, (NEW, NEW))
    raises(RuntimeError, be.move, e, match="mtime changed")
    os.utime(p, (OLD, OLD))
    os.rename(p, p + ".bak")
    shutil.copy2(p + ".bak", p)
    raises(RuntimeError, be.move, e, match="inode changed")
    os.remove(p)
    os.rename(p + ".bak", p)
    # destination parent missing
    e2 = fs_entry(db, "b.txt", "Archive/b.txt")
    e2 = dict(e2, dst="Nope/b.txt")
    raises(RuntimeError, be.move, e2, match="dst parent missing")
    # reconcile from the filesystem
    mk = {"op": "mkdir", "dst": "Fresh"}
    assert be.reconcile(mk, {}) == ("redo", {})
    os.mkdir(os.path.join(root, "Fresh"))
    assert be.reconcile(mk, {}) == ("done", {})
    assert be.reconcile(e, {})[0] == "redo"          # src present, dst absent
    open(os.path.join(root, "Archive", "a.txt"), "w").close()
    assert be.reconcile(e, {})[0] == "halt"          # both present: a person decides
    os.remove(p)
    v_, extra = be.reconcile(e, {})
    assert v_ == "done" and extra == {"src_raw": "a.txt"}
    os.remove(os.path.join(root, "Archive", "a.txt"))
    assert be.reconcile(e, {})[0] == "halt"          # neither present
    # undo_reconcile and undo binding
    assert be.undo_reconcile({"op": "mkdir", "dst": "Fresh"}) == "redo"
    assert be.undo_reconcile({"op": "mkdir", "dst": "Gone"}) == "undone"
    assert be.undo_reconcile({"op": "move", "dst": "x"}) == "halt"  # no src_raw
    rec = {"op": "move", "src_key": e2["src_key"], "src_raw": "b.txt", "dst": "Archive/b.txt"}
    assert be.undo_reconcile(rec) == "undone"         # b.txt is back, dst absent
    raises(RuntimeError, be.undo, rec, None, match="not the reviewed src path")
    raises(RuntimeError, be.undo, rec, {"src": "c.txt"}, match="not the reviewed src path")
    raises(RuntimeError, be.undo, dict(rec, src_key="nope"), {"src": "b.txt"},
           match="does not match the index")
    os.rmdir(os.path.join(root, "Fresh"))
    shutil.rmtree(d)
    print("fs backend: root checks, size/mtime/inode drift, reconcile and undo binding")


@test
def apply_main_fs_flow():
    d, root = fs_setup()
    os.chdir(d)
    man = os.path.join(d, "m.jsonl")
    write_jsonl(man, [{"op": "mkdir", "dst": "Archive/2021"},
                      {"op": "move", "src": "a.txt", "dst": "Archive/2021/a.txt"},
                      {"op": "move", "src": "b.txt", "dst": "Archive/2021/b.txt"},
                      {"op": "move", "src": "c.txt", "dst": "Archive/2021/c.txt"}])
    sha = g.sha256_file(man)
    base = ["--backend", "fs", "--db", os.path.join(d, "fs.sqlite"), "--config",
            os.path.join(d, "cfg.json"), "--manifest", man, "--allow-any-root"]
    rc, out, _ = run_main(ap, base)
    assert rc == 0 and out.startswith("DRY RUN backend=fs ops=4"), out
    assert os.path.exists(os.path.join(root, "a.txt"))  # a dry run touches nothing
    rc, _, _ = run_main(ap, base + ["--backend", "drive"])
    assert "index backend does not match" in str(rc), rc
    rc, _, _ = run_main(ap, base + ["--execute", "--confirm-sha", "0" * 64])
    assert "--confirm-sha must equal" in str(rc), rc
    rc, _, _ = run_main(ap, base + ["--batch", "0"])
    assert rc == 2  # argparse error
    # --max-ops stops early; a rerun resumes; --batch pauses between batches
    ex = base + ["--execute", "--confirm-sha", sha]
    rc, out, _ = run_main(ap, ex + ["--max-ops", "2"])
    assert rc == 0 and "stopped after --max-ops 2" in out, out
    assert os.path.exists(os.path.join(root, "Archive", "2021", "a.txt"))
    assert os.path.exists(os.path.join(root, "b.txt"))
    sleeps = []
    real_sleep, ap.time.sleep = ap.time.sleep, sleeps.append
    try:
        rc, out, _ = run_main(ap, ex + ["--batch", "1", "--pause", "7"])
    finally:
        ap.time.sleep = real_sleep
    assert rc == 0 and "complete: 2 ops this run" in out and sleeps == [7.0, 7.0], (out, sleeps)
    # once undone, the journal cannot drive a fresh forward run
    rc, out, _ = run_main(ap, ex + ["--undo", "--pause", "0"])
    assert rc == 0 and "undone 4 ops" in out, out
    assert sorted(os.listdir(root)) == ["Archive", "a.txt", "b.txt", "c.txt", "proj"]
    rc, _, _ = run_main(ap, ex)
    assert "has been undone" in str(rc), rc
    # a failed op stops later runs until --retry-failed, which reconciles first
    j2 = os.path.join(d, "j2.jsonl")
    blocker = os.path.join(root, "Archive", "2021")
    os.mkdir(blocker)  # mkdir op 0 now fails with EEXIST
    rc, _, _ = run_main(ap, ex + ["--journal", j2])
    assert "op 0 failed" in str(rc), rc
    rc, _, _ = run_main(ap, ex + ["--journal", j2])
    assert "failed earlier" in str(rc) and "--retry-failed" in str(rc), rc
    rc, out, _ = run_main(ap, ex + ["--journal", j2, "--retry-failed", "--pause", "0"])
    assert rc == 0 and "op 0: already applied; recorded as done" in out, out
    # validation errors stop a forward run before any backend is built
    bad = os.path.join(d, "bad.jsonl")
    write_jsonl(bad, [{"op": "move", "src": "nope.txt", "dst": "x.txt"}])
    rc, out, _ = run_main(ap, base[:-3] + ["--manifest", bad, "--allow-any-root"])
    assert "1 validation errors; nothing executed" in str(rc), (rc, out)
    os.chdir(tempfile.gettempdir())
    shutil.rmtree(d)
    print("apply(fs): dry run, sha/backend/batch refusals, --max-ops, pauses, undone journal, "
          "--retry-failed reconcile")


@test
def apply_dry_run_truncates():
    d = tmpdir()
    rows = [item("A", "Archive", "dir", "ROOT")]
    mkdb(os.path.join(d, "idx.sqlite"), rows).close()
    man = os.path.join(d, "m.jsonl")
    write_jsonl(man, [{"op": "mkdir", "dst": f"Archive/d{n:02}"} for n in range(30)])
    rc, out, _ = run_main(ap, ["--backend", "drive", "--db", os.path.join(d, "idx.sqlite"),
                               "--manifest", man])
    assert rc == 0 and "ops=30" in out and "... 5 more" in out, out
    shutil.rmtree(d)
    print("apply dry run lists 25 ops and counts the rest")


# ------------------------------------------------------------------ drive backend


class Req:
    def __init__(self, fn, method="GET"):
        self.fn, self.method = fn, method

    def execute(self):
        return self.fn()


class FakeFiles:
    def __init__(self, st):
        self.st, self.calls = st, []

    def get(self, fileId, fields=None):
        self.calls.append(("get", fileId))
        if fileId == "root":
            return Req(lambda: {"id": "ROOT"})
        return Req(lambda: dict(self.st[fileId], id=fileId))

    def list(self, q=None, **kw):
        self.calls.append(("list", q))
        pid = q.split("'")[1]
        return Req(lambda: {"files": [{"id": k} for k, d in self.st.items()
                                      if pid in d.get("parents", []) and not d.get("trashed")]})

    def update(self, fileId, body=None, addParents=None, removeParents=None, fields=None):
        self.calls.append(("update", fileId))

        def run():
            d = self.st[fileId]
            if removeParents:
                d["parents"].remove(removeParents)
            if addParents:
                d["parents"].append(addParents)
            d.update(body or {})
            return dict(d, id=fileId)
        return Req(run, "PATCH")


class FakeSvc:
    def __init__(self, st):
        self.f = FakeFiles(st)

    def files(self):
        return self.f


def drive_state():
    F = g.FOLDER_MIME
    return {
        "A": {"name": "A", "mimeType": F, "parents": ["ROOT"]},
        "f1": {"name": "f1.txt", "parents": ["A"], "md5Checksum": "m", "size": "5"},
        "keep": {"name": "keep.txt", "parents": ["ROOT"], "md5Checksum": "m", "size": "5",
                 "ownedByMe": True},
        "D": {"name": "D", "mimeType": F, "parents": ["ROOT"]},
        "two": {"name": "two", "parents": ["A", "D"]},
    }


def drive_backend(st, root_id="ROOT", dirs=(("a", "A"), ("d", "D"))):
    d = tmpdir()
    db = mkdb(os.path.join(d, "idx.sqlite"),
              [item(k, p.capitalize() if p in ("a", "d") else p, "dir", "ROOT")
               for p, k in dirs])
    with db:
        g.set_meta(db, "root_id", root_id)
    real = gdrive.service
    gdrive.service = lambda *a, **k: FakeSvc(st)
    try:
        return ap.DriveBackend(db, g.Guard(CFG), "cs", None), d
    finally:
        gdrive.service = real


@test
def drive_backend_trash_and_undo_checks():
    raises(SystemExit, drive_backend, drive_state(), root_id="OTHER",
           match="not this account's My Drive")
    st = drive_state()
    be, d = drive_backend(st)
    fe = {"op": "trash", "src": "A/f1.txt", "src_key": "f1", "src_parent_key": "A",
          "kind": "file", "keep_key": "keep", "expect": {"md5": "m", "name": "f1.txt"}}
    for mut, needle in (
            (lambda s: s["f1"].update(trashed=True), "src is already trashed"),
            (lambda s: s["f1"].update(parents=["D"]), "src parent changed"),
            (lambda s: s["f1"].update(md5Checksum="x"), "src content changed"),
            (lambda s: s["f1"].pop("md5Checksum"), "src content changed"),
            (lambda s: s["keep"].update(trashed=True), "copy to keep is gone"),
            (lambda s: s["keep"].update(size="6"), "no longer identical"),
            (lambda s: s["keep"].update(ownedByMe=False), "copy to keep is not owned by you"),
            (lambda s: s["keep"].pop("ownedByMe"), "copy to keep is not owned by you"),
            (lambda s: s["A"].update(name="Renamed"), "not at the reviewed path"),
            (lambda s: s["A"].update(parents=["ROOT", "D"]), "not at the reviewed path")):
        fresh = drive_state()
        mut(fresh)
        st.clear()
        st.update(fresh)
        raises(RuntimeError, be.trash, fe, match=needle)
        assert not st["f1"].get("trashed") or needle == "src is already trashed"
    st.clear()
    st.update(drive_state())
    assert be.trash(fe) == {"trashed": True} and st["f1"]["trashed"] is True
    # undo of a trash: refused once it left the trash or its folder; else un-trashed
    rec = {"op": "trash", "src_key": "f1", "dst": ""}
    st["f1"]["parents"] = ["D"]
    raises(RuntimeError, be.undo, rec, fe, match="not in its original folder")
    st["f1"]["parents"] = ["A"]
    be.undo(rec, fe)
    assert st["f1"]["trashed"] is False
    raises(RuntimeError, be.undo, rec, fe, match="no longer in the trash")
    # folder trash: drift and live children
    de = {"op": "trash", "src": "D", "src_key": "D", "src_parent_key": "ROOT", "kind": "dir",
          "expect": {"name": "D"}}
    raises(RuntimeError, be.trash, de, match="folder is not empty in Drive")
    st["two"]["parents"] = ["A"]
    st["D"]["parents"] = ["A"]
    raises(RuntimeError, be.trash, de, match="folder parent changed")
    st["D"]["parents"] = ["ROOT"]
    st["D"]["trashed"] = True
    raises(RuntimeError, be.trash, de, match="already trashed")
    st["D"]["trashed"] = False
    assert be.trash(de) == {"trashed": True}
    # a trash or move naming a protected id (src, its parent or the copy to keep) is refused
    # before any Drive call reaches that id
    for bad in (dict(fe, src_key="PROTID"), dict(fe, keep_key="PROTID"),
                dict(fe, src_parent_key="PROTID"), dict(de, src_key="PROTID")):
        raises(RuntimeError, be.trash, bad, match="protected id")
    mv = {"op": "move", "src": "A/f1.txt", "src_key": "PROTID", "src_parent_key": "A",
          "dst": "D/f1.txt", "expect": {"name": "f1.txt"}}
    raises(RuntimeError, be.move, mv, match="protected id")
    raises(RuntimeError, be.move, dict(mv, src_key="f1", src_parent_key="PROTID"),
           match="protected id")
    assert not [c for c in be.svc.f.calls if c[1] == "PROTID"]
    shutil.rmtree(d)
    print("drive backend trash: trashed/parent/md5/keep/path drift, non-empty folder, undo checks")


@test
def drive_backend_reconcile_and_resolution():
    st = drive_state()
    be, d = drive_backend(st, dirs=(("a", "A"), ("d", "D"), ("d", "D2")))
    # trash reconcile: done if trashed, redo if still in place, halt if it moved
    te = {"op": "trash", "src_key": "f1", "src_parent_key": "A"}
    assert be.reconcile(te, {}) == ("redo", {})
    st["f1"]["trashed"] = True
    assert be.reconcile(te, {}) == ("done", {"trashed": True})
    st["f1"].update(trashed=False, parents=["D"])
    assert be.reconcile(te, {}) == ("halt", {})
    # folder ids: ambiguous index paths and protected ids are refused before any write
    raises(RuntimeError, be.folder_id, "D", match="2 matches")
    raises(RuntimeError, be.folder_id, "Nope", match="0 matches")
    be.created[g.pkey("Safe")] = "PROTID"
    raises(RuntimeError, be.folder_id, "Safe", match="protected folder")
    assert be.folder_id("") == "ROOT"
    assert be.live_path_ok("PROTID", "X") is False
    assert be.live_path_ok("two", "A/two") is False  # two parents: not a single reviewed path
    st["A"]["trashed"] = True
    assert be.live_path_ok("A", "A") is False
    st["A"]["trashed"] = False
    raises(RuntimeError, be.adopt_created, {"dst": "A/new", "new_id": None},
           match="invalid or protected")
    raises(RuntimeError, be.adopt_created, {"dst": "A/new", "new_id": "PROTID"},
           match="invalid or protected")
    st["N"] = {"name": "other", "mimeType": g.FOLDER_MIME, "parents": ["A"]}
    raises(RuntimeError, be.adopt_created, {"dst": "A/new", "new_id": "N"},
           match="not the folder this manifest created")
    st["N"]["name"] = "new"
    be.adopt_created({"dst": "A/new", "new_id": "N"})
    assert be.created[g.pkey("A/new")] == "N"
    # undo_reconcile: protected ids halt; trash and mkdir follow live trashed state
    assert be.undo_reconcile({"op": "move", "src_key": "PROTID", "dst": "x"}) == "halt"
    assert be.undo_reconcile({"op": "trash", "src_key": "f1"}) == "undone"
    st["f1"]["trashed"] = True
    assert be.undo_reconcile({"op": "trash", "src_key": "f1"}) == "redo"
    assert be.undo_reconcile({"op": "mkdir", "dst": "A/new"}) == "halt"  # no new_id
    assert be.undo_reconcile({"op": "mkdir", "dst": "A/new", "new_id": "N"}) == "redo"
    st["N"]["trashed"] = True
    assert be.undo_reconcile({"op": "mkdir", "dst": "A/new", "new_id": "N"}) == "undone"
    mv = {"op": "move", "src_key": "keep", "dst": "A/kept.txt", "old_parent": "ROOT",
          "old_name": "keep.txt", "new_parent": "A"}
    assert be.undo_reconcile(mv) == "undone"
    st["keep"].update(parents=["A"], name="kept.txt")
    assert be.undo_reconcile(mv) == "redo"
    st["keep"]["name"] = "something else"
    assert be.undo_reconcile(mv) == "halt"
    st["keep"]["trashed"] = True
    assert be.undo_reconcile(mv) == "halt"
    raises(RuntimeError, be.undo, dict(mv, new_parent="PROTID"), match="protected id")
    raises(RuntimeError, be.undo, mv, None, None, match="no reviewed manifest op")
    raises(RuntimeError, be.undo, mv, None, {"src": "other.txt"}, match="(name)")
    shutil.rmtree(d)
    print("drive backend: trash reconcile, folder id refusals, adopt_created, undo_reconcile")


# ------------------------------------------------------------------ hash-pinned lock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(name):
    with open(os.path.join(REPO, name), encoding="utf-8") as fh:
        return fh.read()


@test
def lock_is_hashed_and_meets_floors():
    import importlib.util
    import re
    spec = importlib.util.spec_from_file_location(
        "check_floors", os.path.join(REPO, "scripts", "check_floors.py"))
    cf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cf)
    lock, pyproject, extra_in, justfile = (_read("requirements.txt"), _read("pyproject.toml"),
                                           _read("requirements.in"), _read("justfile"))
    pins = {}
    for block in re.split(r"\n(?=[A-Za-z0-9])", lock):
        m = re.match(r"([A-Za-z0-9_.-]+)==(\S+)", block)
        if m:
            assert "--hash=sha256:" in block, f"{m.group(1)} pinned without a hash"
            pins.setdefault(m.group(1).lower(), m.group(2))
    assert pins, "no pins in requirements.txt"
    # every declared dependency is locked at or above its pyproject floor
    bad = cf.check(cf.floors(pyproject), lambda d: pins.get(d.lower()))
    assert not bad, bad
    # the build backend is locked too (CI installs with --no-build-isolation)
    build = re.search(r'requires = \["setuptools>=([0-9.]+)"\]', pyproject).group(1)
    assert f"setuptools>={build}" in extra_in, "requirements.in setuptools floor != build-system"
    assert "setuptools" in pins
    # Dependabot reruns the header command; it must be the one `just lock` runs
    header = re.search(r"#    (uv pip compile .*)", lock).group(1)
    recipe = re.search(r"\nlock:\n((?:    .*\n)+)", justfile).group(1)
    recipe = " ".join(recipe.replace("\\\n", " ").split())
    assert recipe == header, (recipe, header)


# ------------------------------------------------------------------ review 2026-11-07 (generators)


def _plan(d, rows, rules, out="plan.jsonl"):
    """Run `plan` on a fresh index with these rows and this RULES body; returns (rc, stdout,
    ops, db path). A hang (the old collision loop) fails the test instead of the run."""
    import signal
    dbp = os.path.join(d, "idx.sqlite")
    if os.path.exists(dbp):
        os.remove(dbp)
    mkdb(dbp, rows).close()
    with open(os.path.join(d, "cfg.json"), "w") as fh:
        json.dump(CFG, fh)
    with open(os.path.join(d, "rules.py"), "w") as fh:
        fh.write(rules)
    outp = os.path.join(d, out)

    def hang(*_):
        raise AssertionError("plan did not terminate")
    old = signal.signal(signal.SIGALRM, hang)
    signal.alarm(20)
    try:
        rc, stdout, err = run_main(planmod, ["--db", dbp, "--config", os.path.join(d, "cfg.json"),
                                             "--rules", os.path.join(d, "rules.py"), "--out", outp])
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)
    ops = [json.loads(x) for x in open(outp)] if rc == 0 else []
    return rc, stdout + err, ops, dbp


def _valid(dbp, ops):
    for n, op in enumerate(ops, 1):
        op["_line"] = n
    errors, _, _ = v.validate(g.open_db(dbp), CFG, ops)
    return errors


@test
def review_dedupe_keeps_an_owned_copy():
    """M2: the kept copy must be one the owner owns, in dedupe, validate and the backend."""
    d = tmpdir()
    rows = [
        # the shared copy sits under the preferred prefix and would win on path rank alone
        item("SH", "Reference/a.pdf", parent="RF", md5="m", size=9, owned=0),
        item("MY", "Downloads/a.pdf", parent="DL", md5="m", size=9),
        item("MY2", "Downloads/old/a.pdf", parent="DO", md5="m", size=9),
        # a group with no owned copy at all yields nothing
        item("S1", "x/b.pdf", parent="X", md5="b", size=9, owned=0),
        item("S2", "y/b.pdf", parent="Y", md5="b", size=9, owned=0),
    ]
    dbp = os.path.join(d, "idx.sqlite")
    mkdb(dbp, rows).close()
    json.dump(CFG, open(os.path.join(d, "cfg.json"), "w"))
    with open(os.path.join(d, "pol.py"), "w") as fh:
        fh.write("KEEP_ORDER = ['Reference']\n")
    out = os.path.join(d, "out.jsonl")
    rc, stdout, _ = run_main(dedupe, ["--db", dbp, "--config", os.path.join(d, "cfg.json"),
                                      "--policy", os.path.join(d, "pol.py"), "--out", out])
    assert rc == 0, stdout
    ops = [json.loads(x) for x in open(out)]
    assert [(o["src_key"], o["keep_key"]) for o in ops] == [("MY2", "MY")], ops
    assert "'no_owned_copy': 1" in stdout and "'not_owned': 1" in stdout, stdout
    # a hand-written op that keeps the shared copy is refused by validate
    db = g.open_db(dbp)
    e, _, _ = errs_for(db, [{"op": "trash", "src": "Downloads/a.pdf", "src_key": "MY",
                             "keep_key": "SH"}])
    assert "keep_key is not owned by you" in e, e
    shutil.rmtree(d)
    print("review M2: dedupe keeps an owned copy; validate and DriveBackend refuse a shared keep")


@test
def review_nested_atomic_roots():
    """M3: a nested atomic root (node_modules inside a repo) is part of the outer unit."""
    d = tmpdir()
    rows = [
        item("AR", "Archive", "dir", "ROOT"),
        item("R", "repo", "dir", "ROOT", atomic_root=1),
        item("NM", "repo/node_modules", "dir", "R", atomic_root=1, under_atomic="repo"),
        item("NMF", "repo/node_modules/x.js", parent="NM", under_atomic="repo"),
    ]
    rc, out, ops, dbp = _plan(d, rows, "RULES = [dict(id='nm', where=\"i.name='node_modules'\", "
                                       "dst='Archive/{lh}')]\n")
    assert rc == 0 and not [o for o in ops if o["op"] == "move"], (out, ops)
    assert "'inside_atomic': 1" in out, out
    e, _, _ = errs_for(g.open_db(dbp), [{"op": "move", "src": "repo/node_modules",
                                         "dst": "Archive/node_modules"}])
    assert "inside atomic unit 'repo'" in e, e
    # the outer unit itself still moves
    e, _, _ = errs_for(g.open_db(dbp), [{"op": "move", "src": "repo", "dst": "Archive/repo"}])
    assert not e, e
    shutil.rmtree(d)
    print("review M3: nested atomic roots move only with the outer unit (plan and validate)")


@test
def review_plan_collision_edges():
    d = tmpdir()
    # M4: an item already at its target stays put, never routed to duplicates as its own twin
    rows = [item("A", "archive", "dir", "ROOT"),
            item("B", "archive/backups", "dir", "A"),
            item("Z", "archive/backups/db.zip", parent="B", md5="z", size=9)]
    rc, out, ops, _ = _plan(d, rows, "RULES = [dict(id='bk', where=\"i.ext='.zip'\", "
                                     "dst='archive/backups/{lh}')]\n")
    assert rc == 0 and ops == [], (out, ops)
    assert "'already_at_target': 1" in out and "duplicates" not in json.dumps(ops), out

    # L1: three identical same-name files in one folder terminate: one takes the name, one goes
    # to duplicates, the third finds that occupied and stays
    rows = [item("D", "dump", "dir", "ROOT")] + [
        item(k, "dump/x.pdf", parent="D", md5="s", size=9) for k in ("X1", "X2", "X3")]
    rc, out, ops, dbp = _plan(d, rows, "RULES = [dict(id='docs', where=\"i.ext='.pdf'\", "
                                       "dst='docs/{lh}')]\n")
    assert rc == 0, out
    moves = sorted(o["dst"] for o in ops if o["op"] == "move")
    assert moves == ["archive/duplicates/dump/x.pdf", "docs/x.pdf"], ops
    assert "'duplicates_target_occupied': 1" in out, out
    assert not _valid(dbp, ops)

    # L3: an outer match that is gated (recent) no longer swallows an inner match that can move
    rows = [item("O", "Old", "dir", "ROOT"),
            item("ON", "Old/new.txt", parent="O", mtime=NEW, md5="n", size=3),
            item("OP", "Old/old.pdf", parent="O", md5="o", size=3)]
    rules = ("RULES = [dict(id='outer', where=\"i.key='O'\", dst='archive/{lh}'),\n"
             "         dict(id='inner', where=\"i.key='OP'\", dst='docs/{lh}')]\n")
    rc, out, ops, dbp = _plan(d, rows, rules)
    assert rc == 0, out
    assert [(o["src_key"], o["dst"]) for o in ops if o["op"] == "move"] == [("OP", "docs/old.pdf")]
    assert "'recent': 1" in out and "nested_in_other_source" not in out, out
    assert not _valid(dbp, ops)

    # L7: a folder holding the target name blocks a file from landing on it
    rows = [item("DC", "docs", "dir", "ROOT"),
            item("DR", "docs/report.pdf", "dir", "DC"),
            item("IN", "inbox", "dir", "ROOT"),
            item("F", "inbox/report.pdf", parent="IN", md5="r", size=3)]
    rc, out, ops, _ = _plan(d, rows, "RULES = [dict(id='docs', where=\"i.key='F'\", "
                                     "dst='docs/{lh}')]\n")
    assert rc == 0 and ops == [] and "'target_occupied': 1" in out, (out, ops)
    shutil.rmtree(d)
    print("review M4/L1/L3/L7: in-place items, triple duplicates, gate before nesting, "
          "folder occupant")


@test
def review_generators_refuse_executed_out():
    """L4: plan, dedupe and empty-dirs never overwrite a manifest that has an apply journal."""
    d = tmpdir()
    rows = [item("A", "a", "dir", "ROOT")]
    rc, out, _, dbp = _plan(d, rows, "RULES = []\n")
    assert rc == 0, out
    outp = os.path.join(d, "plan.jsonl")
    open(outp + ".journal.jsonl", "w").close()
    with open(outp, "w") as fh:
        fh.write("executed\n")
    with open(os.path.join(d, "pol.py"), "w") as fh:
        fh.write("")
    cfgp = os.path.join(d, "cfg.json")
    for mod, extra in ((planmod, ["--rules", os.path.join(d, "rules.py")]),
                       (dedupe, ["--policy", os.path.join(d, "pol.py")]),
                       (empty_dirs, ["--policy", os.path.join(d, "pol.py")])):
        rc, _, _ = run_main(mod, ["--db", dbp, "--config", cfgp, "--out", outp] + extra)
        assert "has an apply journal" in str(rc), (mod.__name__, rc)
        assert open(outp).read() == "executed\n"
    shutil.rmtree(d)
    print("review L4: generators refuse an --out that apply has journaled")


# ------------------------------------------------------------------ main


def main():
    failed = []
    for t in TESTS:
        try:
            t()
        except Exception as ex:  # report every failure, not only the first
            import traceback
            traceback.print_exc()
            failed.append(f"{t.__name__}: {ex!r}")
    if failed:
        print("FAILED:\n  " + "\n  ".join(failed))
        return 1
    print(f"ALL UNIT TESTS PASSED ({len(TESTS)} groups)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
