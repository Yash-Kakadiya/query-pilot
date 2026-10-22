"""Engine lifecycle and resource management for QueryPilot Local Agent Daemon.

Maintains explicit ownership and deterministic disposal of SQLAlchemy engine connection
pools. Ensures caller-provided engines are never prematurely disposed while daemon-owned
engines are reliably disposed on shutdown.
"""

from __future__ import annotations

import logging
import threading
from typing import Dict, Optional, Set

from sqlalchemy.engine import Engine

from query_pilot.db.factory import ProfileConnectionFactory

logger = logging.getLogger(__name__)


class AgentEngineManager:
    """Manages SQLAlchemy engine instances for local connection profiles."""

    def __init__(self, factory: ProfileConnectionFactory):
        self.factory = factory
        self._engines: Dict[str, Engine] = {}
        self._owned_engines: Set[str] = set()
        self._lock = threading.Lock()

    def get_or_create_engine(
        self,
        profile_id: str,
        caller_engine: Optional[Engine] = None,
    ) -> Engine:
        """Retrieve an active engine for the profile, or instantiate one via the factory.

        Args:
            profile_id: Approved profile identifier.
            caller_engine: Optional pre-configured Engine (e.g. mock for offline testing).
                           Caller-owned engines are NOT disposed by this manager.

        Returns:
            Active SQLAlchemy Engine.
        """
        if caller_engine is not None:
            # Caller owns this engine (e.g. testing mock)
            return caller_engine

        with self._lock:
            existing = self._engines.get(profile_id)
            if existing is not None:
                return existing

            engine = self.factory.create_engine(profile_id)
            self._engines[profile_id] = engine
            self._owned_engines.add(profile_id)
            return engine

    def is_engine_owned(self, profile_id: str) -> bool:
        """Check whether the engine for profile_id was created and is owned by this manager."""
        with self._lock:
            return profile_id in self._owned_engines

    def dispose_engine(self, profile_id: str) -> bool:
        """Dispose and remove a specific owned engine pool."""
        with self._lock:
            if profile_id not in self._owned_engines:
                return False

            engine = self._engines.pop(profile_id, None)
            self._owned_engines.discard(profile_id)
            if engine is not None:
                try:
                    engine.dispose()
                    return True
                except Exception as exc:
                    logger.warning("Error disposing engine for profile '%s': %s", profile_id, exc)
            return False

    def dispose_all(self) -> int:
        """Dispose all daemon-owned engine pools during agent shutdown."""
        with self._lock:
            disposed_count = 0
            for profile_id in list(self._owned_engines):
                engine = self._engines.pop(profile_id, None)
                if engine is not None:
                    try:
                        engine.dispose()
                        disposed_count += 1
                    except Exception as exc:
                        logger.warning("Error disposing engine for profile '%s': %s", profile_id, exc)
            self._owned_engines.clear()
            return disposed_count
