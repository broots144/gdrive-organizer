"""core: shared guards, index schema and macOS primitives for gdrive-organizer.

Invariants every script in this folder keeps:
  1. A protected entry is recognized by NAME (or Drive ID) from its parent's listing and is
     never stat'ed, opened, listed, moved or renamed. Its path is recorded once so the
     validator can refuse any op that touches it or one of its ancestors.
  2. Nothing here opens file contents on the local mount. (`peek` reads content
     through the Drive API, never through FileProvider.)
  3. Path comparisons use NFC + casefold, because APFS is case-insensitive and
     normalization-insensitive by default. Operations use the raw path as listed.
  4. Local renames never clobber: renamex_np(RENAME_EXCL) on macOS.

Python 3.9+ compatible (macOS system python is 3.9).
"""
from __future__ import annotations

import ctypes
import ctypes.util
import errno
import hashlib
import json
import os
import re
import sqlite3
import sys
import unicodedata
from contextlib import contextmanager
from typing import Iterable, List, Optional

# ---------------------------------------------------------------- config

CONFIG_DEFAULT = {
    # Folders that must never be listed, stat'ed, read, moved or renamed, nor any ancestor moved.
    # Exact names, compared after NFC + casefold. Put your real names in a PRIVATE config file
    # (private/config.json is gitignored); never commit them.
    "protected_names": [],
    # Optional regex for variants of a protected name ("Copy of X (1)", other separators).
    "protected_regex": None,
    # Drive folder ID(s) of protected folders. Paste from the folder URL yourself:
    # https://drive.google.com/drive/folders/<THIS_PART>
    "protected_ids": [],
    # Names suggesting legal, medical, financial or identity material. Matching items are never
    # peeked, never moved without an explicit per-op override, and are listed only in a local
    # quarantine file. Extend it in your private config with your own terms.
    "sensitive_regex": (
        r"(?i)(attorney|counsel|plaintiff|defendant|deposition|subpoena|lawsuit|litigation|"
        r"privileged|settlement|eeoc|court|legal|\bssn\b|social.security|passport|\bw-?2\b|"
        r"\b1099\b|\b1040\b|tax.return|medical|diagnos|disability)"
    ),
    # Content patterns: a peeked snippet matching this is discarded and only flagged.
    "no_snippet_regex": (
        r"(?i)(attorney[- ]client|privileged|plaintiff|defendant|case no|social security|"
        r"\b\d{3}-\d{2}-\d{4}\b|routing number|account number|diagnos)"
    ),
    # Generic names worth a content peek.
    "generic_name_regex": (
        r"(?i)^(scan|img|image|dsc|dcim|document|doc|untitled|export|download|file|new|"
        r"copy of|screenshot|screen shot|photo|capture|output|data|test|temp|tmp|backup|"
        r"archive|misc|stuff|notes?)[\W_]*(\(?\d+\)?)?[\W_]*"
    ),
    # Directories with files modified within this many days are treated as live backup targets.
    "recent_days": 30,
}

BUNDLE_EXTS = {
    ".app", ".photoslibrary", ".photolibrary", ".aplibrary", ".fcpbundle", ".imovielibrary",
    ".tvlibrary", ".musiclibrary", ".band", ".logicx", ".lrdata", ".sparsebundle", ".bundle",
    ".framework", ".plugin", ".kext", ".xcodeproj", ".xcworkspace", ".playground", ".rtfd",
    ".pages", ".numbers", ".key", ".dsym", ".mlmodelc", ".xcarchive", ".vmwarevm", ".pvm",
    ".utm", ".vbox", ".docset", ".scptd", ".prefpane", ".qlgenerator", ".saver", ".wdgt",
}
HEAVY_DIRS = {
    "node_modules", "venv", ".venv", "__pycache__", ".terraform", ".tox", "bower_components",
    "Pods", "DerivedData", ".gradle", ".mypy_cache", ".pytest_cache", "site-packages",
}
PROJECT_MARKERS = {
    ".git", ".hg", ".svn", "package.json", "pyproject.toml", "setup.py", "Cargo.toml", "go.mod",
    "docker-compose.yml", "docker-compose.yaml", "compose.yaml", "compose.yml", "Makefile",
    ".terraform.lock.hcl", "main.tf", "ansible.cfg", "Vagrantfile", ".obsidian", "CMakeLists.txt",
    "pom.xml", "build.gradle", "Gemfile", "Dockerfile", "playbook.yml", "inventory.ini",
}
GDOC_EXTS = {
    ".gdoc", ".gsheet", ".gslides", ".gform", ".gdraw", ".gmap", ".gsite", ".gjam", ".gtable",
    ".gscript", ".glink",
}
FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
GAPPS_PREFIX = "application/vnd.google-apps."

NAME_MAX_BYTES = 255


def load_config(path: Optional[str]) -> dict:
    cfg = dict(CONFIG_DEFAULT)
    if path:
        with open(path, "r", encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    return cfg


# ---------------------------------------------------------------- names and paths

def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def pkey(path: str) -> str:
    """Comparison key for a relative path: NFC, casefolded, no leading/trailing slash."""
    return nfc(path).casefold().strip("/")


def parent_of(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def is_within(child_pk: str, ancestor_pk: str) -> bool:
    """True if child equals ancestor or lies below it. Both are pkeys. '' is the root."""
    if ancestor_pk == "":
        return True
    return child_pk == ancestor_pk or child_pk.startswith(ancestor_pk + "/")


def ext_of(name: str) -> str:
    base = name.rsplit("/", 1)[-1]
    if "." not in base.lstrip("."):
        return ""
    return "." + base.rsplit(".", 1)[-1].lower()


def name_problems(component: str) -> List[str]:
    probs = []
    if component in ("", ".", ".."):
        probs.append("empty or dot component")
    if "/" in component or "\x00" in component:
        probs.append("slash or NUL in name")
    if len(component.encode("utf-8")) > NAME_MAX_BYTES:
        probs.append("name longer than 255 bytes")
    if component != component.strip():
        probs.append("leading or trailing whitespace")
    if any(ord(c) < 32 for c in component):
        probs.append("control character")
    if ":" in component:
        probs.append("colon (shows as slash in Finder)")
    return probs


class Guard:
    def __init__(self, cfg: dict):
        self.names = {pkey(n) for n in cfg.get("protected_names", [])}
        self.rx = re.compile(cfg["protected_regex"]) if cfg.get("protected_regex") else None
        self.ids = set(cfg.get("protected_ids", []))
        self.sensitive = re.compile(cfg["sensitive_regex"]) if cfg.get("sensitive_regex") else None
        self.no_snippet = re.compile(cfg["no_snippet_regex"]) if cfg.get("no_snippet_regex") else None
        self.protected_pks: List[str] = []  # filled from the index's protected table

    def warn_if_unprotected(self) -> None:
        if not (self.names or self.rx or self.ids):
            eprint("NOTE: no protected folders configured (protected_names / protected_regex / "
                   "protected_ids). Every folder is in scope.")

    def is_protected_name(self, name: str) -> bool:
        n = nfc(name)
        return pkey(n) in self.names or bool(self.rx and self.rx.search(n))

    def is_protected_id(self, drive_id: Optional[str]) -> bool:
        return bool(drive_id) and drive_id in self.ids

    def text_hits_protected(self, text: str) -> bool:
        t = nfc(text)
        if self.rx and self.rx.search(t):
            return True
        return any(n in t.casefold() for n in self.names)

    def path_violation(self, path: str) -> Optional[str]:
        """Reason string if `path` touches protected ground, else None.
        Refuses: a protected-looking component anywhere; the protected path itself;
        anything inside it; any ancestor of it (moving an ancestor moves the folder)."""
        for comp in path.split("/"):
            if comp and self.is_protected_name(comp):
                return "path component matches protected name"
        pk = pkey(path)
        for ppk in self.protected_pks:
            if is_within(pk, ppk):
                return "path is inside protected folder"
            if is_within(ppk, pk):
                return "path is an ancestor of protected folder"
        return None

    def is_sensitive_path(self, path: str) -> bool:
        return bool(self.sensitive and self.sensitive.search(nfc(path)))


# ---------------------------------------------------------------- index database

SCHEMA = """
CREATE TABLE IF NOT EXISTS items(
  key TEXT PRIMARY KEY,
  path TEXT NOT NULL,
  pathkey TEXT NOT NULL,
  name TEXT NOT NULL,
  parent_key TEXT,
  depth INTEGER,
  kind TEXT,
  ext TEXT,
  size INTEGER,
  mtime REAL,
  dataless INTEGER,
  ino INTEGER,
  drive_id TEXT,
  finder_alias INTEGER,
  link_target TEXT,
  mime TEXT,
  md5 TEXT,
  owned_by_me INTEGER,
  viewed_by_me REAL,
  can_move INTEGER,
  atomic_root INTEGER DEFAULT 0,
  under_atomic TEXT,
  sensitive INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_items_pathkey ON items(pathkey);
CREATE INDEX IF NOT EXISTS ix_items_parent ON items(parent_key);
CREATE TABLE IF NOT EXISTS protected(path TEXT, pathkey TEXT, key TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS errors(path TEXT, err TEXT);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS snippets(
  key TEXT PRIMARY KEY, method TEXT, text TEXT, flagged INTEGER, error TEXT);
"""

ITEM_COLS = [
    "key", "path", "pathkey", "name", "parent_key", "depth", "kind", "ext", "size", "mtime",
    "dataless", "ino", "drive_id", "finder_alias", "link_target", "mime", "md5", "owned_by_me",
    "viewed_by_me", "can_move",
]


def open_db(path: str, fresh: bool = False) -> sqlite3.Connection:
    if fresh and os.path.exists(path):
        raise SystemExit(f"refusing to overwrite existing index {path}; move it aside first")
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def insert_item(db: sqlite3.Connection, row: dict) -> None:
    vals = [row.get(c) for c in ITEM_COLS]
    db.execute(
        f"INSERT INTO items({','.join(ITEM_COLS)}) VALUES({','.join('?' * len(ITEM_COLS))})", vals
    )


def set_meta(db: sqlite3.Connection, k: str, v) -> None:
    db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, json.dumps(v)))


def get_meta(db: sqlite3.Connection, k: str, default=None):
    r = db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return json.loads(r[0]) if r else default


def load_protected(db: sqlite3.Connection, guard: Guard) -> None:
    guard.protected_pks = [r[0] for r in db.execute("SELECT pathkey FROM protected")]


def mark_atomic(db: sqlite3.Connection) -> int:
    """Directories containing a project marker become atomic roots. Everything below an
    atomic root (or a bundle / heavy dir) must move with it, never alone."""
    marks = tuple(PROJECT_MARKERS)
    q = f"SELECT DISTINCT parent_key FROM items WHERE name IN ({','.join('?' * len(marks))})"
    parents = [r[0] for r in db.execute(q, marks) if r[0]]
    for pk_ in parents:
        db.execute("UPDATE items SET atomic_root=1 WHERE key=? AND kind='dir'", (pk_,))
    db.execute("UPDATE items SET atomic_root=1 WHERE kind IN ('bundle','heavy')")
    roots = db.execute(
        "SELECT pathkey FROM items WHERE atomic_root=1 ORDER BY depth ASC"
    ).fetchall()
    for (rpk,) in roots:
        db.execute(
            "UPDATE items SET under_atomic=? WHERE under_atomic IS NULL AND "
            "substr(pathkey,1,?)=?",
            (rpk, len(rpk) + 1, rpk + "/"),
        )
    return len(roots)


def mark_sensitive(db: sqlite3.Connection, guard: Guard) -> int:
    n = 0
    rows = db.execute("SELECT key, path FROM items").fetchall()
    for key, path in rows:
        if guard.is_sensitive_path(path):
            db.execute("UPDATE items SET sensitive=1 WHERE key=?", (key,))
            n += 1
    return n


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- macOS primitives

SF_DATALESS = 0x40000000          # xnu bsd/sys/stat.h
RENAME_EXCL = 0x00000004          # xnu bsd/sys/stdio.h
RENAME_NOFOLLOW_ANY = 0x00000010  # xnu bsd/sys/stdio.h
IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES = 3   # xnu bsd/sys/resource.h
IOPOL_SCOPE_THREAD = 1
IOPOL_MATERIALIZE_DATALESS_FILES_OFF = 1
XATTR_NOFOLLOW = 0x0001           # xnu bsd/sys/xattr.h

IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

_libc = None


def libc():
    global _libc
    if _libc is None:
        name = "/usr/lib/libSystem.B.dylib" if IS_MAC else ctypes.util.find_library("c")
        _libc = ctypes.CDLL(name, use_errno=True)
    return _libc


def rename_excl(src: str, dst: str) -> None:
    """Atomic rename that fails with EEXIST instead of replacing an existing dst."""
    s, d = os.fsencode(src), os.fsencode(dst)
    if IS_MAC:
        fn = libc().renamex_np
        fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        fn.restype = ctypes.c_int
        rc = fn(s, d, RENAME_EXCL | RENAME_NOFOLLOW_ANY)
        if rc != 0 and ctypes.get_errno() == errno.EINVAL:
            rc = fn(s, d, RENAME_EXCL)  # older macOS without RENAME_NOFOLLOW_ANY
    elif IS_LINUX:
        fn = libc().renameat2  # glibc 2.28+; used only for tests off-Mac
        fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        fn.restype = ctypes.c_int
        AT_FDCWD, RENAME_NOREPLACE = -100, 1
        rc = fn(AT_FDCWD, s, AT_FDCWD, d, RENAME_NOREPLACE)
    else:
        raise OSError(errno.ENOTSUP, "no non-clobbering rename on this platform")
    if rc != 0:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e), src, None, dst)


def get_xattr(path: str, name: str) -> Optional[bytes]:
    """Read one extended attribute without following symlinks. None if absent."""
    if not IS_MAC:
        try:
            return os.getxattr(path, name, follow_symlinks=False)  # type: ignore[attr-defined]
        except (OSError, AttributeError):
            return None
    fn = libc().getxattr
    fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
                   ctypes.c_uint32, ctypes.c_int]
    fn.restype = ctypes.c_ssize_t
    p, n = os.fsencode(path), name.encode("utf-8")
    size = fn(p, n, None, 0, 0, XATTR_NOFOLLOW)
    if size < 0:
        return None
    buf = ctypes.create_string_buffer(size)
    got = fn(p, n, buf, size, 0, XATTR_NOFOLLOW)
    return buf.raw[:got] if got >= 0 else None


@contextmanager
def no_materialize():
    """Within this block, on this thread, touching a dataless file fails with EDEADLK
    instead of downloading it. Use around any local read you believe is already local.
    Note: stat() still needs dataless PARENT folders materialized, so walk first."""
    if not IS_MAC:
        yield
        return
    lc = libc()
    lc.getiopolicy_np.argtypes = [ctypes.c_int, ctypes.c_int]
    lc.setiopolicy_np.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
    prev = lc.getiopolicy_np(IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES, IOPOL_SCOPE_THREAD)
    if prev < 0 or lc.setiopolicy_np(IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES,
                                      IOPOL_SCOPE_THREAD,
                                      IOPOL_MATERIALIZE_DATALESS_FILES_OFF) != 0:
        raise OSError(ctypes.get_errno(), "setiopolicy_np failed")
    try:
        yield
    finally:
        lc.setiopolicy_np(IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES, IOPOL_SCOPE_THREAD, prev)


def eprint(*a) -> None:
    print(*a, file=sys.stderr, flush=True)


def iter_chunks(seq: List, n: int) -> Iterable[List]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]
