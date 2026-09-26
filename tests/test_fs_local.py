"""End-to-end fs backend test on a throwaway tree in a temp dir (never your Drive).

On macOS this exercises the real ctypes calls (renamex_np, getxattr, st_flags) before anything
points at CloudStorage.

  python3 tests/test_fs_local.py
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PROTECTED = "EXAMPLE_PROTECTED_FOLDER"
ENV = dict(os.environ, PYTHONPATH=REPO)


def cli(*args, ok=(0,)):
    r = subprocess.run([sys.executable, "-m", "gdrive_organizer"] + list(args), cwd=WORK,
                       capture_output=True, text=True, env=ENV)
    if r.returncode not in ok:
        raise SystemExit(f"FAILED {args} rc={r.returncode}\n{r.stdout}\n{r.stderr}")
    return r.stdout


def listing(root):
    out = []
    for d, dirs, files in os.walk(root):
        for n in dirs + files:
            out.append(os.path.relpath(os.path.join(d, n), root))
    return sorted(out)


WORK = os.path.realpath(tempfile.mkdtemp())  # macOS: /var -> /private/var
ROOT = os.path.join(WORK, "My Drive")
subprocess.run([sys.executable, os.path.join(HERE, "make_tree.py"), ROOT], check=True)
with open(os.path.join(WORK, "config.json"), "w") as fh:
    json.dump({"protected_names": [PROTECTED],
               "protected_regex": r"(?i)example[\W_]*protected[\W_]*folder"}, fh)
before = listing(ROOT)
cli("index-fs", "--root", ROOT, "--db", "fs.sqlite", "--config", "config.json", "--allow-any-root")

db = sqlite3.connect(os.path.join(WORK, "fs.sqlite"))
reasons = sorted(r[0] for r in db.execute("SELECT reason FROM protected"))
assert reasons == ["link or shortcut to protected", "name match", "name match"], reasons
leaked = db.execute("SELECT COUNT(*) FROM items WHERE path LIKE '%PROTECTED_FOLDER%'").fetchone()[0]
assert leaked == 0, "protected content reached the index"
print("protected folder, its copy and a symlink to it were excluded")

out = cli("report", "--db", "fs.sqlite", "--config", "config.json",
          "--quarantine-out", "quarantine.txt")
assert "Attorney letter" not in out and "Tax Return" not in out, "sensitive name in report"
print("report masks sensitive names")

plan = [
    {"op": "move", "src": "untitled_export.csv", "dst": "Reports/export.csv"},
    {"op": "mkdir", "dst": "Archive"},
    {"op": "move", "src": "homelab/proxmox-2021", "dst": "Archive/proxmox-2021"},
    {"op": "move", "src": "Scan_001.pdf", "dst": "Archive/2021-03 scan.pdf"},
    {"op": "move", "src": "Café menu.txt", "dst": "Archive/Café menu.txt"},
]
with open(os.path.join(WORK, "plan.jsonl"), "w") as fh:
    fh.write("\n".join(json.dumps(p) for p in plan) + "\n")
bad = [
    {"op": "move", "src": PROTECTED, "dst": "Archive2"},
    {"op": "move", "src": "homelab/ansible-repo/ansible.cfg", "dst": "x.cfg"},
    {"op": "move", "src": "homelab/live-rclone-target", "dst": "y"},
    {"op": "move", "src": "Tax Return 2019.pdf", "dst": "z.pdf"},
]
with open(os.path.join(WORK, "bad.jsonl"), "w") as fh:
    fh.write("\n".join(json.dumps(p) for p in bad) + "\n")
out = cli("validate", "--db", "fs.sqlite", "--config", "config.json", "--manifest", "bad.jsonl",
          ok=(1,))
for needle in ("protected name", "atomic unit", "live backup", "sensitive-looking"):
    assert needle in out, (needle, out)
print("validator blocks protected, atomic, live and sensitive moves")

out = cli("validate", "--db", "fs.sqlite", "--config", "config.json", "--manifest", "plan.jsonl")
sha = out.strip().split("sha256=")[-1]
# plant a collision at op 0's destination after validation: rename must fail, never clobber
BLOCK = os.path.join(ROOT, "Reports", "export.csv")
with open(BLOCK, "w") as fh:
    fh.write("blocker")
common = ["apply", "--backend", "fs", "--db", "fs.sqlite", "--config", "config.json",
          "--manifest", "plan.jsonl", "--allow-any-root", "--execute", "--confirm-sha", sha,
          "--pause", "0"]
cli(*common, ok=(1,))
assert open(BLOCK).read() == "blocker"
assert os.path.exists(os.path.join(ROOT, "untitled_export.csv"))
print("collision refused by non-clobbering rename, blocker and source intact")
os.remove(BLOCK)
cli(*common, "--journal", "run2.jsonl")
assert os.path.isdir(os.path.join(ROOT, "Archive", "proxmox-2021"))
print("moves applied")
cli(*common, "--journal", "run2.jsonl", "--undo")
assert listing(ROOT) == before, "tree not restored"
print("undo restored the tree exactly")

sys.path.insert(0, REPO)
from gdrive_organizer import core  # noqa: E402

with core.no_materialize():  # binds setiopolicy_np on macOS; no-op elsewhere
    with open(os.path.join(ROOT, "Reports", "q1.pdf"), "rb") as fh:
        assert fh.read() == b"r"
print("iopolicy tripwire binding OK")
print("platform:", sys.platform, "ALL FS TESTS PASSED")
