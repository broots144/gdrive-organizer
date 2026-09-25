# Notes: bulk operations on a Google Drive for desktop mount (macOS)

Why this tool uses the Drive API instead of walking `~/Library/CloudStorage/GoogleDrive-*/My Drive`.
Labels: **fact** (a primary source says so), **estimate** (strong secondary evidence), **guess**.
Checked September 2026.

## Hydration and eviction

- **fact** `fileproviderctl evict` and `materialize` were removed in macOS 14.4. Apple answered
  FB13580772 with "won't fix". Any plan of "read a little, then evict" has no supported
  command-line cleanup on current macOS.
  ([i2h3](https://i2h3.de/fileproviderctl-changes-macos-14.4/),
  [Apple forums 748036](https://developer.apple.com/forums/thread/748036))
- **fact** Google's File Provider comparison: "In many settings, you can't stream files and you
  need to fully download files before you can examine them."
  ([Google](https://support.google.com/drive/answer/12178485))
- **estimate** Reading the first bytes of a cloud-only file downloads the whole file. Apple has a
  partial-content API (macOS 12.3+), but Google does not document supporting it.
- **fact** PDF readers such as pypdf read the cross-reference table at the end of the file, so
  "page 1 only" still needs the whole PDF.
- **fact** `stat()` does not download file contents, but it does download the listing of any
  dataless folder in the path; listing a folder for the first time is a network round trip.
  Detect cloud-only files with `st_flags & SF_DATALESS` (0x40000000).
  ([Apple TN3150](https://developer.apple.com/documentation/technotes/tn3150-getting-ready-for-data-less-files))
- **fact** `setiopolicy_np(IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES, scope, OFF)` makes access to a
  dataless file fail (TN3150 says with EDEADLK) instead of downloading it. `core.no_materialize()`
  wraps this.

## Shortcuts, duplicates, pointers

- **fact** Drive shortcuts appear locally as an alias or symlink, with the target placed under a
  hidden `shortcut-targets-by-id` folder. A name-based exclusion can be bypassed through that
  second path. ([Google](https://support.google.com/drive/answer/10864219))
- **fact** Google Docs, Sheets and Slides appear as small `.gdoc`/`.gsheet`/`.gslides` pointer
  files. Copying them does not work. Moving them within the mount is reported to work (estimate).
- **estimate** Drive allows duplicate names in one folder; the local mount disambiguates them with
  " (1)" and which file gets the suffix is not predictable.
- **fact** `rename(2)` silently replaces an existing destination. Use `renamex_np(RENAME_EXCL)`.

## Moves and sharing

- **fact** Without permission to move an item, Drive creates a shortcut in the destination instead.
  Moving an item out of a shared folder removes access that others had through that folder.
  ([Google](https://support.google.com/drive/answer/2375091))
- **fact** Failed syncs can land in Drive for desktop's Lost & Found.
  ([Google](https://support.google.com/drive/answer/2565956))

## Drive API

- **fact** Quotas since May 1, 2026 (new projects): 325,000 units per user per minute;
  `files.list` = 100 units, `files.update` = 50, `files.get` = 5. Max `pageSize` 1000.
  ([Google](https://developers.google.com/workspace/drive/api/guides/limits))
- **fact** `files.list` can return `md5Checksum`, `ownedByMe`, `shortcutDetails`,
  `viewedByMeTime` and `capabilities` without downloading content.
- **fact** `rclone lsjson -R` on Drive uses ListR, which lists every folder and filters afterwards;
  directory excludes only prune traversal with `--disable ListR`.
  ([rclone filtering](https://rclone.org/filtering/))

## Privacy prompts

- **fact** macOS asks before an app first reads files managed by a file provider. A denial shows
  up as EPERM, which a careless walker treats as "empty folder". This tool aborts instead.

## Open questions (please report what you see)

- Does `com.google.drivefs.item-id#S` equal the Drive file ID?
- Does renaming a locally disambiguated " (1)" name rename the cloud file?
- Is Claude Code's Seatbelt path matching case-sensitive on case-insensitive APFS?
