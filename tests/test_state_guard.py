"""scripts/state_guard.py: code changes need STATE.md unless the title is chore:/deps: upkeep."""
import os
import sys

sys.path.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
from state_guard import needs_state  # noqa: E402

assert needs_state(["gdrive_organizer/plan.py"], "plan: fix") == ["gdrive_organizer/plan.py"]
assert needs_state(["gdrive_organizer/plan.py", "STATE.md"], "plan: fix") == []
assert needs_state(["pyproject.toml"], "feat: x") == ["pyproject.toml"]
assert needs_state(["scripts/leak_check.py"], "fix: y") == ["scripts/leak_check.py"]
assert needs_state(["README.md", "docs/usage.md", "tests/test_fs_local.py"], "docs: z") == []
assert needs_state(["pyproject.toml"], "chore(deps): update pypdf requirement") == []
assert needs_state(["gdrive_organizer/core.py"], "chore: tidy") == []
assert needs_state(["pyproject.toml"], "deps: bump") == []
assert needs_state(["pyproject.toml"], "Bump actions/checkout from 4 to 7") == []
assert needs_state(["gdrive_organizer/core.py"], "chores are not an escape") != []
print("ALL STATE GUARD TESTS PASSED")
