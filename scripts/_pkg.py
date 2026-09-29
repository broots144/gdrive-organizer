"""Import the in-repo gdrive_organizer package without putting the repo root on sys.path.

A sandboxed assistant cannot write the package, tests, scripts or root-level *.py files, but it
can create a new root-level directory (./ctypes/, ./sqlite3/, ...). With the repo root on sys.path
ahead of the standard library, such a directory would shadow a stdlib module and run when the
owner runs the tests or scripts outside the sandbox. So the package is loaded by file path and
nothing is added to sys.path.
"""
import importlib.util
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(REPO, "gdrive_organizer")


def load():
    mod = sys.modules.get("gdrive_organizer")
    if mod is not None and os.path.dirname(os.path.abspath(mod.__file__)) == PKG:
        return mod
    spec = importlib.util.spec_from_file_location(
        "gdrive_organizer", os.path.join(PKG, "__init__.py"), submodule_search_locations=[PKG])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gdrive_organizer"] = mod
    spec.loader.exec_module(mod)
    return mod


load()
