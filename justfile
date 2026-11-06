# Verify targets (repo-state pattern). A phase is done only after its verify target passes and the
# output is pasted into STATE.md. Setup: python3 -m venv .venv && .venv/bin/pip install -e ".[all]"
# PY overrides the interpreter; the demo needs the gdrive-organizer entry point on PATH.

py := env_var_or_default("PY", if path_exists(".venv/bin/python") == "true" { ".venv/bin/python" } else { "python3" })
export PATH := if path_exists(".venv/bin") == "true" { justfile_directory() + "/.venv/bin:" + env_var("PATH") } else { env_var("PATH") }

default: check

# validate + lint + test: everything CI runs, plus the leak check
check: lint test demo leak

# byte-compile everything (no third-party linter: core has no dependencies)
lint:
    {{py}} -m compileall -q gdrive_organizer scripts tests examples

test:
    {{py}} tests/test_fs_local.py
    {{py}} tests/test_drive_mock.py
    {{py}} tests/test_rules_to_plan.py
    {{py}} tests/test_peek_limits.py
    {{py}} tests/test_units.py
    {{py}} tests/test_sandbox_settings.py
    {{py}} tests/test_state_guard.py

demo:
    DEMO_FAST=1 bash examples/demo/run.sh >/dev/null
    gdrive-organizer --help >/dev/null
    @echo "demo and CLI smoke ok"

# private patterns live in the main checkout only; a worktree without them still checks the identity
leak:
    python3 scripts/leak_check.py --all

# code changes carry a STATE.md change (CI runs this on every PR)
state-guard:
    python3 scripts/state_guard.py

# Phase 0: core pipeline (index, report, peek, validate, journaled apply and undo)
verify-phase-0:
    {{py}} tests/test_fs_local.py
    {{py}} tests/test_drive_mock.py

# Phase 1: rules generator, dedupe and empty-dirs trash ops, retry reconcile
verify-phase-1:
    {{py}} tests/test_rules_to_plan.py

# Phase 2: CLI subcommands and the fake-tree demo
verify-phase-2: demo

# Phase 3: assistant-boundary hardening (untrusted journal and index, parser limits, sandbox)
verify-phase-3:
    {{py}} tests/test_drive_mock.py
    {{py}} tests/test_peek_limits.py
    {{py}} tests/test_sandbox_settings.py

# Phase 4: plan merges or blocks collisions, never invents holding folders
verify-phase-4:
    {{py}} tests/test_rules_to_plan.py

# Phase 5: Dependabot autopilot (ADR-089): main is green, so n0's merger can act
verify-phase-5:
    test "$(gh run list --repo broots144/gdrive-organizer --branch main --workflow tests -L 1 --json conclusion --jq '.[0].conclusion')" = success
    @echo "main CI green"

# Phase 6: repo-state pattern (this file, STATE.md, the guard)
verify-phase-6: state-guard
    {{py}} tests/test_state_guard.py
    test -f STATE.md -a -f ROADMAP.md -a -f LESSONS.md -a -f docs/DECISIONS.md
