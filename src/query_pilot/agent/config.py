"""Configuration for the QueryPilot Local Agent Daemon.

Enforces strict loopback-only binding, restrictive timeouts, resource ceilings,
and packaged vs. development origin controls.
"""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path
from typing import Optional, Set
import urllib.parse

from pydantic import BaseModel, Field, field_validator


# Loopback host identifiers permitted for socket binding and Host header validation
LOOPBACK_HOSTS: Set[str] = {"127.0.0.1", "localhost", "::1", "[::1]"}


def get_default_static_dir() -> Path:
    """Resolve default location of frontend production build.

    Resolution order:
    1. Package-internal static directory (installed package/wheel layout):
       `.../query_pilot/agent/static` if it exists and contains `index.html`.
    2. Repository checkout layout:
       `<repo_root>/frontend/dist` if it exists and contains `index.html`.
    3. Fallback: package-internal static directory if it exists, otherwise
       repository checkout `frontend/dist`.
    """
    pkg_static = Path(__file__).resolve().parent / "static"
    if pkg_static.is_dir() and (pkg_static / "index.html").is_file():
        return pkg_static

    repo_root = Path(__file__).resolve().parents[3]
    repo_static = repo_root / "frontend" / "dist"
    if repo_static.is_dir() and (repo_static / "index.html").is_file():
        return repo_static

    if pkg_static.is_dir():
        return pkg_static
    return repo_static


def is_loopback_host(host: str) -> bool:
    """Determine whether a host identifier strictly represents loopback."""
    if not host or not host.strip():
        return False
    clean = host.strip().lower()
    if clean in LOOPBACK_HOSTS:
        return True
    try:
        addr = ipaddress.ip_address(clean.strip("[]"))
        return addr.is_loopback
    except ValueError:
        return False


def _get_env_str(keys: list[str], default: str) -> str:
    """Read first non-empty string from listed environment variables or return default."""
    for k in keys:
        v = os.getenv(k)
        if v is not None and v.strip() != "":
            return v.strip()
    return default


def _get_env_int(keys: list[str], default: int) -> int:
    """Read first integer from listed environment variables or return default."""
    for k in keys:
        v = os.getenv(k)
        if v is not None and v.strip() != "":
            try:
                return int(v.strip())
            except ValueError as exc:
                raise ValueError(f"Environment variable '{k}' must be a valid integer, got '{v}'.") from exc
    return default


def _get_env_float(keys: list[str], default: float) -> float:
    """Read first float from listed environment variables or return default."""
    for k in keys:
        v = os.getenv(k)
        if v is not None and v.strip() != "":
            try:
                return float(v.strip())
            except ValueError as exc:
                raise ValueError(f"Environment variable '{k}' must be a valid float, got '{v}'.") from exc
    return default


def _get_env_bool(keys: list[str], default: bool) -> bool:
    """Read first boolean from listed environment variables or return default."""
    for k in keys:
        v = os.getenv(k)
        if v is not None and v.strip() != "":
            clean = v.strip().lower()
            if clean in {"1", "true", "yes", "on"}:
                return True
            if clean in {"0", "false", "no", "off"}:
                return False
            raise ValueError(f"Environment variable '{k}' must be a boolean ('true' or 'false'), got '{v}'.")
    return default


def _get_env_path(keys: list[str], default: Optional[Path]) -> Optional[Path]:
    """Read first path from listed environment variables or return default."""
    for k in keys:
        v = os.getenv(k)
        if v is not None and v.strip() != "":
            clean = v.strip()
            if not clean:
                raise ValueError(f"Environment variable '{k}' path cannot be empty.")
            return Path(clean)
    return default


def get_default_token_dir() -> Path:
    """Resolve default location of the bootstrap token directory."""
    resolved = _get_env_path(["QUERYPILOT_TOKEN_DIR", "QUERYPILOT_AGENT_TOKEN_DIR"], None)
    if resolved is not None:
        return resolved
    return Path.home() / ".query_pilot"


def get_default_profiles_file() -> Path:
    """Resolve default location of the database connection profiles file."""
    resolved = _get_env_path(["QUERYPILOT_PROFILES_FILE"], None)
    if resolved is not None:
        return resolved
    return Path.home() / ".query_pilot" / "profiles.json"


class AgentConfig(BaseModel):
    """Configuration contract for the local-agent daemon.

    Precedence order:
    1. Explicit arguments passed to constructor / CLI.
    2. Supported environment variables.
    3. Existing code defaults.

    Defaults to safe production values:
    - Binds exclusively to 127.0.0.1:8001
    - Disallows development origins by default (packaged mode)
    - Enforces 1-hour idle timeout and 12-hour absolute session lifetime
    - 64 KB request body ceiling
    - Concurrency ceiling of 2 simultaneous requests
    """

    model_config = {"extra": "forbid"}

    bind_host: str = Field(
        default_factory=lambda: _get_env_str(["QUERYPILOT_AGENT_HOST", "QUERYPILOT_AGENT_BIND_HOST"], "127.0.0.1"),
        description="Loopback IP or hostname for local socket binding.",
    )
    bind_port: int = Field(
        default_factory=lambda: _get_env_int(["QUERYPILOT_AGENT_PORT"], 8001),
        ge=1024,
        le=65535,
        description="TCP port for local socket binding.",
    )
    allow_dev_origin: bool = Field(
        default_factory=lambda: _get_env_bool(["QUERYPILOT_AGENT_ALLOW_DEV_ORIGIN"], False),
        description="If True, permits Vite development server origin (http://127.0.0.1:5173). Must be False in packaged mode.",
    )
    token_dir: Path = Field(
        default_factory=get_default_token_dir,
        description="Per-user directory where the agent bootstrap token is provisioned with restricted permissions.",
    )
    token_filename: str = Field(
        default_factory=lambda: _get_env_str(["QUERYPILOT_AGENT_TOKEN_FILENAME"], "agent.token"),
        description="Filename of the bootstrap token.",
    )
    profiles_file: Path = Field(
        default_factory=get_default_profiles_file,
        description="Path to local database connection profiles file (for future onboarding contract).",
    )
    session_idle_seconds: int = Field(
        default_factory=lambda: _get_env_int(["QUERYPILOT_AGENT_SESSION_IDLE_SECONDS"], 3600),
        ge=60,
        le=86400,
        description="Maximum idle seconds before an inactive session expires (default: 1 hour).",
    )
    session_max_lifetime_seconds: int = Field(
        default_factory=lambda: _get_env_int(["QUERYPILOT_AGENT_SESSION_MAX_LIFETIME_SECONDS"], 43200),
        ge=300,
        le=86400,
        description="Maximum absolute session duration before mandatory re-authentication (default: 12 hours).",
    )
    max_request_bytes: int = Field(
        default_factory=lambda: _get_env_int(["QUERYPILOT_AGENT_MAX_REQUEST_BYTES"], 65536),
        ge=1024,
        le=1048576,
        description="Hard ceiling for HTTP request payloads (default: 64 KB).",
    )
    max_concurrent_requests: int = Field(
        default_factory=lambda: _get_env_int(["QUERYPILOT_AGENT_MAX_CONCURRENT_REQUESTS"], 2),
        ge=1,
        le=10,
        description="Maximum concurrent active requests handled by the local agent (default: 2).",
    )
    static_dir: Optional[Path] = Field(
        default_factory=lambda: _get_env_path(["QUERYPILOT_AGENT_STATIC_DIR"], None),
        description="Path to the directory containing production frontend SPA build assets (e.g. frontend/dist).",
    )
    upstream_api_url: str = Field(
        default_factory=lambda: _get_env_str(["QUERYPILOT_UPSTREAM_API_URL", "QUERYPILOT_AGENT_UPSTREAM_API_URL"], "http://127.0.0.1:8000"),
        description="Trusted upstream URL for QueryPilot Hosted API (default: http://127.0.0.1:8000).",
    )
    upstream_timeout_seconds: float = Field(
        default_factory=lambda: _get_env_float(["QUERYPILOT_UPSTREAM_TIMEOUT_SECONDS", "QUERYPILOT_AGENT_UPSTREAM_TIMEOUT_SECONDS"], 30.0),
        ge=1.0,
        le=120.0,
        description="Timeout in seconds for upstream API routing requests (default: 30.0).",
    )

    @field_validator("bind_host")
    @classmethod
    def validate_bind_host(cls, v: str) -> str:
        """Enforce fail-closed check that bind_host is strictly loopback."""
        if not is_loopback_host(v):
            raise ValueError(
                f"SECURITY VIOLATION: Agent daemon cannot bind to non-loopback address '{v}'. "
                f"Only loopback addresses ({', '.join(sorted(LOOPBACK_HOSTS))}) are permitted."
            )
        return v.strip().lower()

    @field_validator("upstream_api_url")
    @classmethod
    def validate_upstream_api_url(cls, v: str) -> str:
        """Validate upstream API URL to prevent SSRF and arbitrary host routing."""
        if not v or not v.strip():
            raise ValueError("upstream_api_url cannot be empty.")
        clean = v.strip().rstrip("/")
        try:
            parsed = urllib.parse.urlparse(clean)
        except Exception as exc:
            raise ValueError(f"Invalid upstream_api_url: {exc}") from exc

        if parsed.scheme not in {"http", "https"}:
            raise ValueError(f"upstream_api_url scheme must be 'http' or 'https', got '{parsed.scheme}'.")

        hostname = parsed.hostname
        if not hostname:
            raise ValueError("upstream_api_url must include a valid hostname.")

        # If HTTP (unencrypted), strictly enforce that hostname is loopback
        if parsed.scheme == "http" and not is_loopback_host(hostname):
            raise ValueError(
                f"SECURITY VIOLATION: Unencrypted HTTP upstream_api_url must be a loopback host (127.0.0.1, localhost, ::1). "
                f"Got '{hostname}'."
            )

        return clean

    @field_validator("profiles_file")
    @classmethod
    def validate_profiles_file(cls, v: Path) -> Path:
        """Validate that profiles_file path is non-empty."""
        raw = str(v).strip()
        if not raw or raw == ".":
            raise ValueError("profiles_file cannot be empty.")
        return v

    @field_validator("token_dir")
    @classmethod
    def validate_token_dir(cls, v: Path) -> Path:
        """Validate that token_dir path is non-empty."""
        raw = str(v).strip()
        if not raw or raw == ".":
            raise ValueError("token_dir cannot be empty.")
        return v

    @property
    def token_path(self) -> Path:
        """Return the resolved absolute path to the bootstrap token file."""
        return self.token_dir / self.token_filename

    @property
    def resolved_static_dir(self) -> Path:
        """Return the resolved absolute path to the static assets directory."""
        if self.static_dir is not None:
            return self.static_dir.resolve()
        return get_default_static_dir().resolve()

    @property
    def resolved_profiles_file(self) -> Path:
        """Return the resolved absolute path to the connection profiles file."""
        return self.profiles_file.resolve()
