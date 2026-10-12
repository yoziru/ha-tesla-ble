"""Release through a validated version-only PR; run from GitHub Actions."""

import json
import os
import re
import subprocess
import time
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


def wait_for_validation(sha: str) -> None:
    for _ in range(60):
        runs = json.loads(
            run(
                "gh",
                "run",
                "list",
                "--workflow",
                "validate.yml",
                "--commit",
                sha,
                "--event",
                "push",
                "--limit",
                "1",
                "--json",
                "databaseId",
            )
        )
        if runs:
            run("gh", "run", "watch", str(runs[0]["databaseId"]), "--exit-status", "--interval", "10")
            return
        time.sleep(5)
    raise RuntimeError("Validation did not start; check RELEASE_TOKEN Actions permissions")


def main() -> None:
    if os.environ.get("GITHUB_REF_NAME") != "main" or not os.environ.get("GH_TOKEN"):
        raise ValueError("Run the Release workflow from main with RELEASE_TOKEN configured")
    if run("git", "status", "--porcelain"):
        raise ValueError("Working tree must be clean")
    manifest = json.loads(MANIFEST.read_text())
    version = next_version(
        manifest["version"], os.environ.get("INPUT_BUMP", "patch"), os.environ.get("INPUT_VERSION", "")
    )
    tag = f"v{version}"
    if run("git", "tag", "--list", tag):
        raise ValueError(f"Tag {tag} already exists")
    branch = f"release/{version}"
    run("git", "config", "user.name", "github-actions[bot]")
    run("git", "config", "user.email", "github-actions[bot]@users.noreply.github.com")
    run("git", "switch", "-c", branch)
    manifest["version"] = version
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")
    run("git", "add", str(MANIFEST))
    run("git", "commit", "-m", f"release: {tag}")
    run("git", "push", "-u", "origin", branch)
    pr = run(
        "gh",
        "pr",
        "create",
        "--base",
        "main",
        "--head",
        branch,
        "--title",
        f"release: {tag}",
        "--body",
        "Version-only bump. The Release workflow publishes after validation and squash merge.",
    )
    print(f"Release PR: {pr}", flush=True)
    wait_for_validation(run("git", "rev-parse", "HEAD"))
    run("gh", "pr", "checks", pr, "--watch", "--interval", "10")
    run("gh", "pr", "merge", pr, "--squash")
    sha = run("gh", "pr", "view", pr, "--json", "mergeCommit", "--jq", ".mergeCommit.oid")
    wait_for_validation(sha)
    run("git", "fetch", "origin", "main")
    released_manifest = json.loads(run("git", "show", f"{sha}:{MANIFEST}"))
    if released_manifest["version"] != version:
        raise ValueError("Merged version does not match the requested release")
    print(
        run(
            "gh",
            "release",
            "create",
            tag,
            "--target",
            sha,
            "--title",
            f"Tesla BLE {version}",
            "--generate-notes",
            "--latest",
        )
    )


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        raise SystemExit(error.stderr or error.stdout) from error
