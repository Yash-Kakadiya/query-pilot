#!/usr/bin/env python3
"""Packaging orchestration for QueryPilot Standalone Python Sidecar (B26-B2).

Packages the QueryPilot Python Local Agent into a standalone Windows x64 distribution
directory (`dist/querypilot-agent/`) using PyInstaller `--onedir`.

Prerequisites:
- Python 3.12 with project virtual environment active.
- Pre-built frontend static assets in `frontend/dist/`.
- PyInstaller >= 6.5.0 installed in environment.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys


def package_sidecar(
    repo_root: Path,
    rebuild_frontend: bool = False,
    clean: bool = True,
) -> Path:
    """Build and verify the frozen standalone local-agent sidecar directory.

    Returns:
        Path to the output distribution directory containing querypilot-agent.exe.
    """
    frontend_dir = repo_root / "frontend"
    frontend_dist = frontend_dir / "dist"
    spec_file = repo_root / "querypilot-agent.spec"

    if not spec_file.is_file():
        raise FileNotFoundError(f"PyInstaller spec file not found at '{spec_file}'.")

    # 1. Frontend asset verification / rebuild
    if rebuild_frontend or not (frontend_dist / "index.html").is_file():
        sys.stdout.write("Building production frontend bundle (npm run build)...\n")
        npm_cmd = "npm.cmd" if sys.platform == "win32" else "npm"
        result = subprocess.run(
            [npm_cmd, "run", "build"],
            cwd=str(frontend_dir),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            sys.stderr.write(f"Frontend build failed:\n{result.stderr}\n")
            raise RuntimeError(f"npm run build failed with exit code {result.returncode}")

    if not (frontend_dist / "index.html").is_file():
        raise FileNotFoundError(
            f"Frontend build directory '{frontend_dist}' does not contain index.html. "
            "Run 'npm run build' inside 'frontend/' first."
        )

    # 2. PyInstaller execution
    sys.stdout.write("Running PyInstaller to package standalone sidecar directory...\n")
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        str(spec_file),
        "--noconfirm",
    ]
    if clean:
        cmd.append("--clean")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root / "src") + os.pathsep + str(repo_root)

    proc = subprocess.run(cmd, cwd=str(repo_root), env=env, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"PyInstaller build failed with exit code {proc.returncode}")

    dist_dir = repo_root / "dist" / "querypilot-agent"
    exe_path = dist_dir / ("querypilot-agent.exe" if sys.platform == "win32" else "querypilot-agent")

    if not exe_path.is_file():
        raise FileNotFoundError(f"Expected frozen executable not found at '{exe_path}'.")

    # 3. Verification of static assets in output distribution
    internal_static = dist_dir / "_internal" / "query_pilot" / "agent" / "static" / "index.html"
    root_static = dist_dir / "query_pilot" / "agent" / "static" / "index.html"

    has_static = internal_static.is_file() or root_static.is_file()
    if not has_static:
        raise FileNotFoundError(
            f"Packaged frontend static assets missing in distribution directory '{dist_dir}'. "
            f"Checked '{internal_static}' and '{root_static}'."
        )

    sys.stdout.write(
        f"\nSUCCESS: Packaged QueryPilot sidecar created at:\n  {dist_dir}\n"
        f"Executable:\n  {exe_path}\n"
    )
    return dist_dir


def main() -> int:
    """CLI entry point for packaging script."""
    parser = argparse.ArgumentParser(description="Package QueryPilot Standalone Python Sidecar.")
    parser.add_argument(
        "--rebuild-frontend",
        action="store_true",
        default=False,
        help="Rebuild frontend production bundle before packaging.",
    )
    parser.add_argument(
        "--no-clean",
        action="store_true",
        default=False,
        help="Do not clean PyInstaller build cache before packaging.",
    )

    args = parser.parse_args()
    repo_root = Path(__file__).resolve().parents[1]

    try:
        package_sidecar(
            repo_root=repo_root,
            rebuild_frontend=args.rebuild_frontend,
            clean=not args.no_clean,
        )
        return 0
    except Exception as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())

