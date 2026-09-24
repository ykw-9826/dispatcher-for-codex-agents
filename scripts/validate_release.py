"""Build scanned candidates and test a fresh wheel; never publish or deploy."""

from __future__ import annotations

import argparse
import configparser
import email.parser
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath

from public_snapshot import build_snapshot


def project_version(root: Path, tag: str | None = None) -> str:
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    version = project["version"]
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("EXPECTED_STABLE_PACKAGE_VERSION")
    if tag is not None and tag != "v" + version:
        raise ValueError("TAG_PACKAGE_VERSION_MISMATCH")
    lock = tomllib.loads((root / "uv.lock").read_text())
    packages = [p for p in lock["package"] if p["name"] == project["name"]]
    if len(packages) != 1 or packages[0]["version"] != version:
        raise ValueError("LOCK_PACKAGE_VERSION_MISMATCH")
    return version


def validate_metadata(raw: bytes, version: str) -> None:
    msg = email.parser.BytesParser().parsebytes(raw)
    if msg["Name"] != "dispatcher-for-codex-agents" or msg["Version"] != version:
        raise ValueError("ARTIFACT_METADATA_MISMATCH")


def canonical_member(name: str) -> None:
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or path.as_posix() != name
        or ".." in path.parts
        or any(char in name for char in ("\\", "\0", ":"))
    ):
        raise ValueError("ARCHIVE_PATH_INVALID")


def validate_zip_members(archive: zipfile.ZipFile) -> list[str]:
    names = archive.namelist()
    if len(names) != len(set(names)):
        raise ValueError("DUPLICATE_ZIP_MEMBER")
    for member in archive.infolist():
        canonical_member(member.filename)
        # These generated artifacts contain regular files, never links/directories.
        if member.is_dir() or stat.S_IFMT(member.external_attr >> 16) not in (
            0,
            stat.S_IFREG,
        ):
            raise ValueError("ZIP_MEMBER_TYPE_INVALID")
    return names


def validate_packages(root: Path, output: Path, version: str) -> Path:
    wheels, sdists = list(output.glob("*.whl")), list(output.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError("EXPECTED_ONE_WHEEL_AND_SDIST")
    modules = {
        p.relative_to(root / "src").as_posix(): p.read_bytes()
        for p in (root / "src").rglob("*.py")
    }
    with zipfile.ZipFile(wheels[0]) as archive:
        names = validate_zip_members(archive)
        metadata = [n for n in names if n.endswith(".dist-info/METADATA")]
        if len(metadata) != 1:
            raise ValueError("WHEEL_MEMBERS_INVALID")
        validate_metadata(archive.read(metadata[0]), version)
        entries = configparser.ConfigParser()
        entries.read_string(
            archive.read(metadata[0].replace("METADATA", "entry_points.txt")).decode()
        )
        expected = {
            "dca": "dispatcher_for_codex_agents.agent_harness.cli:main",
            "dca-notify": "dispatcher_for_codex_agents.notifications.cli:main",
        }
        if dict(entries["console_scripts"]) != expected:
            raise ValueError("ENTRYPOINT_MISMATCH")
        actual = {n: archive.read(n) for n in names if n.endswith(".py")}
        if actual != modules:
            raise ValueError("WHEEL_SOURCE_MISMATCH")
        if any(
            not (n in modules or n.startswith(metadata[0].split("/")[0] + "/"))
            for n in names
        ):
            raise ValueError("UNEXPECTED_WHEEL_CONTENT")
    with tarfile.open(sdists[0]) as archive:
        prefix = f"dispatcher_for_codex_agents-{version}/"
        members = archive.getmembers()
        names = [m.name for m in members]
        if len(names) != len(set(names)):
            raise ValueError("DUPLICATE_SDIST_MEMBER")
        for member in members:
            canonical_member(member.name)
            parts = PurePosixPath(member.name).parts
            if member.name != prefix.rstrip("/") and not member.name.startswith(prefix):
                raise ValueError("SDIST_PATH_INVALID")
            if ".." in parts or not (member.isfile() or member.isdir()):
                raise ValueError("SDIST_MEMBER_INVALID")
            if set(parts) & {
                ".git",
                ".venv",
                "runtime",
                "reports",
                "releases",
                "exports",
                "__pycache__",
            }:
                raise ValueError("PRIVATE_SDIST_MEMBER")
        validate_metadata(archive.extractfile(prefix + "PKG-INFO").read(), version)
        actual = {
            m.name.removeprefix(prefix + "src/"): archive.extractfile(m).read()
            for m in members
            if m.isfile()
            and m.name.startswith(prefix + "src/")
            and m.name.endswith(".py")
        }
        if actual != modules:
            raise ValueError("SDIST_SOURCE_MISMATCH")
    return wheels[0]


def child_environment(root: Path, run: Path) -> dict[str, str]:
    # No credentials, arbitrary PYTHONPATH, host hooks or notification targets.
    allowed = (
        "PATH",
        "LANG",
        "LC_ALL",
        "SYSTEMROOT",
        "UV_CACHE_DIR",
        "UV_PYTHON_INSTALL_DIR",
        "UV_OFFLINE",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    )
    env = {name: os.environ[name] for name in allowed if name in os.environ}
    env.update(
        PYTHONDONTWRITEBYTECODE="1",
        UV_PYTHON_DOWNLOADS="never",
        UV_NO_ENV_FILE="1",
        TMPDIR=str(run),
        TMP=str(run),
        TEMP=str(run),
        DCA_WORKSPACE_CONFIG=str(root / "configs/workspace.json"),
        DCA_NOTIFY_CONFIG=str(root / "configs/notifications.json"),
    )
    return env


def validate(root: Path, output: Path, *, uv: str, tag: str | None = None) -> dict:
    version = project_version(root, tag)
    output = output.absolute()
    if output.resolve() != output or not output.is_relative_to(root / "exports"):
        raise ValueError("OUTPUT_MUST_BE_UNDER_EXPORTS")
    if output.exists():
        raise ValueError("OUTPUT_ALREADY_EXISTS")
    os.umask(0o077)
    lock_before = (root / "uv.lock").read_bytes()
    output.mkdir(parents=True, mode=0o700)
    run = Path(tempfile.mkdtemp(prefix="release-validation-", dir=root / "runtime/tmp"))
    env = child_environment(root, run)

    def call(label: str, args: list[str | Path], *, current_env=env) -> str:
        result = subprocess.run(
            [str(a) for a in args],
            cwd=run,
            env=current_env,
            capture_output=True,
            text=True,
            timeout=600,
        )
        (run / (label + ".log")).write_text(result.stdout + result.stderr)
        if result.returncode:
            raise ValueError(f"{label}_FAILED: see {run / (label + '.log')}")
        return result.stdout

    source_zip = output / f"DCA_v{version}_source.zip"
    snapshot = build_snapshot(root, source_zip)
    source = run / "source"
    source.mkdir()
    with zipfile.ZipFile(source_zip) as archive:
        validate_zip_members(archive)
        archive.extractall(source)  # Newly generated, scanned whitelist; no links.
    constraints = run / "build-constraints.txt"
    lock = tomllib.loads(lock_before.decode())
    constraints.write_text(
        "\n".join(
            f"{p['name']}=={p['version']}"
            for p in lock["package"]
            if "registry" in p["source"]
        )
        + "\n"
    )
    call(
        "build",
        [
            uv,
            "build",
            source,
            "--python",
            sys.executable,
            "--no-python-downloads",
            "--no-create-gitignore",
            "--build-constraints",
            constraints,
            "--out-dir",
            output,
        ],
    )
    wheel = validate_packages(source, output, version)
    venv = run / "installed-venv"
    call(
        "venv", [uv, "venv", "--python", sys.executable, "--no-python-downloads", venv]
    )
    install_env = {**env, "UV_PROJECT_ENVIRONMENT": str(venv)}
    call(
        "dependencies",
        [
            uv,
            "sync",
            "--frozen",
            "--no-dev",
            "--no-install-project",
            "--project",
            source,
        ],
        current_env=install_env,
    )
    python = venv / "bin/python"
    call(
        "install-wheel", [uv, "pip", "install", "--no-deps", "--python", python, wheel]
    )
    call("dependency-check", [uv, "pip", "check", "--python", python])
    for tool in ("dca", "dca-notify"):
        if (
            call(tool + "-version", [venv / "bin" / tool, "--version"]).strip()
            != f"{tool} {version}"
        ):
            raise ValueError("CLI_VERSION_MISMATCH")
        call(tool + "-help", [venv / "bin" / tool, "--help"])
    call("migration-help", [venv / "bin/dca-notify", "hooks-migrate", "--help"])
    smoke = json.loads(
        call(
            "installed-smoke",
            [
                python,
                "-I",
                "-B",
                root / "scripts/installed_release_smoke.py",
                "--wheel",
                wheel,
                "--version",
                version,
                "--output-root",
                run / "smoke",
            ],
        )
    )
    if (root / "uv.lock").read_bytes() != lock_before or (
        source / "uv.lock"
    ).read_bytes() != lock_before:
        raise ValueError("LOCKFILE_CHANGED")
    assets = sorted([source_zip, wheel, *output.glob("*.tar.gz")])
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in assets}
    (output / "SHA256SUMS").write_text(
        "".join(f"{digest}  {name}\n" for name, digest in hashes.items())
    )
    result = {
        "status": "PASS",
        "version": version,
        "tag": tag,
        "source_members": len(snapshot["members"]),
        "hashes": hashes,
        "installed_smoke": smoke,
        "logs": str(run),
        "publishing": "NOT_PERFORMED",
    }
    (run / "validation.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--tag", help="Exact tag to compare with package version; data only"
    )
    parser.add_argument("--uv", default=shutil.which("uv"))
    args = parser.parse_args()
    if not args.uv:
        parser.error("uv executable required")
    try:
        result = validate(
            Path(__file__).resolve().parents[1], args.output, uv=args.uv, tag=args.tag
        )
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        parser.exit(2, f"Candidate validation refused: {exc}\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
