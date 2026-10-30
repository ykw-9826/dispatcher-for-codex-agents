"""Parent-only build oracle: run inside the existing validator sandbox.

The repository mount is read-only. Build a scratch copy under the sandbox's
private /tmp; never grant generated build code a host write path or credentials.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


def validate(repo: Path) -> None:
    expected = {
        p.relative_to(repo).as_posix(): p.read_bytes()
        for p in sorted((repo / "src").glob("*.py"))
    }
    assert expected, "BUILD_NO_SOURCES"
    # Caller selects private temporary storage; production parent runs this
    # only inside run_validators' credential-free, network-free sandbox.
    with tempfile.TemporaryDirectory(prefix="build-oracle-") as temporary:
        scratch = Path(temporary)
        shutil.copytree(repo / "src", scratch / "src")
        shutil.copyfile(repo / "build.py", scratch / "build.py")
        process = subprocess.run(
            [sys.executable, "-B", str(scratch / "build.py")],
            cwd=scratch,
            env={
                "PATH": "/usr/bin:/bin",
                "LANG": "C.UTF-8",
                "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=15,
        )
        assert process.returncode == 0, "BUILD_PROCESS_FAILED"
        with zipfile.ZipFile(scratch / "build/labels.zip") as archive:
            names = archive.namelist()
            assert len(names) == len(set(names)), "BUILD_DUPLICATE_MEMBER"
            assert {n: archive.read(n) for n in names} == expected, "BUILD_BYTES"
    print("BUILD_ORACLE_PASS")


if __name__ == "__main__":
    validate(Path(sys.argv[1]))
