"""QueryPilot Local Agent Daemon package.

Provides loopback-only local execution daemon, single-use bootstrap authentication,
profile management, and dynamic schema introspection.
"""

from query_pilot.agent.api_router import api_router
from query_pilot.agent.app import create_agent_app
from query_pilot.agent.auth import (
    SESSION_COOKIE_NAME,
    BootstrapTokenManager,
    BootstrapTokenProvisioningError,
    Session,
    SessionManager,
)

from query_pilot.agent.config import (
    AgentConfig,
    get_default_profiles_file,
    get_default_token_dir,
    is_loopback_host,
)
from query_pilot.agent.engine_manager import AgentEngineManager
from query_pilot.agent.models import (
    AgentErrorResponse,
    BootstrapRequest,
    BootstrapResponse,
    ExecuteQueryRequest,
    ExecuteQueryResponse,
    LogoutResponse,
    ProfileListResponse,
    ProfileSummary,
    RotateTokenResponse,
)

__all__ = [
    "AgentConfig",
    "AgentErrorResponse",
    "AgentEngineManager",
    "BootstrapRequest",
    "BootstrapResponse",
    "BootstrapTokenManager",
    "BootstrapTokenProvisioningError",
    "ExecuteQueryRequest",
    "ExecuteQueryResponse",
    "LogoutResponse",
    "ProfileListResponse",
    "ProfileSummary",
    "RotateTokenResponse",
    "SESSION_COOKIE_NAME",
    "Session",
    "SessionManager",
    "api_router",
    "build_parser",
    "create_agent_app",
    "get_default_profiles_file",
    "get_default_token_dir",
    "handle_profile_add",
    "handle_profile_list",
    "handle_profile_validate",
    "is_loopback_host",
    "main",
    "run_agent_cli",
]


def __getattr__(name: str):
    if name in (
        "build_parser",
        "run_agent_cli",
        "main",
        "handle_profile_add",
        "handle_profile_list",
        "handle_profile_validate",
    ):
        import query_pilot.agent.cli as agent_cli

        return getattr(agent_cli, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

