"""SQLite database path validation boundary for QueryPilot.

Enforces strict security checks on SQLite database file paths:
1. Rejects UNC network paths (\\\\server\\share), Windows extended-length paths (\\\\?\\),
   and Windows device paths (\\\\.\\).
2. Rejects DOS reserved device names (CON, PRN, AUX, NUL, COM1-9, LPT1-9).
3. Expands user-home references (~) and resolves symlinks and canonical paths.
4. Requires the database file to exist as a regular filesystem file (not directory,
   pipe, socket, FIFO, or device). Creation of missing files is prohibited.
5. Rejects protected QueryPilot application state (.query_pilot, agent.token, profiles.json),
   environment secrets (.env), source control (.git), and operating-system protected locations
   (e.g., C:\\Windows, /etc, /sys, /proc).
6. Validates the canonical resolved target, not just the user-supplied string.

Residual Limitations:
Filesystem checks are performed at validation time. In standard local operating-system
environments, filesystem state could theoretically be altered between validation and later
connection opening (TOCTOU). QueryPilot does not claim atomic TOCTOU protection at the
filesystem boundary; instead, defense-in-depth is enforced during runtime via Phase 1
connection-level read-only protections (URI mode=ro, PRAGMA query_only=ON, and authorizers).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import re
import stat
from typing import FrozenSet, Optional

from query_pilot.db.profile import ProfileConfigurationError

logger = logging.getLogger(__name__)

# DOS reserved device names on Windows (case-insensitive)
DOS_DEVICE_NAMES: FrozenSet[str] = frozenset({
    "con",
    "conin$",
    "conout$",
    "prn",
    "aux",
    "nul",
    "com1",
    "com2",
    "com3",
    "com4",
    "com5",
    "com6",
    "com7",
    "com8",
    "com9",
    "lpt1",
    "lpt2",
    "lpt3",
    "lpt4",
    "lpt5",
    "lpt6",
    "lpt7",
    "lpt8",
    "lpt9",
})

# POSIX operating-system protected directories
POSIX_PROTECTED_ROOTS: tuple[str, ...] = (
    "/etc",
    "/boot",
    "/sys",
    "/proc",
    "/dev",
    "/bin",
    "/sbin",
    "/usr/bin",
    "/usr/sbin",
)

# Protected internal filenames that must never be accessed as databases
PROTECTED_FILENAMES: FrozenSet[str] = frozenset({
    "agent.token",
    "agent.token.sig",
    "profiles.json",
    "profiles.json.lock",
})


def validate_sqlite_database_path(
    raw_path: str,
    base_dir: Optional[Path | str] = None,
    must_exist: bool = True,
) -> str:
    """Validate and canonicalize a SQLite database file path.

    Args:
        raw_path: User-supplied database path string.
        base_dir: Optional base directory to resolve relative paths against.
        must_exist: If True, requires the database file to already exist on disk.

    Returns:
        Canonical absolute path string to the validated database file.

    Raises:
        ProfileConfigurationError: If the path is invalid, malformed, non-regular,
                                   missing, or attempts access to protected locations.
    """
    if not isinstance(raw_path, str):
        raise ProfileConfigurationError("SQLite database path must be a string.")

    trimmed = raw_path.strip()
    if not trimmed:
        raise ProfileConfigurationError("SQLite database path cannot be blank or whitespace.")

    # 0. Prohibit trailing spaces or dots on the raw path (Win32 normalization bypass)
    if raw_path.endswith(" ") or raw_path.endswith("."):
        raise ProfileConfigurationError(
            "SECURITY REJECTION: Path component contains prohibited trailing spaces or dots."
        )

    # 1. Null bytes & control characters
    if any(ord(c) < 32 or ord(c) == 127 for c in trimmed):
        raise ProfileConfigurationError(
            "SECURITY REJECTION: SQLite database path contains prohibited control characters or null bytes."
        )

    lower_raw = trimmed.lower()

    # 2. In-memory databases and URI parameters
    if lower_raw == ":memory:" or ":memory:" in lower_raw or "?mode=memory" in lower_raw:
        raise ProfileConfigurationError(
            "SECURITY REJECTION: In-memory SQLite databases (:memory:) are prohibited as persistent profiles."
        )

    # 3. URI schemes and URLs
    if "://" in lower_raw or any(
        lower_raw.startswith(scheme) for scheme in ("file:", "http:", "https:", "smb:", "ftp:", "sqlite:", "sqlite3:")
    ):
        raise ProfileConfigurationError(
            "SECURITY REJECTION: URI schemes and URLs are prohibited in SQLite database paths."
        )

    # 4. Windows extended / device paths (check before general UNC and ADS)
    if trimmed.startswith(("\\\\?\\", "//?/")):
        raise ProfileConfigurationError(
            "SECURITY REJECTION: Windows extended-length paths (\\\\?\\...) are prohibited for SQLite database paths."
        )

    if trimmed.startswith(("\\\\.\\", "//./")):
        raise ProfileConfigurationError(
            "SECURITY REJECTION: Windows device paths (\\\\.\\...) are prohibited for SQLite database paths."
        )

    if trimmed.startswith(("\\\\", "//")):
        raise ProfileConfigurationError(
            "SECURITY REJECTION: UNC network paths and network shares (\\\\...) are prohibited for SQLite database paths."
        )

    # 4a. Alternate Data Streams (ADS) and ':' stream specifiers
    path_without_drive = re.sub(r"^[a-zA-Z]:", "", trimmed)
    if ":" in path_without_drive:
        raise ProfileConfigurationError(
            "SECURITY REJECTION: Alternate Data Streams (ADS) and ':' stream specifiers are prohibited in SQLite database paths."
        )

    # 4b. Trailing dots and spaces on components (Win32 filename spoofing / normalization bypass)
    raw_components = [p for p in re.split(r"[\\/]+", trimmed) if p]
    for part in raw_components:
        p_clean = part.rstrip("\\/")
        if not p_clean or (len(p_clean) == 2 and p_clean[1] == ":") or p_clean in {".", ".."}:
            continue
        if p_clean.endswith(" ") or p_clean.endswith("."):
            raise ProfileConfigurationError(
                f"SECURITY REJECTION: Path component '{part}' contains prohibited trailing spaces or dots."
            )

    # 5. DOS reserved device names (including extensions e.g. NUL.db, CON.txt, or in directories)
    for part in raw_components:
        p_clean = part.rstrip("\\/")
        base_component = p_clean.split(".")[0].lower()
        if base_component in DOS_DEVICE_NAMES:
            raise ProfileConfigurationError(
                f"SECURITY REJECTION: DOS reserved device name '{base_component}' (in component '{part}') is prohibited as a database path."
            )

    raw_path_obj = Path(trimmed)
    stem = raw_path_obj.stem.lower()
    name = raw_path_obj.name.lower()
    if stem in DOS_DEVICE_NAMES or name in DOS_DEVICE_NAMES:
        raise ProfileConfigurationError(
            f"SECURITY REJECTION: DOS reserved device name '{raw_path_obj.name}' is prohibited as a database path."
        )

    if hasattr(raw_path_obj, "is_reserved") and raw_path_obj.is_reserved():
        raise ProfileConfigurationError(
            f"SECURITY REJECTION: Reserved device name '{raw_path_obj.name}' is prohibited as a database path."
        )

    # 6. User-home expansion
    expanded = os.path.expanduser(trimmed)

    # 7. Resolution and canonicalization
    try:
        if base_dir is not None and not Path(expanded).is_absolute():
            base = Path(base_dir).resolve()
            target = (base / expanded).resolve()
        else:
            target = Path(expanded).resolve()
    except Exception as exc:
        raise ProfileConfigurationError(
            f"Failed to resolve SQLite database path '{trimmed}': {exc}"
        ) from exc

    resolved_str = str(target)

    # Verify resolved target is not UNC
    if resolved_str.startswith(("\\\\", "//")):
        raise ProfileConfigurationError(
            "SECURITY REJECTION: Resolved database path is a UNC network path, which is prohibited."
        )

    # 8. File existence requirement
    if must_exist:
        if not target.exists():
            raise ProfileConfigurationError(
                f"SQLite database file does not exist at '{target}'. "
                "Creation of missing database files is prohibited."
            )

        # 9. Regular file requirement
        if target.is_dir():
            raise ProfileConfigurationError(
                f"SQLite database path '{target}' is a directory, not a regular file."
            )

        try:
            st = target.stat()
            if not stat.S_ISREG(st.st_mode):
                raise ProfileConfigurationError(
                    f"SQLite database path '{target}' is not a regular file (e.g. pipe, socket, device)."
                )
        except OSError as exc:
            raise ProfileConfigurationError(
                f"Failed to inspect SQLite database file attributes at '{target}': {exc}"
            ) from exc

    # 10. Protected application state and secrets
    resolved_name = target.name.lower()
    resolved_parts = [p.lower() for p in target.parts]
    raw_parts = [p.lower() for p in raw_components]
    all_parts = set()
    for p in resolved_parts + raw_parts:
        all_parts.add(p)
        for sub in re.split(r"[\\/]+", p):
            if sub:
                all_parts.add(sub.lower())

    # Rejection of .env files and environment configurations
    if (
        resolved_name == ".env"
        or resolved_name.startswith(".env.")
        or resolved_name.endswith(".env")
        or any(
            p == ".env"
            or p.startswith(".env.")
            or p.endswith(".env")
            for p in all_parts
        )
    ):
        raise ProfileConfigurationError(
            "SECURITY REJECTION: Access to environment / secret files (.env) is strictly prohibited."
        )

    # Rejection of .git source control contents
    if (
        resolved_name == ".git"
        or resolved_name.startswith(".git.")
        or any(
            p == ".git"
            or p.startswith(".git.")
            or p in {".gitignore", ".gitmodules", ".gitattributes"}
            for p in all_parts
        )
    ):
        raise ProfileConfigurationError(
            "SECURITY REJECTION: Access to source control (.git) contents is strictly prohibited."
        )

    # Rejection of QueryPilot internal tokens and profile configurations
    if (
        resolved_name in PROTECTED_FILENAMES
        or any(p in PROTECTED_FILENAMES for p in all_parts)
        or any(p == ".query_pilot" for p in all_parts)
    ):
        raise ProfileConfigurationError(
            f"SECURITY REJECTION: Access to QueryPilot application configuration and token files "
            f"('{resolved_name}') is strictly prohibited."
        )

    # Rejection of OS-protected locations
    # Windows system root (e.g. C:\Windows)
    sys_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    is_in_sys_root = False
    try:
        resolved_sys_root = Path(sys_root).resolve()
        is_in_sys_root = str(target).lower().startswith(str(resolved_sys_root).lower())
    except (ValueError, TypeError):
        is_in_sys_root = False

    if is_in_sys_root:
        raise ProfileConfigurationError(
            f"SECURITY REJECTION: SQLite database path '{target}' resides in a protected operating-system directory."
        )

    # POSIX protected roots
    for root in POSIX_PROTECTED_ROOTS:
        is_in_posix_root = False
        try:
            is_in_posix_root = target.is_relative_to(Path(root))
        except (ValueError, TypeError):
            is_in_posix_root = False
        if is_in_posix_root:
            raise ProfileConfigurationError(
                f"SECURITY REJECTION: SQLite database path '{target}' resides in a protected operating-system directory ('{root}')."
            )

    return str(target)


# Backward-compatible and clear alias
validate_sqlite_path = validate_sqlite_database_path
