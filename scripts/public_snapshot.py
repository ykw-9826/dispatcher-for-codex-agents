"""Build a local source-only ZIP from an explicit whitelist; never publish Git."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import zipfile
from pathlib import Path
from urllib.parse import urlparse

ROOT_FILES = {
    ".gitignore",
    ".python-version",
    "AGENTS.md",
    "README.md",
    "ABOUT.md",
    "LICENSE",
    "CONTRIBUTING.md",
    "pyproject.toml",
    "uv.lock",
}
SOURCE_DIRS = {"src", "tests", "docs", "examples", "scripts"}
PUBLIC_HOSTS = {
    "pypi.org",
    "files.pythonhosted.org",
    "learn.chatgpt.com",
    "developers.openai.com",
    "sctapi.ftqq.com",
}
PUBLIC_REPOSITORY_URLS = {
    "https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/core/src/agent/role.rs",
}


def inspect_content(name: str, content: bytes) -> list[str]:
    text = content.decode("utf-8")
    findings = []
    patterns = {
        "PRIVATE_ABSOLUTE_PATH": r"/(?:home|data/projects|data/miniconda)[^\s\"']*",
        "PRIVATE_NETWORK": (
            r"\b(?:10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|"
            r"172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+)\b"
        ),
        "SESSION_IDENTIFIER": (
            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-" r"[0-9a-f]{4}-[0-9a-f]{12}\b"
        ),
        "PRIVATE_KEY": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        "API_KEY": r"\bsk-[A-Za-z0-9_-]{20,}\b",
        "BEARER_VALUE": r"(?i)Authorization\s*:\s*Bearer\s+[A-Za-z0-9_-]{12,}",
    }
    for label, pattern in patterns.items():
        if re.search(pattern, text):
            findings.append(label)
    for match in re.findall(r"\bSCT[A-Za-z0-9]{8,256}\b", text):
        if not (
            name.startswith("tests/") and re.fullmatch("SCT" r"FAKESECRET\d*", match)
        ):
            findings.append("SERVERCHAN_KEY")
    for url in re.findall(r'https?://[^\s<>"\x27)]+', text):
        parsed = urlparse(url)
        if parsed.username or parsed.password:
            findings.append("URL_CREDENTIALS")
        if (
            parsed.hostname not in PUBLIC_HOSTS
            and not (parsed.hostname or "").endswith(".invalid")
            and url not in PUBLIC_REPOSITORY_URLS
        ):
            findings.append("UNREVIEWED_URL_HOST")
    return sorted(set(findings))


def source_files(root: Path) -> dict[str, Path]:
    names = (root / "configs/public_files.txt").read_text(encoding="utf-8").splitlines()
    names = [name for name in names if name and not name.startswith("#")]
    if len(names) != len(set(names)):
        raise ValueError("DUPLICATE_WHITELIST_MEMBER")
    result = {}
    for name in names:
        relative = Path(name)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or relative.as_posix() != name
        ):
            raise ValueError("INVALID_WHITELIST_PATH")
        allowed = name in ROOT_FILES or relative.parts[0] in SOURCE_DIRS
        allowed |= name == "configs/public_files.txt"
        allowed |= relative.parts[0] == "configs" and name.endswith(".example.json")
        if not allowed or any(
            part
            in {
                ".git",
                ".venv",
                "__pycache__",
                "runtime",
                "reports",
                "runs",
                "releases",
                "exports",
            }
            for part in relative.parts
        ):
            raise ValueError("PRIVATE_MEMBER_FORBIDDEN")
        path = root / relative
        if (
            path.resolve() != path
            or not path.is_file()
            or path.stat().st_size > 2_000_000
        ):
            raise ValueError("REGULAR_BOUNDED_SOURCE_REQUIRED")
        findings = inspect_content(name, path.read_bytes())
        if findings:
            raise ValueError(f"PUBLIC_SCAN_FAILED:{name}:{','.join(findings)}")
        result[name] = path
    return result


def build_snapshot(root: Path, output: Path) -> dict:
    root = root.resolve()
    output = output.absolute()
    if output.resolve() != output or not output.is_relative_to(root / "exports"):
        raise ValueError("OUTPUT_MUST_BE_UNDER_CHECKOUT_EXPORTS")
    if output.exists():
        raise ValueError("PUBLIC_SNAPSHOT_ALREADY_EXISTS")
    selected = source_files(root)
    data = {name: path.read_bytes() for name, path in selected.items()}
    records = [
        {
            "path": name,
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        for name, content in sorted(data.items())
    ]
    data["SHA256SUMS"] = "".join(
        f"{r['sha256']}  {r['path']}\n" for r in records
    ).encode()
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with zipfile.ZipFile(
        output, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for name, content in sorted(data.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            executable = name in selected and bool(
                selected[name].stat().st_mode & 0o111
            )
            info.external_attr = (stat.S_IFREG | (0o755 if executable else 0o644)) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)
    output.chmod(0o600)
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None or sorted(archive.namelist()) != sorted(data):
            raise ValueError("ZIP_VALIDATION_FAILED")
        if any(archive.read(name) != content for name, content in data.items()):
            raise ValueError("ZIP_MEMBER_HASH_MISMATCH")
    return {
        "status": "SOURCE_SNAPSHOT_PREPARED",
        "publishing": "NOT_PERFORMED",
        "license": "MIT",
        "members": records,
        "archive_members": len(data),
        "size": output.stat().st_size,
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "git_history_included": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    try:
        result = build_snapshot(root, args.output)
    except (ValueError, OSError, UnicodeError) as exc:
        parser.exit(2, f"Snapshot refused: {exc}\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
