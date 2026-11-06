# Roadmap

At a glance. Status detail lives in [STATE.md](STATE.md); decisions in [docs/DECISIONS.md](docs/DECISIONS.md).

| Phase | Title | Status | Gate / blocker |
|---|---|---|---|
| 0 | Core pipeline: index, report, peek, validate, journaled apply and undo (0.1.0) | ✅ | `just verify-phase-0` |
| 1 | Rules generator, dedupe and empty-dirs trash ops, retry reconcile (0.2.0) | ✅ | `just verify-phase-1` |
| 2 | CLI subcommands and fake-tree demo (0.3.0) | ✅ | `just verify-phase-2` |
| 3 | Assistant-boundary hardening (0.3.1 to 0.3.4) | ✅ | `just verify-phase-3` |
| 4 | Collisions merge or block, no holding folders (#20) | ✅ | `just verify-phase-4` |
| 5 | Dependabot autopilot (ADR-089) | ✅ | `just verify-phase-5` |
| 6 | Repo-state pattern and STATE.md guard | 🟡 | merge with `state-guard` green |
| 7 | Release 0.3.5 | ⬜ | Phase 6 merged |
| - | Shared drives and "Shared with me" | ⏸ | no demand yet |
| - | Duplicate detection for Google Docs, Sheets, Slides (no checksum) | ⏸ | needs a content fingerprint design |
