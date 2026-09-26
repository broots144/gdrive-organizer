"""Example policy for `gdrive-organizer dedupe` and `empty-dirs`. Copy to private/dedupe.py and edit.

Only byte-identical files (same md5 and size) are considered. One copy per group always stays.
Everything trashed goes to Drive's trash (30 days) and the journal can un-trash it.
"""

# Keep the copy in these places first, in this order (path prefixes).
KEEP_ORDER = ["personal", "finance", "work", "media"]

# Keep a copy here only if no other copy exists.
LAST = ["archive/unsorted", "archive/duplicates"]

# Never trash anything under these prefixes: whole units (software, backups, code trees) and
# folders an app writes to (scanner or browser landing folders).
NEVER_TRASH = ["archive/software", "archive/backups", "archive/work", "Scans"]

# empty-dirs: folders to keep even when empty (your top-level taxonomy)
KEEP_EMPTY = ["personal", "finance", "work", "media", "archive"]
