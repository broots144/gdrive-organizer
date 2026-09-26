# Security

This tool moves, renames and trashes files in real Google Drives. A bug in the exclusion guard,
the validator or the executor is a security issue, not only a correctness issue. The intended
guarantees are listed in [docs/safety-model.md](docs/safety-model.md).

## Reporting

Report suspected problems privately through GitHub's **"Report a vulnerability"** (Security tab >
Advisories), not a public issue. Include the smallest manifest or synthetic tree that reproduces
it, with every real name replaced.

Do not attach real indexes, reports, quarantine files, manifests or journals: they contain your
file and folder names.

## Supported versions

Only the latest release on `main` receives fixes while the project is in alpha.

## Your own setup

- Keep `private/` out of version control (it is gitignored) and install the leak-check hook.
- Your OAuth client and tokens stay on your machine. Revoke access at
  <https://myaccount.google.com/permissions> when you are done.
