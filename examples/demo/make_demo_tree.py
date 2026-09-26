"""Build a small fake 'My Drive' for the demo. Every name is made up. Never point it at a real Drive.

  python3 examples/demo/make_demo_tree.py /tmp/demo/My\\ Drive
"""
import os
import sys
import time

root = sys.argv[1]
OLD = time.mktime((2019, 6, 1, 0, 0, 0, 0, 0, -1))


def mk(path, data=b"x", mtime=OLD):
    p = os.path.join(root, path)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as fh:
        fh.write(data)
    if mtime:
        os.utime(p, (mtime, mtime))


# the folder that must never be seen, touched or moved
mk("Legal - do not touch/contract.pdf", b"PRIVATE")
# loose files at the top level
for name in ("receipt-2019-03.pdf", "receipt-2019-04.pdf", "tax-return-notes.txt",
             "Untitled document.txt", "kids-soccer-schedule.pdf", "IMG_2041.jpg"):
    mk(name, name.encode() * 40)
# an old Dropbox dump
mk("old dropbox/Initech/tps-reports/q1.xlsx", b"q1" * 500)
mk("old dropbox/Initech/tps-reports/q2.xlsx", b"q2" * 500)
mk("old dropbox/apps/PortableEditor/editor.exe", b"MZ" * 4000)
mk("old dropbox/apps/PortableEditor/readme.txt", b"r" * 80)
mk("old dropbox/taxes/2012/return.pdf", b"t12" * 300)
mk("old dropbox/Photos 2014/beach.jpg", b"jpg" * 3000)
mk("old dropbox/Photos 2014/beach copy.jpg", b"jpg" * 3000)          # exact duplicate
mk("old dropbox/scouts/campout-flyer.pdf", b"flyer" * 100)
# a folder something still syncs into every day: must stay put
mk("Camera Uploads/today.jpg", b"new" * 200, mtime=None)
# a code project: moves as one unit or not at all
mk("homelab-ansible/.git/HEAD", b"ref: refs/heads/main")
mk("homelab-ansible/site.yml", b"- hosts: all")
mk("homelab-ansible/roles/web/tasks/main.yml", b"- name: x")
# existing folders the plan merges into
mk("Taxes/2021/return.pdf", b"t21" * 300)
