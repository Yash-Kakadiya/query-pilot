"""Authentication and session management for QueryPilot Local Agent Daemon.

Implements:
1. Cryptographically secure 256-bit single-use bootstrap tokens.
2. Constant-time token verification (secrets.compare_digest).
3. Immediate invalidation and disk removal of consumed tokens.
4. OS-specific file protection (POSIX 0600 / Windows ACLs).
5. Thread-safe in-memory session store with idle and absolute expiry limits.
6. Token rotation and complete session revocation.
7. Zero token or credential leakage into logs.
"""

from __future__ import annotations

import getpass
import logging
import os
from pathlib import Path
import platform
import secrets
import stat
import subprocess
import threading
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# Canonical session cookie name
SESSION_COOKIE_NAME = "qpt_session"


class Session:
    """Represents an active, authenticated loopback session."""

    def __init__(
        self,
        session_id: str,
        created_at: Optional[float] = None,
        last_activity: Optional[float] = None,
    ):
        now = time.time()
        self.session_id = session_id
        self.created_at = created_at if created_at is not None else now
        self.last_activity = last_activity if last_activity is not None else now

    def is_valid(
        self,
        idle_timeout_seconds: int = 3600,
        max_lifetime_seconds: int = 43200,
        now: Optional[float] = None,
    ) -> bool:
        """Evaluate if the session is still active under idle and absolute limits."""
        current_time = now if now is not None else time.time()

        # Check absolute lifetime
        if current_time - self.created_at > max_lifetime_seconds:
            return False

        # Check idle timeout
        if current_time - self.last_activity > idle_timeout_seconds:
            return False

        return True

    def touch(self, now: Optional[float] = None) -> None:
        """Update last activity timestamp on active requests."""
        self.last_activity = now if now is not None else time.time()


class SessionManager:
    """Thread-safe in-memory manager for active browser loopback sessions."""

    def __init__(
        self,
        idle_timeout_seconds: int = 3600,
        max_lifetime_seconds: int = 43200,
    ):
        self.idle_timeout_seconds = idle_timeout_seconds
        self.max_lifetime_seconds = max_lifetime_seconds
        self._sessions: Dict[str, Session] = {}
        self._lock = threading.Lock()

    def create_session() -> Session: ...

    def create_session(self) -> Session:
        """Create and register a new active session with a cryptographically secure token."""
        session_id = secrets.token_urlsafe(32)
        session = Session(session_id=session_id)
        with self._lock:
            self._prune_expired_locked()
            self._sessions[session_id] = session
        return session

    def get_valid_session(self, session_id: Optional[str]) -> Optional[Session]:
        """Retrieve an active session by ID if it has not expired, and refresh its activity timestamp."""
        if not session_id:
            return None

        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None

            if not session.is_valid(
                idle_timeout_seconds=self.idle_timeout_seconds,
                max_lifetime_seconds=self.max_lifetime_seconds,
            ):
                # Expired: remove and return None
                self._sessions.pop(session_id, None)
                return None

            session.touch()
            return session

    def invalidate_session(self, session_id: Optional[str]) -> bool:
        """Invalidate and remove a specific session (e.g. on logout)."""
        if not session_id:
            return False
        with self._lock:
            return self._sessions.pop(session_id, None) is not None

    def invalidate_all(self) -> int:
        """Invalidate all active sessions (e.g. on token rotation or shutdown)."""
        with self._lock:
            count = len(self._sessions)
            self._sessions.clear()
            return count

    def active_session_count(self) -> int:
        """Return the number of currently active, non-expired sessions."""
        with self._lock:
            self._prune_expired_locked()
            return len(self._sessions)

    def _prune_expired_locked(self) -> None:
        """Prune expired sessions while holding the internal lock."""
        now = time.time()
        expired = [
            sid
            for sid, s in self._sessions.items()
            if not s.is_valid(
                idle_timeout_seconds=self.idle_timeout_seconds,
                max_lifetime_seconds=self.max_lifetime_seconds,
                now=now,
            )
        ]
        for sid in expired:
            self._sessions.pop(sid, None)


class BootstrapTokenProvisioningError(Exception):
    """Raised when bootstrap token provisioning or permission hardening fails."""

    pass


def _get_active_windows_trustee() -> str:
    """Return the qualified Windows username/trustee for ACL operations.

    Uses `whoami` to obtain the exact authenticated domain\\user from the active
    process token. Falls back to getpass.getuser() qualified with USERDOMAIN if whoami fails.
    Fails closed if the identity cannot be resolved.
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
        raise BootstrapTokenProvisioningError(
            "Cannot determine active Windows username for ACL hardening."
        )
    domain = os.environ.get("USERDOMAIN", "").strip()
    if domain and "\\" not in fallback:
        return f"{domain}\\{fallback}"
    return fallback


class BootstrapTokenManager:
    """Manages creation, restricted-permission file provisioning, constant-time verification,

    and single-use consumption of local agent bootstrap tokens.
    """

    def __init__(
        self,
        token_dir: Path,
        token_filename: str = "agent.token",
        auto_initialize: bool = False,
    ):
        self.token_dir = Path(token_dir)
        self.token_filename = token_filename
        self.token_path = self.token_dir / self.token_filename
        self._token: Optional[str] = None
        self._is_consumed: bool = False
        self._lock = threading.Lock()

        if auto_initialize:
            self.initialize_token()

    @property
    def has_active_token(self) -> bool:
        """True if an unconsumed bootstrap token is currently active."""
        with self._lock:
            return self._token is not None and not self._is_consumed

    def initialize_token(self) -> str:
        """Generate a new 256-bit bootstrap token, provision it to disk with strict permissions, and store in memory.

        Fails closed: if directory creation, symlink validation, file writing, or ACL hardening
        fails, any partial file is unlinked, the in-memory token is cleared, and
        BootstrapTokenProvisioningError is raised.
        """
        token = secrets.token_hex(32)

        with self._lock:
            # Clear memory token and reset state before provisioning
            self._token = None
            self._is_consumed = False

            # Ensure parent directory exists
            try:
                self.token_dir.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                raise BootstrapTokenProvisioningError(
                    f"Could not create token directory '{self.token_dir}': {exc}"
                ) from exc

            # Write token to file with verified restricted OS permissions
            try:
                self._write_token_file(token)
                self._token = token
            except Exception as exc:
                # Fail closed: wipe file and reset internal state
                self._token = None
                self._is_consumed = False
                self._wipe_token_file()
                logger.warning("Bootstrap token provisioning aborted; failed closed: %s", exc)
                raise

        return token

    def verify_and_consume(self, candidate_token: Optional[str]) -> bool:
        """Verify the submitted candidate token in constant time.

        If matching and active, immediately consumes the token, invalidates it in memory,
        and deletes the token file from disk.
        """
        if not candidate_token or not candidate_token.strip():
            return False

        with self._lock:
            if self._is_consumed or self._token is None:
                return False

            # Constant-time comparison prevents timing attacks
            is_valid = secrets.compare_digest(candidate_token.strip(), self._token.strip())

            if is_valid:
                # Immediate consumption: single-use guarantee
                self._is_consumed = True
                self._token = None
                self._wipe_token_file()
                return True

            return False

    def rotate_token(self) -> str:
        """Rotate the bootstrap token: invalidates previous token and provisions a fresh one."""
        return self.initialize_token()

    def _write_token_file(self, token: str) -> None:
        """Write token to disk with OS-appropriate permission hardening and symlink defense."""
        # 1. Symlink / Reparse Point Protection
        if self.token_path.is_symlink() or os.path.islink(str(self.token_path)):
            raise BootstrapTokenProvisioningError(
                f"SECURITY VIOLATION: Bootstrap token path '{self.token_path}' is an unsafe symlink or reparse point. Refusing to write."
            )

        if self.token_path.exists():
            # Safely remove existing regular file before re-provisioning
            try:
                self.token_path.unlink()
            except Exception as exc:
                raise BootstrapTokenProvisioningError(
                    f"Failed to remove pre-existing token file at '{self.token_path}': {exc}"
                ) from exc

        # 2. POSIX Platform: Exclusive creation with 0600 mode and O_NOFOLLOW
        if platform.system() != "Windows":
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                fd = os.open(str(self.token_path), flags, stat.S_IRUSR | stat.S_IWUSR)
                with open(fd, "w", encoding="utf-8") as f:
                    f.write(token)
                os.chmod(str(self.token_path), stat.S_IRUSR | stat.S_IWUSR)
            except Exception as exc:
                raise BootstrapTokenProvisioningError(
                    f"POSIX token file provisioning failed: {exc}"
                ) from exc
            return

        # 3. Windows Platform: Fail-closed write and explicit icacls ACL verification
        try:
            self.token_path.write_text(token, encoding="utf-8")
        except Exception as exc:
            raise BootstrapTokenProvisioningError(
                f"Failed to write Windows token file at '{self.token_path}': {exc}"
            ) from exc

        self._restrict_windows_acl(self.token_path)

    def _restrict_windows_acl(self, path: Path) -> None:
        """Harden Windows file permissions to restrict access exclusively to the current user.

        Fails closed: if icacls is missing, times out, returns a non-zero exit code,
        or fails post-hardening DACL verification, raises BootstrapTokenProvisioningError.
        """
        trustee = _get_active_windows_trustee()

        cmd = ["icacls", str(path), "/inheritance:r", "/grant:r", f"{trustee}:(R,W)"]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except FileNotFoundError as exc:
            raise BootstrapTokenProvisioningError(
                "icacls utility not found on Windows system; cannot verify restrictive token permissions."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise BootstrapTokenProvisioningError(
                "icacls ACL hardening command timed out; cannot verify restrictive token permissions."
            ) from exc
        except Exception as exc:
            raise BootstrapTokenProvisioningError(
                f"icacls execution encountered an unexpected error: {exc}"
            ) from exc

        if proc.returncode != 0:
            err_detail = proc.stderr.strip() if proc.stderr else proc.stdout.strip()
            raise BootstrapTokenProvisioningError(
                f"Windows ACL hardening via icacls failed with exit code {proc.returncode}: {err_detail}"
            )

        # Verification of DACL:
        # Query icacls to verify that the active qualified trustee is confirmed in the DACL
        try:
            verify_proc = subprocess.run(["icacls", str(path)], capture_output=True, text=True, timeout=5)
            if verify_proc.returncode == 0:
                out = verify_proc.stdout.lower()
                short_trustee = trustee.split("\\")[-1].lower()
                if f"{short_trustee}\\:" in out:
                    raise BootstrapTokenProvisioningError(
                        f"DACL verification failed: trustee '{trustee}' was interpreted as domain prefix without user in '{path}'."
                    )
                if short_trustee not in out and trustee.lower() not in out:
                    raise BootstrapTokenProvisioningError(
                        f"DACL verification failed: trustee '{trustee}' not confirmed in ACL for '{path}'."
                    )
        except Exception as exc:
            if isinstance(exc, BootstrapTokenProvisioningError):
                raise
            logger.warning("Could not execute post-hardening DACL verification on '%s': %s", path, exc)

    def _wipe_token_file(self) -> None:
        """Delete the bootstrap token file from disk once consumed or upon failed provisioning."""
        try:
            if self.token_path.is_symlink() or os.path.islink(str(self.token_path)):
                os.unlink(str(self.token_path))
            elif self.token_path.exists():
                self.token_path.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("Failed to delete bootstrap token file: %s", exc)
