## What and why

## Safety impact
<!-- Does this change what can be moved, trashed, read or undone? How is it tested? -->

## Checklist
- [ ] Tests pass: `python tests/test_fs_local.py && python tests/test_drive_mock.py && python tests/test_rules_to_plan.py`
- [ ] `python3 scripts/leak_check.py --all` is clean, and no real file names appear anywhere
- [ ] Docs updated if behavior changed
