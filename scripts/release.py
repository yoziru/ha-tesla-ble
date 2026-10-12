"""Bump the manifest and publish a release; run from GitHub Actions."""

import json
import os
import re
import subprocess
from pathlib import Path

MANIFEST = Path("custom_components/tesla_ble/manifest.json")


def next_version(current: str, bump: str, override: str = "") -> str:
    def parse(version: str) -> tuple[int, ...]:
        if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version):
            raise ValueError("Version must be X.Y.Z without leading zeros")
        return tuple(map(int, version.split(".")))

    old = parse(current)
    if override:
        new = parse(override)
    else:
        index = ("major", "minor", "patch").index(bump)
        new = (*old[:index], old[index] + 1, *((0,) * (2 - index)))
    if new <= old:
        raise ValueError("Release version must increase")
    return ".".join(map(str, new))


def run(*args: str) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def main() -> None:
    if os.environ.get("GITHUB_REF_NAME") != "main" or not os.environ.get("GH_TOKEN"):
        raise ValueError("Run the Release workflow from main with RELEASE_TOKEN configured")
    if run("git", "status", "--porcelain"):
        raise ValueError("Working tree must be clean")
    run_id = os.environ["GITHUB_RUN_ID"]
    if not run_id.isascii() or not run_id.isdecimal():
        raise ValueError("Invalid workflow run ID")
    marker = f"Release-Run: {run_id}"
    sha = run("git", "log", "origin/main", "--format=%H", "--max-count=1", f"--grep=^{marker}$")
    if sha:
        version = json.loads(run("git", "show", f"{sha}:{MANIFEST}"))["version"]
        tag = f"v{version}"
        if run("git", "rev-parse", f"{tag}^{{commit}}") != sha:
            raise ValueError("Release tag does not match the version commit")
    else:
        manifest = json.loads(MANIFEST.read_text())
        version = next_version(
            manifest["version"], os.environ.get("INPUT_BUMP", "patch"), os.environ.get("INPUT_VERSION", "")
        )
        tag = f"v{version}"
        if run("git", "tag", "--list", tag):
            raise ValueError(f"Tag {tag} already exists")
        run("git", "config", "user.name", "github-actions[bot]")
        run("git", "config", "user.email", "github-actions[bot]@users.noreply.github.com")
        manifest["version"] = version
        MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")
        run("git", "add", str(MANIFEST))
        run("git", "commit", "-m", f"chore(release): {tag}", "-m", marker)
        sha = run("git", "rev-parse", "HEAD")
        run("git", "tag", tag, sha)
        run("git", "push", "--atomic", "origin", "HEAD:refs/heads/main", f"refs/tags/{tag}")
    try:
        url = run("gh", "release", "view", tag, "--json", "url", "--jq", ".url")
    except subprocess.CalledProcessError as error:
        if "release not found" not in (error.stderr or ""):
            raise
        url = run(
            "gh",
            "release",
            "create",
            tag,
            "--verify-tag",
            "--title",
            f"Tesla BLE {version}",
            "--generate-notes",
            "--latest",
        )
    print(url)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        raise SystemExit(error.stderr or error.stdout) from error
