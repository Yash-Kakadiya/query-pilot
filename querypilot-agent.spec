# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller specification for QueryPilot Standalone Local Agent Sidecar.

Packages the local agent into a standalone Windows x64 distribution directory
(dist/querypilot-agent/) containing querypilot-agent.exe and its dependencies,
frontend static assets, and native extensions.
"""

import os
from pathlib import Path
import certifi

block_cipher = None

repo_root = Path(SPECPATH).resolve()
frontend_dist = repo_root / "frontend" / "dist"

datas = []
if frontend_dist.is_dir() and (frontend_dist / "index.html").is_file():
    datas.append((str(frontend_dist), os.path.join("query_pilot", "agent", "static")))

# Explicitly bundle CA certificates for HTTPS upstream communication
datas.append((certifi.where(), "certifi"))

hiddenimports = [
    "uvicorn",
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "sqlalchemy.dialects.sqlite",
    "sqlalchemy.dialects.sqlite.pysqlite",
    "sqlalchemy.dialects.mssql",
    "sqlalchemy.dialects.mssql.pyodbc",
    "sqlglot.dialects",
    "sqlglot.dialects.tsql",
    "sqlglot.dialects.sqlite",
    "pyodbc",
    "sqlite3",
    "query_pilot",
    "query_pilot.agent",
    "query_pilot.agent.app",
    "query_pilot.agent.cli",
    "query_pilot.agent.config",
    "query_pilot.agent.auth",
    "query_pilot.agent.middleware",
    "query_pilot.agent.models",
    "query_pilot.agent.api_router",
    "query_pilot.agent.engine_manager",
    "query_pilot.db",
    "query_pilot.db.connection",
    "query_pilot.db.profile",
    "query_pilot.db.profile_executor",
    "query_pilot.db.profile_introspector",
    "query_pilot.db.sqlite_path",
    "query_pilot.sql",
    "query_pilot.sql.validator",
    "query_pilot.presentation",
    "query_pilot.presentation.formatter",
]

a = Analysis(
    [str(repo_root / "src" / "query_pilot" / "agent" / "cli.py")],
    pathex=[str(repo_root / "src"), str(repo_root)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "scipy",
        "pandas",
        "IPython",
        "notebook",
        "pytest",
        "tests",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="querypilot-agent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="querypilot-agent",
)
