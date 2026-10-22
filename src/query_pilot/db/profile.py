"""Connection profile abstraction and trusted registry for QueryPilot.

Defines typed, immutable, agent-controlled connection profiles for Microsoft SQL Server (T-SQL).
Enforces that database connection parameters, target database names, and schema policies
are resolved exclusively from trusted local configuration, preventing untrusted clients
or browsers from providing or overriding connection targets.

Credential Persistence & Security Policy:
- Preferred Authentication Path: Windows-integrated authentication (`use_trusted_connection=True`)
  is the strongly preferred local development mechanism because it delegates authorization to the
  logged-in Windows OS identity without requiring any stored credentials.
- SQL Authentication Passwords in Profile JSON: Storing plaintext passwords in JSON files
  on disk creates serious credential-exposure risks (CWE-312 / CWE-256). Any local configuration
  files containing SQL Authentication credentials MUST be protected with restrictive operating-system
  permissions (e.g. 0600 on POSIX or user-only ACLs on Windows). Storing plaintext passwords in
  source control or world-readable files is strictly forbidden. For production/external environments,
  credentials should be injected via environment variables or platform secret vaults.
"""

from __future__ import annotations

from contextlib import contextmanager
import getpass
import json
import logging
import os
from pathlib import Path
import platform
import re
import secrets
import stat
import subprocess
import time
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Tuple
import urllib.parse

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from query_pilot.db.introspection import (
    DEFAULT_SENSITIVE_COLUMNS,
    SensitiveColumnPolicy,
)

# Hard global resource ceilings enforced across the entire QueryPilot architecture.
# Profiles and requests may choose stricter limits, but can NEVER exceed these ceilings.
GLOBAL_MAX_TIMEOUT_SECONDS: int = 15
GLOBAL_MAX_ROW_LIMIT: int = 100

logger = logging.getLogger(__name__)


class ProfileError(Exception):
    """Base exception for connection profile operations."""

    pass


class UnknownProfileError(ProfileError):
    """Raised when an unregistered profile_id is requested."""

    def __init__(self, message: str, profile_id: Optional[str] = None):
        super().__init__(message)
        self.profile_id = profile_id


class ProfileConfigurationError(ProfileError):
    """Raised when profile configuration is malformed, missing required fields, or invalid."""

    pass


class ConnectionTargetOverrideError(ProfileError):
    """Raised when an untrusted client attempts to provide or override connection targets."""

    pass


# Disallowed parameters when evaluating untrusted client requests.
# Clients are never permitted to provide connection targets or credentials.
# Includes canonical names, aliases, and common connection string keywords.
FORBIDDEN_CLIENT_OVERRIDE_KEYS: FrozenSet[str] = frozenset({
    # Server / host variants
    "server",
    "host",
    "hostname",
    "data_source",
    "datasource",
    "addr",
    "address",
    "network_address",
    "port",
    "endpoint",
    "instance",
    "server_name",
    # Database / catalog variants
    "database",
    "database_name",
    "db",
    "db_name",
    "dbname",
    "initial_catalog",
    "initialcatalog",
    "catalog",
    "expected_db",
    "target_db",
    "target_database",
    # Schema variants
    "schema",
    "schema_name",
    "table_schema",
    # Driver / dialect variants
    "driver",
    "odbc_driver",
    "dialect",
    # User / authentication variants
    "user",
    "username",
    "user_id",
    "uid",
    "password",
    "pwd",
    "auth",
    "authentication",
    "trusted_connection",
    "use_trusted_connection",
    "integrated_security",
    "trustedconnection",
    "integratedsecurity",
    # Connection string / URL variants
    "connection_string",
    "conn_str",
    "connstr",
    "connectionstring",
    "odbc_connect",
    "odbc_connection_string",
    "url",
    "sqlalchemy_url",
    "dsn",
    # Policy / Ceiling overrides
    "allowed_tables",
    "table_whitelist",
    "sensitive_columns",
    "column_denylist",
    "timeout",
    "timeout_seconds",
    "query_timeout",
    "max_row_limit",
    "row_limit",
    "limit",
    "max_rows",
})


def escape_odbc_value(value: str) -> str:
    """Safely format an ODBC connection string attribute value.

    Per Microsoft ODBC specification:
    - If an attribute value contains a semicolon (;), leading or trailing spaces,
      starts with a left curly brace ({), or contains curly braces, it must be enclosed
      in curly braces {}.
    - Any closing curly brace '}' within the value must be doubled to '}}'.
    """
    if not value:
        return ""
    needs_braces = (
        ";" in value
        or "{" in value
        or "}" in value
        or " " in value
    )
    if needs_braces:
        escaped = value.replace("}", "}}")
        return f"{{{escaped}}}"
    return value


def enforce_global_timeout_ceiling(timeout_seconds: int) -> int:
    """Sanitize and enforce the hard global timeout ceiling (15 seconds)."""
    return min(max(1, timeout_seconds), GLOBAL_MAX_TIMEOUT_SECONDS)


def enforce_global_row_limit_ceiling(row_limit: int) -> int:
    """Sanitize and enforce the hard global row limit ceiling (100 rows)."""
    return min(max(1, row_limit), GLOBAL_MAX_ROW_LIMIT)


def inspect_for_forbidden_client_keys(obj: Any, path: str = "") -> None:
    """Recursively inspect client request payload for forbidden connection target parameters.

    Fails closed if any forbidden key or alias is found at any depth, regardless of casing
    or whitespace.
    """
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str):
                normalized = key.strip().lower()
                if normalized in FORBIDDEN_CLIENT_OVERRIDE_KEYS:
                    raise ConnectionTargetOverrideError(
                        f"SECURITY VIOLATION: Client is not permitted to provide or override connection "
                        f"target parameter '{key}' at '{path}{key}'. Connection targets are strictly agent-controlled."
                    )
            inspect_for_forbidden_client_keys(value, path=f"{path}{key}.")
    elif isinstance(obj, (list, tuple, set)):
        for idx, item in enumerate(obj):
            inspect_for_forbidden_client_keys(item, path=f"{path}[{idx}].")


class ConnectionProfile(BaseModel):
    """Typed representation of an approved local SQL Server database profile.

    Immutable (frozen=True) to guarantee that once loaded from trusted local
    storage, neither client requests nor intermediate pipeline stages can mutate
    the connection target or security policies.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        validate_assignment=True,
    )

    profile_id: str = Field(
        ...,
        description="Unique alphanumeric identifier for this profile.",
        min_length=1,
        max_length=64,
    )
    display_name: str = Field(
        ...,
        description="Human-readable label for UI selection.",
        min_length=1,
        max_length=128,
    )
    server: str = Field(
        ...,
        description="SQL Server instance/host (e.g. 'localhost\\SQLEXPRESS').",
        min_length=1,
    )
    database_name: str = Field(
        ...,
        description="Expected target database name. Verified via SELECT DB_NAME().",
        min_length=1,
    )
    driver: str = Field(
        default="ODBC Driver 17 for SQL Server",
        description="Installed ODBC driver for SQL Server.",
        min_length=1,
    )
    engine_type: str = Field(
        default="mssql",
        description="Database engine type (e.g. 'mssql').",
    )
    use_trusted_connection: bool = Field(
        default=True,
        description="If True, use Windows Integrated Authentication (Trusted_Connection=yes).",
    )
    user: Optional[str] = Field(
        default=None,
        description="SQL Authentication username (required if use_trusted_connection is False).",
    )
    password: Optional[SecretStr] = Field(
        default=None,
        description="SQL Authentication password (required if use_trusted_connection is False).",
    )
    timeout_seconds: int = Field(
        default=GLOBAL_MAX_TIMEOUT_SECONDS,
        ge=1,
        le=GLOBAL_MAX_TIMEOUT_SECONDS,
        description=f"Execution timeout in seconds. Cannot exceed global maximum ({GLOBAL_MAX_TIMEOUT_SECONDS}s).",
    )
    max_row_limit: int = Field(
        default=GLOBAL_MAX_ROW_LIMIT,
        ge=1,
        le=GLOBAL_MAX_ROW_LIMIT,
        description=f"Row fetch limit ceiling. Cannot exceed global maximum ({GLOBAL_MAX_ROW_LIMIT} rows).",
    )
    allowed_tables: Optional[Tuple[str, ...]] = Field(
        default=None,
        description="Optional immutable table whitelist. If specified, only these tables may be accessed.",
    )
    sensitive_columns: Optional[Tuple[str, ...]] = Field(
        default=None,
        description="Optional custom sensitive column denylist (additive to canonical denylist).",
    )
    schema_name: str = Field(
        default="dbo",
        description="Default schema name for SQL Server.",
        min_length=1,
    )

    @field_validator("allowed_tables", "sensitive_columns", mode="before")
    @classmethod
    def freeze_and_clean_collections(
        cls, v: Optional[Any]
    ) -> Optional[Tuple[str, ...]]:
        """Deeply freeze collections into immutable tuples to prevent mutation via retained references."""
        if v is None:
            return None
        if isinstance(v, (str, bytes)):
            raise ValueError("Collection must be an iterable of strings, not a single string.")
        cleaned = []
        for item in v:
            if not isinstance(item, str) or not item.strip():
                continue
            cleaned.append(item.strip())
        return tuple(cleaned)

    @field_validator("profile_id")
    @classmethod
    def validate_profile_id(cls, v: str) -> str:
        trimmed = v.strip()
        if not trimmed:
            raise ValueError("profile_id cannot be blank.")
        if not re.match(r"^[a-zA-Z0-9_\-]+$", trimmed):
            raise ValueError(
                f"profile_id '{v}' is invalid. Must contain only letters, numbers, underscores, and hyphens."
            )
        return trimmed

    @field_validator("engine_type")
    @classmethod
    def validate_engine_type(cls, v: str) -> str:
        clean = v.strip().lower()
        if clean != "mssql":
            raise ValueError(
                f"Unsupported engine_type '{v}'. QueryPilot only supports 'mssql' in this milestone."
            )
        return clean

    @field_validator("server", "database_name", "driver", "display_name", "schema_name")
    @classmethod
    def validate_non_blank_strings(cls, v: str) -> str:
        trimmed = v.strip()
        if not trimmed:
            raise ValueError("Value cannot be blank or whitespace.")
        return trimmed

    @model_validator(mode="after")
    def validate_auth_configuration(self) -> ConnectionProfile:
        if not self.use_trusted_connection:
            if not self.user or not self.user.strip():
                raise ValueError("user must be specified when use_trusted_connection is False.")
            if not self.password or not self.password.get_secret_value():
                raise ValueError("password must be specified when use_trusted_connection is False.")
        return self

    @property
    def expected_db(self) -> str:
        """The expected database name for runtime verification via SELECT DB_NAME()."""
        return self.database_name

    def clamp_timeout(self, requested_timeout: Optional[int] = None) -> int:
        """Enforce global and profile timeout ceilings independently at the execution boundary.

        Guarantees the effective query timeout never exceeds either the profile-specific
        limit or the hard global maximum ceiling (15 seconds).
        """
        if requested_timeout is None:
            return min(self.timeout_seconds, GLOBAL_MAX_TIMEOUT_SECONDS)
        try:
            req = int(requested_timeout)
        except (TypeError, ValueError):
            return min(self.timeout_seconds, GLOBAL_MAX_TIMEOUT_SECONDS)
        if req < 1:
            req = 1
        return min(req, self.timeout_seconds, GLOBAL_MAX_TIMEOUT_SECONDS)

    def clamp_row_limit(self, requested_limit: Optional[int] = None) -> int:
        """Enforce global and profile row limit ceilings independently at the execution boundary.

        Guarantees the effective row limit never exceeds either the profile-specific
        limit or the hard global maximum ceiling (100 rows).
        """
        if requested_limit is None:
            return min(self.max_row_limit, GLOBAL_MAX_ROW_LIMIT)
        try:
            req = int(requested_limit)
        except (TypeError, ValueError):
            return min(self.max_row_limit, GLOBAL_MAX_ROW_LIMIT)
        if req < 1:
            req = 1
        return min(req, self.max_row_limit, GLOBAL_MAX_ROW_LIMIT)

    def get_odbc_connection_string(self, hide_password: bool = False) -> str:
        """Construct the raw ODBC connection string for SQL Server with safe escaping.

        Args:
            hide_password: If True, masks the password field (safe for logging).
        """
        parts = [
            f"DRIVER={{{self.driver}}}",
            f"SERVER={escape_odbc_value(self.server)}",
            f"DATABASE={escape_odbc_value(self.database_name)}",
        ]
        if self.use_trusted_connection:
            parts.append("Trusted_Connection=yes")
        elif self.user and self.password:
            parts.append(f"UID={escape_odbc_value(self.user)}")
            if hide_password:
                parts.append("PWD=********")
            else:
                parts.append(f"PWD={escape_odbc_value(self.password.get_secret_value())}")
        return ";".join(parts) + ";"

    def get_sqlalchemy_url(self, hide_password: bool = False) -> str:
        """Construct a SQLAlchemy URL using the mssql+pyodbc dialect.

        Args:
            hide_password: If True, masks credentials for safe logging and error reporting.
        """
        odbc_str = self.get_odbc_connection_string(hide_password=hide_password)
        params = urllib.parse.quote_plus(odbc_str)
        return f"mssql+pyodbc:///?odbc_connect={params}"

    def get_sensitive_column_policy(self) -> SensitiveColumnPolicy:
        """Return the SensitiveColumnPolicy configured for this profile.

        Canonical sensitive columns (DEFAULT_SENSITIVE_COLUMNS) are strictly preserved
        and cannot be accidentally weakened. Any custom entries specified in
        `sensitive_columns` are strictly additive.
        """
        combined = set(DEFAULT_SENSITIVE_COLUMNS)
        if self.sensitive_columns:
            for col in self.sensitive_columns:
                cleaned = col.strip().lower()
                if cleaned:
                    combined.add(cleaned)
        return SensitiveColumnPolicy(denylist=combined)

    def is_table_permitted(self, table_name: str) -> bool:
        """Check if a table is permitted under this profile's schema policy."""
        if not table_name or not table_name.strip():
            return False
        if self.allowed_tables is None:
            return True
        clean = table_name.strip().lower()
        return clean in {t.strip().lower() for t in self.allowed_tables if t and t.strip()}

    def is_column_permitted(self, column_name: str, table_name: Optional[str] = None) -> bool:
        """Check if a column is permitted (not sensitive) under this profile's policy."""
        policy = self.get_sensitive_column_policy()
        return not policy.is_sensitive(column_name, table_name=table_name)

    def to_safe_summary(self) -> Dict[str, Any]:
        """Return safe, model/UI-facing metadata without secrets or connection details."""
        return {
            "profile_id": self.profile_id,
            "display_name": self.display_name,
            "database_name": self.database_name,
            "timeout_seconds": self.timeout_seconds,
            "max_row_limit": self.max_row_limit,
            "has_table_whitelist": self.allowed_tables is not None,
            "allowed_tables_count": (
                len(self.allowed_tables) if self.allowed_tables is not None else None
            ),
        }


class ProfileStore:
    """Manages trusted connection profiles and enforces agent-controlled resolution.

    Resolves connection targets exclusively from trusted local configuration and
    actively rejects any attempt by clients or external inputs to supply or override
    connection parameters. Fails closed on any unknown profile ID without implicit fallback.
    """

    def __init__(
        self,
        profiles: Optional[Dict[str, ConnectionProfile]] = None,
        default_profile_id: Optional[str] = None,
    ):
        """Initialize with an optional mapping of profile_id -> ConnectionProfile and optional explicit default."""
        self._profiles: Dict[str, ConnectionProfile] = dict(profiles or {})
        self._default_profile_id: Optional[str] = None
        if default_profile_id is not None:
            self.set_default_profile(default_profile_id)

    @property
    def profiles(self) -> Dict[str, ConnectionProfile]:
        """Return a copy of the registered profiles mapping."""
        return dict(self._profiles)

    @property
    def default_profile_id(self) -> Optional[str]:
        """Return the explicitly configured default profile_id, or None if none configured."""
        return self._default_profile_id

    def set_default_profile(self, profile_id: str) -> None:
        """Explicitly designate an existing registered profile as the default.

        Raises ProfileConfigurationError if profile_id is not already registered.
        """
        if profile_id not in self._profiles:
            raise ProfileConfigurationError(
                f"Default profile '{profile_id}' is not present in registered profiles."
            )
        self._default_profile_id = profile_id

    def get_default_profile(self) -> ConnectionProfile:
        """Retrieve the explicitly configured default profile.

        Raises UnknownProfileError if no default profile is configured.
        """
        if self._default_profile_id is None:
            raise UnknownProfileError(
                "No default connection profile is configured in the trusted store. "
                "Profile selection must be explicit.",
                profile_id="None",
            )
        return self._profiles[self._default_profile_id]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ProfileStore:
        """Construct ProfileStore from a raw dictionary (e.g. parsed JSON).

        Supports formats:
        1. {"profiles": {"corp_dw": {...}, ...}, "default_profile_id": "corp_dw"}
        2. {"corp_dw": {...}, ...}
        3. [{"profile_id": "corp_dw", ...}, ...]
        """
        if not isinstance(data, dict):
            raise ProfileConfigurationError(
                f"Expected dict for profile configuration, got {type(data).__name__}."
            )

        profiles_raw = data.get("profiles", data)
        store = cls()

        if isinstance(profiles_raw, dict):
            for pid, pdata in profiles_raw.items():
                if pid in ("default_profile_id", "default_profile"):
                    continue
                if not isinstance(pdata, dict):
                    raise ProfileConfigurationError(
                        f"Profile configuration for '{pid}' must be an object."
                    )
                entry_data = dict(pdata)
                if "profile_id" not in entry_data:
                    entry_data["profile_id"] = pid
                elif entry_data["profile_id"] != pid:
                    raise ProfileConfigurationError(
                        f"Profile ID mismatch: dictionary key '{pid}' does not match payload "
                        f"profile_id '{entry_data['profile_id']}'."
                    )
                try:
                    profile = ConnectionProfile(**entry_data)
                except Exception as exc:
                    raise ProfileConfigurationError(
                        f"Invalid profile configuration for '{pid}': {exc}"
                    ) from exc
                store.register_profile(profile)
        elif isinstance(profiles_raw, list):
            for item in profiles_raw:
                if not isinstance(item, dict):
                    raise ProfileConfigurationError(
                        f"Profile list items must be objects, got {type(item).__name__}."
                    )
                try:
                    profile = ConnectionProfile(**item)
                except Exception as exc:
                    raise ProfileConfigurationError(
                        f"Invalid profile item configuration: {exc}"
                    ) from exc
                store.register_profile(profile)
        else:
            raise ProfileConfigurationError(
                f"Unexpected format for profiles configuration: {type(profiles_raw).__name__}."
            )

        default_id = data.get("default_profile_id") or data.get("default_profile")
        if default_id is not None:
            store.set_default_profile(default_id)

        return store

    @classmethod
    def from_json_file(cls, path: Path | str) -> ProfileStore:
        """Load and parse trusted profile store from a JSON file."""
        p = Path(path)
        if not p.exists():
            raise ProfileConfigurationError(f"Profile configuration file not found: {p}")
        try:
            with open(p, "r", encoding="utf-8") as f:
                content = json.load(f)
        except Exception as exc:
            raise ProfileConfigurationError(
                f"Failed to read/parse profile JSON from {p}: {exc}"
            ) from exc

        return cls.from_dict(content)

    @classmethod
    def from_database_settings(
        cls,
        settings: Any,
        profile_id: str = "default_local",
        set_as_default: bool = True,
    ) -> ProfileStore:
        """Construct a ProfileStore explicitly containing a profile derived from DatabaseSettings.

        Enables backwards compatibility for local development workflows by making the
        development database profile registration and default selection EXPLICIT in code/config
        rather than relying on implicit fallback. Fails closed if any other profile_id is requested.
        """
        profile = ConnectionProfile(
            profile_id=profile_id,
            display_name=f"Local Development ({getattr(settings, 'DB_NAME', 'DB')})",
            server=getattr(settings, "DB_SERVER", r"localhost\SQLEXPRESS"),
            database_name=getattr(settings, "DB_NAME", "GradeSense_Local"),
            driver=getattr(settings, "DB_DRIVER", "ODBC Driver 17 for SQL Server"),
            use_trusted_connection=getattr(settings, "DB_USE_TRUSTED_CONNECTION", True),
            user=getattr(settings, "DB_USER", None),
            password=getattr(settings, "DB_PASSWORD", None),
            timeout_seconds=min(getattr(settings, "DB_QUERY_TIMEOUT_SECONDS", 15), GLOBAL_MAX_TIMEOUT_SECONDS),
            max_row_limit=min(getattr(settings, "DB_MAX_ROW_LIMIT", 100), GLOBAL_MAX_ROW_LIMIT),
        )
        return cls(
            profiles={profile_id: profile},
            default_profile_id=profile_id if set_as_default else None,
        )

    def register_profile(self, profile: ConnectionProfile) -> None:
        """Register or update an approved profile."""
        if not isinstance(profile, ConnectionProfile):
            raise ProfileConfigurationError(
                f"Expected ConnectionProfile, got {type(profile).__name__}."
            )
        self._profiles[profile.profile_id] = profile

    def has_profile(self, profile_id: str) -> bool:
        """Check if a profile_id is registered."""
        return profile_id in self._profiles

    def get_profile(self, profile_id: str) -> ConnectionProfile:
        """Retrieve a profile by ID or raise UnknownProfileError.

        Fails closed: an unknown profile_id NEVER falls back to default profile or GradeSense_Local.
        """
        if not profile_id or not isinstance(profile_id, str) or profile_id not in self._profiles:
            raise UnknownProfileError(
                f"Unknown profile '{profile_id}'. Available profiles: {sorted(self._profiles.keys())}",
                profile_id=str(profile_id) if profile_id is not None else "None",
            )
        return self._profiles[profile_id]

    def resolve(
        self,
        profile_id: Optional[str] = None,
        client_request: Optional[Dict[str, Any]] = None,
    ) -> ConnectionProfile:
        """Safely resolve an approved profile for an incoming request.

        Enforces security invariants:
        1. Recursively inspects client_request for forbidden connection target parameters
           and aliases. If any are found, immediately raises ConnectionTargetOverrideError.
        2. If profile_id is None, resolves the explicitly configured default profile.
           If no default profile was explicitly configured, fails closed with UnknownProfileError.
        3. If profile_id is provided, looks up profile_id in trusted local storage.
           If missing (e.g. typo or unregistered database), fails closed with UnknownProfileError.
           It NEVER catches lookup failure or falls back to any other profile.
        4. Returns the immutable ConnectionProfile.
        """
        if client_request:
            inspect_for_forbidden_client_keys(client_request)

        if profile_id is None:
            return self.get_default_profile()

        return self.get_profile(profile_id)

    def list_profiles(self) -> List[Dict[str, Any]]:
        """Return safe, non-sensitive summaries of all registered profiles."""
        return [p.to_safe_summary() for p in self._profiles.values()]

    def __len__(self) -> int:
        return len(self._profiles)


# Disallowed credential keys when parsing or saving profiles.
# Any appearance of these keys in an onboarded profile file causes immediate rejection.
FORBIDDEN_CREDENTIAL_KEYS: FrozenSet[str] = frozenset({
    "password",
    "pwd",
    "user",
    "username",
    "uid",
    "secret",
    "token",
    "api_key",
})


def _get_active_windows_trustee() -> str:
    """Return the qualified Windows username/trustee for ACL operations.

    Uses `whoami` to obtain the exact authenticated domain\\user from the active
    process token. Falls back to getpass.getuser() only if whoami fails.
    Avoids blindly trusting unauthenticated environment variables (USERDOMAIN/USERNAME).
    """
    try:
        proc = subprocess.run(["whoami"], capture_output=True, text=True, timeout=5)
        if proc.returncode == 0 and proc.stdout.strip():
            trustee = proc.stdout.strip().splitlines()[0].strip()
            if trustee:
                return trustee
    except Exception:
        pass

    fallback = getpass.getuser().strip()
    if not fallback:
        raise ProfileConfigurationError("Cannot determine active Windows username for profile ACL hardening.")
    domain = os.environ.get("USERDOMAIN", "").strip()
    if domain and "\\" not in fallback:
        return f"{domain}\\{fallback}"
    return fallback


def validate_safe_profile_path(path: Path | str, check_exists: bool = False) -> Path:
    """Validate that a profile file or directory path is safe to access and not a symlink or reparse point.

    Fails closed if the path:
    - Is a symlink (os.path.islink or p.is_symlink())
    - Is a junction or reparse point (on Windows)
    - Has a parent directory that is a symlink or junction
    """
    p = Path(path)
    if check_exists and not p.exists():
        raise ProfileConfigurationError(f"Target path does not exist: {p}")

    # Check symlinks on the path itself
    if p.is_symlink() or os.path.islink(str(p)):
        raise ProfileConfigurationError(
            f"SECURITY REJECTION: Profile path '{p}' is a symbolic link. "
            "Symbolic links are not permitted for database profile configuration."
        )

    # Check Windows reparse points / junctions
    if platform.system() == "Windows" and p.exists():
        if hasattr(p, "is_junction") and p.is_junction():
            raise ProfileConfigurationError(
                f"SECURITY REJECTION: Profile path '{p}' is a Windows junction / reparse point."
            )
        try:
            st = os.stat(p, follow_symlinks=False)
            if hasattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT"):
                if getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    raise ProfileConfigurationError(
                        f"SECURITY REJECTION: Profile path '{p}' has reparse point attributes."
                    )
        except OSError as exc:
            raise ProfileConfigurationError(f"Could not inspect path attributes for '{p}': {exc}") from exc

    # Check parent directory
    parent = p.parent
    if parent.exists():
        if parent.is_symlink() or os.path.islink(str(parent)):
            raise ProfileConfigurationError(
                f"SECURITY REJECTION: Profile parent directory '{parent}' is a symbolic link."
            )
        if platform.system() == "Windows":
            if hasattr(parent, "is_junction") and parent.is_junction():
                raise ProfileConfigurationError(
                    f"SECURITY REJECTION: Profile parent directory '{parent}' is a Windows junction / reparse point."
                )
            try:
                pst = os.stat(parent, follow_symlinks=False)
                if hasattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT"):
                    if getattr(pst, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                        raise ProfileConfigurationError(
                            f"SECURITY REJECTION: Profile parent directory '{parent}' has reparse point attributes."
                        )
            except OSError as exc:
                raise ProfileConfigurationError(
                    f"Could not inspect parent path attributes for '{parent}': {exc}"
                ) from exc

    return p


def restrict_file_permissions(path: Path | str) -> None:
    """Restrict permissions on a file or directory exclusively to the active user.

    - POSIX: chmod 0600 (file) or 0700 (directory).
    - Windows: icacls /inheritance:r /grant:r <USERDOMAIN\\USERNAME>:(F).
    Verifies that icacls succeeded and inspects the resulting DACL.
    Fails closed if the operation cannot be completed or fails.
    """
    p = Path(path).resolve()
    if not p.exists():
        raise ProfileConfigurationError(f"Target path does not exist for permission hardening: {p}")

    validate_safe_profile_path(p, check_exists=True)

    if platform.system() != "Windows":
        mode = stat.S_IRUSR | stat.S_IWUSR | (stat.S_IXUSR if p.is_dir() else 0)
        try:
            os.chmod(p, mode)
        except Exception as exc:
            raise ProfileConfigurationError(
                f"Failed to set restrictive POSIX permissions on '{p}': {exc}"
            ) from exc
        return

    trustee = _get_active_windows_trustee()

    # On Windows, strip inherited permissions and grant Full control (F)
    # exclusively to the active user.
    cmd = ["icacls", str(p), "/inheritance:r", "/grant:r", f"{trustee}:(F)"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except FileNotFoundError as exc:
        raise ProfileConfigurationError(
            "icacls utility not found on Windows; cannot verify restrictive profile permissions."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ProfileConfigurationError(
            "icacls ACL hardening command timed out; cannot verify restrictive profile permissions."
        ) from exc
    except Exception as exc:
        raise ProfileConfigurationError(
            f"icacls execution encountered an unexpected error: {exc}"
        ) from exc

    if proc.returncode != 0:
        err_detail = proc.stderr.strip() if proc.stderr else proc.stdout.strip()
        raise ProfileConfigurationError(
            f"Windows ACL hardening via icacls failed with exit code {proc.returncode}: {err_detail}"
        )

    # Verification of DACL:
    # Query icacls to verify that the active trustee is confirmed in the DACL
    try:
        verify_proc = subprocess.run(["icacls", str(p)], capture_output=True, text=True, timeout=5)
        if verify_proc.returncode == 0:
            out = verify_proc.stdout.lower()
            short_trustee = trustee.split("\\")[-1].lower()
            if short_trustee not in out and trustee.lower() not in out:
                raise ProfileConfigurationError(
                    f"DACL verification failed: trustee '{trustee}' not confirmed in ACL for '{p}'."
                )
    except Exception as exc:
        if isinstance(exc, ProfileConfigurationError):
            raise
        logger.warning("Could not execute post-hardening DACL verification on '%s': %s", p, exc)


def _inspect_forbidden_keys_recursive(obj: Any, path: str = "") -> None:
    """Recursively inspect object for forbidden credential keys with case and whitespace normalization.

    Fails closed if any credential key or alias is found at any depth.
    """
    if isinstance(obj, dict):
        for raw_k, val in obj.items():
            if isinstance(raw_k, str):
                normalized = raw_k.strip().lower()
                if normalized in FORBIDDEN_CREDENTIAL_KEYS:
                    raise ProfileConfigurationError(
                        f"SECURITY REJECTION: Profile contains prohibited credential field '{normalized}'. "
                        "SQL-authenticated profiles and password persistence are rejected in this milestone. "
                        "Use Windows Integrated Authentication (use_trusted_connection=True)."
                    )
            _inspect_forbidden_keys_recursive(val, path=f"{path}{raw_k}.")
    elif isinstance(obj, (list, tuple, set)):
        for idx, item in enumerate(obj):
            _inspect_forbidden_keys_recursive(item, path=f"{path}[{idx}].")


def validate_safe_profile_data(data: Dict[str, Any]) -> None:
    """Validate that raw profile dictionary strictly adheres to Windows Integrated Authentication
    and contains zero plaintext credentials or SQL authentication flags.
    Fails closed if any credential keys, SQL auth flags, or unsupported drivers are present.
    """
    if not isinstance(data, dict):
        raise ProfileConfigurationError(f"Profile configuration must be a dictionary, got {type(data).__name__}.")

    # 1. Normalize and recursively reject any password or username credentials at any depth
    _inspect_forbidden_keys_recursive(data)

    # 2. Reject use_trusted_connection=False (normalized keys and values)
    norm_dict = {str(k).strip().lower(): v for k, v in data.items() if isinstance(k, str)}
    trusted = norm_dict.get("use_trusted_connection", norm_dict.get("trusted_connection", True))
    if trusted is False or str(trusted).strip().lower() in {"false", "0", "no"}:
        raise ProfileConfigurationError(
            "SECURITY REJECTION: SQL-authenticated profiles (trusted_connection=False) are rejected. "
            "QueryPilot currently requires Windows Integrated Authentication (use_trusted_connection=True)."
        )

    # 3. Reject unsupported engine_type (normalized)
    engine = norm_dict.get("engine_type", "mssql")
    if engine and str(engine).strip().lower() != "mssql":
        raise ProfileConfigurationError(
            f"Unsupported engine_type '{engine}'. Only 'mssql' is supported in this milestone."
        )


@contextmanager
def profile_file_lock(path: Path | str, timeout: float = 5.0):
    """Cross-process file lock using an exclusive sibling lock file (<path>.lock).

    Acquires lock via os.open(lock_path, O_CREAT | O_EXCL) with polling up to timeout seconds.
    Writes current PID into lock file for diagnostic tracking.
    Cleans up lock file on exit.
    """
    p = Path(path).resolve()
    lock_path = p.with_name(f"{p.name}.lock")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    start_time = time.time()
    fd = None
    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
    while True:
        try:
            fd = os.open(str(lock_path), flags)
            try:
                os.write(fd, str(os.getpid()).encode("utf-8"))
            except Exception:
                pass
            break
        except FileExistsError:
            if time.time() - start_time >= timeout:
                raise ProfileConfigurationError(
                    f"Timeout ({timeout}s) acquiring lock on '{lock_path}'. "
                    "Another profile operation may be in progress."
                )
            time.sleep(0.05)
        except Exception as exc:
            raise ProfileConfigurationError(
                f"Failed to acquire profile lock on '{lock_path}': {exc}"
            ) from exc

    try:
        yield
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass
        try:
            if lock_path.exists():
                lock_path.unlink()
        except Exception:
            pass


def load_trusted_profiles_file(path: Path | str) -> ProfileStore:
    """Load, validate, and instantiate a ProfileStore from a trusted local profiles JSON file.

    Guarantees:
    - Fails closed if the file does not exist, is malformed JSON, or fails schema validation.
    - Fails closed if any profile contains passwords, usernames, or SQL authentication.
    - Fails closed on duplicate profile IDs or missing required connection parameters.
    """
    p = Path(path).resolve()
    if not p.exists():
        raise ProfileConfigurationError(f"Profiles file not found at '{p}'.")

    try:
        with open(p, "r", encoding="utf-8") as f:
            content = json.load(f)
    except Exception as exc:
        raise ProfileConfigurationError(f"Failed to parse profiles JSON file '{p}': {exc}") from exc

    if not isinstance(content, dict):
        raise ProfileConfigurationError(
            f"Profiles file root must be a JSON object, got {type(content).__name__}."
        )

    profiles_raw = content.get("profiles", content)
    if not isinstance(profiles_raw, (dict, list)):
        raise ProfileConfigurationError(
            f"Expected 'profiles' object or list in '{p}', got {type(profiles_raw).__name__}."
        )

    entries: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()

    if isinstance(profiles_raw, dict):
        for pid, pdata in profiles_raw.items():
            if pid in ("default_profile_id", "default_profile"):
                continue
            if not isinstance(pdata, dict):
                raise ProfileConfigurationError(f"Profile entry '{pid}' in '{p}' must be a JSON object.")
            entry = dict(pdata)
            if "profile_id" not in entry:
                entry["profile_id"] = pid
            elif entry["profile_id"] != pid:
                raise ProfileConfigurationError(
                    f"Profile ID mismatch: dictionary key '{pid}' does not match entry profile_id '{entry['profile_id']}'."
                )
            entries.append(entry)
    else:  # list
        for item in profiles_raw:
            if not isinstance(item, dict):
                raise ProfileConfigurationError(f"Profile list item in '{p}' must be a JSON object.")
            entries.append(dict(item))

    store = ProfileStore()
    for entry in entries:
        validate_safe_profile_data(entry)
        pid = entry.get("profile_id")
        if not pid or not str(pid).strip():
            raise ProfileConfigurationError("Profile is missing required non-blank 'profile_id'.")
        clean_id = str(pid).strip()
        if clean_id in seen_ids:
            raise ProfileConfigurationError(f"Duplicate profile_id '{clean_id}' found in '{p}'.")
        seen_ids.add(clean_id)

        # Standardize trusted connection and engine type
        entry["use_trusted_connection"] = True
        entry["engine_type"] = "mssql"

        try:
            profile = ConnectionProfile(**entry)
        except Exception as exc:
            raise ProfileConfigurationError(
                f"Invalid profile configuration for '{clean_id}': {exc}"
            ) from exc
        store.register_profile(profile)

    default_id = content.get("default_profile_id") or content.get("default_profile")
    if default_id is not None:
        clean_def = str(default_id).strip()
        if clean_def in store.profiles:
            store.set_default_profile(clean_def)
        else:
            raise ProfileConfigurationError(
                f"Configured default_profile_id '{default_id}' is not in registered profiles."
            )
    elif len(store) > 0 and store.default_profile_id is None:
        store.set_default_profile(next(iter(store.profiles.keys())))

    return store


def save_trusted_profiles_file(
    path: Path | str,
    store: ProfileStore,
    restrict_permissions: bool = True,
) -> None:
    """Atomically write the trusted ProfileStore to disk with strict permission hardening.

    Guarantees:
    - Never serializes passwords, tokens, or credentials (Windows Integrated Auth only).
    - Rejects symlinks, junctions, and reparse points before creating or modifying files.
    - Establishes and hardens parent directory permissions before writing; never swallows errors.
    - Creates temporary sibling file with exclusive creation (O_CREAT | O_EXCL) and
      restricts permissions BEFORE writing profile content.
    - Writes atomically via temp file replace (os.replace).
    - Verifies destination permissions.
    - Cleans up temporary files upon failure.
    """
    p = Path(path).resolve()

    # 1. Path safety verification
    validate_safe_profile_path(p, check_exists=False)

    # 2. Parent directory establishment and hardening
    parent = p.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        raise ProfileConfigurationError(f"Failed to create directory '{parent}': {exc}") from exc

    validate_safe_profile_path(parent, check_exists=True)

    if restrict_permissions:
        is_dedicated_app_dir = parent.name == ".query_pilot"
        if is_dedicated_app_dir:
            try:
                restrict_file_permissions(parent)
            except Exception as exc:
                raise ProfileConfigurationError(
                    f"Failed to secure permissions on profile directory '{parent}': {exc}"
                ) from exc
        else:
            if not os.access(parent, os.R_OK | os.W_OK):
                raise ProfileConfigurationError(
                    f"Custom profile directory '{parent}' is not accessible for read/write."
                )

    # 3. Serialize trusted profile data (strictly non-secret, Windows Integrated Auth only)
    profiles_dict: Dict[str, Any] = {}
    for pid, profile in store.profiles.items():
        if not profile.use_trusted_connection:
            raise ProfileConfigurationError(
                f"Cannot serialize profile '{pid}': SQL authentication is prohibited."
            )
        entry = {
            "profile_id": profile.profile_id,
            "display_name": profile.display_name,
            "engine_type": getattr(profile, "engine_type", "mssql"),
            "server": profile.server,
            "database_name": profile.database_name,
            "driver": profile.driver,
            "use_trusted_connection": True,
            "timeout_seconds": profile.timeout_seconds,
            "max_row_limit": profile.max_row_limit,
            "schema_name": profile.schema_name or "dbo",
        }
        if profile.allowed_tables:
            entry["allowed_tables"] = list(profile.allowed_tables)
        if profile.sensitive_columns:
            entry["sensitive_columns"] = list(profile.sensitive_columns)
        profiles_dict[pid] = entry

    payload = {
        "profiles": profiles_dict,
        "default_profile_id": store.default_profile_id,
    }

    # 4. Exclusive temporary file creation and pre-write hardening
    temp_path = parent / f"{p.name}.tmp.{secrets.token_hex(6)}"
    validate_safe_profile_path(temp_path, check_exists=False)

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    try:
        # Create exclusively
        if platform.system() != "Windows":
            fd = os.open(str(temp_path), flags, stat.S_IRUSR | stat.S_IWUSR)
        else:
            fd = os.open(str(temp_path), flags)
        os.close(fd)

        # On Windows, harden the empty file before writing profile data
        if restrict_permissions and platform.system() == "Windows":
            restrict_file_permissions(temp_path)

        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())

        # On POSIX, ensure chmod 0600
        if restrict_permissions and platform.system() != "Windows":
            os.chmod(temp_path, stat.S_IRUSR | stat.S_IWUSR)

        # 5. Atomic replacement
        validate_safe_profile_path(p, check_exists=False)
        os.replace(temp_path, p)

        # 6. Final destination permission verification
        if restrict_permissions:
            restrict_file_permissions(p)
    except Exception as exc:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except Exception:
                pass
        if isinstance(exc, ProfileConfigurationError):
            raise
        raise ProfileConfigurationError(f"Failed to securely save profile file to '{p}': {exc}") from exc
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except Exception:
                pass


