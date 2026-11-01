"""Engine lifecycle and resource management for QueryPilot Local Agent Daemon.

Maintains explicit ownership and deterministic disposal of SQLAlchemy engine connection
pools. Ensures caller-provided engines are never prematurely disposed while daemon-owned
engines are reliably disposed on shutdown.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional, Set

from sqlalchemy.engine import Engine

from query_pilot.db.factory import ProfileConnectionFactory

logger = logging.getLogger(__name__)


class AgentEngineManager:
    """Manages SQLAlchemy engine instances for local connection profiles."""

    def __init__(self, factory: ProfileConnectionFactory):
        self.factory = factory
        self._engines: Dict[str, Engine] = {}
        self._owned_engines: Set[str] = set()
        self._engine_targets: Dict[str, Any] = {}
        self._lock = threading.Lock()

    def _dispose_key_locked(self, key: str) -> bool:
        """Internal helper to dispose and discard an engine key under lock."""
        engine = self._engines.pop(key, None)
        self._owned_engines.discard(key)
        self._engine_targets.pop(key, None)
        if engine is not None:
            try:
                engine.dispose()
                return True
            except Exception as exc:
                logger.warning("Error disposing engine '%s': %s", key, exc)
        return False

    def get_or_create_engine(
        self,
        profile_id: str,
        caller_engine: Optional[Engine] = None,
        mode: str = "execution",
    ) -> Engine:
        """Retrieve an active engine for the profile, or instantiate one via the factory.

        Detects target modifications (e.g. database_path changes, profile reloads),
        disposes stale cached engines, and instantiates fresh engines safely.

        Args:
            profile_id: Approved profile identifier.
            caller_engine: Optional pre-configured Engine (e.g. mock for offline testing).
                           Caller-owned engines are NOT disposed by this manager.
            mode: Connection security mode: 'execution' or 'introspection'.

        Returns:
            Active SQLAlchemy Engine.
        """
        if caller_engine is not None:
            # Caller owns this engine (e.g. testing mock)
            return caller_engine

        # Retrieve profile configuration to verify existence and compute target identity
        profile = self.factory.store.get_profile(profile_id)
        target_spec = (
            profile.engine_type,
            profile.database_path if profile.engine_type == "sqlite" else (profile.server, profile.database_name),
            profile.allowed_tables,
            profile.sensitive_columns,
        )

        cache_key = f"{profile_id}:{mode}"

        with self._lock:
            existing = self._engines.get(cache_key)
            if existing is not None:
                cached_target = self._engine_targets.get(cache_key)
                if cached_target == target_spec:
                    return existing

                # Target specification changed (e.g. database_path changed, or profile updated).
                # Stale engine must be disposed cleanly and replaced.
                logger.info("Detected profile target change for '%s'; disposing stale engine.", cache_key)
                self._dispose_key_locked(cache_key)

            engine = self.factory.create_engine(profile_id, mode=mode)
            self._engines[cache_key] = engine
            self._owned_engines.add(cache_key)
            self._engine_targets[cache_key] = target_spec
            return engine

    def is_engine_owned(self, profile_id: str) -> bool:
        """Check whether an engine for profile_id was created and is owned by this manager."""
        with self._lock:
            return any(k == profile_id or k.startswith(f"{profile_id}:") for k in self._owned_engines)

    def invalidate_profile(self, profile_id: str) -> bool:
        """Explicitly invalidate and dispose any cached engines for profile_id."""
        return self.dispose_engine(profile_id)

    def dispose_engine(self, profile_id: str) -> bool:
        """Dispose and remove specific owned engine pools for this profile."""
        with self._lock:
            keys_to_remove = [
                k for k in set(self._engines.keys()).union(self._owned_engines)
                if k == profile_id or k.startswith(f"{profile_id}:")
            ]
            if not keys_to_remove:
                return False

            disposed_any = False
            for k in keys_to_remove:
                if self._dispose_key_locked(k):
                    disposed_any = True
            return disposed_any

    def dispose_all(self) -> int:
        """Dispose all daemon-owned engine pools during agent shutdown."""
        with self._lock:
            disposed_count = 0
            for k in list(self._engines.keys()):
                if self._dispose_key_locked(k):
                    disposed_count += 1
            self._owned_engines.clear()
            self._engine_targets.clear()
            return disposed_count
