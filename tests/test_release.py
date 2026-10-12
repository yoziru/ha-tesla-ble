"""Release version inputs must be valid and strictly increasing."""

import json
import os
import subprocess

import pytest

from scripts import release
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


def test_release_publish_failure_resumes_same_version(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)

    def git(*args, cwd=None):
        return subprocess.run(("git", *args), cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

    remote = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    git("init", "--bare", "--initial-branch=main", str(remote))
    git("init", "--initial-branch=main", str(seed))
    manifest = seed / release.MANIFEST
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"domain": "tesla_ble", "version": "1.0.1"}))
    git("config", "user.name", "Test", cwd=seed)
    git("config", "user.email", "test@example.test", cwd=seed)
    git("add", ".", cwd=seed)
    git("commit", "-m", "initial", cwd=seed)
    git("push", str(remote), "main", cwd=seed)
    for name, value in {
        "GITHUB_REF_NAME": "main",
        "GH_TOKEN": "test",
        "GITHUB_RUN_ID": "42",
        "INPUT_BUMP": "patch",
        "INPUT_VERSION": "",
    }.items():
        monkeypatch.setenv(name, value)
    real_run = release.run
    releases = []
    publish_attempts = 0

    def github(*args):
        nonlocal publish_attempts
        if args[0] != "gh":
            return real_run(*args)
        command = args[1:3]
        if command == ("release", "view"):
            if releases:
                return "https://example.test/releases/v1.0.2"
            raise subprocess.CalledProcessError(1, args, stderr="release not found")
        if command == ("release", "create"):
            publish_attempts += 1
            if publish_attempts == 1:
                raise subprocess.CalledProcessError(1, args, stderr="test: publish failed")
            tag = args[3]
            assert real_run("git", "ls-remote", "origin", f"refs/tags/{tag}")
            releases.append(tag)
            return "https://example.test/releases/v1.0.2"
        raise AssertionError(f"Unexpected GitHub operation: {args}")

    monkeypatch.setattr(release, "run", github)
    for attempt in range(3):
        checkout = tmp_path / f"checkout-{attempt}"
        git("clone", str(remote), str(checkout))
        monkeypatch.chdir(checkout)
        if attempt == 0:
            with pytest.raises(subprocess.CalledProcessError) as error:
                release.main()
            assert error.value.stderr == "test: publish failed"
            assert git("ls-remote", "origin", "refs/tags/v1.0.2")
        else:
            release.main()
            tagged = json.loads(git("show", f"v1.0.2:{release.MANIFEST}"))
            assert tagged == {"domain": "tesla_ble", "version": "1.0.2"}
            assert git("rev-parse", "v1.0.2") == git("rev-parse", "origin/main")
            assert releases == ["v1.0.2"]
