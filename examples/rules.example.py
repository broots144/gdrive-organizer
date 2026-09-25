"""Example rules for scripts/rules_to_plan.py. Copy to private/rules.py and edit.

  python3 scripts/rules_to_plan.py --db private/index.sqlite --config private/config.json \
      --rules private/rules.py --out private/plan.jsonl
  gdrive-organizer validate --db private/index.sqlite --config private/config.json \
      --manifest private/plan.jsonl

Each rule is a SQL WHERE clause over the `items` table (alias i) plus a destination template.
First match wins. Useful columns: path, pathkey (NFC + casefolded), name, parent_key, depth
(0 = top level), kind (dir, file, gdoc, bundle, shortcut, dot), ext, size, mtime, md5,
owned_by_me, sensitive. REGEXP is available. Template fields:
  {lh}    name lowercase-hyphenated for folders ("Tax Returns" -> "tax-returns"); files unchanged
  {orig}  original path with folder components lowercase-hyphenated, file name unchanged

The generator never deletes or renames files. It refuses protected ground, dot entries, items
you do not own, items inside atomic units and anything modified within recent_days, and adds
override ["sensitive"] to sensitive-named sources so each one is visible in the manifest.
"""

# Optional: a dump folder to empty completely. Every direct child needs a rule; end with a
# catch-all so nothing is left behind.
DUMP = "old dropbox"
DUMP_CHILD = (f"i.parent_key = (SELECT key FROM items WHERE depth=0 AND kind='dir' "
              f"AND pathkey='{DUMP}')")

# Optional settings (defaults shown).
# DUMP_NEST = "from-old-dropbox"          # where a colliding dump child lands inside its target
# DUPLICATES_DIR = "archive/duplicates"
# UNSORTED_DIR = "archive/unsorted"
LIVE_TOPS = ["personal", "finance", "work", "media"]  # only affects the summary split


def _in(*names):
    return ",".join("'" + n.replace("'", "''").casefold() + "'" for n in names)


def top(*names):
    """Top-level folders by exact name (case-insensitive)."""
    return f"(i.kind='dir' AND i.depth=0 AND i.pathkey IN ({_in(*names)}))"


def dump(*names):
    """Direct child folders of DUMP by exact name (case-insensitive)."""
    return f"({DUMP_CHILD} AND i.kind='dir' AND lower(i.name) IN ({_in(*names)}))"


RULES = [
    # Whole machine backups wherever they are; the outermost match wins.
    dict(id="backups",
         where=r"i.kind IN ('dir','bundle') AND i.name REGEXP "
               r"'(?i)(macbook|time.?machine|^takeout$|\.sparsebundle$|\.backupdb$)'",
         dst="archive/backups/{lh}"),
    dict(id="old-employers",
         where=f"{DUMP_CHILD} AND i.kind='dir' AND i.name REGEXP '(?i)(initech|globex)'",
         dst="archive/work/{lh}"),
    # Loose dump files that duplicate a file kept outside the dump.
    dict(id="duplicates",
         where=f"{DUMP_CHILD} AND i.kind IN ('file','gdoc') AND i.md5 IN (SELECT md5 FROM items "
               f"WHERE md5 IS NOT NULL AND substr(pathkey,1,{len(DUMP) + 1})<>'{DUMP}/')",
         dst="archive/duplicates/{orig}"),
    dict(id="finance", where=f"{top('taxes', 'receipts')} OR {dump('taxes')}",
         dst="finance/{lh}"),
    dict(id="personal", where=f"{top('family photos', 'journal')} OR {dump('school')}",
         dst="personal/{lh}"),
    dict(id="unsorted", where=DUMP_CHILD, dst="archive/unsorted/{orig}"),
]
