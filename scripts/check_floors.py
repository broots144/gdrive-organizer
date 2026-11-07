#!/usr/bin/env python3
"""Fail when an installed dependency is missing or older than the floor pyproject.toml declares.

A venv made before a floor bump keeps the old version until someone reinstalls, and pip does not
warn. `just check` and CI run this, so a stale venv (for example a pypdf with known parser DoS
advisories, which `peek` feeds untrusted PDFs) is caught before it parses anything.

  python3 scripts/check_floors.py [--extras api,peek]   # default: every extra
"""
import argparse
import importlib.metadata
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REQ = re.compile(r'"([A-Za-z0-9_.-]+)(?:\[[^\]]*\])?>=([0-9][0-9.]*)"')


def vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", re.match(r"[0-9.]*", v).group(0))[:3])


def floors(text, extras=None):
    """{dist: floor} from [project.optional-dependencies] (a self-reference like all is skipped)."""
    sect = re.search(r"^\[project\.optional-dependencies\]\n(.*?)(?=^\[)", text, re.M | re.S)
    out = {}
    for line in (sect.group(1) if sect else "").splitlines():
        m = re.match(r"\s*([A-Za-z0-9_-]+)\s*=\s*\[(.*)\]", line)
        if not m or (extras and m.group(1) not in extras):
            continue
        for dist, floor in REQ.findall(m.group(2)):
            out[dist] = floor
    return out


def check(required, installed=None):
    installed = installed or _installed
    bad = []
    for dist, floor in sorted(required.items()):
        have = installed(dist)
        if have is None:
            bad.append(f"{dist}: not installed (need >={floor})")
        elif vtuple(have) < vtuple(floor):
            bad.append(f"{dist}: {have} installed, below the declared floor {floor}")
    return bad


def _installed(dist):
    try:
        return importlib.metadata.version(dist)
    except importlib.metadata.PackageNotFoundError:
        return None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--extras", help="comma-separated extras to check (default: all)")
    a = ap.parse_args(argv)
    with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as fh:
        required = floors(fh.read(), set(a.extras.split(",")) if a.extras else None)
    bad = check(required)
    if bad:
        print("installed dependencies below pyproject.toml floors:", *bad, sep="\n  ")
        print('fix: just install (pip install --require-hashes -r requirements.txt, then --no-deps -e .)')
        return 1
    print(f"dependency floors ok ({len(required)} packages)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
