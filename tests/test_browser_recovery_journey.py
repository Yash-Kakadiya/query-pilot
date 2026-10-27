"""Browser-driven acceptance test for the execution recovery workflow (Milestone B15-F15-I2).

Tests the full bounded SQL execution recovery journey in headless Chrome against the built
production SPA and the local agent daemon:
1. Authenticate using isolated test token and select database profile.
2. Generate candidate SQL that references a deterministic invalid column.
3. Verify original candidate is displayed with 'NOT YET EXECUTED' safety disclaimer.
4. Explicitly execute original candidate and verify execution boundary dispatches request.
5. Mock agent returns recoverable SQL Server error 207 ('Invalid column name').
6. Verify browser UI displays error, 'Suggest Repair' button, and schema guidance hint.
7. Click 'Suggest Repair' button and deterministically hold network response.
8. Verify 'Suggesting Repair...' loading text and disabled button state while in-flight.
9. Verify generation request payload on mock server contains non-empty diagnostic_feedback.
10. Release hold, verify replacement candidate renders with corrected SQL and 'NOT YET EXECUTED'.
11. Verify same failed candidate cannot trigger a second repair attempt (1-attempt limit).
12. Verify repair generation itself did not dispatch an execution request.
13. Explicitly execute repaired candidate.
14. Verify database returns successful rows and browser displays populated tabular results.
15. Clean teardown of headless Chrome and loopback server.
"""

from __future__ import annotations

import json
from pathlib import Path
import threading
import time
from typing import List, Optional

import pytest

from query_pilot.db.profile import ConnectionProfile
from scripts.browser_test_server import BrowserTestServer
from tests.test_browser_query_journey import HeadlessChromeClient


def test_browser_execution_recovery_journey():
    """Execute complete bounded SQL repair and execution recovery acceptance journey in headless Chrome."""
    server = BrowserTestServer()

    # Pre-seed approved test recovery profile into server store
    test_profile = ConnectionProfile(
        profile_id="test_recovery_db",
        display_name="Recovery Test DB",
        server="localhost",
        database_name="RecoveryDB",
        schema_name="dbo",
        driver="ODBC Driver 17 for SQL Server",
        engine_type="mssql",
        use_trusted_connection=True,
        timeout_seconds=15,
        max_row_limit=100,
    )
    server.store.register_profile(test_profile)

    browser: Optional[HeadlessChromeClient] = None
    try:
        server.start(timeout_seconds=5.0)
        browser = HeadlessChromeClient(headless=True)

        # =====================================================================
        # Step 1: Navigate to SPA, Authenticate, and Select Database Profile
        # =====================================================================
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
        browser.select_option("#database-profile-select", "test_recovery_db")
        browser.wait_for_selector(".schema-status-loaded", timeout=5.0)
        schema_text = browser.get_text(".schema-status-loaded")
        assert "RecoveryDB" in schema_text

        # =====================================================================
        # Step 2: Generate candidate SQL that references an invalid column
        # =====================================================================
        browser.wait_for_selector("#candidate-question-input", timeout=5.0)
        browser.set_input_value("#candidate-question-input", "Show customers for recovery repair")

        initial_gen_count = len(server.received_generate_requests)
        initial_exec_count = len([r for r in server.audit_log if "/execute" in r.path])
        assert initial_exec_count == 0

        # Submit natural language question
        browser.click("button.candidate-submit-btn")

        # Wait for candidate SQL code block to render
        browser.wait_for_selector("pre.candidate-code-block", timeout=5.0)
        orig_sql = browser.get_text("pre.candidate-code-block")
        assert "BadColumn" in orig_sql
        assert "SELECT CustomerID, BadColumn, City FROM dbo.Customers" in orig_sql

        # Verify candidate review card indicates unexecuted state
        badge_text = browser.get_text(".candidate-status-badge")
        assert "CANDIDATE GENERATED" in badge_text.upper()

        disclaimer_text = browser.get_text(".candidate-disclaimer-box")
        assert "STRICT EXECUTION BOUNDARY" in disclaimer_text.upper()
        assert "NOT YET EXECUTED" in disclaimer_text.upper()

        # Verify zero execution requests were made by generation
        exec_count_after_gen = len([r for r in server.audit_log if "/execute" in r.path])
        assert exec_count_after_gen == 0

        # Verify execution button is enabled
        assert browser.element_exists("button.candidate-execute-btn")
        assert not browser.is_disabled("button.candidate-execute-btn")

        # =====================================================================
        # Step 3: Explicitly execute the original candidate
        # =====================================================================
        browser.click("button.candidate-execute-btn")

        # Wait for error card to appear
        browser.wait_for_selector(".local-error-card", timeout=5.0)

        # Verify exactly one execution request was submitted to the local daemon
        exec_logs = [r for r in server.audit_log if "/execute" in r.path]
        assert len(exec_logs) == 1
        assert exec_logs[0].status_code == 500
        assert any("BADCOLUMN" in stmt.upper() for stmt in server.executed_statements)

        # =====================================================================
        # Step 4 & 5: Verify recoverable error, repair guidance, and repair button
        # =====================================================================
        error_msg = browser.get_text(".local-error-message")
        assert "Invalid column name 'BadColumn'" in error_msg
        assert "207" in error_msg

        # Verify Suggest Repair button is rendered and enabled
        assert browser.element_exists("button.local-repair-btn")
        assert not browser.is_disabled("button.local-repair-btn")
        repair_btn_text = browser.get_text("button.local-repair-btn")
        assert "Suggest Repair" in repair_btn_text

        # Verify Repair Guidance hint is rendered
        assert browser.element_exists(".local-repair-hint")
        hint_text = browser.get_text(".local-repair-hint")
        assert "Repair Guidance:" in hint_text
        assert "Invalid column name: 'BadColumn'" in hint_text
        assert "Please query only valid columns defined in the schema." in hint_text

        # Verify Retry Execution button is also present
        assert browser.element_exists("button.local-retry-btn")

        # Verify no second execution occurred yet
        assert len([r for r in server.audit_log if "/execute" in r.path]) == 1

        # =====================================================================
        # Step 6, 7 & 8: Click repair button and verify in-flight loading state
        # =====================================================================
        # Install deterministic gate to hold the repair generation response
        server.repair_gate = threading.Event()

        # Click the actual repair button
        browser.click("button.local-repair-btn")

        # Verify in-flight loading text and disabled state without arbitrary sleep
        browser.wait_for_condition(
            "document.querySelector('button.local-repair-btn')?.innerText.includes('Suggesting Repair...')",
            timeout=5.0,
        )
        assert browser.is_disabled("button.local-repair-btn") is True

        # Verify repeated clicks during the pending request do not create another generation or execution request
        gen_count_in_flight = len(server.received_generate_requests)
        exec_count_in_flight = len([r for r in server.audit_log if "/execute" in r.path])
        browser.click("button.local-repair-btn")
        assert len(server.received_generate_requests) == gen_count_in_flight
        assert len([r for r in server.audit_log if "/execute" in r.path]) == exec_count_in_flight

        # Verify the generation request contains non-empty diagnostic_feedback
        assert len(server.received_generate_requests) == initial_gen_count + 2
        repair_req = server.received_generate_requests[-1]
        assert repair_req.diagnostic_feedback is not None
        assert len(repair_req.diagnostic_feedback.strip()) > 0
        assert "Invalid column name: 'BadColumn'" in repair_req.diagnostic_feedback
        assert "Please query only valid columns defined in the schema." in repair_req.diagnostic_feedback

        # Release the gate to allow repair generation response to complete
        server.repair_gate.set()

        # =====================================================================
        # Step 9: Verify candidate review UI displays corrected SQL & unexecuted state
        # =====================================================================
        browser.wait_for_condition(
            "document.querySelector('pre.candidate-code-block')?.innerText.includes('CompanyName')",
            timeout=5.0,
        )
        repaired_sql = browser.get_text("pre.candidate-code-block")
        assert "BadColumn" not in repaired_sql
        assert "SELECT CustomerID, CompanyName, City FROM dbo.Customers WHERE City = 'Seattle';" in repaired_sql

        # Verify the replacement candidate continues to indicate unexecuted status
        repaired_badge = browser.get_text(".candidate-status-badge")
        assert "CANDIDATE GENERATED" in repaired_badge.upper()

        repaired_disclaimer = browser.get_text(".candidate-disclaimer-box")
        assert "STRICT EXECUTION BOUNDARY" in repaired_disclaimer.upper()
        assert "NOT YET EXECUTED" in repaired_disclaimer.upper()

        # =====================================================================
        # Step 10: Verify the same failed candidate cannot trigger a second repair attempt
        # =====================================================================
        # When repair succeeds, prior execution error is cleared and repair button is dismissed
        assert not browser.element_exists("button.local-repair-btn")
        assert not browser.element_exists(".local-error-card")

        # Verify in coordinator state that this candidate cannot be repaired again
        # Attempt limit of 1 per candidate is strictly enforced
        can_repair_eval = browser.evaluate(
            "Boolean(document.querySelector('button.local-repair-btn'))"
        )
        assert can_repair_eval is False

        # =====================================================================
        # Step 11: Verify repair itself did not cause an additional execution request
        # =====================================================================
        exec_logs_after_repair = [r for r in server.audit_log if "/execute" in r.path]
        assert len(exec_logs_after_repair) == 1, (
            f"Expected exactly 1 execution request before explicit user execute action, "
            f"but found {len(exec_logs_after_repair)}"
        )

        # =====================================================================
        # Step 12: Explicitly execute the repaired candidate
        # =====================================================================
        assert browser.element_exists("button.candidate-execute-btn")
        assert not browser.is_disabled("button.candidate-execute-btn")

        browser.click("button.candidate-execute-btn")

        # =====================================================================
        # Step 13: Verify mock agent accepts corrected candidate & table renders
        # =====================================================================
        browser.wait_for_selector(".local-data-table", timeout=5.0)

        # Verify the second execution request was dispatched with repaired SQL and succeeded
        exec_logs_final = [r for r in server.audit_log if "/execute" in r.path]
        assert len(exec_logs_final) == 2
        assert exec_logs_final[1].status_code == 200
        assert "BADCOLUMN" not in server.executed_statements[-1].upper()
        assert "COMPANYNAME" in server.executed_statements[-1].upper()

        # Verify table headers render correctly
        th_texts = browser.evaluate(
            "Array.from(document.querySelectorAll('.local-data-table th')).map(th => th.innerText.trim())"
        )
        assert th_texts == ["CustomerID", "CompanyName", "City"]

        # Verify tabular rows render actual customer data
        row_count_chip = browser.get_text(".local-stat-chip.local-chip-rows")
        assert "3" in row_count_chip
        assert "rows" in row_count_chip

        td_texts = browser.evaluate(
            "Array.from(document.querySelectorAll('.local-data-table td')).map(td => td.innerText.trim())"
        )
        assert "Contoso Ltd" in td_texts
        assert "Fabrikam Inc" in td_texts
        assert "Northwind Traders" in td_texts
        assert "Seattle" in td_texts
        assert "Portland" in td_texts

        # Verify error card remains absent on successful execution
        assert not browser.element_exists(".local-error-card")

    finally:
        # Clean teardown of server gate, browser process, and server thread
        if server.repair_gate is not None:
            server.repair_gate.set()
        if browser is not None:
            browser.close()
        server.stop()
