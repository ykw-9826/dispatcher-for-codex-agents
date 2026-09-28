"""Candidate helper and workflow boundaries; synthetic inputs only."""

import importlib
import io
import re
import stat
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def helper(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    return importlib.import_module("validate_release")


@pytest.mark.parametrize("tag", [None, "v1.0.3"])
def test_version_and_tag(helper, tag):
    assert helper.project_version(ROOT, tag) == "1.0.3"


@pytest.mark.parametrize("tag", ["", "v9.9.9", "v1.0.3; exit 0", "$(id)", "--help"])
def test_tag_is_data_and_must_match(helper, tag, tmp_path):
    with pytest.raises(ValueError, match="TAG_PACKAGE_VERSION_MISMATCH"):
        helper.validate(ROOT, tmp_path / "out", uv="must-not-execute", tag=tag)
    assert not (tmp_path / "out").exists()


def test_lock_version_mismatch(helper, tmp_path):
    (tmp_path / "pyproject.toml").write_bytes((ROOT / "pyproject.toml").read_bytes())
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname="dispatcher-for-codex-agents"\nversion="9.0.0"\n'
    )
    with pytest.raises(ValueError, match="LOCK_PACKAGE_VERSION_MISMATCH"):
        helper.project_version(tmp_path)


def test_reject_outside_output(helper, tmp_path):
    with pytest.raises(ValueError, match="OUTPUT_MUST_BE_UNDER_EXPORTS"):
        helper.validate(ROOT, tmp_path / "out", uv="must-not-execute")


def test_subprocess_environment_excludes_credentials(helper, monkeypatch, tmp_path):
    for key in (
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "API_KEY",
        "CODEX_HOME",
        "PYTHONPATH",
        "UV_INDEX_URL",
        "DCA_NOTIFY_CONFIG",
    ):
        monkeypatch.setenv(key, "synthetic-sensitive-value")
    env = helper.child_environment(ROOT, tmp_path)
    assert "synthetic-sensitive-value" not in env.values()
    assert env["UV_PYTHON_DOWNLOADS"] == "never"
    assert env["DCA_NOTIFY_CONFIG"] == str(ROOT / "configs/notifications.json")


@pytest.mark.parametrize(
    "wrong",
    [
        "Name: wrong\nVersion: 1.0.3\n",
        "Name: dispatcher-for-codex-agents\nVersion: 0.0.0\n",
    ],
)
def test_metadata_reject(helper, wrong):
    with pytest.raises(ValueError, match="METADATA_MISMATCH"):
        helper.validate_metadata(wrong.encode(), "1.0.3")


def packages(tmp_path, *, altered=False, extra=False, sdist_extra=None, missing=False):
    source = tmp_path / "source"
    module = source / "src/dispatcher_for_codex_agents/__init__.py"
    module.parent.mkdir(parents=True)
    module.write_text('VERSION = "synthetic"\n')
    output = tmp_path / "out"
    output.mkdir()
    meta = b"Name: dispatcher-for-codex-agents\nVersion: 1.0.3\n"
    with zipfile.ZipFile(output / "synthetic.whl", "w") as z:
        z.writestr(
            "dispatcher_for_codex_agents/__init__.py",
            b"altered" if altered else module.read_bytes(),
        )
        z.writestr("dispatcher_for_codex_agents-1.0.3.dist-info/METADATA", meta)
        z.writestr(
            "dispatcher_for_codex_agents-1.0.3.dist-info/entry_points.txt",
            "[console_scripts]\ndca=dispatcher_for_codex_agents.agent_harness.cli:main\ndca-notify=dispatcher_for_codex_agents.notifications.cli:main\n",
        )
        if extra:
            z.writestr("runtime/private.txt", "synthetic")
    with tarfile.open(output / "synthetic.tar.gz", "w:gz") as t:
        contents = {"PKG-INFO": meta}
        if not missing:
            contents["src/dispatcher_for_codex_agents/__init__.py"] = (
                module.read_bytes()
            )
        for name, content in contents.items():
            info = tarfile.TarInfo("dispatcher_for_codex_agents-1.0.3/" + name)
            info.size = len(content)
            t.addfile(info, io.BytesIO(content))
        if sdist_extra is not None:
            t.addfile(sdist_extra, io.BytesIO(b""))
    return source, output


def test_packages_verified(helper, tmp_path):
    source, output = packages(tmp_path)
    assert helper.validate_packages(source, output, "1.0.3").name == "synthetic.whl"


@pytest.mark.parametrize(
    "kw,error",
    [
        ({"altered": True}, "WHEEL_SOURCE_MISMATCH"),
        ({"extra": True}, "UNEXPECTED_WHEEL_CONTENT"),
    ],
)
def test_packages_reject_changed_or_private(helper, tmp_path, kw, error):
    source, output = packages(tmp_path, **kw)
    with pytest.raises(ValueError, match=error):
        helper.validate_packages(source, output, "1.0.3")


@pytest.mark.parametrize(
    "name",
    [
        "dispatcher_for_codex_agents-1.0.3.dist-info/../../escape",
        "/absolute",
        "./relative",
        "a//b",
        "a/../b",
        "a\\b",
        "C:/absolute",
    ],
)
def test_wheel_rejects_noncanonical_paths(helper, tmp_path, name):
    source, output = packages(tmp_path)
    with zipfile.ZipFile(output / "synthetic.whl", "a") as archive:
        archive.writestr(name, b"synthetic")
    with pytest.raises(ValueError, match="ARCHIVE_PATH_INVALID"):
        helper.validate_packages(source, output, "1.0.3")


@pytest.mark.parametrize("kind", [stat.S_IFLNK, stat.S_IFIFO, stat.S_IFDIR])
def test_wheel_rejects_nonregular_members(helper, tmp_path, kind):
    source, output = packages(tmp_path)
    info = zipfile.ZipInfo("dispatcher_for_codex_agents-1.0.3.dist-info/extra")
    info.create_system = 3
    info.external_attr = (kind | 0o777) << 16
    with zipfile.ZipFile(output / "synthetic.whl", "a") as archive:
        archive.writestr(info, b"../../escape")
    with pytest.raises(ValueError, match="ZIP_MEMBER_TYPE_INVALID"):
        helper.validate_packages(source, output, "1.0.3")


@pytest.mark.parametrize("missing", [False, True])
def test_sdist_rejects_extra_or_missing_modules(helper, tmp_path, missing):
    extra = tarfile.TarInfo(
        "dispatcher_for_codex_agents-1.0.3/src/dispatcher_for_codex_agents/extra.py"
    )
    source, output = packages(
        tmp_path, sdist_extra=None if missing else extra, missing=missing
    )
    with pytest.raises(ValueError, match="SDIST_SOURCE_MISMATCH"):
        helper.validate_packages(source, output, "1.0.3")


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE])
def test_sdist_rejects_links(helper, tmp_path, kind):
    extra = tarfile.TarInfo("dispatcher_for_codex_agents-1.0.3/link")
    extra.type = kind
    extra.linkname = "../../escape"
    source, output = packages(tmp_path, sdist_extra=extra)
    with pytest.raises(ValueError, match="SDIST_MEMBER_INVALID"):
        helper.validate_packages(source, output, "1.0.3")


def test_actual_failed_build_is_not_swallowed(helper, tmp_path):
    (tmp_path / "configs").mkdir()
    (tmp_path / "runtime/tmp").mkdir(parents=True)
    for name in ("pyproject.toml", "uv.lock"):
        (tmp_path / name).write_bytes((ROOT / name).read_bytes())
    (tmp_path / "configs/public_files.txt").write_text("pyproject.toml\nuv.lock\n")
    output = tmp_path / "exports/candidate"
    with pytest.raises(ValueError, match="build_FAILED"):
        helper.validate(tmp_path, output, uv="/bin/false")
    assert len(list((tmp_path / "runtime/tmp").glob("*/build.log"))) == 1
    assert not list(output.glob("*.whl"))
    assert not (output / "SHA256SUMS").exists()


def test_snapshot_only_allows_two_exact_workflows(helper, tmp_path):
    snapshot = importlib.import_module("public_snapshot")
    assert ".github/workflows/ci.yml" in snapshot.source_files(ROOT)
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs/public_files.txt").write_text(".github/private.json\n")
    with pytest.raises(ValueError, match="PRIVATE_MEMBER_FORBIDDEN"):
        snapshot.source_files(tmp_path)


def test_workflow_safety_contract():
    for name in ("ci.yml", "release-validation.yml"):
        text = (ROOT / ".github/workflows" / name).read_text()
        assert "pull_request_target" not in text and "secrets." not in text
        assert "contents: read" in text and "write" not in text
        assert "persist-credentials: false" in text
        assert "timeout-minutes: 20" in text
        assert "uv sync --frozen" in text
        assert "--no-bin" in text
        actions = re.findall(r"uses: ([^\s]+)", text)
        assert actions and all(
            re.fullmatch(
                r"(?:actions/checkout|actions/upload-artifact|astral-sh/setup-uv)@[0-9a-f]{40}",
                a,
            )
            for a in actions
        )
        for line in text.splitlines():
            if "run:" in line:
                assert "${{" not in line
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "pull_request:" in ci and "branches: [main]" in ci
    assert "python -m pytest" in ci and "cancel-in-progress: true" in ci
    release = (ROOT / ".github/workflows/release-validation.yml").read_text()
    assert "workflow_dispatch:" in release and "tags: ['v*']" in release
    assert '--tag "$RELEASE_REF_NAME"' in release


def test_smoke_network_guard(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    smoke = importlib.import_module("installed_release_smoke")
    with pytest.raises(AssertionError, match="NETWORK_FORBIDDEN"):
        smoke.forbidden_network()
    # No real credential files, production paths or send commands in this helper.
    text = (ROOT / "scripts/installed_release_smoke.py").read_text()
    assert ".send(" not in text and "secret_value(" not in text
    assert '"OFF"' in text and '"HOOK_MIGRATION_PREVIEW"' in text
