# Privacy policy

gdrive-organizer is an open-source command-line tool that you run on your own computer against
your own Google Drive, using an OAuth client that you create in your own Google Cloud project.

- There is no hosted service. Nothing you do with the tool is sent to the author or to any
  third party.
- The tool reads Google Drive metadata (and, only when you run the optional `peek` command,
  short content snippets) and writes them to a local SQLite index in the `private/` folder on
  your machine. It never uploads that data anywhere.
- OAuth tokens are stored only on your machine, in files you control, and can be revoked at
  any time from your Google Account's third-party access page.
- Changes to your Drive happen only when you run `apply --execute` yourself, and every change
  is journaled locally so it can be undone.

Questions: open an issue at https://github.com/broots144/gdrive-organizer/issues.
