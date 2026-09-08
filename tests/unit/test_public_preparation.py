"""Public-checkout deployment and fake example tests; no real host or secret."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from dispatcher_for_codex_agents.agent_harness.cli import build_parser
from dispatcher_for_codex_agents.main_agent_bridge.cli import preflight
from dispatcher_for_codex_agents.main_agent_bridge.contracts import SupervisorSpec

ROOT = Path(__file__).resolve().parents[2]


def load_script(name):
    path = ROOT / name
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def new_checkout(tmp_path):
    root = tmp_path / "checkout with spaces"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='synthetic-project'\n")
    return root


def test_init_preview_private_permissions_and_no_overwrite(tmp_path):
    root = new_checkout(tmp_path)
    init = load_script("scripts/init-workspace.py")
    assert init.initialize(root, dry_run=True)["status"] == "DRY_RUN"
    assert not (root / "configs").exists()
    assert init.initialize(root)["status"] == "INITIALIZED"
    for name in ("configs/workspace.json", "configs/notifications.json"):
        assert (root / name).stat().st_mode & 0o777 == 0o600
    for name in ("", "runtime/tmp", "runtime/cache", "runs"):
        assert (root / name).stat().st_mode & 0o777 == 0o700
    config = (root / "configs/notifications.json").read_bytes()
    assert json.loads(config)["sinks"] == []
    with pytest.raises(ValueError, match="NOT_OVERWRITTEN"):
        init.initialize(root)
    assert (root / "configs/notifications.json").read_bytes() == config


def test_initializer_rejects_symlink_before_any_write(tmp_path):
    root = new_checkout(tmp_path)
    external = tmp_path / "unrelated"
    external.mkdir()
    (root / "configs").symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError, match="SYMLINK"):
        load_script("scripts/init-workspace.py").initialize(root)
    assert list(external.iterdir()) == []
    assert not (root / "runtime").exists()


def test_snapshot_config_under_outer_git_repo(tmp_path):
    root = new_checkout(tmp_path)
    load_script("scripts/init-workspace.py").initialize(root)
    code = (
        "from dispatcher_for_codex_agents.workspace_paths import workspace_root; "
        "from dispatcher_for_codex_agents.notifications.core import load_config; "
        "config = workspace_root() / 'configs/notifications.json'; "
        "assert load_config(config)['sinks'] == []"
    )
    process = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code],
        cwd=root,
        env={
            "PATH": os.defpath,
            "DCA_WORKSPACE_CONFIG": str(root / "configs/workspace.json"),
        },
        text=True,
        capture_output=True,
    )
    assert process.returncode == 0, process.stderr


def test_portable_env_defaults_and_explicit_override(tmp_path):
    root = new_checkout(tmp_path)
    load_script("scripts/init-workspace.py").initialize(root)
    (root / "scripts").mkdir()
    script = root / "scripts/dca-env.sh"
    shutil.copy2(ROOT / "scripts/dca-env.sh", script)
    environment = {"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"}
    code = "import os,json; print(json.dumps(dict(os.environ)))"
    arguments = [str(script), sys.executable, "-I", "-B", "-c", code]
    process = subprocess.run(arguments, env=environment, text=True, capture_output=True)
    assert process.returncode == 0, process.stderr
    value = json.loads(process.stdout)
    assert value["UV_CACHE_DIR"] == str(root / "runtime/cache/uv")
    assert value["UV_PYTHON_INSTALL_DIR"] == str(root / "runtime/toolchain/python")
    assert value["TMPDIR"] == str(root / "runtime/tmp")
    assert value["UV_PYTHON_DOWNLOADS"] == "never"
    shared = tmp_path / "approved shared tools"
    environment.update(DCA_UV_ROOT=str(shared), UV_CACHE_DIR=str(shared / "cache"))
    process = subprocess.run(arguments, env=environment, text=True, capture_output=True)
    assert process.returncode == 0
    value = json.loads(process.stdout)
    assert value["UV_PYTHON_INSTALL_DIR"] == str(shared / "python")
    assert value["UV_CACHE_DIR"] == str(shared / "cache")
    assert not shared.exists()  # Environment declaration is not installation.


def test_bridge_example_expired_before_host_access(monkeypatch):
    spec = SupervisorSpec.model_validate_json(
        (ROOT / "examples/bridge.example.json").read_bytes()
    )
    assert (
        spec.expires_at == 0.0 and spec.wake_budget == 1 and spec.main_turn_budget == 2
    )
    with pytest.raises(ValueError, match="EXPIRE_WITHIN_72_HOURS"):
        preflight(spec)


def test_readme_languages_commands_and_contract_agree():
    readme = (ROOT / "README.md").read_text()
    english, chinese = readme.split('<a id="chinese"></a>')

    def blocks(text):
        return re.findall(r"```bash\n(.*?)\n```", text, re.S)

    assert blocks(english) == blocks(chinese)
    for token in (
        "CodexCliAdapter",
        "MIT License",
        "0.153.4",
        "3.11.15",
        "examples/test_demo.py",
        "examples/cross_provider_demo.py",
        "dca-notify",
        "--no-bin",
    ):
        assert token in english and token in chinese
    for block in blocks(english):
        for line in block.splitlines():
            words = shlex.split(line)
            if Path(words[0]).name == "dca":
                with pytest.raises(SystemExit) as result:
                    build_parser().parse_args(words[1:])
                assert result.value.code == 0


def test_fake_demo_uses_existing_cli_and_is_immutable(tmp_path, monkeypatch):
    root = new_checkout(tmp_path)
    load_script("scripts/init-workspace.py").initialize(root)
    monkeypatch.setenv("DCA_WORKSPACE_CONFIG", str(root / "configs/workspace.json"))
    demo = load_script("examples/test_demo.py")
    result = demo.run_demo(root / "runs/demo")
    assert result["exact_once_coverage"] == "PASS"
    assert result["virtual_agent_invocations"] == 4
    assert result["real_model_calls"] == result["notification_requests"] == 0
    assert result["shard_hashes_verified"] > 0
    with pytest.raises(ValueError, match="ALREADY_EXISTS"):
        demo.run_demo(root / "runs/demo")


def test_dependency_urls_are_public_and_credentials_not_bundled():
    import tomllib
    from urllib.parse import urlparse

    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    for package in lock["package"]:
        urls = [row["url"] for row in package.get("wheels", [])]
        if "sdist" in package:
            urls.append(package["sdist"]["url"])
        registry = package.get("source", {}).get("registry")
        if registry:
            urls.append(registry)
        for url in urls:
            parsed = urlparse(url)
            assert parsed.hostname in {"pypi.org", "files.pythonhosted.org"}
            assert (
                parsed.scheme == "https" and not parsed.username and not parsed.password
            )


def test_snapshot_whitelist_scanned_and_history_not_included(tmp_path):
    snapshot = load_script("scripts/public_snapshot.py")
    members = snapshot.source_files(ROOT)
    assert "scripts/init-workspace.py" in members
    assert "examples/test_demo.py" in members
    assert "LICENSE" in members and "CONTRIBUTING.md" in members
    assert all(
        not name.startswith((".git/", "runtime/", "reports/")) for name in members
    )
    with pytest.raises(ValueError, match="OUTPUT_MUST_BE"):
        snapshot.build_snapshot(ROOT, tmp_path / "unauthorized.zip")


@pytest.mark.parametrize(
    "bad_member", ["reports/private.md", "../secret", ".git/config"]
)
def test_snapshot_rejects_private_or_traversal_member(tmp_path, bad_member):
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs/public_files.txt").write_text(bad_member + "\n")
    with pytest.raises(ValueError):
        load_script("scripts/public_snapshot.py").source_files(tmp_path)


def test_snapshot_privacy_scanner_and_fake_credential_exception():
    inspect = load_script("scripts/public_snapshot.py").inspect_content
    home_path = "/" + "home" + "/example-user/private"
    assert inspect("docs/sample.md", home_path.encode()) == ["PRIVATE_ABSOLUTE_PATH"]
    key = "SCT" + "NOT_A_REAL_KEY_12345".replace("_", "")
    assert "SERVERCHAN_KEY" in inspect("examples/sample.txt", key.encode())
    assert inspect("tests/unit/fake.py", b"SCTFAKESECRET1234") == []
    assert "SERVERCHAN_KEY" in inspect("docs/sample.md", b"SCTFAKESECRET1234")


def test_approved_public_repository_does_not_allow_unreviewed_github_urls():
    snapshot = load_script("scripts/public_snapshot.py")
    for url in snapshot.PUBLIC_REPOSITORY_URLS:
        assert snapshot.inspect_content("README.md", url.encode()) == []
    unreviewed = "https://" + "github.com/unreviewed-owner/private-example"
    assert snapshot.inspect_content("docs/sample.md", unreviewed.encode()) == [
        "UNREVIEWED_URL_HOST"
    ]


def test_approved_mit_license_and_package_metadata():
    import tomllib

    text = (ROOT / "LICENSE").read_text()
    assert text.startswith("MIT License\n\nCopyright (c) 2026 ykw-9826\n")
    assert 'THE SOFTWARE IS PROVIDED "AS IS"' in text
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["license"] == "MIT"
    assert project["license-files"] == ["LICENSE"]
    assert project["version"] == "1.0.0"
