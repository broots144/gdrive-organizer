"""peek's parser guards: capped download, zip-bomb refusal, parse time limit, pypdf floor."""
import io
import os
import re
import sqlite3
import tempfile
import sys
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
import _pkg  # noqa: E402,F401  (loads the package without the repo root on sys.path)
from gdrive_organizer import drive_api as gdrive  # noqa: E402
from gdrive_organizer import peek as p  # noqa: E402


class Req:
    def __init__(self, body):
        self.headers, self.body = {}, body

    def execute(self):
        rng = self.headers.get("Range", "")
        end = int(rng.split("-")[1]) + 1 if rng else len(self.body)
        return self.body[:end]


class Svc:
    def __init__(self, body):
        self.body, self.last = body, None

    def files(self):
        return self

    def get_media(self, fileId):
        self.last = Req(self.body)
        return self.last


def expect_refused(fn, needle):
    try:
        fn()
    except p.PeekRefused as ex:
        assert needle in str(ex), ex
        return
    raise AssertionError("expected PeekRefused")


def test_download_is_range_capped():
    gdrive.call = lambda req, tries=8: req.execute()
    svc = Svc(b"x" * 100)
    assert p._download_capped(svc, "k", 100) == b"x" * 100
    assert svc.last.headers["Range"] == "bytes=0-100"
    expect_refused(lambda: p._download_capped(Svc(b"x" * 101), "k", 100), "larger than 100")


def test_zip_bomb_refused_and_normal_docx_passes():
    bomb = io.BytesIO()
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", b"\0" * (p.ZIP_MAX_TOTAL + 1))
    expect_refused(lambda: p._check_zip(bomb.getvalue()), "safety limit")
    ratio = io.BytesIO()
    with zipfile.ZipFile(ratio, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", b"A" * 5_000_000)
    expect_refused(lambda: p._check_zip(ratio.getvalue()), "compressed suspiciously")
    normal = io.BytesIO()
    with zipfile.ZipFile(normal, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", os.urandom(4096).hex())
    p._check_zip(normal.getvalue())


def expect_timeout(fn, within):
    start = time.time()
    try:
        fn()
    except p.ParseTimeout as ex:
        assert "longer than 1s" in str(ex), ex
        assert time.time() - start < within, time.time() - start
        return
    raise AssertionError("expected ParseTimeout")


def test_slow_parse_is_cut_off():
    def spin():
        with p._time_limit(1):
            while True:
                pass
    expect_timeout(spin, 5)


def test_timeout_is_not_swallowed_by_broad_except():
    # pypdf's recovery loops catch Exception; the timeout must still get out
    def stubborn():
        with p._time_limit(1):
            while True:
                try:
                    while True:
                        pass
                except Exception:
                    continue
    expect_timeout(stubborn, 5)


def test_swallowed_timeout_fires_again():
    # even a bare except that eats the first signal gets the next one a second later
    seen = []

    def bare():
        with p._time_limit(1):
            while True:
                try:
                    while True:
                        pass
                except BaseException:
                    if seen:
                        raise
                    seen.append(1)
    expect_timeout(bare, 5)
    assert seen == [1]


def test_timer_is_disarmed_after_parse():
    import signal
    before = signal.getsignal(signal.SIGALRM)
    with p._time_limit(1):
        pass
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    assert signal.getsignal(signal.SIGALRM) is before
    time.sleep(1.2)  # nothing fires later


def test_peek_safely_records_timeout_and_errors():
    real = p.peek

    def times_out(*a):
        raise p.ParseTimeout("parsing took longer than 1s")
    try:
        p.peek = times_out
        method, text, err = p.peek_safely(None, {}, 1, 1)
        assert method == "error" and text is None and "ParseTimeout" in err, err
        p.peek = lambda *a: (_ for _ in ()).throw(ValueError("bad xref"))
        method, text, err = p.peek_safely(None, {}, 1, 1)
        assert method == "error" and "bad xref" in err, err
    finally:
        p.peek = real


def test_old_pypdf_is_refused():
    real = p._installed
    pdf = {"mime": "application/pdf", "ext": ".pdf", "size": 10, "key": "k"}
    try:
        for have in ("6.19.0", "6.21.3", None):
            p._installed = lambda dist, have=have: have
            expect_refused(lambda: p.peek(Svc(b"%PDF-1.4"), pdf, 10**6, 100), "below 6.21.4")
        p._installed = lambda dist: "6.21.4"
        p._require_pypdf()
        p._installed = lambda dist: "7.0.0rc1"
        p._require_pypdf()
    finally:
        p._installed = real
    p._require_pypdf()  # the installed pypdf (CI, the owner's venv) meets the floor


def test_floor_not_above_pyproject():
    # Dependabot may raise the pyproject floor; peek's runtime floor must never exceed it
    with open(os.path.join(HERE, "..", "pyproject.toml"), encoding="utf-8") as fh:
        declared = re.search(r'"pypdf>=([0-9.]+)"', fh.read()).group(1)
    assert p._version_tuple(p.PYPDF_FLOOR) <= p._version_tuple(declared), (p.PYPDF_FLOOR, declared)


def test_check_floors_script():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "check_floors", os.path.join(HERE, "..", "scripts", "check_floors.py"))
    cf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cf)
    toml = ('[project.optional-dependencies]\napi = ["google-api-python-client>=2.201.0"]\n'
            'peek = ["pypdf>=6.21.4", "python-docx>=1.2.0"]  # note\n'
            'all = ["gdrive-organizer[api,peek]"]\n\n[project.scripts]\nx = "y>=9"\n')
    req = cf.floors(toml)
    assert req == {"google-api-python-client": "2.201.0", "pypdf": "6.21.4",
                   "python-docx": "1.2.0"}, req
    assert cf.floors(toml, {"peek"}) == {"pypdf": "6.21.4", "python-docx": "1.2.0"}
    have = {"google-api-python-client": "2.201.0", "pypdf": "6.19.0"}.get
    bad = cf.check(req, have)
    assert bad == ["pypdf: 6.19.0 installed, below the declared floor 6.21.4",
                   "python-docx: not installed (need >=1.2.0)"], bad
    assert cf.check({"pypdf": "6.21.4"}, lambda d: "7.0.0rc1") == []
    assert cf.check({"pypdf": "6.21.4"}, lambda d: "6.21.10") == []


def test_legacy_snippets_table_gets_untrusted_column():
    from gdrive_organizer import core as g
    path = os.path.join(tempfile.mkdtemp(prefix="gdo-legacy-"), "old.sqlite")
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE snippets(key TEXT PRIMARY KEY, method TEXT, text TEXT, "
                "flagged INTEGER, error TEXT)")
    old.execute("INSERT INTO snippets VALUES('k','export','x',0,NULL)")
    old.commit()
    old.close()
    db = g.open_db(path)
    cols = [r[1] for r in db.execute("PRAGMA table_info(snippets)")]
    assert "untrusted" in cols, cols
    assert db.execute("SELECT untrusted FROM snippets").fetchone()[0] is None


def test_real_pdf_and_docx_still_parse():
    import docx
    from pypdf import PdfWriter

    gdrive.call = lambda req, tries=8: req.execute()
    pdf = io.BytesIO()
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    w.write(pdf)
    method, text = p.peek(Svc(pdf.getvalue()), {"mime": "application/pdf", "ext": ".pdf",
                                                "size": len(pdf.getvalue()), "key": "k"}, 10**6, 100)
    assert method == "pdf_p1" and text == ""
    d = docx.Document()
    d.add_paragraph("Quarterly garage inventory")
    buf = io.BytesIO()
    d.save(buf)
    method, text = p.peek(Svc(buf.getvalue()), {"mime": "", "ext": ".docx",
                                                "size": len(buf.getvalue()), "key": "k"}, 10**6, 100)
    assert method == "docx" and "Quarterly garage inventory" in text


def main():
    test_download_is_range_capped()
    test_real_pdf_and_docx_still_parse()
    test_zip_bomb_refused_and_normal_docx_passes()
    test_slow_parse_is_cut_off()
    test_timeout_is_not_swallowed_by_broad_except()
    test_swallowed_timeout_fires_again()
    test_timer_is_disarmed_after_parse()
    test_peek_safely_records_timeout_and_errors()
    test_old_pypdf_is_refused()
    test_floor_not_above_pyproject()
    test_legacy_snippets_table_gets_untrusted_column()
    test_check_floors_script()
    print("ALL PEEK LIMIT TESTS PASSED")


if __name__ == "__main__":
    main()
