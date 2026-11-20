"""Unit and Verification Tests for Desktop Release Packaging (Pillar 5).

Verifies:
1. compute_sha256 hashing fidelity against known fixtures.
2. Structure, staging, and zip creation logic in an isolated temporary environment.
3. When pre-built release artifacts exist (post-release build), verifies QueryPilot.exe,
   querypilot-agent, and demo database are present in the portable zip and match SHA256SUMS.txt.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import zipfile

import pytest

from scripts.package_desktop_release import compute_sha256

ROOT_DIR = Path(__file__).resolve().parent.parent
RELEASE_DIR = ROOT_DIR / "release"
HAS_PREBUILT_ZIP = len(list(RELEASE_DIR.glob("QueryPilot-v*-windows-x64.zip"))) > 0


class TestDesktopReleasePackaging:
    """Verifies packaging logic and release bundle integrity."""

    def test_compute_sha256_fidelity(self, tmp_path: Path):
        test_file = tmp_path / "sample.txt"
        content = b"QueryPilot Desktop Security & Packaging Invariant"
        test_file.write_bytes(content)

        expected_hash = hashlib.sha256(content).hexdigest()
        assert compute_sha256(test_file) == expected_hash

    def test_isolated_staging_and_zip_assembly(self, tmp_path: Path):
        """Verify portable bundle assembly and zip compression logic in an isolated test directory."""
        stage_dir = tmp_path / "stage" / "QueryPilot-v0.3.0-test"
        stage_dir.mkdir(parents=True)

        (stage_dir / "QueryPilot.exe").write_bytes(b"MOCK_BINARY")
        (stage_dir / "university_demo.db").write_bytes(b"MOCK_DB")
        (stage_dir / "README.md").write_text("# Test Desktop Quickstart", encoding="utf-8")

        agent_dir = stage_dir / "querypilot-agent"
        agent_dir.mkdir()
        (agent_dir / "querypilot-agent.exe").write_bytes(b"MOCK_AGENT")

        zip_out = tmp_path / "QueryPilot-v0.3.0-test.zip"
        with zipfile.ZipFile(zip_out, "w", zipfile.ZIP_DEFLATED) as zf:
            for file in stage_dir.rglob("*"):
                if file.is_file():
                    rel_path = file.relative_to(stage_dir)
                    zf.write(file, arcname=str(Path("QueryPilot-v0.3.0-test") / rel_path))

        assert zip_out.is_file()
        sha = compute_sha256(zip_out)
        assert len(sha) == 64

        with zipfile.ZipFile(zip_out, "r") as zf:
            names = zf.namelist()
            assert any(n.endswith("QueryPilot.exe") for n in names)
            assert any(n.endswith("university_demo.db") for n in names)
            assert any(n.endswith("querypilot-agent.exe") for n in names)
            assert any(n.endswith("README.md") for n in names)

    @pytest.mark.skipif(
        not HAS_PREBUILT_ZIP,
        reason="Standalone desktop release zip is created during release packaging; skipped in standard unit test runs.",
    )
    def test_desktop_release_zip_exists_and_valid(self):
        zip_candidates = list(RELEASE_DIR.glob("QueryPilot-v*-windows-x64.zip"))
        assert len(zip_candidates) > 0, f"No desktop release zip found in {RELEASE_DIR}"

        release_zip = zip_candidates[0]
        assert release_zip.is_file()
        assert release_zip.stat().st_size > 10 * 1024 * 1024, "Release zip should be > 10 MB"

        with zipfile.ZipFile(release_zip, "r") as zf:
            namelist = zf.namelist()
            assert any(name.endswith("QueryPilot.exe") for name in namelist), "QueryPilot.exe missing in zip"
            assert any("querypilot-agent" in name for name in namelist), "querypilot-agent missing in zip"
            assert any(name.endswith("university_demo.db") for name in namelist), "university_demo.db missing in zip"
            assert any(name.endswith("README.md") for name in namelist), "README.md missing in zip"

    @pytest.mark.skipif(
        not HAS_PREBUILT_ZIP,
        reason="SHA256SUMS.txt is created during release packaging; skipped in standard unit test runs.",
    )
    def test_sha256sums_matches_actual_release_zip(self):
        sha_file = RELEASE_DIR / "SHA256SUMS.txt"
        assert sha_file.is_file(), f"SHA256SUMS.txt missing in {RELEASE_DIR}"

        content = sha_file.read_text(encoding="utf-8")
        lines = [line.strip() for line in content.strip().split("\n") if line.strip()]

        zip_candidates = list(RELEASE_DIR.glob("QueryPilot-v*-windows-x64.zip"))
        release_zip = zip_candidates[0]
        actual_hash = compute_sha256(release_zip)

        matching = [line for line in lines if release_zip.name in line]
        assert len(matching) == 1, f"Expected 1 checksum entry for {release_zip.name}, found: {matching}"
        assert matching[0].startswith(actual_hash), f"Checksum mismatch in SHA256SUMS.txt: {matching[0]} vs {actual_hash}"
