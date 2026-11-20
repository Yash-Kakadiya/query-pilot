#!/usr/bin/env python3
"""Desktop Release Packaging Orchestrator for QueryPilot v0.2.

Automates the production of the standalone Windows Desktop release bundle:
1. Builds frontend static bundle (npm run build).
2. Packages frozen Python sidecar daemon via PyInstaller (scripts/package_sidecar.py).
3. Compiles Tauri Windows Desktop binary in release mode (cargo build --release).
4. Assembles portable distribution directory containing:
   - QueryPilot.exe (Tauri desktop shell with WebView2)
   - querypilot-agent/ (frozen Python sidecar runtime with SQLite & ODBC drivers)
   - university_demo.db (sample university database)
   - README.md (desktop quickstart guide)
5. Compresses the distribution into QueryPilot-v{version}-windows-x64.zip.
6. Computes cryptographic SHA-256 checksums and writes SHA256SUMS.txt.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

DEFAULT_VERSION = "0.3.0"


def compute_sha256(file_path: Path) -> str:
    """Compute hex-encoded SHA-256 checksum of a file."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def build_desktop_release(
    version: str = DEFAULT_VERSION,
    rebuild: bool = False,
    output_dir: Path | None = None,
) -> Path:
    """Build and package the complete QueryPilot standalone Windows desktop release."""
    if output_dir is None:
        output_dir = REPO_ROOT / "release"
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'=' * 60}")
    print(f"QUERYPILOT DESKTOP RELEASE PACKAGING (v{version})")
    print(f"{'=' * 60}")

    # 1. Frontend bundle check / build
    frontend_dist = REPO_ROOT / "frontend" / "dist"
    if rebuild or not (frontend_dist / "index.html").is_file():
        print("\n[1/5] Building frontend production bundle (npm run build)...")
        npm_cmd = "npm.cmd" if sys.platform == "win32" else "npm"
        proc = subprocess.run(
            [npm_cmd, "run", "build"],
            cwd=str(REPO_ROOT / "frontend"),
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"Frontend build failed with exit code {proc.returncode}")
    else:
        print("\n[1/5] Using existing frontend production bundle.")

    # 2. Sidecar package check / build
    sidecar_dir = REPO_ROOT / "dist" / "querypilot-agent"
    sidecar_exe = sidecar_dir / ("querypilot-agent.exe" if sys.platform == "win32" else "querypilot-agent")
    if rebuild or not sidecar_exe.is_file():
        print("\n[2/5] Packaging Python agent sidecar via PyInstaller...")
        try:
            from scripts.package_sidecar import package_sidecar
        except ImportError:
            from package_sidecar import package_sidecar
        package_sidecar(repo_root=REPO_ROOT, rebuild_frontend=False, clean=True)
    else:
        print("\n[2/5] Using existing frozen sidecar at dist/querypilot-agent/.")

    # 3. Tauri release compilation check / build
    tauri_target_exe = REPO_ROOT / "src-tauri" / "target" / "release" / ("querypilot.exe" if sys.platform == "win32" else "querypilot")
    nsis_bundle_dir = REPO_ROOT / "src-tauri" / "target" / "release" / "bundle" / "nsis"

    if rebuild or not tauri_target_exe.is_file():
        print("\n[3/5] Compiling Tauri desktop binary and installer bundle...")
        npx_cmd = "npx.cmd" if sys.platform == "win32" else "npx"
        # Attempt to run Tauri CLI to build both the release executable and NSIS installer
        tauri_build_ok = False
        try:
            print("  -> Attempting Tauri bundler (npx @tauri-apps/cli build --bundles nsis)...")
            proc = subprocess.run(
                [npx_cmd, "--yes", "@tauri-apps/cli", "build", "--bundles", "nsis"],
                cwd=str(REPO_ROOT),
                check=False,
            )
            if proc.returncode == 0:
                tauri_build_ok = True
                print("  + Tauri build and NSIS installer bundling succeeded.")
        except Exception as e:
            print(f"  ! Tauri CLI bundling encountered error: {e}")

        # Fallback to direct cargo release build if Tauri CLI was not available or makensis missing
        if not tauri_build_ok and not tauri_target_exe.is_file():
            print("  -> Running cargo build --release fallback...")
            cargo_cmd = "cargo"
            fallback_proc = subprocess.run(
                [cargo_cmd, "build", "--release", "--manifest-path", str(REPO_ROOT / "src-tauri" / "Cargo.toml")],
                cwd=str(REPO_ROOT),
                check=False,
            )
            if fallback_proc.returncode != 0:
                raise RuntimeError(f"Cargo build --release failed with exit code {fallback_proc.returncode}")
    else:
        print("\n[3/5] Using existing compiled Tauri release executable.")

    # 4. Assemble portable distribution staging folder
    bundle_name = f"QueryPilot-v{version}-windows-x64"
    stage_dir = output_dir / bundle_name
    print(f"\n[4/5] Staging portable distribution directory at {stage_dir}...")

    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    stage_dir.mkdir(parents=True, exist_ok=True)

    # 4a. Copy QueryPilot.exe
    dest_exe = stage_dir / "QueryPilot.exe"
    shutil.copy2(tauri_target_exe, dest_exe)
    print(f"  + Copied {dest_exe.name} ({dest_exe.stat().st_size / (1024 * 1024):.1f} MB)")

    # 4b. Copy querypilot-agent sidecar directory
    dest_sidecar = stage_dir / "querypilot-agent"
    shutil.copytree(sidecar_dir, dest_sidecar)
    print(f"  + Bundled querypilot-agent sidecar directory")

    # 4c. Copy demo database
    demo_db_src = REPO_ROOT / "data" / "demo" / "university_demo.db"
    if demo_db_src.is_file():
        dest_demo_db = stage_dir / "university_demo.db"
        shutil.copy2(demo_db_src, dest_demo_db)
        print(f"  + Bundled demo database: {dest_demo_db.name}")

    # 4d. Write desktop README
    readme_content = f"""# QueryPilot v{version} - Desktop Standalone Edition

QueryPilot is an autonomous, on-premise AI data analyst and SQL copilot for Windows.
All database connections, schema introspection, and SQL execution occur **100% locally**
on your machine. Your database credentials never leave your local environment.

## Quickstart

1. Double-click `QueryPilot.exe` to launch the application.
2. The application will start the supervised local agent sidecar in the background.
3. Add a database profile:
   - **SQLite**: Click "+ Add Profile" -> "Select SQLite File" and choose `university_demo.db`.
   - **Cloud SQL / MSSQL**: Select AWS RDS, Azure SQL, Google Cloud SQL, or MonsterASP and enter your credentials.
4. Enter your free Google Gemini API key in the AI Service header to enable natural language query generation.
5. Ask analytical questions in plain English or write manual SQL queries!

## System Requirements
- Windows 10 or Windows 11 (64-bit)
- Microsoft Edge WebView2 Runtime (included in modern Windows 10/11)
- 4 GB RAM minimum

## Support & Updates
- Repository: https://github.com/query-pilot/query-pilot
"""
    (stage_dir / "README.md").write_text(readme_content, encoding="utf-8")
    print(f"  + Created README.md")

    # 5. Create Portable ZIP distribution
    zip_path = output_dir / f"{bundle_name}.zip"
    print(f"\n[5/5] Creating portable distribution zip archive: {zip_path.name}...")

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in stage_dir.rglob("*"):
            if file.is_file():
                rel_path = file.relative_to(stage_dir)
                zf.write(file, arcname=str(Path(bundle_name) / rel_path))

    zip_size_mb = zip_path.stat().st_size / (1024 * 1024)
    print(f"  + Created {zip_path.name} ({zip_size_mb:.1f} MB)")

    # 6. Generate SHA256 checksums
    checksum_lines = []
    zip_sha = compute_sha256(zip_path)
    checksum_lines.append(f"{zip_sha}  {zip_path.name}")

    # Copy and catalog setup exe if NSIS was built
    setup_candidates = list(nsis_bundle_dir.glob("*.exe")) if nsis_bundle_dir.is_dir() else []
    for cand in setup_candidates:
        dest_installer = output_dir / f"QueryPilot-v{version}-Setup.exe"
        shutil.copy2(cand, dest_installer)
        dest_installer_std = output_dir / f"QueryPilot_{version}_x64-setup.exe"
        shutil.copy2(cand, dest_installer_std)
        print(f"  + Bundled Windows installer executable: {dest_installer.name}")

    dest_installer = output_dir / f"QueryPilot-v{version}-Setup.exe"
    if dest_installer.is_file():
        setup_sha = compute_sha256(dest_installer)
        checksum_lines.append(f"{setup_sha}  {dest_installer.name}")

    nsis_setup = output_dir / f"QueryPilot_{version}_x64-setup.exe"
    if nsis_setup.is_file() and nsis_setup != dest_installer:
        setup_sha = compute_sha256(nsis_setup)
        checksum_lines.append(f"{setup_sha}  {nsis_setup.name}")

    sha_file = output_dir / "SHA256SUMS.txt"
    sha_file.write_text("\n".join(checksum_lines) + "\n", encoding="utf-8")
    print(f"  + Generated checksums in {sha_file.name}")

    print("\n" + "=" * 60)
    print("DESKTOP RELEASE BUILD COMPLETED SUCCESSFULLY")
    print("=" * 60)
    print(f"Distribution Zip:   {zip_path}")
    print(f"SHA-256 Checksum:   {zip_sha}")
    print("=" * 60)

    return zip_path


def main() -> int:
    parser = argparse.ArgumentParser(description="QueryPilot Desktop Release Packaging")
    parser.add_argument("--version", type=str, default=DEFAULT_VERSION, help="Release version (e.g. 0.2.0)")
    parser.add_argument("--rebuild", action="store_true", help="Force rebuild of frontend, sidecar, and Tauri binary")
    parser.add_argument("--output-dir", type=Path, default=None, help="Output directory for release assets")
    args = parser.parse_args()

    try:
        build_desktop_release(version=args.version, rebuild=args.rebuild, output_dir=args.output_dir)
        return 0
    except Exception as exc:
        print(f"\nERROR: Desktop release packaging failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
