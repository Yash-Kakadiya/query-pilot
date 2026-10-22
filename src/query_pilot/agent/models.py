"""Data models for QueryPilot Local Agent Daemon HTTP contracts.

Enforces narrow, typed contracts with extra='forbid' to prevent unvalidated
parameter injection, client overrides, or credential disclosure.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


class BootstrapRequest(BaseModel):
    """Payload submitted to POST /agent/v1/auth/bootstrap."""

    model_config = {"extra": "forbid"}

    bootstrap_token: str = Field(
        ...,
        min_length=16,
        max_length=128,
        description="Single-use bootstrap token read from the local agent terminal or ~/.query_pilot/agent.token.",
    )


class BootstrapResponse(BaseModel):
    """Response returned upon successful bootstrap authentication."""

    model_config = {"extra": "forbid"}

    status: str = Field(default="authenticated")
    message: str = Field(
        default="Bootstrap authentication successful. Ambient loopback session established."
    )


class LogoutResponse(BaseModel):
    """Response returned upon explicit session logout."""

    model_config = {"extra": "forbid"}

    status: str = Field(default="logged_out")
    message: str = Field(default="Session successfully terminated and invalidated.")


class RotateTokenResponse(BaseModel):
    """Response returned upon token rotation / revocation."""

    model_config = {"extra": "forbid"}

    status: str = Field(default="token_rotated")
    message: str = Field(
        default="All active sessions have been invalidated. A fresh single-use bootstrap token has been provisioned."
    )


class ProfileSummary(BaseModel):
    """Safe, redacted summary of an approved local database connection profile.

    Security Guarantee: Never exposes database credentials, passwords, raw
    connection strings, driver configurations, or server hostnames.
    """

    model_config = {"extra": "forbid"}

    profile_id: str = Field(description="Unique identifier of the approved connection profile.")
    display_name: str = Field(description="Human-readable display name for UI dropdown selection.")
    schema_name: Optional[str] = Field(
        default="dbo",
        description="Target relational schema name configured on the profile.",
    )
    timeout_seconds: int = Field(
        description="Statement execution timeout configured for this profile (clamped <= 15s)."
    )
    max_row_limit: int = Field(
        description="Maximum row limit configured for this profile (clamped <= 100 rows)."
    )
    has_allowlist: bool = Field(
        description="True if the profile enforces a table allowlist, False if discovery is unconstrained."
    )
    allowed_tables: Optional[List[str]] = Field(
        default=None,
        description="List of allowed table names, if an allowlist is configured.",
    )


class ProfileListResponse(BaseModel):
    """Response returned by GET /agent/v1/profiles."""

    model_config = {"extra": "forbid"}

    profiles: List[ProfileSummary] = Field(
        default_factory=list,
        description="List of safe, redacted profiles registered in the local agent ProfileStore.",
    )


class AgentErrorResponse(BaseModel):
    """Standardized error contract for local agent failures."""

    model_config = {"extra": "forbid"}

    error: str = Field(description="Machine-readable error category code.")
    detail: str = Field(description="Sanitized, human-readable description.")


class ExecuteQueryRequest(BaseModel):
    """Payload submitted to POST /agent/v1/profiles/{profile_id}/execute.

    Strictly forbids client-supplied overrides (database, host, timeout, max_rows, credentials).
    """

    model_config = {"extra": "forbid"}

    sql: str = Field(
        ...,
        min_length=1,
        max_length=10000,
        description="Read-only SQL query candidate to validate and execute.",
    )
    request_id: Optional[str] = Field(
        default=None,
        max_length=128,
        description="Optional client-supplied correlation or request ID.",
    )

    @field_validator("sql")
    @classmethod
    def validate_sql_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("SQL candidate query cannot be empty or whitespace only.")
        return v.strip()


class ExecuteQueryResponse(BaseModel):
    """Response returned upon successful query execution."""

    model_config = {"extra": "forbid"}

    columns: List[str] = Field(
        ...,
        description="List of returned column names preserving SELECT order.",
    )
    rows: List[Dict[str, Any]] = Field(
        ...,
        description="List of serialized rows as JSON-compatible dictionaries.",
    )
    row_count: int = Field(
        ...,
        description="Number of rows returned in this batch.",
    )
    truncated: bool = Field(
        ...,
        description="True if query results exceeded the effective row limit and were truncated.",
    )
    execution_time_ms: float = Field(
        ...,
        description="Server-side execution time in milliseconds.",
    )
    request_id: Optional[str] = Field(
        default=None,
        description="Optional client correlation ID echoed from request.",
    )
