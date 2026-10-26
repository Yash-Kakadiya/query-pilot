"""Browser-driven acceptance tests for the multi-turn QueryPilot journey (Milestone B12-F12C).

Tests the full, real multi-turn user journey inside headless Chrome against the built production SPA
and the local agent daemon:
1. Authenticate using isolated test token.
2. Select database profile and verify schema introspection.
3. Submit first natural-language question and verify candidate SQL generation.
4. Verify candidate 1 has NOT yet executed (audit log + DOM).
5. Explicitly execute candidate 1 on database and verify deterministic columns in result.
6. Verify conversation banner appears showing 1 prior turn in volatile memory.
7. Submit follow-up question (Turn 2).
8. Verify server received generation request containing turn 1 context (user question + result columns, zero SQL, zero rows).
9. Verify candidate 2 is strictly NOT yet executed until explicit user action.
10. Explicitly execute candidate 2 and verify context advances to 2 prior turns in memory.
11. Verify question modification triggers stale drift notice and blocks execution.
12. Click 'New Topic' reset button and verify conversation context, candidate, and results are cleared.
13. Submit a fresh question after reset and verify generation request sends empty conversation context.
14. Reliable offline teardown of browser and server.
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


def test_multi_turn_browser_query_journey():
    """Execute complete multi-turn QueryPilot journey with context accumulation and reset in headless Chrome."""
    server = BrowserTestServer()

    # Pre-seed approved test profile into server store
    test_profile = ConnectionProfile(
        profile_id="test_inv_db",
        display_name="Inventory Production DB",
        server="localhost",
        database_name="InventoryDB",
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

        # Step 1: Navigate to SPA
        browser.navigate(server.app_url)

        # Authenticate using server bootstrap token
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
        browser.select_option("#database-profile-select", "test_inv_db")
        browser.wait_for_selector(".schema-status-loaded", timeout=5.0)
        schema_text = browser.get_text(".schema-status-loaded")
        assert "InventoryDB" in schema_text

        # =====================================================================
        # Step 2: Turn 1 - Question 1 & Candidate Generation
        # =====================================================================
        browser.wait_for_selector("#candidate-question-input", timeout=5.0)

        # No conversation banner should exist initially
        assert not browser.element_exists(".candidate-conversation-banner")

        question_1 = "Show all customers in Seattle"
        browser.set_input_value("#candidate-question-input", question_1)
        browser.click("button.candidate-submit-btn")

        # Wait for candidate review panel to render
        browser.wait_for_selector(".candidate-review-panel", timeout=5.0)
        candidate_1_sql = browser.get_text(".candidate-code-block code")
        assert "Customers" in candidate_1_sql
        assert "Seattle" in candidate_1_sql

        # Verify Candidate 1 has NOT yet executed
        disclaimer_text = browser.get_text(".candidate-disclaimer-box")
        assert "NOT YET EXECUTED" in disclaimer_text

        audit_paths_after_gen1 = [r.path for r in server.audit_log]
        assert "/api/v1/generate-sql" in audit_paths_after_gen1
        assert "/agent/v1/profiles/test_inv_db/execute" not in audit_paths_after_gen1
        assert not browser.element_exists(".local-execution-card")

        # Server received generate request 1 without context
        assert len(server.received_generate_requests) == 1
        req1 = server.received_generate_requests[0]
        assert req1.question == question_1
        assert not req1.conversation_context

        # =====================================================================
        # Step 3: Turn 1 - Explicit Execution & Result Receipt
        # =====================================================================
        assert not browser.is_disabled("button.candidate-execute-btn")
        browser.click("button.candidate-execute-btn")

        # Wait for execution results table
        browser.wait_for_selector(".local-execution-card", timeout=5.0)
        browser.wait_for_selector("table.local-data-table", timeout=5.0)

        table_headers = browser.get_text("table.local-data-table thead")
        assert "CustomerID" in table_headers
        assert "CompanyName" in table_headers
        assert "City" in table_headers

        # Verify audit log recorded execution
        exec_indices = [i for i, r in enumerate(server.audit_log) if r.path == "/agent/v1/profiles/test_inv_db/execute"]
        assert len(exec_indices) == 1

        # =====================================================================
        # Step 4: Re-execution Duplicate Prevention Check
        # =====================================================================
        # The conversation banner must indicate 1 prior turn in memory
        browser.wait_for_selector(".candidate-conversation-banner", timeout=5.0)
        banner_text = browser.get_text(".candidate-conversation-banner")
        assert "Follow-up mode:" in banner_text
        assert "1 prior turn" in banner_text
        assert browser.element_exists(".candidate-reset-conversation-btn")

        # Re-execute the exact same candidate a second time (idempotent re-run)
        browser.click("button.candidate-execute-btn")
        browser.wait_for_selector("table.local-data-table", timeout=5.0)

        # Audit log has 2 executions, but conversation memory MUST remain strictly at 1 turn
        exec_indices_after_reexec = [i for i, r in enumerate(server.audit_log) if r.path == "/agent/v1/profiles/test_inv_db/execute"]
        assert len(exec_indices_after_reexec) == 2
        banner_text_after = browser.get_text(".candidate-conversation-banner")
        assert "1 prior turn" in banner_text_after
        assert "2 prior turns" not in banner_text_after

        # =====================================================================
        # Step 5: Turn 2 - Follow-Up Mode Display & Request Context
        # =====================================================================
        # Submit follow-up question (Turn 2)
        question_2 = "Show high value orders for these customers"
        browser.set_input_value("#candidate-question-input", question_2)
        browser.click("button.candidate-submit-btn")

        # Wait for Candidate 2 to render
        browser.wait_for_selector(".candidate-review-panel", timeout=5.0)
        candidate_2_sql = browser.get_text(".candidate-code-block code")
        assert "Orders" in candidate_2_sql

        # Verify Candidate 2 request sent Turn 1 context to the server
        assert len(server.received_generate_requests) == 2
        req2 = server.received_generate_requests[1]
        assert req2.question == question_2
        assert req2.conversation_context is not None
        assert len(req2.conversation_context) == 1

        turn_1_ctx = req2.conversation_context[0]
        assert turn_1_ctx.role == "user"
        assert turn_1_ctx.user_question == question_1
        assert "CustomerID" in turn_1_ctx.result_columns
        assert "CompanyName" in turn_1_ctx.result_columns
        assert "City" in turn_1_ctx.result_columns

        # Privacy invariant: zero raw SQL or row values in conversation turn
        raw_turn_dict = turn_1_ctx.model_dump()
        assert "candidate_sql" not in raw_turn_dict
        assert "sql" not in raw_turn_dict
        assert "rows" not in raw_turn_dict
        assert "row_data" not in raw_turn_dict

        # Verify Candidate 2 has NOT yet executed automatically (audit log has only the 2 prior Candidate 1 executions)
        assert len([r for r in server.audit_log if r.path == "/agent/v1/profiles/test_inv_db/execute"]) == 2

        # =====================================================================
        # Step 6: Turn 2 - Explicit Execution
        # =====================================================================
        browser.click("button.candidate-execute-btn")
        browser.wait_for_selector("table.local-data-table", timeout=5.0)

        # Confirm Orders table columns are rendered
        table_headers_2 = browser.get_text("table.local-data-table thead")
        assert "OrderID" in table_headers_2
        assert "TotalAmount" in table_headers_2

        # Verify conversation banner updates to show 2 prior turns
        browser.wait_for_condition(
            "document.querySelector('.candidate-conversation-banner') && document.querySelector('.candidate-conversation-banner').textContent.includes('2 prior turns')",
            timeout=5.0,
        )

        # =====================================================================
        # Step 6: Meaningful Stale Operation Protection
        # =====================================================================
        # Editing question after candidate generation triggers stale banner and disables execution
        browser.set_input_value("#candidate-question-input", "Show high value orders sorted by total")
        browser.wait_for_selector(".candidate-stale-banner", timeout=5.0)
        assert browser.is_disabled("button.candidate-execute-btn")

        # =====================================================================
        # Step 7: Exercise 'New Topic' Reset
        # =====================================================================
        assert browser.element_exists(".candidate-reset-conversation-btn")
        browser.click(".candidate-reset-conversation-btn")

        # Context banner must be removed upon reset
        browser.wait_for_condition(
            "document.querySelector('.candidate-conversation-banner') === null",
            timeout=5.0,
        )
        assert not browser.element_exists(".candidate-conversation-banner")
        assert not browser.element_exists(".local-execution-card")

        # =====================================================================
        # Step 8: Post-Reset Clean Topic Generation
        # =====================================================================
        fresh_question = "Show all customers in Seattle"
        browser.set_input_value("#candidate-question-input", fresh_question)
        browser.click("button.candidate-submit-btn")

        browser.wait_for_selector(".candidate-review-panel", timeout=5.0)

        # Verify Request 3 sent after reset has strictly empty conversation context
        assert len(server.received_generate_requests) == 3
        req3 = server.received_generate_requests[2]
        assert req3.question == fresh_question
        assert not req3.conversation_context

    finally:
        if browser:
            browser.close()
        server.stop()
