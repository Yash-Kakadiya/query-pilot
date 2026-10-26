"""Browser-driven acceptance tests for the clarification resolution workflow (Milestone B13-F13B).

Tests the full clarification workflow in headless Chrome against the built production SPA
and the local agent daemon:
1. Authenticate using isolated test token and select database profile.
2. Submit ambiguous question triggering clarification_needed status with structured options.
3. Verify clarification card renders prompt and accessible native option buttons.
4. Verify clarification state hides misleading candidate-generated and execution disclaimers.
5. Click option with suggested_question -> question updates to suggested query.
6. Verify option selection alone makes zero network generation requests and zero execution requests.
7. Click option without suggested_question -> keeps original base question, does not accumulate text.
8. User edits the clarified question and explicitly clicks 'Generate Candidate SQL'.
9. Verify generation request is dispatched with user's edited question and returns candidate SQL.
10. Verify candidate SQL renders with 'NOT YET EXECUTED' safety disclaimer and execute button.
11. Explicitly execute query and verify result table renders with column headers.
12. Reliable offline teardown of browser and server.
"""

from __future__ import annotations

import json
from pathlib import Path
import time
from typing import List

import pytest

from query_pilot.db.profile import ConnectionProfile
from scripts.browser_test_server import BrowserTestServer
from tests.test_browser_query_journey import HeadlessChromeClient


def test_clarification_workflow_browser_acceptance_journey():
    """Execute complete clarification workflow journey in headless Chrome."""
    server = BrowserTestServer()

    # Pre-seed approved test profile into server store
    test_profile = ConnectionProfile(
        profile_id="test_clar_db",
        display_name="Clarification Test DB",
        server="localhost",
        database_name="ClarificationDB",
        schema_name="dbo",
        driver="ODBC Driver 17 for SQL Server",
        engine_type="mssql",
        use_trusted_connection=True,
        timeout_seconds=15,
        max_row_limit=100,
    )
    server.store.register_profile(test_profile)

    browser = None
    try:
        server.start(timeout_seconds=5.0)
        browser = HeadlessChromeClient(headless=True)

        # Step 1: Navigate to SPA and Authenticate
        browser.navigate(server.app_url)

        browser.wait_for_condition(
            "document.querySelector('#bootstrap-token-input') !== null || document.querySelector('.agent-auth-btn') !== null",
            timeout=5.0,
        )
        if not browser.element_exists("#bootstrap-token-input"):
            browser.click(".agent-auth-btn")

        browser.wait_for_selector("#bootstrap-token-input", timeout=5.0)
        browser.set_input_value("#bootstrap-token-input", server.bootstrap_token)
        browser.evaluate("document.querySelector('form.agent-auth-form').requestSubmit()")

        # Wait for profile select to become available
        browser.wait_for_selector("#database-profile-select", timeout=10.0)
        assert "Connected" in browser.get_text(".agent-session-text")

        # Select database profile and confirm schema
        browser.select_option("#database-profile-select", "test_clar_db")
        browser.wait_for_selector(".schema-status-loaded", timeout=5.0)
        schema_text = browser.get_text(".schema-status-loaded")
        assert "ClarificationDB" in schema_text

        # =====================================================================
        # Step 2: Submit ambiguous question to trigger clarification_needed
        # =====================================================================
        browser.wait_for_selector("#candidate-question-input", timeout=5.0)
        initial_question = "Show customers clarification"
        browser.set_input_value("#candidate-question-input", initial_question)

        # Record generate request count before submission
        initial_gen_count = len(server.received_generate_requests)

        # Submit question
        browser.click("button.candidate-submit-btn")

        # Step 3: Verify clarification card and options rendered
        browser.wait_for_selector(".candidate-outcome-clarification", timeout=5.0)
        clarification_card_text = browser.get_text(".candidate-outcome-clarification")
        assert "Clarification Requested:" in clarification_card_text
        assert "Please specify whether you want active or all customers." in clarification_card_text

        # Verify badge status
        badge_text = browser.get_text(".candidate-badge-clarification")
        assert "CLARIFICATION NEEDED" in badge_text.upper()

        # Verify options are rendered as native buttons
        assert browser.element_exists("button.candidate-clarification-option-btn")
        option_buttons = browser.evaluate(
            "Array.from(document.querySelectorAll('button.candidate-clarification-option-btn')).map(b => b.innerText)"
        )
        assert len(option_buttons) == 2

        # Verify option 1 (opt_active with suggested_question)
        assert "opt_active" in option_buttons[0]
        assert "Active customers with orders in the last 90 days" in option_buttons[0]
        assert "Show active customers with recent orders" in option_buttons[0]

        # Verify option 2 (opt_seattle without suggested_question)
        assert "opt_seattle" in option_buttons[1]
        assert "All registered customers located in Seattle" in option_buttons[1]

        # Requirement 9: Verify clarification state does NOT show candidate SQL or execution disclaimer
        assert not browser.element_exists(".candidate-code-container")
        assert not browser.element_exists(".candidate-disclaimer-box")
        assert not browser.element_exists(".candidate-execute-btn")

        # Record request counts before clicking options
        gen_count_after_first_submit = len(server.received_generate_requests)
        assert gen_count_after_first_submit == initial_gen_count + 1

        # Verify audit log recorded no execution before click
        exec_records_before = [
            r for r in server.audit_log if "/execute" in r.path
        ]
        assert len(exec_records_before) == 0

        # =====================================================================
        # Step 4: Click option 1 (has suggested_question)
        # =====================================================================
        # Click opt_active button
        browser.evaluate(
            "document.querySelectorAll('button.candidate-clarification-option-btn')[0].click()"
        )

        # Verify question input was updated with suggested_question
        updated_q1 = browser.evaluate("document.querySelector('#candidate-question-input').value")
        assert updated_q1 == "Show active customers with recent orders"

        # Verify clarification-specific stale banner copy
        assert browser.element_exists(".candidate-stale-banner")
        stale_banner_text = browser.get_text(".candidate-stale-banner")
        assert "Question updated from clarification" in stale_banner_text
        assert "Generate again to refresh the candidate before executing" not in stale_banner_text

        # Requirement 5: Selection alone makes NO new generation request and NO execution request
        assert len(server.received_generate_requests) == gen_count_after_first_submit
        exec_records_after_opt1 = [
            r for r in server.audit_log if "/execute" in r.path
        ]
        assert len(exec_records_after_opt1) == 0

        # =====================================================================
        # Step 5: Click option 2 (without suggested_question) -> tests re-selection
        # =====================================================================
        # Click opt_seattle button
        browser.evaluate(
            "document.querySelectorAll('button.candidate-clarification-option-btn')[1].click()"
        )

        # Requirement 3 & 4: Derived from base question, does NOT accumulate prior option text or parentheticals
        updated_q2 = browser.evaluate("document.querySelector('#candidate-question-input').value")
        assert updated_q2 == initial_question
        assert "Show active customers with recent orders" not in updated_q2
        assert "(" not in updated_q2
        assert ")" not in updated_q2

        # Still no automatic generation or execution
        assert len(server.received_generate_requests) == gen_count_after_first_submit
        assert len([r for r in server.audit_log if "/execute" in r.path]) == 0

        # =====================================================================
        # Step 6: User edits the question and explicitly submits
        # =====================================================================
        user_refined_question = "Show customers in Seattle with active status"
        browser.set_input_value("#candidate-question-input", user_refined_question)

        # Explicit submission
        browser.click("button.candidate-submit-btn")

        # Requirement 7: Explicit submission triggers candidate generation request
        browser.wait_for_condition(
            f"document.querySelector('.candidate-code-block') !== null",
            timeout=5.0,
        )
        assert len(server.received_generate_requests) == gen_count_after_first_submit + 1
        last_request = server.received_generate_requests[-1]
        assert last_request.question == user_refined_question

        # Requirement 8 & 9: Candidate arrives, SQL block and execution disclaimer now appear
        assert browser.element_exists(".candidate-code-container")
        assert browser.element_exists(".candidate-disclaimer-box")
        disclaimer_text = browser.get_text(".candidate-disclaimer-box")
        assert "STRICT EXECUTION BOUNDARY" in disclaimer_text
        assert "NOT YET EXECUTED" in disclaimer_text

        candidate_sql = browser.get_text(".candidate-code-block code")
        assert "SELECT" in candidate_sql
        assert "Customers" in candidate_sql

        # Execution has NOT happened yet
        assert len([r for r in server.audit_log if "/execute" in r.path]) == 0

        # =====================================================================
        # Step 7: Explicitly execute approved query on database
        # =====================================================================
        browser.wait_for_selector(".candidate-execute-btn", timeout=5.0)
        assert not browser.is_disabled(".candidate-execute-btn")
        browser.click(".candidate-execute-btn")

        # Wait for local execution results
        browser.wait_for_selector(".local-execution-card", timeout=5.0)
        browser.wait_for_selector("table.local-data-table", timeout=5.0)
        table_text = browser.get_text("table.local-data-table")
        assert "CustomerID" in table_text
        assert "CompanyName" in table_text

        # Verify audit log recorded execution
        exec_records = [r for r in server.audit_log if "/execute" in r.path]
        assert len(exec_records) == 1
        assert exec_records[0].status_code == 200

        # Verify conversation context advanced to 1 prior turn
        browser.wait_for_selector(".candidate-conversation-banner", timeout=5.0)
        banner_text = browser.get_text(".candidate-conversation-banner")
        assert "1 prior turn" in banner_text

    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        server.stop()
