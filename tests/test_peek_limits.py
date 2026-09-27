"""peek's parser guards: capped download, zip-bomb refusal, parse time limit."""
import io
import os
import sys
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
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


def test_slow_parse_is_cut_off():
    start = time.time()
    expect_refused(lambda: _spin(p), "longer than 1s")
    assert time.time() - start < 5


def _spin(mod):
    with mod._time_limit(1):
        while True:
            pass


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
    print("ALL PEEK LIMIT TESTS PASSED")


if __name__ == "__main__":
    main()
