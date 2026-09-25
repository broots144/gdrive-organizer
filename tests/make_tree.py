"""Build a fake 'My Drive' tree for tests. Never point this at a real Drive."""
import os
import sys
import time
import unicodedata

root = sys.argv[1]
PROTECTED = "ACME_V_EXAMPLECORP_LEGAL"
old = time.mktime((2021, 3, 1, 0, 0, 0, 0, 0, -1))


def mk(path, data=b"x", mtime=None):
    p = os.path.join(root, path)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as fh:
        fh.write(data)
    if mtime:
        os.utime(p, (mtime, mtime))


os.makedirs(root, exist_ok=True)
mk(f"{PROTECTED}/complaint.pdf", b"SECRET")
mk(f"{PROTECTED}/sub/exhibit.pdf", b"SECRET")
mk(f"Copy of {PROTECTED} (1)/x.pdf", b"SECRET")
os.symlink(os.path.join(root, PROTECTED), os.path.join(root, "legal-link"))
mk("Scan_001.pdf", b"%PDF-1.4", old)
mk("untitled_export.csv", b"a,b", old)
mk("Tax Return 2019.pdf", b"t", old)
mk("Attorney letter.pdf", b"t", old)
mk(unicodedata.normalize("NFD", "Café menu.txt"), b"c", old)
mk("homelab/proxmox-2021/vzdump-100.tar.gz", b"z" * 5000, old)
mk("homelab/proxmox-2021/vzdump-101.tar.gz", b"z" * 7000, old)
mk("homelab/live-rclone-target/today.bak", b"z" * 100)          # recent mtime
mk("homelab/ansible-repo/.git/HEAD", b"ref", old)
mk("homelab/ansible-repo/ansible.cfg", b"[defaults]", old)
mk("homelab/ansible-repo/roles/web/tasks/main.yml", b"- x", old)
mk("homelab/ansible-repo/node_modules/left-pad/index.js", b"x", old)
mk("Photos.photoslibrary/database/Photos.sqlite", b"db", old)
mk("Reports/q1.pdf", b"r", old)
mk("stuff/notes.txt", b"n", old)
mk("stuff/a.gdoc", b'{"doc_id":"abc"}', old)
print("tree ok")
