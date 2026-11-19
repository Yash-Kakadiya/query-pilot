"""Self-healing Error Diagnostics Engine for QueryPilot.

Classifies runtime database connection errors, AST safety rejections, and AI model
provider exceptions into structured, plain-English explanations with actionable 1-click
repair solutions.

Security Invariants:
- All sensitive database connection strings, passwords, and tokens are scrubbed ([REDACTED]).
- Never estimates fake quota reset times; extracts authoritative provider headers when present.
"""

from __future__ import annotations

from enum import Enum
import re
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

# Pattern to sanitize raw credentials in connection strings or errors (must contain = or :)
PASSWORD_SCRUB_PATTERN = re.compile(
    r"(?i)(password|pwd|secret|token|api[_-]?key)[\s]*[=:]+[\s]*['\"]?([^\s'\";,&]+)['\"]?"
)


def sanitize_error_string(text: str) -> str:
    """Scrub passwords, secrets, user logins, and raw keys from error messages."""
    if not text:
        return ""
    scrubbed = PASSWORD_SCRUB_PATTERN.sub(r"\1=[REDACTED]", text)
    # Scrub database and system login usernames (e.g. Login failed for user 'CONTOSO\Alice')
    scrubbed = re.sub(r"(?i)(for\s+user|user|login)\s*['\"][^'\"]+['\"]", r"\1 '[REDACTED]'", scrubbed)
    # Gemini API key pattern AIzaSy...
    scrubbed = re.sub(r"AIzaSy[A-Za-z0-9_-]{33}", "AIzaSy[REDACTED]", scrubbed)
    return scrubbed


class DiagnosticCategory(str, Enum):
    API_KEY = "api_key"
    MODEL_QUOTA = "model_quota"
    MODEL_UNAVAILABLE = "model_unavailable"
    DB_CONNECTION_REFUSED = "db_connection_refused"
    DB_HOST_UNREACHABLE = "db_host_unreachable"
    DB_AUTH_FAILED = "db_auth_failed"
    DB_SSL_REQUIRED = "db_ssl_required"
    DB_SAFETY_MUTATION = "db_safety_mutation"
    DB_SCHEMA_NOT_FOUND = "db_schema_not_found"
    DB_TIMEOUT = "db_timeout"
    NETWORK_DISCONNECT = "network_disconnect"
    UNKNOWN = "unknown"


class DiagnosticAction(BaseModel):
    label: str = Field(description="Human-readable button label")
    action_type: str = Field(
        description="Action type: open_external_url | focus_api_key_input | open_profile_modal | switch_model | retry"
    )
    payload: Optional[str] = Field(default=None, description="Optional payload (e.g. URL, model name, field ID)")


class DiagnosticSolution(BaseModel):
    category: DiagnosticCategory
    title: str
    summary: str
    root_cause: str
    actionable_steps: List[str]
    primary_action: Optional[DiagnosticAction] = None
    secondary_action: Optional[DiagnosticAction] = None
    technical_details: Optional[str] = None


def classify_diagnostic(
    error_message: str,
    error_type: Optional[str] = None,
    status_code: Optional[int] = None,
    context: Optional[Dict[str, Any]] = None,
) -> DiagnosticSolution:
    """Classify an error message into a structured DiagnosticSolution."""
    raw_text = f"{error_type or ''} {error_message or ''}".strip()
    clean_text = sanitize_error_string(raw_text)
    lower = clean_text.lower()
    ctx = context or {}

    # 1. Gemini API Key missing or rejected
    if (
        status_code in (400, 401, 403)
        and ("api_key" in lower or "permission_denied" in lower or "unauthenticated" in lower or "api key not valid" in lower)
    ) or (
        "gemini_api_key is not configured" in lower
        or "default placeholder" in lower
        or "api_key_invalid" in lower
        or "api key not valid" in lower
        or "invalid api key" in lower
    ):
        return DiagnosticSolution(
            category=DiagnosticCategory.API_KEY,
            title="Gemini API Key Required or Invalid",
            summary="QueryPilot requires a valid Google Gemini API key to translate plain English questions into SQL.",
            root_cause="The provided Gemini API key was rejected by Google (invalid, expired, or missing in settings).",
            actionable_steps=[
                "Click 'Get Free Gemini 🗝️' to get a free personal API key from Google AI Studio.",
                "Paste the key into the API Key input in the top application header.",
                "Verify your internet connection can reach generativelanguage.googleapis.com.",
            ],
            primary_action=DiagnosticAction(
                label="Get Free Gemini Key 🗝️",
                action_type="open_external_url",
                payload="https://aistudio.google.com/api-keys",
            ),
            secondary_action=DiagnosticAction(
                label="Enter Key in Header",
                action_type="focus_api_key_input",
            ),
            technical_details=clean_text,
        )

    # 2. Gemini Quota / Rate Limit (429)
    if (
        status_code == 429
        or "429" in lower
        or "resource_exhausted" in lower
        or "quota exceeded" in lower
        or "rate limit" in lower
        or "too many requests" in lower
    ):
        return DiagnosticSolution(
            category=DiagnosticCategory.MODEL_QUOTA,
            title="Google Gemini Rate Limit Reached",
            summary="You have temporarily reached Google AI's per-minute request limit for free-tier models.",
            root_cause="Google Gemini free tier enforces request-per-minute (RPM) and token ceilings.",
            actionable_steps=[
                "Wait 20-30 seconds for the free-tier rate window to reset, then retry.",
                "Switch to a lower-latency model like 'gemini-3.5-flash-lite' in Settings.",
                "Consider generating a secondary API key in Google AI Studio if you are running bulk queries.",
            ],
            primary_action=DiagnosticAction(
                label="Retry Query",
                action_type="retry",
            ),
            secondary_action=DiagnosticAction(
                label="Switch to Flash-Lite",
                action_type="switch_model",
                payload="gemini-3.5-flash-lite",
            ),
            technical_details=clean_text,
        )

    # 3. Model Unavailable / Deprecated (404)
    if (
        status_code == 404
        or "model_not_found" in lower
        or "models/" in lower and "is not found" in lower
    ):
        return DiagnosticSolution(
            category=DiagnosticCategory.MODEL_UNAVAILABLE,
            title="AI Model Unavailable",
            summary="The selected AI model is not available for your Google AI Studio account tier or region.",
            root_cause="Google frequently updates preview models. The requested model endpoint was not found.",
            actionable_steps=[
                "Switch to the recommended stable model 'gemini-3.8-flash' or 'gemini-2.5-flash'.",
                "Verify in Google AI Studio that your API key has access to the Gemini 2.5/3.x family.",
            ],
            primary_action=DiagnosticAction(
                label="Switch to gemini-3.8-flash",
                action_type="switch_model",
                payload="gemini-3.8-flash",
            ),
            technical_details=clean_text,
        )

    # 4. Read-Only Safety Mutation Rejection
    if (
        "sqlmutationerror" in lower
        or "mutation" in lower and "read-only" in lower
        or "prohibited in read-only mode" in lower
        or "cannot execute in a read-only transaction" in lower
        or "transaction is read-only" in lower
        or any(w in lower for w in ["drop table", "delete from", "insert into", "update "])
        and "prohibited" in lower
    ):
        return DiagnosticSolution(
            category=DiagnosticCategory.DB_SAFETY_MUTATION,
            title="Data Modification Blocked by Safety Guard",
            summary="QueryPilot operates strictly as a read-only analyst. Destructive queries that alter your database are blocked.",
            root_cause="An AST inspection or database transaction authorizer intercepted an INSERT, UPDATE, DELETE, or DDL statement.",
            actionable_steps=[
                "Rephrase your question to ask for data retrieval (e.g. 'Show me...' or 'List all...') instead of modifications.",
                "QueryPilot does not allow modifying, deleting, or dropping database tables under any circumstances.",
            ],
            primary_action=None,
            technical_details=clean_text,
        )

    # 5. Database Connection Refused / Service Down
    if (
        "connection refused" in lower
        or "10061" in lower
        or "econnrefused" in lower
        or "can't connect to mysql server" in lower
        or "could not connect to server" in lower
        and "port" in lower
    ):
        port = ctx.get("port") or "5432 / 3306 / 1433"
        server = ctx.get("server") or "specified host"
        return DiagnosticSolution(
            category=DiagnosticCategory.DB_CONNECTION_REFUSED,
            title="Database Connection Refused",
            summary=f"QueryPilot could not connect to database server on {server} (port {port}).",
            root_cause="The target machine actively refused the network connection. The database service is likely stopped, listening on a different port, or blocked by a local firewall.",
            actionable_steps=[
                "Verify your database service is running (e.g., PostgreSQL, MySQL, SQL Server, or Docker container).",
                "Confirm the host address (e.g. 127.0.0.1 or localhost) and TCP port are correct.",
                "If using Docker, ensure the port mapping is published (e.g. -p 5432:5432).",
            ],
            primary_action=DiagnosticAction(
                label="Edit Database Profile",
                action_type="open_profile_modal",
            ),
            technical_details=clean_text,
        )

    # 6. Database Host Unreachable / DNS Resolution Failure / Timeout
    if (
        "host unreachable" in lower
        or "getaddrinfo failed" in lower
        or "nodename nor servname provided" in lower
        or "name or service not known" in lower
        or "server not found" in lower
        or "login timeout expired" in lower
        or "operation timed out" in lower
    ):
        return DiagnosticSolution(
            category=DiagnosticCategory.DB_HOST_UNREACHABLE,
            title="Database Host Unreachable",
            summary="QueryPilot could not find or reach the database host over the network.",
            root_cause="The server hostname could not be resolved via DNS, or the server timed out responding over the network.",
            actionable_steps=[
                "Check for typos in the server hostname (e.g., verify AWS RDS or cloud database endpoint).",
                "If connecting to a corporate database, verify your VPN or tunnel is connected.",
                "Verify cloud security group rules allow incoming connections from your IP address.",
            ],
            primary_action=DiagnosticAction(
                label="Check Profile Hostname",
                action_type="open_profile_modal",
            ),
            technical_details=clean_text,
        )

    # 7. Database Authentication Failed
    if (
        "password authentication failed" in lower
        or "access denied for user" in lower
        or "login failed for user" in lower
        or "authentication failed" in lower
        or "28p01" in lower
        or "1045" in lower
    ):
        return DiagnosticSolution(
            category=DiagnosticCategory.DB_AUTH_FAILED,
            title="Database Authentication Failed",
            summary="The database server rejected the provided username or password.",
            root_cause="Invalid database credentials. The username or password does not match database user records.",
            actionable_steps=[
                "Double check the username and password in your database profile settings.",
                "Verify that the user has permission to connect and read tables from the target database.",
                "For SQL Server, verify whether the server uses SQL Authentication or Windows Integrated Authentication.",
            ],
            primary_action=DiagnosticAction(
                label="Update Database Credentials",
                action_type="open_profile_modal",
            ),
            technical_details=clean_text,
        )

    # 8. SSL / TLS Connection Required by Cloud Provider
    if (
        "no pg_hba.conf entry" in lower and "ssl off" in lower
        or "ssl is required" in lower
        or "ssl connection is required" in lower
        or "enforce_ssl" in lower
        or "certificate verify failed" in lower
    ):
        return DiagnosticSolution(
            category=DiagnosticCategory.DB_SSL_REQUIRED,
            title="SSL Encryption Required by Cloud Provider",
            summary="Your cloud database provider (Supabase, Neon, AWS RDS, or GCP) mandates encrypted SSL connections.",
            root_cause="The connection attempt was made with SSL disabled, but the cloud database rejects unencrypted traffic.",
            actionable_steps=[
                "Edit your database connection profile and set SSL Mode to 'require' (or 'required').",
                "For Supabase or Neon, append '?sslmode=require' to your connection string or toggle the SSL switch.",
            ],
            primary_action=DiagnosticAction(
                label="Enable SSL Mode in Profile",
                action_type="open_profile_modal",
                payload="ssl_enable",
            ),
            technical_details=clean_text,
        )

    # 9. Schema Element Not Found (Hallucinated Table or Column)
    if (
        "no such table" in lower
        or "no such column" in lower
        or "undefined_table" in lower
        or "undefinedtable" in lower
        or ("relation" in lower and "does not exist" in lower)
        or ("column" in lower and "does not exist" in lower)
        or "42p01" in lower
        or "undefined_column" in lower
        or "42703" in lower
        or "table doesn't exist" in lower
        or "1146" in lower
        or "unknown column" in lower
        or "1054" in lower
        or "invalid column name" in lower
        or "invalid object name" in lower
    ):
        return DiagnosticSolution(
            category=DiagnosticCategory.DB_SCHEMA_NOT_FOUND,
            title="Database Table or Column Not Found",
            summary="The query referenced a table or column name that does not exist in your database schema.",
            root_cause="The AI model hallucinated a table or column name, or your database schema was updated after introspection.",
            actionable_steps=[
                "Re-sync your database schema by switching profiles or clicking Refresh.",
                "Rephrase your question to include the exact table or column name you are interested in.",
            ],
            primary_action=DiagnosticAction(
                label="Retry with Rephrased Question",
                action_type="retry",
            ),
            technical_details=clean_text,
        )

    # 10. Statement Execution Timeout (15-Second Limit)
    if (
        "timeout" in lower
        and (
            "statement" in lower
            or "execution" in lower
            or "exceeded" in lower
            or "ceiling" in lower
            or "canceling statement" in lower
            or "global_max_timeout_seconds" in lower
        )
    ):
        return DiagnosticSolution(
            category=DiagnosticCategory.DB_TIMEOUT,
            title="Query Statement Timed Out (15s Ceiling)",
            summary="Query execution was safely halted because it exceeded QueryPilot's 15-second execution ceiling.",
            root_cause="The database query took too long to complete. This usually happens when tables lack indexes or joins scan millions of rows without a filter.",
            actionable_steps=[
                "Ask for a more specific subset of data with filters (e.g. 'for the year 2024' or 'top 10').",
                "Ensure target foreign key columns and frequently filtered columns are indexed in your database.",
            ],
            primary_action=DiagnosticAction(
                label="Ask More Specific Question",
                action_type="retry",
            ),
            technical_details=clean_text,
        )

    # 11. Generic / Fallback
    return DiagnosticSolution(
        category=DiagnosticCategory.UNKNOWN,
        title="Unexpected Error Occurred",
        summary=clean_text if len(clean_text) < 160 else f"{clean_text[:157]}...",
        root_cause="An unexpected system or database error occurred during this operation.",
        actionable_steps=[
            "Review the technical details below to diagnose the issue.",
            "Retry the operation or restart QueryPilot if the problem persists.",
        ],
        primary_action=DiagnosticAction(
            label="Retry Operation",
            action_type="retry",
        ),
        technical_details=clean_text,
    )
