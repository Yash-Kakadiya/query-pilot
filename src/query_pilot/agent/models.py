"""Data models for QueryPilot Local Agent Daemon HTTP contracts.

Enforces narrow, typed contracts with extra='forbid' to prevent unvalidated
parameter injection, client overrides, or credential disclosure.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from query_pilot.db.profile import (
    validate_local_server_target_policy,
    validate_safe_database_identifier,
    validate_safe_profile_id,
    validate_safe_schema_identifier,
)


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
    connection strings, driver configurations, server hostnames, or local database paths.
    """

    model_config = {"extra": "forbid"}

    profile_id: str = Field(description="Unique identifier of the approved connection profile.")
    display_name: str = Field(description="Human-readable display name for UI dropdown selection.")
    engine_type: str = Field(
        default="mssql",
        description="Database engine type: 'mssql' or 'sqlite'.",
    )
    database_name: Optional[str] = Field(
        default=None,
        description="Logical database catalog name (for MSSQL). Never contains local filesystem paths.",
    )
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
    is_default: bool = Field(
        default=False,
        description="True if this profile was designated as the default profile.",
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


class ValidateProfileConfigRequest(BaseModel):
    """Payload submitted to POST /agent/v1/profiles/validate-config for offline profile validation.

    Strictly forbids client-supplied credentials, unapproved remote targets, system databases,
    and unknown fields (extra='forbid').
    """

    model_config = {"extra": "forbid"}

    profile_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Unique identifier for the profile (letters, numbers, underscores, hyphens).",
    )
    display_name: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Human-readable label for UI selection.",
    )
    server: Optional[str] = Field(
        default=None,
        max_length=255,
        description="Local SQL Server instance target (e.g. 'localhost\\SQLEXPRESS' or '.\\SQLEXPRESS'). Required for mssql.",
    )
    database_name: Optional[str] = Field(
        default=None,
        max_length=128,
        description="Target database identifier. Required for mssql.",
    )
    database_path: Optional[str] = Field(
        default=None,
        description="Path to local SQLite database file. Required for sqlite.",
    )
    schema_name: Optional[str] = Field(
        default=None,
        max_length=128,
        description="Default relational schema name ('dbo' for mssql, 'main' for sqlite).",
    )
    driver: Optional[str] = Field(
        default=None,
        max_length=128,
        description="Installed ODBC driver name for SQL Server.",
    )
    engine_type: str = Field(
        default="mssql",
        description="Database engine type ('mssql' or 'sqlite').",
    )
    use_trusted_connection: Optional[bool] = Field(
        default=True,
        description="Must be True (Windows Integrated Authentication) for mssql.",
    )
    timeout_seconds: int = Field(
        default=15,
        ge=1,
        le=15,
        description="Statement execution timeout ceiling (1-15 seconds).",
    )
    max_row_limit: int = Field(
        default=100,
        ge=1,
        le=100,
        description="Maximum row limit ceiling (1-100 rows).",
    )
    allowed_tables: Optional[List[str]] = Field(
        default=None,
        description="Optional list of permitted table names.",
    )
    sensitive_columns: Optional[List[str]] = Field(
        default=None,
        description="Optional list of sensitive column names to redact.",
    )
    set_as_default: Optional[bool] = Field(
        default=False,
        description="Optionally designate this profile as the default. If False and no default exists, it becomes default automatically.",
    )

    @field_validator("profile_id")
    @classmethod
    def validate_pid(cls, v: str) -> str:
        return validate_safe_profile_id(v)

    @field_validator("display_name")
    @classmethod
    def validate_dname(cls, v: str) -> str:
        trimmed = v.strip()
        if not trimmed:
            raise ValueError("display_name cannot be blank or whitespace only.")
        return trimmed

    @field_validator("server")
    @classmethod
    def validate_srv(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            return validate_local_server_target_policy(v)
        return None

    @field_validator("database_name")
    @classmethod
    def validate_dbname(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            return validate_safe_database_identifier(v)
        return None

    @field_validator("schema_name")
    @classmethod
    def validate_sname(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            return validate_safe_schema_identifier(v)
        return None

    @field_validator("driver")
    @classmethod
    def validate_drv(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        trimmed = v.strip()
        if not trimmed:
            raise ValueError("driver cannot be blank or whitespace only.")
        if any(c in trimmed for c in (";", "=", "{", "}", "'", '"')):
            raise ValueError("driver cannot contain ODBC delimiter characters (; = { } ' \").")
        if any(ord(c) < 32 or ord(c) == 127 for c in trimmed):
            raise ValueError("driver cannot contain control characters.")
        return trimmed

    @field_validator("engine_type")
    @classmethod
    def validate_engine(cls, v: Optional[str]) -> str:
        if v is None:
            return "mssql"
        clean = v.strip().lower()
        if clean not in ("mssql", "sqlite"):
            raise ValueError(f"Unsupported engine_type '{v}'. QueryPilot supports 'mssql' and 'sqlite'.")
        return clean

    @field_validator("use_trusted_connection")
    @classmethod
    def validate_trusted_auth(cls, v: Optional[bool]) -> Optional[bool]:
        if v is False:
            raise ValueError(
                "SECURITY REJECTION: SQL-authenticated profiles (use_trusted_connection=False) are rejected. "
                "QueryPilot currently requires Windows Integrated Authentication (use_trusted_connection=True)."
            )
        return True

    @model_validator(mode="after")
    def validate_engine_and_fields(self) -> ValidateProfileConfigRequest:
        if self.engine_type == "mssql":
            if not self.server or not self.server.strip():
                raise ValueError("server must be specified for mssql profiles.")
            if not self.database_name or not self.database_name.strip():
                raise ValueError("database_name must be specified for mssql profiles.")
            if not self.driver or not self.driver.strip():
                self.driver = "ODBC Driver 17 for SQL Server"
            if not self.schema_name or not self.schema_name.strip():
                self.schema_name = "dbo"
            if not self.use_trusted_connection:
                raise ValueError(
                    "SECURITY REJECTION: SQL-authenticated profiles (use_trusted_connection=False) are rejected. "
                    "QueryPilot currently requires Windows Integrated Authentication (use_trusted_connection=True)."
                )
        elif self.engine_type == "sqlite":
            if not self.database_path or not self.database_path.strip():
                raise ValueError("database_path must be specified for sqlite profiles.")
            from query_pilot.db.sqlite_path import validate_sqlite_database_path
            self.database_path = validate_sqlite_database_path(self.database_path)
            self.server = None
            self.driver = None
            if not self.database_name or not self.database_name.strip():
                from pathlib import Path
                self.database_name = Path(self.database_path).stem
            if not self.schema_name or not self.schema_name.strip():
                self.schema_name = "main"
        return self

    @field_validator("allowed_tables")
    @classmethod
    def validate_tables(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is None:
            return None
        cleaned = []
        for t in v:
            t_clean = t.strip()
            if not t_clean:
                raise ValueError("allowed_tables cannot contain empty or blank table names.")
            if len(t_clean) > 128:
                raise ValueError(f"Table name '{t_clean}' exceeds maximum length of 128 characters.")
            if not re.match(r"^[a-zA-Z0-9_#$]+$", t_clean):
                raise ValueError(f"Table name '{t_clean}' contains invalid characters.")
            cleaned.append(t_clean)
        return cleaned

    @field_validator("sensitive_columns")
    @classmethod
    def validate_cols(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is None:
            return None
        cleaned = []
        for c in v:
            c_clean = c.strip()
            if not c_clean:
                raise ValueError("sensitive_columns cannot contain empty or blank column names.")
            if len(c_clean) > 128:
                raise ValueError(f"Column name '{c_clean}' exceeds maximum length of 128 characters.")
            if not re.match(r"^[a-zA-Z0-9_#$]+$", c_clean):
                raise ValueError(f"Column name '{c_clean}' contains invalid characters.")
            cleaned.append(c_clean)
        return cleaned


class ValidateProfileConfigResponse(BaseModel):
    """Response returned upon successful offline profile validation."""

    model_config = {"extra": "forbid"}

    valid: bool = Field(default=True, description="True if the configuration passed all validation checks.")
    message: str = Field(
        default="Profile configuration is valid and complies with security policies.",
        description="Human-readable validation confirmation message.",
    )


class CreateProfileRequest(ValidateProfileConfigRequest):
    """Payload submitted to POST /agent/v1/profiles for creating a new connection profile.

    Inherits strict validation: extra='forbid', loopback targets only, system database rejection,
    Windows Integrated Auth enforcement, and resource ceiling bounds.
    """

    set_as_default: bool = Field(
        default=False,
        description="Optionally designate this profile as the default. If False and no default exists, it becomes default automatically.",
    )


class CreateProfileResponse(BaseModel):
    """Response returned upon successful profile creation and persistence."""

    model_config = {"extra": "forbid"}

    profile: ProfileSummary = Field(
        description="Safe, redacted summary of the newly created profile."
    )
    is_default: bool = Field(
        description="True if this profile was designated as the default profile."
    )
    message: str = Field(
        default="Database connection profile created and registered successfully."
    )


class DeleteProfileResponse(BaseModel):
    """Response returned upon successful profile deletion."""

    model_config = {"extra": "forbid"}

    success: bool = Field(
        default=True,
        description="True if the profile was deleted successfully."
    )
    profile_id: str = Field(
        description="Identifier of the deleted profile."
    )
    message: str = Field(
        default="Database connection profile deleted successfully."
    )


class TestConnectionRequest(BaseModel):
    """Optional payload for POST /agent/v1/profiles/{profile_id}/test-connection.

    Strictly forbids any extra fields or client-supplied connection parameters.
    """

    __test__ = False
    model_config = {"extra": "forbid"}


class TestConnectionResponse(BaseModel):
    """Response returned by POST /agent/v1/profiles/{profile_id}/test-connection."""

    __test__ = False
    model_config = {"extra": "forbid"}

    connected: bool = Field(
        description="True if connection succeeded and database identity was verified; False otherwise."
    )
    database_name: Optional[str] = Field(
        default=None,
        description="Verified database name confirmed via SELECT DB_NAME(), or None on failure."
    )
    latency_ms: Optional[float] = Field(
        default=None,
        description="Round-trip connection and probe latency in milliseconds, if measurable."
    )
    message: str = Field(
        description="Sanitized human-readable diagnostic status message."
    )
    failure_code: Optional[str] = Field(
        default=None,
        description="Bounded diagnostic failure code (e.g. 'AUTH_FAILED', 'UNREACHABLE', 'TIMEOUT', 'DATABASE_MISMATCH', 'DRIVER_ERROR', 'CONNECTION_FAILED')."
    )


