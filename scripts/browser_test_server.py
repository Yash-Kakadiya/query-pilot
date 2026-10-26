"""Browser Test Server for QueryPilot Onboarding Verification (Milestone B12-F10).

Runs an isolated local agent daemon strictly on loopback (127.0.0.1:8005 by default)
with:
1. Static production frontend serving from frontend/dist at /app/.
2. Isolated temporary profile store and bootstrap token directory.
3. Deterministic connection-test engine mocking (no real database calls).
4. Deterministic schema introspection mocking for created profiles.
5. In-memory request auditing to record request sequence.
6. Clean HTTP shutdown endpoint (POST /agent/v1/test/shutdown).
"""

from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
import json
import logging
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

from fastapi import FastAPI, HTTPException, Request, Response, status
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool
import uvicorn

from query_pilot.agent.app import create_agent_app
from query_pilot.agent.auth import (
    SESSION_COOKIE_NAME,
    BootstrapTokenManager,
    SessionManager,
)
from query_pilot.agent.config import AgentConfig
from query_pilot.agent.engine_manager import AgentEngineManager
from query_pilot.db.factory import ProfileConnectionFactory
from query_pilot.db.models import (
    ColumnMetadata,
    DatabaseSchema,
    TableMetadata,
)
from query_pilot.db.profile import (
    ConnectionProfile,
    ProfileStore,
)
from query_pilot.api.models import (
    AnswerGroundingStatus,
    GenerateSQLRequest,
    GenerateSQLResponse,
    GenerationStatus,
    SynthesizeAnswerRequest,
    SynthesizeAnswerResponse,
)

logger = logging.getLogger("browser_test_server")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIST_DIR = REPO_ROOT / "frontend" / "dist"
TEST_BOOTSTRAP_TOKEN = "a1b2c3d4e5f60718293a4b5c6d7e8f901234567890abcdef1234567890abcdef"


@dataclass
class AuditRecord:
    timestamp: float
    method: str
    path: str
    status_code: int


class MockCursorResult:
    """Mock SQLAlchemy CursorResult for deterministic offline query execution."""

    def __init__(self, columns: List[str], rows: List[tuple], scalar_val: Any = None):
        self._columns = columns
        self._rows = rows
        self._scalar_val = scalar_val

    def scalar(self) -> Any:
        return self._scalar_val

    def keys(self) -> List[str]:
        return list(self._columns)

    def fetchmany(self, size: int) -> List[tuple]:
        return self._rows[:size]

    def close(self) -> None:
        pass


class MockProfileConnectionFactory(ProfileConnectionFactory):
    """Deterministic connection factory for offline browser testing."""

    def create_engine(self, profile_id: str, **kwargs) -> Engine:
        profile = self._store.get_profile(profile_id)
        mock_engine = MagicMock(spec=Engine)
        mock_conn = MagicMock(spec=Connection)

        # Trigger simulated connection failure if profile_id or database_name contains 'fail' or 'unreachable'
        name_lower = (profile_id + " " + profile.database_name).lower()
        if "fail" in name_lower or "unreachable" in name_lower:
            exc = OperationalError(
                "08001 Named Pipes Provider: Could not open a connection to SQL Server [2].",
                None,
                None,
            )
            mock_conn.execute.side_effect = exc
            mock_engine.connect.side_effect = exc
            return mock_engine

        if "auth_err" in name_lower:
            exc = OperationalError(
                "28000 Login failed for user 'DOMAIN\\user'. (SSPI)",
                None,
                None,
            )
            mock_conn.execute.side_effect = exc
            mock_engine.connect.side_effect = exc
            return mock_engine

        # DBAPI connection mock with timeout support for driver-level checks
        raw_dbapi = MagicMock()
        raw_dbapi.timeout = profile.timeout_seconds
        mock_conn.connection.dbapi_connection = raw_dbapi

        def execute_statement(statement, *args, **kwargs):
            sql_str = str(statement).strip()

            # 1. DB context verification or scalar DB name probe
            if "DB_NAME()" in sql_str.upper():
                if "db_mismatch" in name_lower:
                    return MockCursorResult(
                        columns=["db_name"],
                        rows=[("Mismatched_DB",)],
                        scalar_val="Mismatched_DB",
                    )
                return MockCursorResult(
                    columns=["db_name"],
                    rows=[(profile.database_name,)],
                    scalar_val=profile.database_name,
                )

            # 2. Timeout simulation trigger via profile configuration
            if "timeout" in name_lower or profile.timeout_seconds == 1:
                raise OperationalError(
                    "SELECT 1",
                    {},
                    Exception("HYT00: [Microsoft][ODBC] Query timeout expired"),
                )

            # 3. Connection disruption simulation
            if "link_fail" in name_lower:
                raise OperationalError(
                    "08S01 Communication link failure",
                    {},
                    Exception("08S01: Connection reset by peer"),
                )

            # 4. Deterministic query execution results based on referenced table
            upper_sql = sql_str.upper()
            if "ORDERS" in upper_sql:
                cols = ["OrderID", "CustomerID", "OrderDate", "TotalAmount"]
                rows = [
                    (1001, 1, "2024-01-15T10:00:00", 250.50),
                    (1002, 2, "2024-01-16T14:30:00", 89.99),
                    (1003, 1, "2024-01-18T09:15:00", 420.00),
                ]
            else:
                # Default Customers table data
                cols = ["CustomerID", "CompanyName", "City"]
                if "truncate" in name_lower or profile.max_row_limit < 3:
                    # Provide enough rows to trigger profile truncation detection
                    rows = [
                        (i, f"Company {i}", "Seattle" if i % 2 == 0 else "Portland")
                        for i in range(1, 15)
                    ]
                else:
                    rows = [
                        (1, "Contoso Ltd", "Seattle"),
                        (2, "Fabrikam Inc", "Seattle"),
                        (3, "Northwind Traders", "Portland"),
                    ]

            return MockCursorResult(
                columns=cols,
                rows=rows,
                scalar_val=rows[0][0] if rows else None,
            )

        mock_conn.execute.side_effect = execute_statement
        mock_conn.execution_options.return_value = mock_conn
        mock_conn.__enter__.return_value = mock_conn
        mock_conn.__exit__.return_value = None
        mock_engine.connect.return_value = mock_conn
        return mock_engine


def build_sample_schema(database_name: str) -> DatabaseSchema:
    """Build a deterministic model-facing schema for testing schema loading."""
    return DatabaseSchema(
        database_name=database_name,
        tables=[
            TableMetadata(
                schema_name="dbo",
                table_name="Customers",
                columns=[
                    ColumnMetadata(name="CustomerID", data_type="int", nullable=False, primary_key=True),
                    ColumnMetadata(name="CompanyName", data_type="nvarchar(100)", nullable=False),
                    ColumnMetadata(name="City", data_type="nvarchar(50)", nullable=True),
                ],
            ),
            TableMetadata(
                schema_name="dbo",
                table_name="Orders",
                columns=[
                    ColumnMetadata(name="OrderID", data_type="int", nullable=False, primary_key=True),
                    ColumnMetadata(name="CustomerID", data_type="int", nullable=False),
                    ColumnMetadata(name="OrderDate", data_type="datetime", nullable=True),
                    ColumnMetadata(name="TotalAmount", data_type="decimal(10,2)", nullable=False),
                ],
            ),
        ],
    )


def find_free_port(bind_host: str = "127.0.0.1") -> int:
    """Allocate an available ephemeral port on the specified loopback host."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((bind_host, 0))
        return int(s.getsockname()[1])


class BrowserTestServer:
    """Encapsulates a managed loopback uvicorn test server for browser runs."""

    def __init__(
        self,
        port: Optional[int] = None,
        bind_host: str = "127.0.0.1",
        dist_dir: Optional[Path] = None,
        temp_dir: Optional[Path] = None,
    ):
        self.port = port if port is not None and port > 0 else find_free_port(bind_host)
        self.bind_host = bind_host
        self.dist_dir = dist_dir or DEFAULT_DIST_DIR
        self._temp_dir_obj: Optional[tempfile.TemporaryDirectory] = None

        if temp_dir is None:
            self._temp_dir_obj = tempfile.TemporaryDirectory(prefix="qp_browser_test_")
            self.temp_dir = Path(self._temp_dir_obj.name)
        else:
            self.temp_dir = temp_dir

        self.token_dir = self.temp_dir / "tokens"
        self.token_dir.mkdir(parents=True, exist_ok=True)
        self.profiles_file = self.temp_dir / "profiles.json"

        self.audit_log: List[AuditRecord] = []
        self.received_generate_requests: List[GenerateSQLRequest] = []
        self._server: Optional[uvicorn.Server] = None
        self._thread: Optional[threading.Thread] = None

        # App dependencies
        self.store = ProfileStore()
        self.config = AgentConfig(
            bind_host=self.bind_host,
            bind_port=self.port,
            token_dir=self.token_dir,
            token_filename="agent.token",
            profiles_file=self.profiles_file,
            static_dir=self.dist_dir,
            allow_dev_origin=True,
        )
        self.factory = MockProfileConnectionFactory(store=self.store)
        self.engine_manager = AgentEngineManager(factory=self.factory)
        self.token_manager = BootstrapTokenManager(token_dir=self.token_dir)
        self._bootstrap_token = self.token_manager.initialize_token()

        # For repeatable offline browser testing, allow the designated bootstrap token
        orig_verify = self.token_manager.verify_and_consume
        def test_verify(candidate: Optional[str]) -> bool:
            if candidate and (candidate == self._bootstrap_token or candidate == TEST_BOOTSTRAP_TOKEN):
                return True
            return orig_verify(candidate)
        self.token_manager.verify_and_consume = test_verify

        self.session_manager = SessionManager()

        # Deterministic mock upstream client for instant offline /api/health responses
        import httpx
        def mock_upstream_handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health":
                return httpx.Response(
                    200,
                    json={
                        "status": "online",
                        "version": "0.1.0-test",
                        "dialect": "mssql",
                        "tracing_enabled": False,
                    },
                )

            if request.url.path == "/api/v1/generate-sql":
                try:
                    req_data = json.loads(request.content.decode("utf-8"))
                    gen_req = GenerateSQLRequest.model_validate(req_data)
                except Exception as exc:
                    return httpx.Response(422, json={"detail": f"Request validation failed: {exc}"})

                self.received_generate_requests.append(gen_req)
                req_id = gen_req.request_id or request.headers.get("x-request-id") or "req-test-mock-gen"
                q_lower = gen_req.question.lower()

                # Controlled generation-failure triggers based on question text
                if "unsupported" in q_lower:
                    resp = GenerateSQLResponse(
                        status=GenerationStatus.UNSUPPORTED,
                        unsupported_reason="The requested business question is not supported by the available database schema.",
                        request_id=req_id,
                    )
                elif "clarification" in q_lower:
                    resp = GenerateSQLResponse(
                        status=GenerationStatus.CLARIFICATION_NEEDED,
                        clarification_prompt="Please specify whether you want active or all customers.",
                        request_id=req_id,
                    )
                elif "fail" in q_lower or "error" in q_lower:
                    resp = GenerateSQLResponse(
                        status=GenerationStatus.GENERATION_FAILED,
                        request_id=req_id,
                    )
                else:
                    # Deterministic candidate SQL matching the active sample schema
                    if "order" in q_lower:
                        candidate_sql = "SELECT OrderID, CustomerID, OrderDate, TotalAmount FROM dbo.Orders WHERE TotalAmount > 100;"
                    else:
                        candidate_sql = "SELECT CustomerID, CompanyName, City FROM dbo.Customers WHERE City = 'Seattle';"

                    resp = GenerateSQLResponse(
                        candidate_sql=candidate_sql,
                        status=GenerationStatus.GENERATED,
                        request_id=req_id,
                    )

                return httpx.Response(200, json=resp.model_dump(mode="json"))

            if request.url.path == "/api/v1/synthesize-answer":
                try:
                    synth_data = json.loads(request.content.decode("utf-8"))
                    synth_req = SynthesizeAnswerRequest.model_validate(synth_data)
                except Exception as exc:
                    return httpx.Response(422, json={"detail": f"Request validation failed: {exc}"})

                synth_resp = SynthesizeAnswerResponse(
                    summary=f"Found {synth_req.row_count} matching records across columns: {', '.join(synth_req.columns)}.",
                    status=AnswerGroundingStatus.GROUNDED_FROM_METADATA,
                    grounding_notes="Offline deterministic test synthesis grounded in result metadata.",
                    request_id=synth_req.request_id,
                )
                return httpx.Response(200, json=synth_resp.model_dump(mode="json"))

            return httpx.Response(404, json={"detail": f"Route '{request.url.path}' not found in mock upstream"})

        self.mock_upstream_client = httpx.AsyncClient(
            transport=httpx.MockTransport(mock_upstream_handler)
        )

        # Build app
        self.app = create_agent_app(
            config=self.config,
            store=self.store,
            factory=self.factory,
            engine_manager=self.engine_manager,
            token_manager=self.token_manager,
            session_manager=self.session_manager,
            upstream_client=self.mock_upstream_client,
        )

        # Attach audit middleware
        @self.app.middleware("http")
        async def audit_middleware(request: Request, call_next):
            response = await call_next(request)
            self.audit_log.append(
                AuditRecord(
                    timestamp=time.time(),
                    method=request.method,
                    path=request.url.path,
                    status_code=response.status_code,
                )
            )
            return response

        # Attach test shutdown endpoint
        @self.app.post("/agent/v1/test/shutdown", include_in_schema=False)
        async def test_shutdown():
            if self._server:
                self._server.should_exit = True
            return {"status": "shutting_down"}

        self._patcher: Optional[Any] = None

    @property
    def base_url(self) -> str:
        return f"http://{self.bind_host}:{self.port}"

    @property
    def app_url(self) -> str:
        return f"{self.base_url}/app/"

    @property
    def bootstrap_token(self) -> str:
        return self._bootstrap_token

    def start(self, timeout_seconds: float = 5.0) -> None:
        """Start server in a background daemon thread and wait for socket readiness."""
        from query_pilot.db.profile_introspector import ProfileDatabaseIntrospector

        def mock_introspect(*args, **kwargs):
            if args:
                intro_self = args[0]
                cfg = getattr(intro_self, "_config", None)
                db = getattr(cfg, "expected_db", "TestDB") if cfg else "TestDB"
            else:
                db = "TestDB"
            return build_sample_schema(db)

        import unittest.mock
        self._patcher = unittest.mock.patch.object(
            ProfileDatabaseIntrospector,
            "introspect_model_facing",
            autospec=True,
            side_effect=mock_introspect,
        )
        self._patcher.start()

        uvicorn_config = uvicorn.Config(
            app=self.app,
            host=self.bind_host,
            port=self.port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config=uvicorn_config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()

        # Wait for socket liveness
        import urllib.request
        deadline = time.time() + timeout_seconds
        started = False
        while time.time() < deadline:
            try:
                req = urllib.request.Request(self.app_url, method="GET")
                with urllib.request.urlopen(req, timeout=0.5) as resp:
                    if resp.status == 200:
                        started = True
                        break
            except Exception:
                time.sleep(0.1)

        if not started:
            self.stop()
            raise RuntimeError(f"Server on {self.app_url} failed to start within {timeout_seconds}s.")

    def __enter__(self) -> BrowserTestServer:
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.stop()

    def stop(self) -> None:
        """Signal uvicorn to exit and join thread cleanly."""
        if hasattr(self, "_patcher") and self._patcher:
            try:
                self._patcher.stop()
            except Exception:
                pass
            self._patcher = None
        if self._server:
            self._server.should_exit = True
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
            if self._thread.is_alive() and self._server:
                self._server.force_exit = True
                self._thread.join(timeout=2.0)
        self._thread = None
        if self._temp_dir_obj:
            try:
                self._temp_dir_obj.cleanup()
            except Exception:
                pass
            self._temp_dir_obj = None


def main():
    parser = argparse.ArgumentParser(description="QueryPilot Browser Test Server")
    parser.add_argument("--port", type=int, default=8005, help="Loopback port (default: 8005)")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Loopback host (default: 127.0.0.1)")
    parser.add_argument("--token", type=str, default=None, help="Custom bootstrap token (default: auto-generated)")
    args = parser.parse_args()

    server = BrowserTestServer(port=args.port, bind_host=args.host)
    if args.token:
        # Overwrite initialized token with specified token
        server.token_dir.mkdir(parents=True, exist_ok=True)
        (server.token_dir / "agent.token").write_text(args.token.strip() + "\n", encoding="utf-8")
        server._bootstrap_token = args.token.strip()

    print(f"Starting browser test server on {server.app_url}...", flush=True)
    print(f"Bootstrap Token: {server.bootstrap_token}", flush=True)
    print("Press Ctrl+C to terminate.", flush=True)

    server.start()
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nStopping browser test server...")
        server.stop()
        print("Server stopped cleanly.")


if __name__ == "__main__":
    main()
