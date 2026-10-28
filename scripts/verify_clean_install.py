#!/usr/bin/env python3
"""Clean-Install Verification Harness for QueryPilot (Milestone B19-S1B).

Verifies standalone wheel packaging, clean-environment installation, module origin
isolation, static asset bundling, entry point executables, and optional loopback
daemon serving without mutating or linking the repository development environment.

Modes supported:
1. dependency-resolved (default):
   Installs the built wheel in a fresh, isolated virtual environment outside the
   repository. Resolves runtime dependencies via standard package index or ambient
   pip wheel cache. Strictly forbids --no-deps or .pth repository linkage.
2. offline:
   Installs with '--no-index --find-links <wheelhouse>' using a pre-populated wheelhouse
   directory. Forbids network access. Fails clearly if the wheelhouse is missing,
   incomplete, or lacks required binary wheels.
3. no-deps:
   Preserves the fast packaging-only asset smoke test (--no-deps). Explicitly labeled
   as asset-only verification without dependency resolution.

Safety Guarantees:
- The active development virtual environment (.venv) is NEVER modified or linked.
- The repository source tree (repo_root) is NEVER mutated.
- All temporary staging, virtual environments, probe directories, and token directories
  are isolated outside the repository and cleaned up reliably in finally blocks.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from typing import Optional

# Ensure repository root is on sys.path for relative imports when executed directly
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Reuse established distribution packaging helpers
try:
    from scripts.build_distribution import (
        build_wheel_in_isolated_staging,
        inspect_and_verify_wheel,
        resolve_python_toolchain,
    )
except ImportError:
    from build_distribution import (
        build_wheel_in_isolated_staging,
        inspect_and_verify_wheel,
        resolve_python_toolchain,
    )


def get_free_port() -> int:
    """Find an ephemeral TCP port on loopback.

    Note: Ephemeral port allocation has an inherent brief race between socket
    closure and daemon bind. The harness minimizes this window and logs the port.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get_venv_binaries(venv_dir: Path) -> dict[str, Path]:
    """Resolve executable and site-packages paths for the target virtual environment."""
    if platform.system() == "Windows":
        scripts_dir = venv_dir / "Scripts"
        site_packages = venv_dir / "Lib" / "site-packages"
        return {
            "python": scripts_dir / "python.exe",
            "pip": scripts_dir / "pip.exe",
            "agent": scripts_dir / "querypilot-agent.exe",
            "api": scripts_dir / "querypilot-api.exe",
            "site_packages": site_packages,
        }
    else:
        bin_dir = venv_dir / "bin"
        py_ver = f"python{sys.version_info.major}.{sys.version_info.minor}"
        site_packages = venv_dir / "lib" / py_ver / "site-packages"
        return {
            "python": bin_dir / "python",
            "pip": bin_dir / "pip",
            "agent": bin_dir / "querypilot-agent",
            "api": bin_dir / "querypilot-api",
            "site_packages": site_packages,
        }


def is_relative_to(path: Path, parent: Path) -> bool:
    """Check if path is relative to parent (compatible with Python 3.8+)."""
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


class VerificationHarness:
    """Orchestrates isolated installation and verification of the QueryPilot package."""

    def __init__(
        self,
        repo_root: Path,
        mode: str = "dependency-resolved",
        wheelhouse: Optional[Path] = None,
        wheel_path: Optional[Path] = None,
        dist_dir: Optional[Path] = None,
        skip_loopback: bool = False,
        verbose: bool = False,
    ):
        self.repo_root = repo_root.resolve()
        self.mode = mode
        self.wheelhouse = wheelhouse.resolve() if wheelhouse else None
        self.wheel_path = wheel_path.resolve() if wheel_path else None
        self.dist_dir = dist_dir.resolve() if dist_dir else None
        self.skip_loopback = skip_loopback
        self.verbose = verbose

        self._temp_dirs_to_clean: list[Path] = []

    def _create_temp_dir(self, prefix: str) -> Path:
        """Create a temporary directory outside the repository root."""
        d = Path(tempfile.mkdtemp(prefix=prefix))
        self._temp_dirs_to_clean.append(d)
        return d

    def cleanup(self) -> None:
        """Safely remove all temporary directories created by this harness."""
        for d in self._temp_dirs_to_clean:
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
        self._temp_dirs_to_clean.clear()

    def run_command(
        self,
        cmd: list[str],
        cwd: Optional[Path] = None,
        env: Optional[dict[str, str]] = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Execute a subprocess command with structured logging and environment isolation."""
        if self.verbose:
            print(f"[exec] {' '.join(str(c) for c in cmd)} (cwd: {cwd or os.getcwd()})")

        clean_env = env if env is not None else os.environ.copy()
        # Strictly strip PYTHONPATH to avoid bleeding checkout modules into isolated venv
        clean_env.pop("PYTHONPATH", None)

        proc = subprocess.run(
            [str(c) for c in cmd],
            cwd=str(cwd) if cwd else None,
            env=clean_env,
            capture_output=True,
            text=True,
            check=False,
        )

        if self.verbose and proc.stdout:
            print(f"[stdout]\n{proc.stdout.strip()}")
        if proc.returncode != 0:
            if self.verbose and proc.stderr:
                print(f"[stderr]\n{proc.stderr.strip()}")
            if check:
                raise RuntimeError(
                    f"Command failed (exit code {proc.returncode}): {' '.join(str(c) for c in cmd)}\n"
                    f"STDOUT:\n{proc.stdout}\n"
                    f"STDERR:\n{proc.stderr}"
                )
        return proc

    def verify(self) -> dict[str, any]:
        """Run the full clean-install verification lifecycle."""
        results: dict[str, any] = {
            "mode": self.mode,
            "wheel_verified": False,
            "venv_isolated": False,
            "install_success": False,
            "module_origin_verified": False,
            "packaged_assets_verified": False,
            "cli_smoke_verified": False,
            "loopback_serving_verified": False,
            "errors": [],
        }

        try:
            # 1. Resolve or build wheel
            python_exe, has_setuptools = resolve_python_toolchain()
            if not has_setuptools:
                raise RuntimeError("No Python toolchain with setuptools found on system.")

            if self.wheel_path:
                print(f"[step 1/6] Using pre-built wheel: {self.wheel_path}")
                target_wheel = self.wheel_path
            else:
                print("[step 1/6] Building wheel distribution artifact in isolated staging...")
                frontend_dist = self.repo_root / "frontend" / "dist"
                if not (frontend_dist / "index.html").is_file():
                    raise RuntimeError(
                        f"Frontend production build missing at {frontend_dist}. Run 'npm run build' first."
                    )
                build_dist_dir = self.dist_dir or self._create_temp_dir("qp_dist_out_")
                target_wheel = build_wheel_in_isolated_staging(
                    self.repo_root,
                    build_dist_dir,
                    frontend_dist,
                    python_exe,
                )

            wheel_summary = inspect_and_verify_wheel(target_wheel)
            print(f"[step 1/6] Wheel inspected: {target_wheel.name} (static files: {wheel_summary['static_files_count']})")
            results["wheel_verified"] = True
            results["wheel_path"] = str(target_wheel)

            # 2. Provision fresh isolated virtual environment outside repo
            print("[step 2/6] Provisioning clean isolated virtual environment outside repository...")
            temp_venv = self._create_temp_dir("qp_clean_venv_")
            self.run_command([python_exe, "-m", "venv", str(temp_venv)], check=True)
            bins = get_venv_binaries(temp_venv)

            if not bins["python"].is_file():
                raise RuntimeError(f"Python binary missing in created venv: {bins['python']}")
            if not bins["pip"].is_file():
                raise RuntimeError(f"Pip binary missing in created venv: {bins['pip']}")

            # Verify that venv is strictly outside repo_root
            if is_relative_to(temp_venv, self.repo_root):
                raise RuntimeError(f"Isolation failure: temp venv {temp_venv} is inside repo root {self.repo_root}")
            results["venv_isolated"] = True
            print(f"[step 2/6] Clean virtual environment created: {temp_venv}")

            # 3. Install wheel into isolated virtual environment
            print(f"[step 3/6] Installing wheel in mode '{self.mode}'...")
            pip_cmd = [str(bins["pip"]), "install"]

            if self.mode == "offline":
                if not self.wheelhouse:
                    raise ValueError(
                        "True offline mode requires --wheelhouse <dir> containing pre-downloaded wheels.\n"
                        "Fallback to package indexes is disabled via --no-index."
                    )
                if not self.wheelhouse.is_dir():
                    raise ValueError(f"Specified wheelhouse directory does not exist: {self.wheelhouse}")

                available_wheels = list(self.wheelhouse.glob("*.whl"))
                if not available_wheels:
                    raise ValueError(f"Specified wheelhouse directory contains no .whl files: {self.wheelhouse}")

                print(f"[install] Strictly offline installation (--no-index, find-links: {self.wheelhouse})")
                pip_cmd.extend(["--no-index", "--find-links", str(self.wheelhouse), str(target_wheel)])

            elif self.mode == "dependency-resolved":
                print("[install] Dependency-resolved installation (resolving runtime dependencies via index/cache)...")
                pip_cmd.append(str(target_wheel))

            elif self.mode == "no-deps":
                print("[install] Packaged-asset smoke installation (--no-deps)...")
                pip_cmd.extend(["--no-deps", str(target_wheel)])
            else:
                raise ValueError(f"Unsupported verification mode: {self.mode}")

            install_proc = self.run_command(pip_cmd, check=False)
            if install_proc.returncode != 0:
                if self.mode == "offline":
                    raise RuntimeError(
                        f"Offline installation failed. Missing required wheels in wheelhouse:\n{install_proc.stderr}"
                    )
                raise RuntimeError(f"Package installation failed:\n{install_proc.stderr}")

            results["install_success"] = True
            print("[step 3/6] Wheel installed successfully into isolated environment.")

            # 4. Probe module origin and packaged assets
            print("[step 4/6] Verifying installed module origin and static assets...")
            probe_workdir = self._create_temp_dir("qp_probe_workdir_")

            if self.mode == "no-deps":
                print("[notice] no-deps mode: validating unpacked assets directly in site-packages (no runtime dependencies).")
                installed_mod_file = bins["site_packages"] / "query_pilot" / "__init__.py"
                installed_static_dir = bins["site_packages"] / "query_pilot" / "agent" / "static"
                if not installed_mod_file.is_file():
                    raise AssertionError(f"Missing unpacked module at {installed_mod_file}")
            else:
                probe_code = (
                    "import sys, pathlib, json\n"
                    "try:\n"
                    "    import query_pilot\n"
                    "    import query_pilot.agent.config as c\n"
                    "    qp_file = str(pathlib.Path(query_pilot.__file__).resolve())\n"
                    "    static_dir = str(c.get_default_static_dir().resolve())\n"
                    "    sys.stdout.write('PROBE_DATA:' + json.dumps({'module_file': qp_file, 'static_dir': static_dir}) + '\\n')\n"
                    "except Exception as exc:\n"
                    "    sys.stderr.write(f'PROBE_IMPORT_ERROR: {exc}\\n')\n"
                    "    sys.exit(2)\n"
                )

                probe_proc = self.run_command(
                    [str(bins["python"]), "-c", probe_code],
                    cwd=probe_workdir,
                    check=True,
                )

                probe_data = None
                for line in probe_proc.stdout.splitlines():
                    if line.startswith("PROBE_DATA:"):
                        probe_data = json.loads(line[len("PROBE_DATA:") :].strip())
                        break

                if not probe_data:
                    raise RuntimeError(f"Failed to extract probe data from subprocess:\n{probe_proc.stdout}")

                installed_mod_file = Path(probe_data["module_file"])
                installed_static_dir = Path(probe_data["static_dir"])

            # Module origin assertions
            if not is_relative_to(installed_mod_file, temp_venv):
                raise AssertionError(
                    f"Module origin failure: query_pilot was loaded from {installed_mod_file}, "
                    f"which is outside the target venv {temp_venv}"
                )
            if is_relative_to(installed_mod_file, self.repo_root / "src"):
                raise AssertionError(
                    f"Module origin failure: query_pilot was loaded from repository source tree: {installed_mod_file}"
                )
            results["module_origin_verified"] = True
            print(f"[step 4/6] Module origin confirmed: {installed_mod_file}")

            # Static assets assertions
            if not is_relative_to(installed_static_dir, temp_venv):
                raise AssertionError(
                    f"Static asset failure: static directory {installed_static_dir} is outside venv {temp_venv}"
                )
            if is_relative_to(installed_static_dir, self.repo_root):
                raise AssertionError(
                    f"Static asset failure: static directory resolves inside repo root: {installed_static_dir}"
                )

            index_html = installed_static_dir / "index.html"
            if not index_html.is_file():
                raise AssertionError(f"Missing packaged index.html at {index_html}")

            js_files = list((installed_static_dir / "assets").glob("*.js"))
            css_files = list((installed_static_dir / "assets").glob("*.css"))
            if not js_files:
                raise AssertionError(f"No packaged JS assets found in {installed_static_dir / 'assets'}")
            if not css_files:
                raise AssertionError(f"No packaged CSS assets found in {installed_static_dir / 'assets'}")

            results["packaged_assets_verified"] = True
            print(f"[step 4/6] Packaged assets confirmed: index.html, {len(js_files)} JS, {len(css_files)} CSS files.")

            # 5. CLI entry point smoke tests
            print("[step 5/6] Verifying installed CLI entry point executables...")
            if not bins["agent"].is_file():
                raise AssertionError(f"CLI executable missing: {bins['agent']}")
            if not bins["api"].is_file():
                raise AssertionError(f"CLI executable missing: {bins['api']}")

            if self.mode == "no-deps":
                print("[notice] CLI execution skipped in no-deps mode (requires runtime dependencies).")
                results["cli_smoke_verified"] = "skipped (no-deps)"
            else:
                # Check querypilot-agent --help
                agent_help = self.run_command([str(bins["agent"]), "--help"], cwd=probe_workdir, check=True)
                if (
                    "querypilot-agent" not in agent_help.stdout
                    and "query-pilot-agent" not in agent_help.stdout
                    and "QueryPilot Local Agent Daemon" not in agent_help.stdout
                ):
                    raise AssertionError("querypilot-agent --help did not output expected usage description.")

                # Check querypilot-api --help
                api_help = self.run_command([str(bins["api"]), "--help"], cwd=probe_workdir, check=True)
                if (
                    "querypilot-api" not in api_help.stdout
                    and "QueryPilot Hosted/Local API Service" not in api_help.stdout
                ):
                    raise AssertionError("querypilot-api --help did not output expected usage description.")

                # Check querypilot-agent auth info --token-dir <isolated_dir>
                isolated_token_dir = self._create_temp_dir("qp_probe_tokens_")
                auth_info = self.run_command(
                    [str(bins["agent"]), "auth", "info", "--token-dir", str(isolated_token_dir)],
                    cwd=probe_workdir,
                    check=True,
                )
                if "QueryPilot Bootstrap Token Diagnostic Info" not in auth_info.stdout:
                    raise AssertionError("querypilot-agent auth info did not output expected diagnostic banner.")

                results["cli_smoke_verified"] = True
                print("[step 5/6] CLI smoke tests passed: querypilot-agent --help, querypilot-api --help, auth info.")

            # 6. Loopback static-serving daemon smoke test
            if self.skip_loopback or self.mode == "no-deps":
                reason = "via --skip-loopback" if self.skip_loopback else "in no-deps mode"
                print(f"[step 6/6] Loopback serving test skipped {reason}.")
                results["loopback_serving_verified"] = f"skipped ({reason})"
            else:
                print("[step 6/6] Executing loopback static-serving smoke test...")
                port = get_free_port()
                daemon_token_dir = self._create_temp_dir("qp_daemon_tokens_")
                daemon_cmd = [
                    str(bins["agent"]),
                    "--host", "127.0.0.1",
                    "--port", str(port),
                    "--token-dir", str(daemon_token_dir),
                ]

                daemon_proc = subprocess.Popen(
                    daemon_cmd,
                    cwd=str(probe_workdir),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )

                try:
                    base_url = f"http://127.0.0.1:{port}"
                    app_url = f"{base_url}/app/"

                    ready = False
                    for _ in range(50):
                        try:
                            req = urllib.request.Request(app_url, method="GET")
                            with urllib.request.urlopen(req, timeout=0.5) as resp:
                                if resp.status == 200:
                                    ready = True
                                    break
                        except Exception:
                            time.sleep(0.1)

                    if not ready:
                        stdout, stderr = daemon_proc.communicate(timeout=2)
                        raise RuntimeError(
                            f"Daemon failed to start on {app_url} within 5.0s.\n"
                            f"STDOUT:\n{stdout}\nSTDERR:\n{stderr}"
                        )

                    # Verify /app/ returns HTML with title and root container
                    with urllib.request.urlopen(app_url, timeout=2.0) as resp:
                        if resp.status != 200:
                            raise AssertionError(f"Expected HTTP 200 from {app_url}, got {resp.status}")
                        content = resp.read().decode("utf-8")
                        if "<title>QueryPilot</title>" not in content or '<div id="root">' not in content:
                            raise AssertionError("Packaged index.html did not contain expected QueryPilot HTML shell.")

                    # Verify packaged JS asset
                    js_url = f"{base_url}/app/assets/{js_files[0].name}"
                    with urllib.request.urlopen(js_url, timeout=2.0) as resp:
                        if resp.status != 200:
                            raise AssertionError(f"Expected HTTP 200 for JS asset {js_url}")
                        content_type = resp.headers.get("content-type", "")
                        if "javascript" not in content_type:
                            raise AssertionError(f"Expected javascript Content-Type for {js_url}, got {content_type}")

                    # Verify packaged CSS asset
                    css_url = f"{base_url}/app/assets/{css_files[0].name}"
                    with urllib.request.urlopen(css_url, timeout=2.0) as resp:
                        if resp.status != 200:
                            raise AssertionError(f"Expected HTTP 200 for CSS asset {css_url}")
                        content_type = resp.headers.get("content-type", "")
                        if "css" not in content_type:
                            raise AssertionError(f"Expected css Content-Type for {css_url}, got {content_type}")

                    results["loopback_serving_verified"] = True
                    print(f"[step 6/6] Loopback static serving verified successfully on {app_url}.")

                finally:
                    daemon_proc.terminate()
                    try:
                        daemon_proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        daemon_proc.kill()
                        daemon_proc.wait(timeout=2)

        except Exception as exc:
            results["errors"].append(str(exc))
            print(f"[error] Verification failed: {exc}")
            raise
        finally:
            self.cleanup()

        return results


def build_arg_parser() -> argparse.ArgumentParser:
    """Build parser for verify_clean_install CLI."""
    parser = argparse.ArgumentParser(
        prog="verify_clean_install",
        description=(
            "QueryPilot Clean-Install Verification Harness.\n\n"
            "Verifies distribution wheel building, fresh virtualenv installation outside the repo,\n"
            "module origin isolation, packaged assets, and CLI entry points."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--mode",
        choices=["dependency-resolved", "offline", "no-deps"],
        default=None,
        help=(
            "Verification installation mode:\n"
            "  dependency-resolved (default): installs in a fresh venv, resolving runtime dependencies\n"
            "                                 via configured index or ambient pip cache.\n"
            "  offline:                       installs with '--no-index --find-links <wheelhouse>'.\n"
            "                                 Strictly forbids network access; requires --wheelhouse.\n"
            "  no-deps:                       installs with '--no-deps' (packaged-asset smoke test only)."
        ),
    )
    parser.add_argument(
        "--wheelhouse",
        type=Path,
        default=None,
        help="Path to directory containing pre-downloaded wheels for true offline verification.",
    )
    parser.add_argument(
        "--wheel",
        type=Path,
        default=None,
        help="Path to pre-existing wheel artifact (skips building a new wheel).",
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        default=False,
        help="Skip wheel building (requires --wheel <path>).",
    )
    parser.add_argument(
        "--dist-dir",
        type=Path,
        default=None,
        help="Custom output directory for built wheel (defaults to an isolated temporary directory).",
    )
    parser.add_argument(
        "--skip-loopback",
        action="store_true",
        default=False,
        help="Skip loopback static-serving HTTP daemon smoke test.",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        default=False,
        help="Enable verbose output from subprocesses and commands.",
    )
    return parser


def main() -> int:
    """CLI runner for verify_clean_install."""
    parser = build_arg_parser()
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]

    # Validate argument combinations
    if args.skip_build and not args.wheel:
        parser.error("--skip-build requires --wheel <path>.")

    # Determine mode
    if args.mode is None:
        if args.wheelhouse is not None:
            mode = "offline"
        else:
            mode = "dependency-resolved"
    else:
        mode = args.mode

    if mode == "offline":
        if args.wheelhouse is None:
            parser.error(
                "Offline mode requires --wheelhouse <dir> containing pre-downloaded wheels.\n"
                "Fallback to package indexes is disabled via --no-index."
            )
        if not args.wheelhouse.is_dir():
            parser.error(f"Wheelhouse path is not a directory: {args.wheelhouse}")

    if mode == "dependency-resolved" and args.wheelhouse is not None:
        parser.error(
            "Conflicting arguments: --wheelhouse is intended for --mode offline.\n"
            "Use --mode offline --wheelhouse <dir> for strictly offline verification."
        )

    print("=" * 70)
    print(" QueryPilot Clean-Install Verification Harness")
    print(f" Mode:       {mode}")
    print(f" Wheelhouse: {args.wheelhouse or 'N/A'}")
    print(f" Wheel:      {args.wheel or 'Build automatically'}")
    print("=" * 70)

    harness = VerificationHarness(
        repo_root=repo_root,
        mode=mode,
        wheelhouse=args.wheelhouse,
        wheel_path=args.wheel,
        dist_dir=args.dist_dir,
        skip_loopback=args.skip_loopback,
        verbose=args.verbose,
    )

    try:
        summary = harness.verify()
        print("\n" + "=" * 70)
        print(" Verification Summary: ALL CHECKS PASSED")
        print(f" Mode:                     {summary['mode']}")
        print(f" Wheel Verified:           {summary['wheel_verified']}")
        print(f" Clean Venv Isolated:      {summary['venv_isolated']}")
        print(f" Installation:             {summary['install_success']}")
        print(f" Module Origin Isolated:   {summary['module_origin_verified']}")
        print(f" Packaged Assets Verified: {summary['packaged_assets_verified']}")
        print(f" CLI Entry Points Tested:  {summary['cli_smoke_verified']}")
        print(f" Loopback Server Verified: {summary['loopback_serving_verified']}")
        print("=" * 70)
        return 0
    except Exception as exc:
        print("\n" + "=" * 70)
        print(" Verification Summary: FAILED")
        print(f" Error: {exc}")
        print("=" * 70)
        return 1


if __name__ == "__main__":
    sys.exit(main())
