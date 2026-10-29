"""Tests for distribution packaging, wheel building, and installed-wheel serving (Milestone B12-E4B).

Verifies:
1. Wheel archive builds cleanly and contains all required runtime metadata.
2. Wheel includes packaged production static assets (query_pilot/agent/static/).
3. Wheel excludes forbidden items (.env, credentials, profiles, local database files, venvs).
4. Wheel installs into an isolated temporary environment.
5. Installed wheel executes outside repository checkout and correctly serves frontend assets.
6. Missing assets return HTTP 404.
7. Documented --static-dir override takes precedence over packaged assets.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import sysconfig
import time
import urllib.request
import zipfile

import pytest

# Ensure repository root is on sys.path for importing scripts/
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.build_distribution import (
    REQUIRED_RUNTIME_DEPS,
    build_wheel_in_isolated_staging,
    inspect_and_verify_wheel,
    resolve_python_toolchain,
)


def get_venv_binaries(venv_dir: Path) -> dict[str, Path]:
    """Resolve executable and site-packages paths for target virtual environment."""
    if sys.platform == "win32":
        bin_dir = venv_dir / "Scripts"
        site_packages = venv_dir / "Lib" / "site-packages"
        ext = ".exe"
    else:
        bin_dir = venv_dir / "bin"
        py_ver = f"python{sys.version_info.major}.{sys.version_info.minor}"
        site_packages = venv_dir / "lib" / py_ver / "site-packages"
        ext = ""
    if not site_packages.is_dir():
        candidates = list(venv_dir.glob("**/site-packages"))
        if candidates:
            site_packages = candidates[0]
    return {
        "python": bin_dir / f"python{ext}",
        "pip": bin_dir / f"pip{ext}",
        "agent": bin_dir / f"querypilot-agent{ext}",
        "api": bin_dir / f"querypilot-api{ext}",
        "site_packages": site_packages,
    }


def get_host_site_packages() -> Path:
    """Resolve the active Python environment's site-packages directory."""
    purelib = Path(sysconfig.get_path("purelib"))
    if purelib.is_dir():
        return purelib
    for p in sys.path:
        path_p = Path(p)
        if path_p.name == "site-packages" and path_p.is_dir():
            return path_p
    repo_site = _REPO_ROOT / ".venv" / "Lib" / "site-packages"
    if repo_site.is_dir():
        return repo_site
    raise RuntimeError("Unable to locate active site-packages directory for test dependency linkage.")


def get_free_port() -> int:
    """Find an unused TCP port on loopback."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory) -> Path:
    """Build a distribution wheel once for the module tests in an isolated directory."""
    python_exe, has_setuptools = resolve_python_toolchain()
    if not has_setuptools:
        pytest.skip("No Python environment with setuptools found on system.")

    repo_root = Path(__file__).resolve().parents[1]
    frontend_dist = repo_root / "frontend" / "dist"
    if not (frontend_dist / "index.html").is_file():
        pytest.skip(f"Frontend production build missing at {frontend_dist}")

    temp_dist_dir = tmp_path_factory.mktemp("dist_out")
    wheel_path = build_wheel_in_isolated_staging(repo_root, temp_dist_dir, frontend_dist, python_exe)
    assert wheel_path.is_file()
    return wheel_path


def test_wheel_metadata_and_asset_contents(built_wheel: Path):
    """Verify that the built wheel contains all metadata, entry points, and static assets."""
    summary = inspect_and_verify_wheel(built_wheel)
    assert summary["static_files_count"] >= 3
    assert "querypilot-agent" in summary["entry_points"]
    assert "querypilot-api" in summary["entry_points"]

    with zipfile.ZipFile(built_wheel, "r") as z:
        names = z.namelist()
        # Verify static asset files
        assert "query_pilot/agent/static/index.html" in names
        assert any(n.startswith("query_pilot/agent/static/assets/") and n.endswith(".js") for n in names)
        assert any(n.startswith("query_pilot/agent/static/assets/") and n.endswith(".css") for n in names)

        # Verify entry_points.txt format
        entry_points = [n for n in names if n.endswith(".dist-info/entry_points.txt")][0]
        content = z.read(entry_points).decode("utf-8")
        assert "querypilot-agent = query_pilot.agent.cli:main" in content
        assert "querypilot-api = query_pilot.api.cli:main" in content


def test_wheel_installed_serves_static_outside_repository(built_wheel: Path, tmp_path: Path):
    """Verify packaged-asset smoke test (--no-deps with linked dependencies) outside repo.

    Note: This preserves the established fast smoke test verifying wheel asset unpacking
    and runtime config discovery outside the repo. For clean dependency-resolved installation
    without .pth linking, see scripts/verify_clean_install.py.
    """
    repo_root = _REPO_ROOT
    host_site = get_host_site_packages()

    # Create fresh isolated venv using current interpreter (Python 3.12)
    venv_dir = tmp_path / "isolated_venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)

    bins = get_venv_binaries(venv_dir)
    venv_pip = bins["pip"]
    venv_python = bins["python"]
    venv_agent = bins["agent"]
    venv_site = bins["site_packages"]

    # Install built wheel with --no-deps
    subprocess.run([str(venv_pip), "install", "--no-deps", str(built_wheel)], check=True)
    assert venv_agent.is_file(), f"Expected installed binary {venv_agent}"

    # Link repo runtime dependencies into isolated venv site-packages via .pth
    (venv_site / "_local_deps.pth").write_text(str(host_site) + "\n", encoding="utf-8")

    # Working directory completely outside repo
    run_cwd = tmp_path / "scratch_workdir"
    run_cwd.mkdir()

    # Verify that get_default_static_dir() inside installed package resolves to site-packages
    # and that query_pilot module resolves strictly to isolated venv (not repo src/)
    probe_code = (
        "import pathlib\n"
        "import query_pilot\n"
        "import query_pilot.agent.config as c\n"
        "print('RESOLVED_MODULE:' + str(pathlib.Path(query_pilot.__file__).resolve()))\n"
        "print('RESOLVED_STATIC:' + str(c.get_default_static_dir().resolve()))\n"
    )
    probe_proc = subprocess.run(
        [str(venv_python), "-c", probe_code],
        cwd=str(run_cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    resolved_module = None
    resolved_path = None
    for line in probe_proc.stdout.splitlines():
        if line.startswith("RESOLVED_MODULE:"):
            resolved_module = Path(line.split(":", 1)[1].strip())
        elif line.startswith("RESOLVED_STATIC:"):
            resolved_path = Path(line.split(":", 1)[1].strip())

    # Module origin assertions (strict path comparison, not substring)
    assert resolved_module is not None
    assert resolved_module.resolve().is_relative_to(venv_dir.resolve())
    assert not resolved_module.resolve().is_relative_to(repo_root.resolve())

    # Static asset assertions
    assert resolved_path is not None
    assert resolved_path.is_dir()
    assert (resolved_path / "index.html").is_file()
    assert resolved_path.resolve().is_relative_to(venv_dir.resolve())
    assert not resolved_path.resolve().is_relative_to(repo_root.resolve())

    # Launch installed querypilot-agent daemon on isolated loopback port
    port = get_free_port()
    token_dir = tmp_path / "tokens"
    token_dir.mkdir()
    cmd = [
        str(venv_agent),
        "--port", str(port),
        "--token-dir", str(token_dir),
    ]

    agent_proc = subprocess.Popen(
        cmd,
        cwd=str(run_cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        # Wait for daemon startup
        base_url = f"http://127.0.0.1:{port}"
        health_url = f"{base_url}/agent/v1/health"
        app_url = f"{base_url}/app/"

        ready = False
        for _ in range(50):
            try:
                with urllib.request.urlopen(app_url, timeout=0.5) as resp:
                    if resp.status == 200:
                        ready = True
                        break
            except Exception:
                time.sleep(0.1)

        if not ready:
            stdout, stderr = agent_proc.communicate(timeout=2)
            pytest.fail(f"Agent daemon failed to start on {app_url}.\nSTDOUT: {stdout}\nSTDERR: {stderr}")

        # 1. Verify /app/ returns HTML with title and root element
        with urllib.request.urlopen(app_url, timeout=2.0) as resp:
            assert resp.status == 200
            content = resp.read().decode("utf-8")
            assert "<title>QueryPilot</title>" in content
            assert '<div id="root">' in content

        # 2. Verify static JS and CSS assets resolve from packaged directory
        # Find asset names from resolved directory
        js_files = list((resolved_path / "assets").glob("*.js"))
        css_files = list((resolved_path / "assets").glob("*.css"))
        assert len(js_files) > 0
        assert len(css_files) > 0

        js_url = f"{base_url}/app/assets/{js_files[0].name}"
        with urllib.request.urlopen(js_url, timeout=2.0) as resp:
            assert resp.status == 200
            assert "javascript" in resp.headers.get("content-type", "")

        css_url = f"{base_url}/app/assets/{css_files[0].name}"
        with urllib.request.urlopen(css_url, timeout=2.0) as resp:
            assert resp.status == 200
            assert "css" in resp.headers.get("content-type", "")

        # 3. Verify missing static asset returns 404 (not swallowed by SPA)
        time.sleep(0.2)
        missing_url = f"{base_url}/app/assets/missing_asset.js"
        try:
            urllib.request.urlopen(missing_url, timeout=2.0)
            pytest.fail("Expected HTTP 404 for missing static asset.")
        except urllib.error.HTTPError as err:
            assert err.code == 404

    finally:
        agent_proc.terminate()
        try:
            agent_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            agent_proc.kill()


def test_wheel_installed_static_dir_override(built_wheel: Path, tmp_path: Path):
    """Verify that --static-dir override takes precedence over packaged static assets."""
    host_site = get_host_site_packages()

    venv_dir = tmp_path / "override_venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)

    bins = get_venv_binaries(venv_dir)
    venv_pip = bins["pip"]
    venv_agent = bins["agent"]
    venv_site = bins["site_packages"]

    subprocess.run([str(venv_pip), "install", "--no-deps", str(built_wheel)], check=True)
    (venv_site / "_local_deps.pth").write_text(str(host_site) + "\n", encoding="utf-8")

    # Create custom static directory with custom index.html
    custom_static = tmp_path / "custom_static_dir"
    custom_static.mkdir()
    (custom_static / "index.html").write_text(
        "<!doctype html><html><head><title>Custom Static Override</title></head><body>Override</body></html>",
        encoding="utf-8",
    )

    run_cwd = tmp_path / "override_workdir"
    run_cwd.mkdir()

    port = get_free_port()
    token_dir = tmp_path / "tokens"
    token_dir.mkdir()
    cmd = [
        str(venv_agent),
        "--port", str(port),
        "--token-dir", str(token_dir),
        "--static-dir", str(custom_static),
    ]

    agent_proc = subprocess.Popen(
        cmd,
        cwd=str(run_cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        base_url = f"http://127.0.0.1:{port}"
        health_url = f"{base_url}/agent/v1/health"
        app_url = f"{base_url}/app/"

        ready = False
        for _ in range(50):
            try:
                with urllib.request.urlopen(app_url, timeout=0.5) as resp:
                    if resp.status == 200:
                        ready = True
                        break
            except Exception:
                time.sleep(0.1)

        if not ready:
            stdout, stderr = agent_proc.communicate(timeout=2)
            pytest.fail(f"Agent daemon failed to start on {app_url}.\nSTDOUT: {stdout}\nSTDERR: {stderr}")

        with urllib.request.urlopen(app_url, timeout=2.0) as resp:
            assert resp.status == 200
            content = resp.read().decode("utf-8")
            assert "<title>Custom Static Override</title>" in content
            assert "Override" in content

    finally:
        agent_proc.terminate()
        try:
            agent_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            agent_proc.kill()


# ==============================================================================
# B12-E4C Cleanup Safety & Compatibility Tests
# ==============================================================================


def test_cleanup_safety_success_preserves_sentinels(tmp_path: Path):
    """Verify that build_wheel_in_isolated_staging leaves all pre-existing sentinels intact."""
    python_exe, has_setuptools = resolve_python_toolchain()
    if not has_setuptools:
        pytest.skip("No Python environment with setuptools found on system.")

    # Create mock repo layout with pre-existing directories and sentinel files
    mock_repo = tmp_path / "mock_repo"
    mock_repo.mkdir()

    # Minimal pyproject.toml
    pyproject_content = (
        '[build-system]\nrequires = ["setuptools>=61.0"]\nbuild-backend = "setuptools.build_meta"\n\n'
        '[project]\nname = "test-pkg"\nversion = "0.1.0"\ndescription = "Test"\n'
        'requires-python = ">=3.12"\ndependencies = []\n\n'
        '[tool.setuptools.packages.find]\nwhere = ["src"]\n\n'
        '[tool.setuptools.package-data]\n"query_pilot.agent" = ["static/**/*"]\n'
    )
    (mock_repo / "pyproject.toml").write_text(pyproject_content, encoding="utf-8")
    (mock_repo / "README.md").write_text("# Test", encoding="utf-8")

    mock_src_agent = mock_repo / "src" / "query_pilot" / "agent"
    mock_src_agent.mkdir(parents=True)
    (mock_src_agent / "__init__.py").write_text("", encoding="utf-8")
    (mock_src_agent.parent / "__init__.py").write_text("", encoding="utf-8")

    # 1. Pre-existing static directory with sentinel
    pre_existing_static = mock_src_agent / "static"
    pre_existing_static.mkdir()
    sentinel_static = pre_existing_static / "sentinel_static.txt"
    sentinel_static.write_text("DO_NOT_DELETE_STATIC", encoding="utf-8")

    # 2. Pre-existing build directory with sentinel
    pre_existing_build = mock_repo / "build"
    pre_existing_build.mkdir()
    sentinel_build = pre_existing_build / "sentinel_build.txt"
    sentinel_build.write_text("DO_NOT_DELETE_BUILD", encoding="utf-8")

    # 3. Pre-existing egg-info directory with sentinel
    pre_existing_egg = mock_repo / "src" / "query_pilot.egg-info"
    pre_existing_egg.mkdir()
    sentinel_egg = pre_existing_egg / "sentinel_egg.txt"
    sentinel_egg.write_text("DO_NOT_DELETE_EGG", encoding="utf-8")

    # 4. Mock frontend dist with index.html and sentinel
    mock_frontend_dist = mock_repo / "frontend" / "dist"
    mock_frontend_dist.mkdir(parents=True)
    (mock_frontend_dist / "index.html").write_text("<html>Frontend</html>", encoding="utf-8")
    sentinel_frontend = mock_frontend_dist / "sentinel_frontend.txt"
    sentinel_frontend.write_text("DO_NOT_DELETE_FRONTEND", encoding="utf-8")

    # Execute isolated build
    out_dir = tmp_path / "mock_dist"
    wheel = build_wheel_in_isolated_staging(mock_repo, out_dir, mock_frontend_dist, python_exe)

    assert wheel.is_file()

    # VERIFY all pre-existing sentinels remain untouched and unaltered
    assert sentinel_static.is_file()
    assert sentinel_static.read_text(encoding="utf-8") == "DO_NOT_DELETE_STATIC"

    assert sentinel_build.is_file()
    assert sentinel_build.read_text(encoding="utf-8") == "DO_NOT_DELETE_BUILD"

    assert sentinel_egg.is_file()
    assert sentinel_egg.read_text(encoding="utf-8") == "DO_NOT_DELETE_EGG"

    assert sentinel_frontend.is_file()
    assert sentinel_frontend.read_text(encoding="utf-8") == "DO_NOT_DELETE_FRONTEND"


def test_cleanup_safety_simulated_failure_preserves_sentinels(tmp_path: Path):
    """Verify that build failures in isolated staging do not destroy source-tree sentinels."""
    mock_repo = tmp_path / "mock_repo_fail"
    mock_repo.mkdir()

    (mock_repo / "pyproject.toml").write_text("[project]\nname = 'fail-pkg'\nversion = '0.1.0'\n", encoding="utf-8")
    (mock_repo / "README.md").write_text("# Fail", encoding="utf-8")

    mock_src_agent = mock_repo / "src" / "query_pilot" / "agent"
    mock_src_agent.mkdir(parents=True)
    (mock_src_agent / "__init__.py").write_text("", encoding="utf-8")

    # Sentinels
    pre_existing_static = mock_src_agent / "static"
    pre_existing_static.mkdir()
    sentinel_static = pre_existing_static / "sentinel_static.txt"
    sentinel_static.write_text("STILL_HERE_STATIC", encoding="utf-8")

    pre_existing_build = mock_repo / "build"
    pre_existing_build.mkdir()
    sentinel_build = pre_existing_build / "sentinel_build.txt"
    sentinel_build.write_text("STILL_HERE_BUILD", encoding="utf-8")

    mock_frontend_dist = mock_repo / "frontend" / "dist"
    mock_frontend_dist.mkdir(parents=True)
    (mock_frontend_dist / "index.html").write_text("<html>Frontend</html>", encoding="utf-8")
    sentinel_frontend = mock_frontend_dist / "sentinel_frontend.txt"
    sentinel_frontend.write_text("STILL_HERE_FRONTEND", encoding="utf-8")

    out_dir = tmp_path / "mock_dist_fail"

    # Pass an invalid Python executable to guarantee failure during the wheel build step
    invalid_python = "nonexistent_python_binary_xyz"

    with pytest.raises(Exception):
        build_wheel_in_isolated_staging(mock_repo, out_dir, mock_frontend_dist, invalid_python)

    # VERIFY sentinels survived the failure completely untouched
    assert sentinel_static.is_file()
    assert sentinel_static.read_text(encoding="utf-8") == "STILL_HERE_STATIC"

    assert sentinel_build.is_file()
    assert sentinel_build.read_text(encoding="utf-8") == "STILL_HERE_BUILD"

    assert sentinel_frontend.is_file()
    assert sentinel_frontend.read_text(encoding="utf-8") == "STILL_HERE_FRONTEND"


def test_wheel_requires_python_and_metadata_compatibility(built_wheel: Path):
    """Verify that the wheel specifies Requires-Python: >=3.12 and valid purelib tags."""
    summary = inspect_and_verify_wheel(built_wheel)
    assert summary["requires_python"] == ">=3.12"

    with zipfile.ZipFile(built_wheel, "r") as z:
        metadata_names = [n for n in z.namelist() if n.endswith(".dist-info/METADATA")]
        assert len(metadata_names) == 1
        meta_content = z.read(metadata_names[0]).decode("utf-8")

        # Check Requires-Python
        assert "Requires-Python: >=3.12" in meta_content

        # Check WHEEL tags
        wheel_names = [n for n in z.namelist() if n.endswith(".dist-info/WHEEL")]
        assert len(wheel_names) == 1
        wheel_content = z.read(wheel_names[0]).decode("utf-8")
        assert "Root-Is-Purelib: true" in wheel_content
        assert "Tag: py3-none-any" in wheel_content


# ==============================================================================
# B19-S1B Clean-Install & Offline Harness Verification Tests
# ==============================================================================


def test_verify_clean_install_cli_argument_validation():
    """Verify verify_clean_install CLI argument parsing and safety validation."""
    from scripts.verify_clean_install import build_arg_parser

    parser = build_arg_parser()

    # 1. Default mode is None (resolved to dependency-resolved at runtime)
    args = parser.parse_args([])
    assert args.mode is None
    assert args.wheelhouse is None

    # 2. --mode offline accepted
    args_offline = parser.parse_args(["--mode", "offline", "--wheelhouse", "some/path"])
    assert args_offline.mode == "offline"
    assert args_offline.wheelhouse == Path("some/path")

    # 3. Invalid mode rejected
    with pytest.raises(SystemExit):
        parser.parse_args(["--mode", "invalid-mode"])

    # 4. --skip-build flag parsed
    args_skip = parser.parse_args(["--skip-build", "--wheel", "my.whl"])
    assert args_skip.skip_build is True
    assert args_skip.wheel == Path("my.whl")


def test_offline_wheelhouse_installation(built_wheel: Path):
    """Verify true offline installation using an explicit wheelhouse (--no-index).

    Opt-in via QUERYPILOT_TEST_WHEELHOUSE pointing to a directory of pre-downloaded wheels.
    Strictly skips if no wheelhouse is configured, and fails if the configured path is invalid.
    Never produces a false positive.
    """
    wheelhouse_env = os.getenv("QUERYPILOT_TEST_WHEELHOUSE")
    if not wheelhouse_env:
        pytest.skip(
            "True offline installation test requires QUERYPILOT_TEST_WHEELHOUSE "
            "pointing to a directory containing pre-downloaded wheels."
        )

    wheelhouse_path = Path(wheelhouse_env).resolve()
    if not wheelhouse_path.is_dir():
        pytest.fail(f"Configured QUERYPILOT_TEST_WHEELHOUSE directory does not exist: {wheelhouse_path}")

    wheels = list(wheelhouse_path.glob("*.whl"))
    if not wheels:
        pytest.fail(f"Configured QUERYPILOT_TEST_WHEELHOUSE contains no .whl files: {wheelhouse_path}")

    from scripts.verify_clean_install import VerificationHarness

    repo_root = Path(__file__).resolve().parents[1]
    harness = VerificationHarness(
        repo_root=repo_root,
        mode="offline",
        wheelhouse=wheelhouse_path,
        wheel_path=built_wheel,
        skip_loopback=True,
    )
    summary = harness.verify()
    assert summary["install_success"] is True
    assert summary["module_origin_verified"] is True


def test_clean_install_dependency_resolved(built_wheel: Path):
    """Verify clean-environment dependency-resolved installation outside repo.

    Opt-in via QUERYPILOT_TEST_CLEAN_INSTALL=1 to keep default test runs fast.
    """
    if os.getenv("QUERYPILOT_TEST_CLEAN_INSTALL") != "1":
        pytest.skip(
            "Dependency-resolved clean install test is opt-in (set QUERYPILOT_TEST_CLEAN_INSTALL=1) "
            "to keep standard unit test runs fast and deterministic."
        )

    from scripts.verify_clean_install import VerificationHarness

    repo_root = Path(__file__).resolve().parents[1]
    harness = VerificationHarness(
        repo_root=repo_root,
        mode="dependency-resolved",
        wheel_path=built_wheel,
        skip_loopback=False,
    )
    summary = harness.verify()
    assert summary["install_success"] is True
    assert summary["module_origin_verified"] is True
    assert summary["packaged_assets_verified"] is True
    assert summary["cli_smoke_verified"] is True
    assert summary["loopback_serving_verified"] is True


# ==============================================================================
# B19-S3A Reproducible Build & Asset Freshness Tests
# ==============================================================================


def test_source_date_epoch_resolution_and_validation(tmp_path: Path):
    """Verify SOURCE_DATE_EPOCH resolution precedence, range clamping, and error handling."""
    from scripts.build_distribution import (
        MIN_SOURCE_DATE_EPOCH,
        MAX_SOURCE_DATE_EPOCH,
        resolve_source_date_epoch,
    )

    repo_root = Path(__file__).resolve().parents[1]

    # 1. Explicit integer timestamp within valid range
    assert resolve_source_date_epoch(repo_root, explicit_epoch=1700000000) == 1700000000

    # 2. Explicit string timestamp within valid range
    assert resolve_source_date_epoch(repo_root, explicit_epoch="1700000000") == 1700000000

    # 3. Invalid non-integer string raises ValueError
    with pytest.raises(ValueError, match="must be an integer UNIX timestamp"):
        resolve_source_date_epoch(repo_root, explicit_epoch="invalid_epoch_val")

    # 4. Out-of-bounds: before 1980-01-01 (ZIP header constraint) raises ValueError
    with pytest.raises(ValueError, match="ZIP archive header constraints"):
        resolve_source_date_epoch(repo_root, explicit_epoch=MIN_SOURCE_DATE_EPOCH - 1)

    # 5. Out-of-bounds: past year 2100 raises ValueError
    with pytest.raises(ValueError, match="exceeds maximum supported year"):
        resolve_source_date_epoch(repo_root, explicit_epoch=MAX_SOURCE_DATE_EPOCH + 1)

    # 6. Fallback to git commit metadata when no explicit value or env var is present
    orig_env = os.environ.pop("SOURCE_DATE_EPOCH", None)
    try:
        git_ts = resolve_source_date_epoch(repo_root, explicit_epoch=None)
        assert isinstance(git_ts, int)
        assert git_ts >= MIN_SOURCE_DATE_EPOCH
    finally:
        if orig_env is not None:
            os.environ["SOURCE_DATE_EPOCH"] = orig_env

    # 7. Non-git directory without environment variable returns documented fallback
    empty_dir = tmp_path / "not_a_git_repo"
    empty_dir.mkdir()
    orig_env = os.environ.pop("SOURCE_DATE_EPOCH", None)
    try:
        fallback_epoch = resolve_source_date_epoch(empty_dir, explicit_epoch=None)
        assert fallback_epoch == 1700000000
    finally:
        if orig_env is not None:
            os.environ["SOURCE_DATE_EPOCH"] = orig_env


def test_frontend_asset_freshness_notice_emitted(capsys):
    """Verify build_frontend_bundle emits clear freshness notice when reusing existing dist."""
    from scripts.build_distribution import build_frontend_bundle

    repo_root = Path(__file__).resolve().parents[1]
    frontend_dist = repo_root / "frontend" / "dist"
    if not (frontend_dist / "index.html").is_file():
        pytest.skip("frontend/dist/index.html is not present")

    res = build_frontend_bundle(repo_root, force=False)
    assert res == frontend_dist
    captured = capsys.readouterr()
    assert "[notice] Using pre-existing production frontend build" in captured.out
    assert "Freshness limitation" in captured.out
    assert "--rebuild-frontend" in captured.out


def test_wheel_build_repeat_deterministic_hashes(tmp_path: Path):
    """Verify repeat builds of the same revision produce bitwise identical wheel hashes.

    Opt-in via QUERYPILOT_TEST_REPEAT_BUILD=1 to keep standard unit test runs fast.
    """
    if os.getenv("QUERYPILOT_TEST_REPEAT_BUILD") != "1":
        pytest.skip(
            "Repeat-build reproducibility test is opt-in (set QUERYPILOT_TEST_REPEAT_BUILD=1) "
            "to keep standard unit test runs fast and deterministic."
        )

    import hashlib
    from scripts.build_distribution import (
        build_wheel_in_isolated_staging,
        resolve_python_toolchain,
    )

    repo_root = Path(__file__).resolve().parents[1]
    frontend_dist = repo_root / "frontend" / "dist"
    if not (frontend_dist / "index.html").is_file():
        pytest.skip(f"Frontend production build missing at {frontend_dist}")

    python_exe, has_setuptools = resolve_python_toolchain()
    if not has_setuptools:
        pytest.skip("No Python environment with setuptools found.")

    dir1 = tmp_path / "repeat_build_1"
    dir2 = tmp_path / "repeat_build_2"
    dir1.mkdir()
    dir2.mkdir()

    wheel1 = build_wheel_in_isolated_staging(repo_root, dir1, frontend_dist, python_exe)
    wheel2 = build_wheel_in_isolated_staging(repo_root, dir2, frontend_dist, python_exe)

    hash1 = hashlib.sha256(wheel1.read_bytes()).hexdigest()
    hash2 = hashlib.sha256(wheel2.read_bytes()).hexdigest()

    if hash1 != hash2:
        with zipfile.ZipFile(wheel1) as z1, zipfile.ZipFile(wheel2) as z2:
            mismatches = []
            for n in z1.namelist():
                if n in z2.namelist():
                    i1 = z1.getinfo(n)
                    i2 = z2.getinfo(n)
                    if i1.CRC != i2.CRC or i1.date_time != i2.date_time:
                        mismatches.append(f"{n}: CRC {i1.CRC} vs {i2.CRC}, mtime {i1.date_time} vs {i2.date_time}")
            pytest.fail(f"Wheel hashes differ:\nBuild 1: {hash1}\nBuild 2: {hash2}\nMismatched entries:\n" + "\n".join(mismatches))

    assert hash1 == hash2
