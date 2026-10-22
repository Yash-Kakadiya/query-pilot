"""Documentation and Release-Claim Alignment Tests (Milestone B12-F1A).

Verifies that repository documentation strictly reflects the verified implementation,
configuration defaults, hard safety ceilings, model integration status, and preview boundaries
established in the B12-F0 release evidence reconciliation audit.
"""

from pathlib import Path
import re
import pytest

from query_pilot.agent.cli import build_parser
from query_pilot.agent.config import AgentConfig
from query_pilot.config import get_settings
from query_pilot.db.profile import GLOBAL_MAX_ROW_LIMIT, GLOBAL_MAX_TIMEOUT_SECONDS


REPO_ROOT = Path(__file__).resolve().parent.parent
README_PATH = REPO_ROOT / "README.md"
DOCS_DIR = REPO_ROOT / "docs"
CONFIG_DOC_PATH = DOCS_DIR / "configuration.md"
DEPLOY_DOC_PATH = DOCS_DIR / "deployment.md"
API_DOC_PATH = DOCS_DIR / "api.md"


@pytest.fixture
def readme_text() -> str:
    assert README_PATH.is_file(), f"Missing {README_PATH}"
    return README_PATH.read_text(encoding="utf-8")


@pytest.fixture
def config_doc_text() -> str:
    assert CONFIG_DOC_PATH.is_file(), f"Missing {CONFIG_DOC_PATH}"
    return CONFIG_DOC_PATH.read_text(encoding="utf-8")


@pytest.fixture
def deploy_doc_text() -> str:
    assert DEPLOY_DOC_PATH.is_file(), f"Missing {DEPLOY_DOC_PATH}"
    return DEPLOY_DOC_PATH.read_text(encoding="utf-8")


@pytest.fixture
def api_doc_text() -> str:
    assert API_DOC_PATH.is_file(), f"Missing {API_DOC_PATH}"
    return API_DOC_PATH.read_text(encoding="utf-8")


# ==============================================================================
# 1. Verification of Code Safety Limits against Constants
# ==============================================================================


def test_code_constants_and_defaults(tmp_path: Path):
    """Verify that Python code constants reflect the verified safety limits."""
    # Hard database execution ceilings
    assert GLOBAL_MAX_TIMEOUT_SECONDS == 15
    assert GLOBAL_MAX_ROW_LIMIT == 100

    # Agent configuration defaults & ceilings
    default_agent = AgentConfig(
        bind_host="127.0.0.1",
        token_dir=tmp_path / "tokens",
        profiles_file=tmp_path / "profiles.json",
    )
    assert default_agent.max_request_bytes == 65536
    assert default_agent.upstream_timeout_seconds == 30.0

    # AgentConfig max_request_bytes bound: 1 MiB (1048576)
    max_bytes_field = AgentConfig.model_fields["max_request_bytes"]
    assert any(getattr(m, "le", None) == 1048576 for m in max_bytes_field.metadata)

    # API default request bytes
    api_settings = get_settings()
    assert api_settings.QUERY_PILOT_MAX_REQUEST_BYTES == 65536


# ==============================================================================
# 2. Documentation Safety Limit Verification
# ==============================================================================


def test_readme_documents_hard_execution_ceilings(readme_text: str):
    """Verify README documents the 15s timeout and 100 row ceiling."""
    assert "15-second statement timeout" in readme_text or "15 seconds" in readme_text
    assert "100-row result limit" in readme_text or "100 rows" in readme_text
    assert "GLOBAL_MAX_TIMEOUT_SECONDS = 15" in readme_text
    assert "GLOBAL_MAX_ROW_LIMIT = 100" in readme_text


def test_config_doc_documents_hard_execution_ceilings(config_doc_text: str):
    """Verify docs/configuration.md documents the 15s and 100 row hard ceilings."""
    assert "GLOBAL_MAX_TIMEOUT_SECONDS = 15" in config_doc_text
    assert "GLOBAL_MAX_ROW_LIMIT = 100" in config_doc_text
    assert "15-second statement timeout" in config_doc_text or "hard-clamped to 15s" in config_doc_text
    assert "100-row result limit" in config_doc_text or "hard-clamped to 100" in config_doc_text


def test_api_doc_documents_execution_ceilings(api_doc_text: str):
    """Verify docs/api.md documents the 15s and 100 row ceilings."""
    assert "15-second statement timeout" in api_doc_text or "15s" in api_doc_text
    assert "100-row result limit" in api_doc_text or "100 rows" in api_doc_text


def test_docs_do_not_claim_erroneous_limits(readme_text: str, config_doc_text: str, api_doc_text: str):
    """Verify that documentation does NOT state SQL execution default is 30s or 1,000 rows."""
    # Check for erroneous SQL timeout phrasing
    erroneous_sql_timeout = re.compile(r"SQL(?:\s+\w+){0,3}\s+timeout(?:\s+\w+){0,3}\s+(?:default:?\s*)?30\s*s", re.IGNORECASE)
    assert not erroneous_sql_timeout.search(readme_text)
    assert not erroneous_sql_timeout.search(config_doc_text)

    # Check for erroneous local query 1000-row phrasing
    erroneous_row_limit = re.compile(r"local(?:\s+\w+){0,3}\s+(?:row\s+limit|max_rows)(?:\s+\w+){0,3}\s+(?:default:?\s*)?1000", re.IGNORECASE)
    assert not erroneous_row_limit.search(readme_text)
    assert not erroneous_row_limit.search(config_doc_text)


def test_docs_distinguish_upstream_proxy_from_sql_timeout(readme_text: str, config_doc_text: str, api_doc_text: str):
    """Verify documentation distinguishes 30s upstream HTTP proxy timeout from 15s SQL execution timeout."""
    assert "upstream_timeout_seconds" in config_doc_text
    assert "distinct from SQL statement execution timeout" in config_doc_text or "distinct from" in config_doc_text
    assert "Distinct Proxy vs. SQL Timeouts" in readme_text
    assert "distinct from database statement execution timeouts" in api_doc_text or "distinct from" in api_doc_text


def test_docs_specify_request_payload_bounds(readme_text: str, config_doc_text: str, api_doc_text: str):
    """Verify documentation specifies 64 KB (65,536 bytes) default and 1 MiB ceiling."""
    assert "65,536" in readme_text or "65536" in readme_text
    assert "65536" in config_doc_text
    assert "1 MiB" in readme_text or "1048576" in readme_text
    assert "1 MiB" in config_doc_text or "1048576" in config_doc_text


# ==============================================================================
# 3. Model-Integration & Testing Evidence Alignment
# ==============================================================================


def test_docs_accurately_state_mocked_model_generation(readme_text: str, config_doc_text: str):
    """Verify documentation acknowledges that B11/B12 journeys used mocked model generation."""
    assert "mocked model generation" in readme_text.lower() or "mocked" in readme_text.lower()
    assert "mocked model generation" in config_doc_text.lower() or "mocked" in config_doc_text.lower()


def test_docs_state_live_gemini_unverified_end_to_end(readme_text: str, config_doc_text: str, api_doc_text: str):
    """Verify documentation explicitly states live Gemini generation is unverified end-to-end."""
    assert "unverified" in readme_text.lower()
    assert "unverified" in config_doc_text.lower()
    assert "unverified" in api_doc_text.lower()


def test_docs_clarify_offline_tests_boundary(readme_text: str, config_doc_text: str):
    """Verify documentation states offline tests do not establish live Gemini accuracy/latency."""
    assert "offline test" in readme_text.lower()
    assert "offline test" in config_doc_text.lower()


def test_docs_state_sqlserver_testing_history(readme_text: str, config_doc_text: str):
    """Verify documentation notes live SQL Server testing occurred in earlier milestones (B9, B11)."""
    assert "b9" in readme_text.lower() and "b11" in readme_text.lower()
    assert "b9" in config_doc_text.lower() and "b11" in config_doc_text.lower()


# ==============================================================================
# 4. Installation, Preview Status, and CLI-Only Profile Setup
# ==============================================================================


def test_docs_specify_python_version_requirement(readme_text: str):
    """Verify documentation specifies Python >=3.12."""
    assert ">=3.12" in readme_text


def test_docs_distinguish_controlled_preview_from_public_release(readme_text: str, config_doc_text: str):
    """Verify documentation distinguishes controlled preview from public release."""
    assert "Controlled Preview" in readme_text
    assert "NO-GO" in readme_text or "BLOCKED" in readme_text
    assert "Controlled Preview" in config_doc_text
    assert "NO-GO" in config_doc_text or "BLOCKED" in config_doc_text


def test_docs_note_wheel_smoke_test_and_clean_install_status(readme_text: str):
    """Verify documentation records --no-deps wheel smoke test and unverified clean install."""
    assert "--no-deps" in readme_text
    assert "clean dependency-resolved" in readme_text.lower() or "fresh installation" in readme_text.lower()


def test_docs_state_no_browser_profile_wizard(readme_text: str, config_doc_text: str):
    """Verify documentation clarifies that profile creation is CLI-based with no browser wizard."""
    assert "no browser-based profile creation wizard" in readme_text.lower() or "no browser" in readme_text.lower()
    assert "no browser-based profile creation wizard" in config_doc_text.lower() or "no browser" in config_doc_text.lower()


def test_docs_state_loopback_only(readme_text: str, config_doc_text: str):
    """Verify documentation mandates loopback-only binding."""
    assert "127.0.0.1" in readme_text
    assert "127.0.0.1" in config_doc_text


# ==============================================================================
# 5. CLI Options Alignment
# ==============================================================================


def test_documented_cli_options_match_parser(readme_text: str, config_doc_text: str):
    """Verify that CLI options documented in README and configuration match build_parser()."""
    parser = build_parser()
    actions = {act.dest: act for act in parser._actions}

    # Root options
    assert "host" in actions
    assert "port" in actions
    assert "allow_dev_origin" in actions
    assert "static_dir" in actions
    assert "upstream_api_url" in actions
    assert "token_dir" in actions
    assert "profiles_file" in actions
    assert "open_browser" in actions

    # Subcommands
    subparser_act = next(act for act in parser._actions if act.dest == "subcommand")
    assert "profile" in subparser_act.choices
    profile_subparser = subparser_act.choices["profile"]
    profile_action = next(act for act in profile_subparser._actions if act.dest == "profile_action")
    assert "add" in profile_action.choices
    assert "list" in profile_action.choices
    assert "validate" in profile_action.choices

    # Verify options appear in README
    for opt in ["--host", "--port", "--static-dir", "--upstream-api-url", "--token-dir", "--profiles-file"]:
        assert opt in readme_text, f"Missing {opt} in README"
        assert opt in config_doc_text, f"Missing {opt} in docs/configuration.md"
