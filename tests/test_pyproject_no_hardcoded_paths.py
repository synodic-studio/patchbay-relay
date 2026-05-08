"""Guard: pyproject.toml must not contain absolute paths from a developer's home dir."""

import re
from pathlib import Path


def test_pyproject_has_no_absolute_user_paths():
    pyproject = (Path(__file__).parent.parent / "pyproject.toml").read_text()
    # Match /Users/<name>/ or /home/<name>/ — anything user-specific
    matches = re.findall(r"(/Users/[^/]+/|/home/[^/]+/)", pyproject)
    assert not matches, (
        f"pyproject.toml contains hardcoded user paths: {matches}. "
        "Use relative paths (e.g. ../foo) or env-driven sources instead."
    )
