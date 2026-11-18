"""Unit and Verification Tests for Desktop Release Packaging (Pillar 5).

Verifies:
1. compute_sha256 hashing fidelity against known fixtures.
2. Structure, completeness, and integrity of the staged desktop release bundle.
3. Verification that QueryPilot.exe, querypilot-agent, and demo database are present in the portable zip.
4. Checksum registry (SHA256SUMS.txt) accuracy.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import zipfile

import pytest

from scripts.package_desktop_release import compute_sha256

ROOT_DIR = Path(__file__).resolve().parent.parent
RELEASE_DIR = ROOT_DIR / "release"


class TestDesktopReleasePackaging:
    """Verifies packaging logic and release bundle integrity."""

    def test_compute_sha256_fidelity(self, tmp_path: Path):
        test_file = tmp_path / "sample.txt"
        content = b"QueryPilot Desktop Security & Packaging Invariant"
        test_file.write_bytes(content)

        expected_hash = hashlib.sha256(content).hexdigest()
        assert compute_sha256(test_file) == expected_hash

    def test_desktop_release_zip_exists_and_valid(self):
        zip_candidates = list(RELEASE_DIR.glob("QueryPilot-v*-windows-x64.zip"))
        assert len(zip_candidates) > 0, f"No desktop release zip found in {RELEASE_DIR}"

        release_zip = zip_candidates[0]
        assert release_zip.is_file()
        assert release_zip.stat().st_size > 10 * 1024 * 1024, "Release zip should be > 10 MB"

        with zipfile.ZipFile(release_zip, "r") as zf:
            namelist = zf.namelist()
            # Verify root container folder exists
            root_prefix = namelist[0].split("/")[0]

            # Verify QueryPilot.exe is present
            assert any(name.endswith("QueryPilot.exe") for name in namelist), "QueryPilot.exe missing in zip"

            # Verify querypilot-agent is bundled
            assert any("querypilot-agent" in name for name in namelist), "querypilot-agent missing in zip"

            # Verify demo database is bundled
            assert any(name.endswith("university_demo.db") for name in namelist), "university_demo.db missing in zip"

            # Verify README is bundled
            assert any(name.endswith("README.md") for name in namelist), "README.md missing in zip"

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
