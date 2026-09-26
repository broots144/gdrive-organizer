"""Demo taxonomy: finance, family, projects and an archive. First match wins."""

DUMP = "old dropbox"
DUMP_CHILD = ("i.parent_key = (SELECT key FROM items WHERE depth=0 AND kind='dir' "
              "AND pathkey='old dropbox')")
LIVE_TOPS = ["finance", "family", "projects"]
ROOT_FILE = "i.depth=0 AND i.kind='file'"

RULES = [
    dict(id="finance", where="(i.depth=0 AND i.kind='dir' AND i.pathkey='taxes') OR "
         f"({DUMP_CHILD} AND i.name='taxes') OR ({ROOT_FILE} AND i.name REGEXP '(?i)receipt|tax')",
         dst="finance/{lh}"),
    dict(id="family", where=f"({DUMP_CHILD} AND i.name IN ('Photos 2014','scouts')) OR "
         f"({ROOT_FILE} AND i.name REGEXP '(?i)soccer|^IMG_')", dst="family/{lh}"),
    dict(id="projects", where="i.depth=0 AND i.kind='dir' AND i.pathkey='homelab-ansible'",
         dst="projects/{lh}"),
    dict(id="old-work", where=f"{DUMP_CHILD} AND i.name REGEXP '(?i)initech'",
         dst="archive/work/{lh}"),
    dict(id="software", where=f"{DUMP_CHILD} AND i.name='apps'", dst="archive/software/{lh}"),
    dict(id="unsorted", where=f"{DUMP_CHILD} OR {ROOT_FILE}", dst="archive/unsorted/{orig}"),
]
