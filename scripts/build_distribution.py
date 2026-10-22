#!/usr/bin/env python3
"""Distribution build orchestration for QueryPilot (Milestones B12-E4B & B12-E4C).

Builds the production frontend bundle, stages assets in an isolated temporary
build directory outside the source tree, builds the standalone wheel distribution
artifact, and verifies its metadata and assets.

Safety Guarantees (B12-E4C):
- Source Tree Immutability: The repository source tree (repo_root) is NEVER mutated.
- Zero Cleanup Collateral: Pre-existing src/query_pilot/agent/static, build/, or
  .egg-info directories are strictly preserved and never deleted.
- All temporary staging occurs in an isolated temp directory that is safely removed
  in a finally block, preserving sentinel files and pre-existing directories in both
  success and failure paths.
- Preserves frontend/dist/ and all unrelated files.

Prerequisites:
- Node.js >= 18 and npm (if building frontend from scratch)
- Python >= 3.12 with setuptools >= 61.0
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile

# Required runtime dependencies that must appear in METADATA Requires-Dist
REQUIRED_RUNTIME_DEPS = {
    "fastapi",
    "google-genai",
    "httpx",
    "langgraph",
    "langsmith",
    "pydantic",
    "pydantic-settings",
    "pyodbc",
    "python-dotenv",
    "sqlalchemy",
    "sqlglot",
    "uvicorn",
}

FORBIDDEN_FILE_PATTERNS = [
    ".env",
    "agent.token",
    "profiles.json",
    ".venv",
    ".pytest_cache",
    "tests/",
]


def resolve_python_toolchain() -> tuple[str, bool]:
    """Find a Python executable with setuptools available for building wheels."""
    # Check current interpreter
    try:
        import setuptools.build_meta  # noqa: F401
        return sys.executable, True
    except ImportError:
        pass

    # Check known Python launchers / locations on system
    candidates = [
        r"C:\Users\YASH\AppData\Local\Programs\Python\Python313\python.exe",
        r"C:\Users\YASH\AppData\Local\Programs\Python\Python312\python.exe",
        "python",
        "py",
    ]
    for c in candidates:
        try:
            res = subprocess.run(
                [c, "-c", "import setuptools.build_meta; print('ok')"],
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode == 0 and "ok" in res.stdout:
                return c, True
        except Exception:
            continue

    return sys.executable, False


def build_frontend_bundle(repo_root: Path, force: bool = False) -> Path:
    """Build the production frontend into frontend/dist if needed or requested."""
    frontend_dir = repo_root / "frontend"
    dist_dir = frontend_dir / "dist"
    index_html = dist_dir / "index.html"

    if index_html.is_file() and not force:
        print(f"[build] Using existing production frontend build at: {dist_dir}")
        return dist_dir

    print(f"[build] Building production frontend in {frontend_dir}...")
    npm_cmd = "npm.cmd" if sys.platform == "win32" else "npm"
    subprocess.run([npm_cmd, "run", "build"], cwd=str(frontend_dir), check=True)

    if not index_html.is_file():
        raise RuntimeError(f"Frontend build completed but {index_html} was not generated.")

    print(f"[build] Frontend successfully built at: {dist_dir}")
    return dist_dir


def build_wheel_in_isolated_staging(
    repo_root: Path,
    dist_dir: Path,
    frontend_dist: Path,
    python_exe: str,
) -> Path:
    """Build a distribution wheel using an isolated temporary staging directory.

    Guarantees:
    - Never modifies or deletes any file or directory in the source tree (repo_root).
    - Preserves pre-existing src/query_pilot/agent/static, build/, or .egg-info directories.
    - All staging and intermediate build artifacts are isolated in a temporary directory
      and cleaned up in a finally block regardless of build success or failure.
    - Preserves frontend/dist/ and only copies from it.
    """
    dist_dir.mkdir(parents=True, exist_ok=True)
    abs_dist = str(dist_dir.resolve())

    temp_stage = Path(tempfile.mkdtemp(prefix="qp_pkg_stage_"))
    try:
        # Copy minimal project manifest and source tree into isolated staging
        shutil.copy2(repo_root / "pyproject.toml", temp_stage / "pyproject.toml")
        readme = repo_root / "README.md"
        if readme.is_file():
            shutil.copy2(readme, temp_stage / "README.md")
        license_file = repo_root / "LICENSE"
        if license_file.is_file():
            shutil.copy2(license_file, temp_stage / "LICENSE")

        shutil.copytree(repo_root / "src", temp_stage / "src")

        # Stage static assets directly into the isolated copy
        staged_static = temp_stage / "src" / "query_pilot" / "agent" / "static"
        if staged_static.exists():
            shutil.rmtree(staged_static)
        shutil.copytree(frontend_dist, staged_static)

        # Build wheel with cwd set to the isolated staging directory
        code = (
            "import setuptools.build_meta as bm\n"
            f"wheel_name = bm.build_wheel(r'''{abs_dist}''')\n"
            "print('WHEEL_NAME:' + wheel_name)\n"
        )
        proc = subprocess.run(
            [python_exe, "-c", code],
            cwd=str(temp_stage),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            print("[error] Wheel build failed in isolated stage:")
            print(proc.stdout)
            print(proc.stderr)
            raise RuntimeError(f"setuptools.build_meta failed with code {proc.returncode}")

        wheel_name = None
        for line in proc.stdout.splitlines():
            if line.startswith("WHEEL_NAME:"):
                wheel_name = line.split(":", 1)[1].strip()
                break

        if not wheel_name:
            raise RuntimeError("Failed to detect produced wheel filename from build output.")

        wheel_path = dist_dir / wheel_name
        if not wheel_path.is_file():
            raise RuntimeError(f"Produced wheel not found at expected path: {wheel_path}")

        print(f"[build] Wheel created: {wheel_path}")
        return wheel_path
    finally:
        # Only clean the isolated temporary directory
        shutil.rmtree(temp_stage, ignore_errors=True)


# Backwards compatibility wrappers
def stage_static_assets(dist_dir: Path, target_static_dir: Path) -> None:
    """Stage frontend distribution files into target static directory."""
    if target_static_dir.exists():
        shutil.rmtree(target_static_dir)
    target_static_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(dist_dir, target_static_dir)
    print(f"[stage] Staged static assets to: {target_static_dir}")


def clean_staging_artifacts(repo_root: Path, staged_static_dir: Path) -> None:
    """Clean only staged static directory if explicitly specified."""
    if staged_static_dir.exists():
        try:
            shutil.rmtree(staged_static_dir)
            print(f"[clean] Removed staged directory: {staged_static_dir}")
        except Exception as exc:
            print(f"[warn] Failed to remove {staged_static_dir}: {exc}")


def build_wheel_artifact(
    repo_root: Path,
    out_dir: Path,
    python_exe: str,
) -> Path:
    """Legacy wrapper calling isolated staging build."""
    frontend_dist = repo_root / "frontend" / "dist"
    return build_wheel_in_isolated_staging(repo_root, out_dir, frontend_dist, python_exe)


def inspect_and_verify_wheel(wheel_path: Path) -> dict[str, any]:
    """Inspect wheel archive contents, entry points, compatibility, and dependencies."""
    print(f"[verify] Inspecting wheel archive: {wheel_path}")

    with zipfile.ZipFile(wheel_path, "r") as z:
        names = z.namelist()

        # 1. Verify frontend assets are present in package static directory
        static_files = [n for n in names if n.startswith("query_pilot/agent/static/")]
        index_html_present = "query_pilot/agent/static/index.html" in names
        if not index_html_present:
            raise ValueError("Wheel verification failed: query_pilot/agent/static/index.html is missing!")
        if len(static_files) < 3:
            raise ValueError(f"Wheel verification failed: insufficient static assets ({len(static_files)} files).")

        # 2. Verify forbidden files are NOT present
        for pattern in FORBIDDEN_FILE_PATTERNS:
            forbidden_matches = [n for n in names if pattern in n]
            if forbidden_matches:
                raise ValueError(
                    f"SECURITY VIOLATION: Forbidden files matching '{pattern}' found in wheel: {forbidden_matches}"
                )

        # 3. Verify entry points
        entry_points_file = None
        metadata_file = None
        wheel_meta_file = None
        for n in names:
            if n.endswith(".dist-info/entry_points.txt"):
                entry_points_file = n
            elif n.endswith(".dist-info/METADATA"):
                metadata_file = n
            elif n.endswith(".dist-info/WHEEL"):
                wheel_meta_file = n

        if not entry_points_file:
            raise ValueError("Wheel verification failed: entry_points.txt not found.")
        if not metadata_file:
            raise ValueError("Wheel verification failed: METADATA not found.")
        if not wheel_meta_file:
            raise ValueError("Wheel verification failed: WHEEL metadata not found.")

        entry_points_content = z.read(entry_points_file).decode("utf-8")
        if "querypilot-agent = query_pilot.agent.cli:main" not in entry_points_content:
            raise ValueError("Wheel verification failed: querypilot-agent entry point missing.")
        if "querypilot-api = query_pilot.api.cli:main" not in entry_points_content:
            raise ValueError("Wheel verification failed: querypilot-api entry point missing.")

        # 4. Verify Python compatibility and dependencies in METADATA
        metadata_content = z.read(metadata_file).decode("utf-8")

        # Check Requires-Python
        requires_python = None
        for line in metadata_content.splitlines():
            if line.startswith("Requires-Python:"):
                requires_python = line.split(":", 1)[1].strip()
                break
        if requires_python != ">=3.12":
            raise ValueError(f"Wheel verification failed: expected Requires-Python '>=3.12', got '{requires_python}'")

        requires_dist = [
            line.split("Requires-Dist:", 1)[1].strip()
            for line in metadata_content.splitlines()
            if line.startswith("Requires-Dist:")
        ]

        missing_deps = []
        for req in REQUIRED_RUNTIME_DEPS:
            found = False
            for rd in requires_dist:
                pkg_name = rd.split(";")[0].split(">=")[0].split("<")[0].split("==")[0].split()[0].strip()
                if pkg_name.lower().replace("_", "-") == req.lower().replace("_", "-"):
                    if "extra ==" not in rd:
                        found = True
                        break
            if not found:
                missing_deps.append(req)

        if missing_deps:
            raise ValueError(f"Wheel verification failed: missing runtime dependencies in METADATA: {missing_deps}")

        # Check pytest is test extra only
        pytest_in_runtime = False
        pytest_in_extra = False
        for rd in requires_dist:
            if "pytest" in rd.lower():
                if 'extra == "test"' in rd or "extra == 'test'" in rd:
                    pytest_in_extra = True
                else:
                    pytest_in_runtime = True

        if pytest_in_runtime:
            raise ValueError("Wheel verification failed: pytest must not be in unconditional runtime dependencies.")
        if not pytest_in_extra:
            raise ValueError("Wheel verification failed: pytest missing from optional-dependencies extra 'test'.")

        # 5. Verify WHEEL file contents
        wheel_content = z.read(wheel_meta_file).decode("utf-8")
        if "Root-Is-Purelib: true" not in wheel_content:
            raise ValueError("Wheel verification failed: expected Root-Is-Purelib: true")
        if "Tag: py3-none-any" not in wheel_content:
            raise ValueError("Wheel verification failed: expected Tag: py3-none-any")

    summary = {
        "wheel_path": str(wheel_path),
        "total_files": len(names),
        "static_files_count": len(static_files),
        "static_files": static_files,
        "entry_points": ["querypilot-agent", "querypilot-api"],
        "runtime_dependencies_count": len(REQUIRED_RUNTIME_DEPS),
        "requires_python": requires_python,
    }
    print(
        f"[verify] PASS: {len(names)} total files packaged, {len(static_files)} static assets verified, "
        f"Requires-Python: {requires_python}."
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="QueryPilot Distribution Build Orchestrator")
    parser.add_argument(
        "--dist-dir",
        type=Path,
        default=None,
        help="Directory to place the built wheel (default: <repo_root>/dist).",
    )
    parser.add_argument(
        "--rebuild-frontend",
        action="store_true",
        help="Force rebuild of frontend assets with 'npm run build'.",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    dist_dir = args.dist_dir or (repo_root / "dist")

    python_exe, has_setuptools = resolve_python_toolchain()
    if not has_setuptools:
        print("[error] No Python environment with setuptools found. Please install setuptools in your toolchain.")
        return 1
    print(f"[build] Using Python toolchain: {python_exe}")

    frontend_dist = build_frontend_bundle(repo_root, force=args.rebuild_frontend)

    wheel_path = build_wheel_in_isolated_staging(repo_root, dist_dir, frontend_dist, python_exe)
    inspect_and_verify_wheel(wheel_path)
    print(f"\n[success] QueryPilot wheel successfully built and verified:\n  {wheel_path}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
