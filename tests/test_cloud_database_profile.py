"""Unit and integration tests for Cloud Database profiles in QueryPilot.

Verifies:
1. Cloud server target policy validation (syntax, port normalization, SSRF/metadata rejection).
2. ConnectionProfile construction, ODBC connection string with SQL Auth, Encrypt=yes, TrustServerCertificate=yes.
3. Credential masking in get_sqlalchemy_url(hide_password=True).
4. ProfileStore serialization and deserialization of cloud profiles.
5. REST endpoints POST /agent/v1/profiles/validate-config and POST /agent/v1/profiles for cloud profiles.
6. Zero leakage of passwords or credentials in ProfileSummary.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import status
from fastapi.testclient import TestClient
import pytest

from query_pilot.agent.app import create_agent_app
from query_pilot.agent.auth import (
    SESSION_COOKIE_NAME,
    BootstrapTokenManager,
    SessionManager,
)
from query_pilot.agent.config import AgentConfig
from query_pilot.agent.engine_manager import AgentEngineManager
from query_pilot.agent.models import (
    CreateProfileRequest,
    ValidateProfileConfigRequest,
    ValidateProfileConfigResponse,
)
from query_pilot.db.factory import ProfileConnectionFactory
from query_pilot.db.profile import (
    ConnectionProfile,
    ProfileConfigurationError,
    ProfileStore,
    load_trusted_profiles_file,
    save_trusted_profiles_file,
    validate_cloud_server_target_policy,
)


# ==============================================================================
# 1. Cloud Server Target Policy Tests
# ==============================================================================


@pytest.mark.parametrize(
    "valid_target,expected_output",
    [
        ("sql5001.monsterasp.net", "sql5001.monsterasp.net"),
        ("sql5001.monsterasp.net,1433", "sql5001.monsterasp.net,1433"),
        ("sql5001.monsterasp.net:1433", "sql5001.monsterasp.net,1433"),
        ("database-1.c9xxxx.us-east-1.rds.amazonaws.com", "database-1.c9xxxx.us-east-1.rds.amazonaws.com"),
        ("database-1.c9xxxx.us-east-1.rds.amazonaws.com:1433", "database-1.c9xxxx.us-east-1.rds.amazonaws.com,1433"),
        ("database-1.c9xxxx.us-east-1.rds.amazonaws.com,1433", "database-1.c9xxxx.us-east-1.rds.amazonaws.com,1433"),
        ("34.123.45.67", "34.123.45.67"),
        ("34.123.45.67:1433", "34.123.45.67,1433"),
        ("34.123.45.67,1433", "34.123.45.67,1433"),
        ("gradesense.database.windows.net", "gradesense.database.windows.net"),
        ("gradesense.database.windows.net:1433", "gradesense.database.windows.net,1433"),
        ("gradesense.database.windows.net,1433", "gradesense.database.windows.net,1433"),
        ("db.example.com", "db.example.com"),
        ("db.example.com,5000", "db.example.com,5000"),
    ],
)
def test_validate_cloud_server_target_policy_accepted(valid_target: str, expected_output: str):
    assert validate_cloud_server_target_policy(valid_target) == expected_output


@pytest.mark.parametrize(
    "rejected_target,match_err",
    [
        ("169.254.169.254", "cloud metadata or link-local"),
        ("169.254.1.1", "cloud metadata or link-local"),
        ("metadata.google.internal", "cloud metadata endpoint"),
        ("server with space", "cannot contain whitespace"),
        ("http://sql.monsterasp.net", "prohibited"),
        ("\\\\sql.monsterasp.net\\share", "prohibited"),
        ("sql.monsterasp.net;DROP_TABLE", "prohibited ODBC delimiter"),
        ("sql.monsterasp.net,999999", "out of valid range"),
    ],
)
def test_validate_cloud_server_target_policy_rejected(rejected_target: str, match_err: str):
    with pytest.raises(ProfileConfigurationError, match=match_err):
        validate_cloud_server_target_policy(rejected_target)


# ==============================================================================
# 2. ConnectionProfile Cloud MSSQL Tests
# ==============================================================================


def test_cloud_mssql_profile_generates_correct_odbc_string():
    profile = ConnectionProfile(
        profile_id="gradesense_cloud",
        display_name="GradeSense Cloud DB",
        server="sql5001.monsterasp.net,1433",
        database_name="gradesense_db",
        engine_type="mssql",
        is_cloud=True,
        use_trusted_connection=False,
        user="gradesense_admin",
        password="MySecretPassword123!",
        encrypt=True,
        trust_server_certificate=True,
    )

    odbc_str = profile.get_odbc_connection_string(hide_password=False)
    assert "DRIVER={ODBC Driver 17 for SQL Server}" in odbc_str
    assert "SERVER=sql5001.monsterasp.net,1433" in odbc_str
    assert "DATABASE=gradesense_db" in odbc_str
    assert "UID=gradesense_admin" in odbc_str
    assert "PWD=MySecretPassword123!" in odbc_str
    assert "Encrypt=yes" in odbc_str
    assert "TrustServerCertificate=yes" in odbc_str
    assert "Trusted_Connection=yes" not in odbc_str


def test_cloud_mssql_database_name_with_dots_and_dashes():
    """Verify that cloud database names can contain dots and dashes (e.g. MonsterASP.net db52589.databaseasp.net)."""
    from query_pilot.db.profile import validate_safe_database_identifier
    from query_pilot.agent.models import ValidateProfileConfigRequest

    # Should accept dotted and dashed names when is_cloud=True
    assert validate_safe_database_identifier("db52589.databaseasp.net", is_cloud=True) == "db52589.databaseasp.net"
    assert validate_safe_database_identifier("my-cloud-db", is_cloud=True) == "my-cloud-db"

    # Should reject path traversal or empty dots
    with pytest.raises(Exception):
        validate_safe_database_identifier("..invalid", is_cloud=True)
    with pytest.raises(Exception):
        validate_safe_database_identifier("invalid..db", is_cloud=True)

    # ValidateProfileConfigRequest accepts it
    req = ValidateProfileConfigRequest(
        profile_id="gradesense_cloud",
        display_name="GradeSense Cloud",
        engine_type="mssql",
        is_cloud=True,
        server="db52589.public.databaseasp.net",
        database_name="db52589.databaseasp.net",
        user="myuser",
        password="mypassword",
        use_trusted_connection=False,
    )
    assert req.database_name == "db52589.databaseasp.net"
    assert req.server == "db52589.public.databaseasp.net"


def test_cloud_mssql_profile_masks_password_in_safe_url():
    profile = ConnectionProfile(
        profile_id="gradesense_cloud",
        display_name="GradeSense Cloud DB",
        server="sql5001.monsterasp.net",
        database_name="gradesense_db",
        engine_type="mssql",
        is_cloud=True,
        use_trusted_connection=False,
        user="gradesense_admin",
        password="MySecretPassword123!",
    )

    safe_url = profile.get_sqlalchemy_url(hide_password=True)
    assert "PWD%3D%2A%2A%2A%2A%2A%2A%2A%2A" in safe_url or "PWD=********" in safe_url
    assert "MySecretPassword123!" not in safe_url


def test_cloud_mssql_profile_requires_user_and_password():
    with pytest.raises(Exception, match="user must be specified"):
        ConnectionProfile(
            profile_id="gradesense_cloud",
            display_name="GradeSense Cloud DB",
            server="sql5001.monsterasp.net",
            database_name="gradesense_db",
            engine_type="mssql",
            is_cloud=True,
            use_trusted_connection=False,
            password="pwd",
        )

    with pytest.raises(Exception, match="password must be specified"):
        ConnectionProfile(
            profile_id="gradesense_cloud",
            display_name="GradeSense Cloud DB",
            server="sql5001.monsterasp.net",
            database_name="gradesense_db",
            engine_type="mssql",
            is_cloud=True,
            use_trusted_connection=False,
            user="user",
        )


# ==============================================================================
# 3. Disk Persistence (save and load trusted profiles file)
# ==============================================================================


def test_save_and_load_cloud_profile(tmp_path: Path):
    store = ProfileStore()
    cloud_profile = ConnectionProfile(
        profile_id="gradesense_monsterasp",
        display_name="GradeSense MonsterASP DB",
        server="sql5001.monsterasp.net,1433",
        database_name="gradesense_prod",
        engine_type="mssql",
        is_cloud=True,
        use_trusted_connection=False,
        user="db_user_123",
        password="SuperSecretPassword!",
        encrypt=True,
        trust_server_certificate=True,
    )
    store.register_profile(cloud_profile)
    store.set_default_profile("gradesense_monsterasp")

    save_path = tmp_path / "profiles.json"
    save_trusted_profiles_file(save_path, store, restrict_permissions=False)

    loaded_store = load_trusted_profiles_file(save_path)
    assert "gradesense_monsterasp" in loaded_store.profiles
    loaded_profile = loaded_store.get_profile("gradesense_monsterasp")
    assert loaded_profile.is_cloud is True
    assert loaded_profile.use_trusted_connection is False
    assert loaded_profile.user == "db_user_123"
    assert loaded_profile.password.get_secret_value() == "SuperSecretPassword!"
    assert loaded_profile.server == "sql5001.monsterasp.net,1433"
    assert loaded_profile.database_name == "gradesense_prod"
    assert loaded_profile.encrypt is True
    assert loaded_profile.trust_server_certificate is True


# ==============================================================================
# 4. Agent Endpoint Tests (validate-config & create-profile)
# ==============================================================================


@pytest.fixture
def agent_test_client(tmp_path: Path):
    profiles_path = tmp_path / "profiles.json"
    store = ProfileStore()
    save_trusted_profiles_file(profiles_path, store, restrict_permissions=False)

    token_mgr = BootstrapTokenManager(token_dir=tmp_path)
    token = token_mgr.initialize_token()
    session_mgr = SessionManager()

    config = AgentConfig(
        token_dir=tmp_path,
        profiles_file=profiles_path,
    )
    factory = ProfileConnectionFactory(store=store)
    engine_mgr = AgentEngineManager(factory=factory)
    app = create_agent_app(
        config=config,
        store=store,
        factory=factory,
        token_manager=token_mgr,
        session_manager=session_mgr,
        engine_manager=engine_mgr,
    )
    client = TestClient(app, base_url="http://127.0.0.1:8001")
    login_resp = client.post(
        "/agent/v1/auth/bootstrap",
        headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
        json={"bootstrap_token": token},
    )
    assert login_resp.status_code == status.HTTP_200_OK
    session_id = login_resp.cookies.get(SESSION_COOKIE_NAME)
    assert session_id is not None

    return {
        "client": client,
        "session_id": session_id,
        "profiles_path": profiles_path,
    }


def test_endpoint_validate_cloud_config_success(agent_test_client):
    client = agent_test_client["client"]
    session_id = agent_test_client["session_id"]

    payload = {
        "profile_id": "gradesense_cloud_test",
        "display_name": "GradeSense MonsterASP Cloud",
        "server": "sql5001.monsterasp.net:1433",
        "database_name": "gradesense_db",
        "engine_type": "mssql",
        "is_cloud": True,
        "user": "gradesense_user",
        "password": "Password123!",
        "encrypt": True,
        "trust_server_certificate": True,
    }
    resp = client.post(
        "/agent/v1/profiles/validate-config",
        headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: session_id},
        json=payload,
    )
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["valid"] is True


def test_endpoint_create_cloud_profile_success(agent_test_client):
    client = agent_test_client["client"]
    session_id = agent_test_client["session_id"]

    payload = {
        "profile_id": "gradesense_cloud_created",
        "display_name": "GradeSense MonsterASP Cloud",
        "server": "sql5001.monsterasp.net,1433",
        "database_name": "gradesense_db",
        "engine_type": "mssql",
        "is_cloud": True,
        "user": "gradesense_user",
        "password": "Password123!",
        "set_as_default": True,
    }
    resp = client.post(
        "/agent/v1/profiles",
        headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: session_id},
        json=payload,
    )
    assert resp.status_code == status.HTTP_201_CREATED
    data = resp.json()
    profile = data["profile"]
    assert profile["profile_id"] == "gradesense_cloud_created"
    assert profile["display_name"] == "GradeSense MonsterASP Cloud"
    assert profile["engine_type"] == "mssql"
    assert profile["database_name"] == "gradesense_db"
    assert profile["is_cloud"] is True
    assert profile["is_default"] is True

    # Critical security invariant: passwords, usernames, and server strings NEVER leaked in summary
    assert "password" not in profile
    assert "user" not in profile
    assert "server" not in profile


def test_cloud_mssql_batch_introspection():
    """Verify that _introspect_mssql_batch executes batch catalog queries and builds TableMetadata."""
    from query_pilot.db.profile_introspector import ProfileDatabaseIntrospector

    cloud_profile = ConnectionProfile(
        profile_id="test_cloud_batch",
        display_name="Cloud Batch Test",
        server="sql5001.monsterasp.net,1433",
        database_name="gradesense_db",
        engine_type="mssql",
        is_cloud=True,
        user="test_user",
        password="test_password",
    )

    mock_conn = MagicMock()
    # Mock tables query
    mock_tables_res = MagicMock()
    mock_tables_res.fetchall.return_value = [("Students",), ("Courses",)]

    # Mock columns query
    mock_cols_res = MagicMock()
    mock_cols_res.fetchall.return_value = [
        ("Students", "Id", "int", False, True, None, 1),
        ("Students", "Name", "nvarchar", False, False, None, 0),
        ("Courses", "Id", "int", False, True, None, 1),
    ]

    # Mock foreign keys query
    mock_fks_res = MagicMock()
    mock_fks_res.fetchall.return_value = []

    # Mock indexes query
    mock_idx_res = MagicMock()
    mock_idx_res.fetchall.return_value = []

    mock_conn.execute.side_effect = [
        mock_tables_res,
        mock_cols_res,
        mock_fks_res,
        mock_idx_res,
    ]

    introspector = ProfileDatabaseIntrospector(config=cloud_profile)
    tables = introspector._introspect_mssql_batch(
        conn=mock_conn,
        target_schema="dbo",
        allowed_set=None,
        metadata_schema_name="dbo",
    )

    assert tables is not None
    assert len(tables) == 2
    assert tables[0].table_name == "Courses"
    assert tables[1].table_name == "Students"
    assert len(tables[1].columns) == 2
    assert tables[1].primary_keys == ["Id"]
    assert tables[1].columns[0].is_identity is True


def test_cloud_profiles_multi_cloud_platform_validation():
    """Verify ValidateProfileConfigRequest accepts valid AWS RDS, GCP Cloud SQL, and Azure SQL configs."""
    # 1. AWS RDS
    aws_req = ValidateProfileConfigRequest(
        profile_id="aws_rds_prod",
        display_name="AWS RDS SQL Server",
        engine_type="mssql",
        is_cloud=True,
        server="database-1.c9xxxx.us-east-1.rds.amazonaws.com:1433",
        database_name="production_db",
        schema_name="dbo",
        driver="ODBC Driver 17 for SQL Server",
        use_trusted_connection=False,
        user="admin",
        password="AwsMasterPassword123!",
        encrypt=True,
        trust_server_certificate=True,
    )
    # Server should normalize colon to comma
    assert aws_req.server == "database-1.c9xxxx.us-east-1.rds.amazonaws.com,1433"
    assert aws_req.user == "admin"

    aws_profile = ConnectionProfile(**aws_req.model_dump(exclude={"set_as_default"}))
    assert aws_profile.server == "database-1.c9xxxx.us-east-1.rds.amazonaws.com,1433"
    assert "UID=admin" in aws_profile.get_odbc_connection_string(hide_password=False)
    assert "PWD=AwsMasterPassword123!" in aws_profile.get_odbc_connection_string(hide_password=False)

    # 2. GCP Cloud SQL
    gcp_req = ValidateProfileConfigRequest(
        profile_id="gcp_sql_prod",
        display_name="GCP Cloud SQL Server",
        engine_type="mssql",
        is_cloud=True,
        server="34.123.45.67",
        database_name="gcp_db",
        schema_name="dbo",
        driver="ODBC Driver 17 for SQL Server",
        use_trusted_connection=False,
        user="sqlserver",
        password="GcpSqlPassword456!",
        encrypt=True,
        trust_server_certificate=True,
    )
    assert gcp_req.server == "34.123.45.67"
    gcp_profile = ConnectionProfile(**gcp_req.model_dump(exclude={"set_as_default"}))
    assert "UID=sqlserver" in gcp_profile.get_odbc_connection_string(hide_password=False)
    assert "SERVER=34.123.45.67" in gcp_profile.get_odbc_connection_string(hide_password=False)

    # 3. Azure SQL Database
    azure_req = ValidateProfileConfigRequest(
        profile_id="azure_sql_prod",
        display_name="Azure SQL Database",
        engine_type="mssql",
        is_cloud=True,
        server="myserver.database.windows.net",
        database_name="azure_sales_db",
        schema_name="dbo",
        driver="ODBC Driver 17 for SQL Server",
        use_trusted_connection=False,
        user="serveradmin",
        password="AzurePassword789!",
        encrypt=True,
        trust_server_certificate=True,
    )
    assert azure_req.server == "myserver.database.windows.net"
    azure_profile = ConnectionProfile(**azure_req.model_dump(exclude={"set_as_default"}))
    assert "UID=serveradmin" in azure_profile.get_odbc_connection_string(hide_password=False)
    assert "SERVER=myserver.database.windows.net" in azure_profile.get_odbc_connection_string(hide_password=False)


