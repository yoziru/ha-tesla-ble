"""Release version inputs must be valid and strictly increasing."""

import pytest

from scripts.release import next_version


@pytest.mark.parametrize(
    ("bump", "override", "expected"),
    [("patch", "", "1.2.4"), ("minor", "", "1.3.0"), ("major", "", "2.0.0"), ("patch", "2.3.4", "2.3.4")],
)
def test_release_version(bump, override, expected):
    assert next_version("1.2.3", bump, override) == expected


@pytest.mark.parametrize("override", ["1.2.3", "1.2.2", "1.2", "01.3.0", "1.3.0-beta", "1.3.0\n", "1.3.0; echo unsafe"])
def test_invalid_release_version(override):
    with pytest.raises(ValueError):
        next_version("1.2.3", "patch", override)
