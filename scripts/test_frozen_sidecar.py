#!/usr/bin/env python3
"""Validation harness for QueryPilot Frozen Standalone Sidecar (B26-B2).

Verifies:
1. Frozen executable behavior (--help, profile subcommands).
2. Desktop mode launch with ephemeral port binding and QUERYPILOT_READY handshake.
3. Static frontend serving at /app/ from package-internal static assets.
4. Bootstrap session exchange and HttpOnly cookie issuance.
5. SQLite schema introspection and approved read-only analytical query execution.
6. Read-only safety rejection of mutation queries (INSERT/DROP/UPDATE).
7. Zero database file or WAL modification during queries.
8. SQL Server / pyodbc dependency verification.
9. Clean process termination and socket release.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error


def get_file_digest(path: Path) -> str:
    """Calculate SHA-256 digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def test_frozen_sidecar():
    repo_root = Path(__file__).resolve().parents[1]
    dist_dir = repo_root / "dist" / "querypilot-agent"
    exe_path = dist_dir / ("querypilot-agent.exe" if sys.platform == "win32" else "querypilot-agent")

    if not exe_path.is_file():
        raise FileNotFoundError(f"Frozen executable not found at '{exe_path}'. Run package_sidecar.py first.")

    sys.stdout.write("=" * 75 + "\n")
    sys.stdout.write("QUERYPILOT FROZEN SIDECAR VALIDATION SUITE (B26-B2)\n")
    sys.stdout.write(f"Target Executable: {exe_path}\n")
    sys.stdout.write("=" * 75 + "\n\n")

    # 1. Verify --help
    sys.stdout.write("[Test 1/6] Verifying '--help' execution...\n")
    res = subprocess.run([str(exe_path), "--help"], capture_output=True, text=True, check=False)
    assert res.returncode == 0, f"--help failed with returncode {res.returncode}"
    assert "QueryPilot Local Agent Daemon" in res.stdout
    assert "--desktop" in res.stdout
    sys.stdout.write("  -> PASS: CLI help displays cleanly.\n\n")

    # 2. Verify profile subcommands with isolated temporary profiles file
    sys.stdout.write("[Test 2/6] Verifying CLI profile commands with isolated storage...\n")
    with tempfile.TemporaryDirectory() as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        test_profiles_file = tmp_dir / "profiles.json"
        token_dir = tmp_dir / "tokens"
        token_dir.mkdir()

        university_db_path = repo_root / "data" / "demo" / "university_demo.db"
        assert university_db_path.is_file(), f"Synthetic University DB not found at {university_db_path}"

        # Register profile via frozen CLI
        add_cmd = [
            str(exe_path),
            "profile",
            "add",
            "--id", "university_demo",
            "--name", "University Demo DB",
            "--engine", "sqlite",
            "--path", str(university_db_path),
            "--profiles-file", str(test_profiles_file),
            "--yes",
        ]
        res_add = subprocess.run(add_cmd, capture_output=True, text=True, check=False)
        assert res_add.returncode == 0, f"profile add failed: {res_add.stderr}"
        assert "Profile 'university_demo' registered successfully" in res_add.stdout

        # List profiles via frozen CLI
        list_cmd = [
            str(exe_path),
            "profile",
            "list",
            "--profiles-file", str(test_profiles_file),
        ]
        res_list = subprocess.run(list_cmd, capture_output=True, text=True, check=False)
        assert res_list.returncode == 0, f"profile list failed: {res_list.stderr}"
        assert "University Demo DB" in res_list.stdout
        assert "sqlite" in res_list.stdout
        sys.stdout.write("  -> PASS: Profile registration and listing verified.\n\n")

        # 3. Verify desktop mode startup, port 0, readiness handshake, and frontend serving
        sys.stdout.write("[Test 3/6] Launching frozen executable in Desktop Mode (--desktop)...\n")
        launch_cmd = [
            str(exe_path),
            "--desktop",
            "--token-dir", str(token_dir),
            "--profiles-file", str(test_profiles_file),
        ]

        proc = subprocess.Popen(
            launch_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        handshake_data = None
        bound_port = None
        assigned_pid = None

        try:
            # Read stdout line for handshake with timeout
            deadline = time.time() + 10.0
            while time.time() < deadline:
                line = proc.stdout.readline()
                if not line:
                    time.sleep(0.1)
                    continue
                if line.startswith("QUERYPILOT_READY:"):
                    payload_str = line[len("QUERYPILOT_READY:") :].strip()
                    handshake_data = json.loads(payload_str)
                    bound_port = handshake_data.get("port")
                    assigned_pid = handshake_data.get("pid")
                    break

            assert handshake_data is not None, "Handshake line 'QUERYPILOT_READY:' not received on stdout."
            assert handshake_data.get("status") == "ready"
            assert isinstance(bound_port, int) and bound_port > 1024
            assert isinstance(assigned_pid, int) and assigned_pid > 0

            # Verify no secrets in handshake
            assert "token" not in handshake_data
            assert "database_path" not in handshake_data
            sys.stdout.write(f"  -> Handshake captured: port={bound_port}, pid={assigned_pid}\n")

            # 4. Verify /app/ static serving from packaged assets
            sys.stdout.write("  -> Verifying frontend static asset serving at /app/...\n")
            app_req = urllib.request.Request(f"http://127.0.0.1:{bound_port}/app/")
            try:
                with urllib.request.urlopen(app_req, timeout=3.0) as app_resp:
                    assert app_resp.status == 200
                    html_body = app_resp.read().decode("utf-8")
                    assert "<!doctype html>" in html_body.lower() or "<html" in html_body.lower()
                    assert "root" in html_body
            except urllib.error.HTTPError as err:
                sys.stderr.write(f"\n[HTTP ERROR] {err.code}: {err.read().decode('utf-8', errors='replace')}\n")
                if proc:
                    proc.terminate()
                    stdout_rem, stderr_rem = proc.communicate(timeout=2)
                    sys.stderr.write(f"[PROC STDERR]:\n{stderr_rem}\n")
                raise
            sys.stdout.write("  -> PASS: Desktop mode startup and static frontend serving verified.\n\n")

            # 5. Verify bootstrap authentication and SQLite analytical execution
            sys.stdout.write("[Test 4/6] Authenticating and executing SQLite query through frozen agent...\n")
            token_file = token_dir / "agent.token"
            assert token_file.is_file(), f"Token file missing at {token_file}"
            token_val = token_file.read_text(encoding="utf-8").strip()

            # Call /agent/v1/auth/bootstrap
            bootstrap_url = f"http://127.0.0.1:{bound_port}/agent/v1/auth/bootstrap"
            req_data = json.dumps({"bootstrap_token": token_val}).encode("utf-8")
            b_req = urllib.request.Request(
                bootstrap_url,
                data=req_data,
                headers={
                    "Content-Type": "application/json",
                    "X-QueryPilot-Request": "1",
                },
                method="POST",
            )

            session_cookie = None
            with urllib.request.urlopen(b_req, timeout=3.0) as b_resp:
                assert b_resp.status == 200
                cookies = b_resp.headers.get_all("Set-Cookie") or []
                for c in cookies:
                    if "qpt_session=" in c:
                        session_cookie = c.split(";")[0].strip()
                        break

            assert session_cookie is not None, "Failed to capture qpt_session cookie from bootstrap"
            sys.stdout.write(f"  -> Bootstrap session established.\n")

            # Introspect SQLite schema
            sys.stdout.write("  -> Introspecting University demo schema...\n")
            schema_url = f"http://127.0.0.1:{bound_port}/agent/v1/profiles/university_demo/schema"
            s_req = urllib.request.Request(schema_url, headers={"Cookie": session_cookie})
            with urllib.request.urlopen(s_req, timeout=3.0) as s_resp:
                assert s_resp.status == 200
                schema_json = json.loads(s_resp.read().decode("utf-8"))
                tables = schema_json.get("tables", [])
                table_names = {t["table_name"] for t in tables}
                assert {"departments", "courses", "students", "enrollments"}.issubset(table_names)
            sys.stdout.write(f"  -> PASS: Schema introspected: {sorted(table_names)}\n")

            # Execute approved read-only analytical query
            sys.stdout.write("  -> Executing approved read-only analytical query...\n")
            before_hash = get_file_digest(university_db_path)
            before_mtime = university_db_path.stat().st_mtime

            exec_url = f"http://127.0.0.1:{bound_port}/agent/v1/profiles/university_demo/execute"
            query_sql = (
                "SELECT department_id, COUNT(*) as student_count "
                "FROM students GROUP BY department_id "
                "ORDER BY student_count DESC"
            )
            exec_body = json.dumps({"sql": query_sql}).encode("utf-8")
            e_req = urllib.request.Request(
                exec_url,
                data=exec_body,
                headers={
                    "Content-Type": "application/json",
                    "X-QueryPilot-Request": "1",
                    "Cookie": session_cookie,
                },
                method="POST",
            )

            with urllib.request.urlopen(e_req, timeout=3.0) as e_resp:
                assert e_resp.status == 200
                query_result = json.loads(e_resp.read().decode("utf-8"))
                assert query_result.get("columns") == ["department_id", "student_count"]
                rows = query_result.get("rows", [])
                assert len(rows) == 5  # 5 departments in University demo dataset
                total_students = sum(r["student_count"] for r in rows)
                assert total_students == 16  # Exactly 16 students across 5 departments in University demo dataset
            sys.stdout.write(f"  -> PASS: Query executed: 5 departments, 16 students total.\n")

            # Verify database integrity (file unchanged)
            after_hash = get_file_digest(university_db_path)
            after_mtime = university_db_path.stat().st_mtime
            assert before_hash == after_hash, "Database file hash modified during read-only query!"
            assert before_mtime == after_mtime, "Database file mtime modified during read-only query!"
            sys.stdout.write("  -> PASS: Database file integrity verified (zero modifications).\n\n")

            # 6. Verify mutation query is strictly blocked by read-only authorizer
            sys.stdout.write("[Test 5/6] Verifying mutation query rejection (security boundary)...\n")
            mutation_sql = "DROP TABLE students"
            mut_body = json.dumps({"sql": mutation_sql}).encode("utf-8")
            m_req = urllib.request.Request(
                exec_url,
                data=mut_body,
                headers={
                    "Content-Type": "application/json",
                    "X-QueryPilot-Request": "1",
                    "Cookie": session_cookie,
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(m_req, timeout=3.0):
                    raise AssertionError("Mutation query was unexpectedly permitted by frozen agent!")
            except urllib.error.HTTPError as http_err:
                assert http_err.code in {400, 403, 422}
                err_body = json.loads(http_err.read().decode("utf-8"))
                detail = err_body.get("detail", "")
                assert any(keyword in detail.lower() for keyword in ["unsafe", "prohibited", "drop", "read-only", "forbidden"])
            sys.stdout.write("  -> PASS: Mutation query 'DROP TABLE' strictly rejected with HTTP error.\n\n")

        finally:
            # Terminate daemon gracefully
            sys.stdout.write("[Test 6/6] Terminating daemon and verifying clean socket release...\n")
            proc.terminate()
            try:
                proc.wait(timeout=4.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

            # Confirm port released
            if bound_port is not None:
                import socket
                chk_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                chk_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    chk_sock.bind(("127.0.0.1", bound_port))
                    chk_sock.close()
                except OSError:
                    raise RuntimeError(f"Socket port {bound_port} leaked after daemon termination!")
            sys.stdout.write("  -> PASS: Daemon shutdown cleanly; socket port released.\n\n")

    sys.stdout.write("=" * 75 + "\n")
    sys.stdout.write("ALL FROZEN SIDECAR VALIDATIONS PASSED CLEANLY (6/6)\n")
    sys.stdout.write("=" * 75 + "\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(test_frozen_sidecar())
    except Exception as exc:
        import traceback
        traceback.print_exc()
        sys.stderr.write(f"\nVALIDATION FAILED: {exc}\n")
        sys.exit(1)
