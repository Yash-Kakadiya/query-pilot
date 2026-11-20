"""Persistent Query History and Favorites Store.

Manages durable query history and starred bookmarks stored in ~/.query_pilot/history.json.
Guarantees:
- Thread-safe operations via threading.RLock.
- Atomic disk updates via temporary file and os.replace.
- Automatic password/secret scrubbing from SQL and error messages.
- Capacity ceiling: clamps to 500 entries, evicting oldest unstarred items while preserving all favorites.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import threading
import time
from typing import Dict, List, Optional
import uuid

from query_pilot.agent.models import HistoryItem, HistoryItemCreate

logger = logging.getLogger(__name__)

MAX_HISTORY_ENTRIES = 500

# Regular expression to sanitize sensitive connection secrets from SQL/errors
PASSWORD_PATTERN = re.compile(
    r"(?i)\b(password|pwd|identified\s+by)\s*[:=]?\s*['\"]?[^\s'\";,]+['\"]?"
)


def sanitize_history_string(text: Optional[str]) -> Optional[str]:
    """Scrub sensitive passwords and credentials from text before storing."""
    if text is None:
        return None
    return PASSWORD_PATTERN.sub(r"\1=[REDACTED]", text)


class HistoryStore:
    """Thread-safe persistent store for query history and favorites."""

    def __init__(self, history_file: Path):
        self._history_file = history_file.resolve()
        self._lock = threading.RLock()
        self._entries: Dict[str, HistoryItem] = {}
        self._load()

    @property
    def history_file(self) -> Path:
        return self._history_file

    def _load(self) -> None:
        """Load history entries from disk if file exists."""
        with self._lock:
            if not self._history_file.is_file():
                self._entries = {}
                return

            try:
                raw_data = self._history_file.read_text(encoding="utf-8")
                if not raw_data.strip():
                    self._entries = {}
                    return
                parsed = json.loads(raw_data)
                entries_list = parsed.get("entries", []) if isinstance(parsed, dict) else parsed

                loaded: Dict[str, HistoryItem] = {}
                for item in entries_list:
                    try:
                        entry = HistoryItem(**item)
                        loaded[entry.id] = entry
                    except Exception as err:
                        logger.warning("Skipping malformed history entry: %s", err)
                self._entries = loaded
            except Exception as exc:
                logger.error("Failed to load history file '%s': %s", self._history_file, exc)
                self._entries = {}

    def _save(self) -> None:
        """Persist history entries to disk atomically."""
        with self._lock:
            parent_dir = self._history_file.parent
            parent_dir.mkdir(parents=True, exist_ok=True)

            # Sort entries by timestamp descending
            sorted_entries = sorted(
                self._entries.values(),
                key=lambda e: e.timestamp,
                reverse=True,
            )

            payload = {
                "version": "1.0",
                "entries": [e.model_dump() for e in sorted_entries],
            }

            temp_file = parent_dir / f"{self._history_file.name}.tmp.{uuid.uuid4().hex}"
            try:
                temp_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                temp_file.replace(self._history_file)
            except Exception as exc:
                logger.error("Failed to atomically save history file: %s", exc)
                if temp_file.exists():
                    try:
                        temp_file.unlink()
                    except OSError:
                        pass
                raise

    def add_entry(self, data: HistoryItemCreate) -> HistoryItem:
        """Record an executed query into history, applying sanitization and capacity clamps."""
        with self._lock:
            entry_id = data.id or str(uuid.uuid4())
            ts = data.timestamp or int(time.time() * 1000)

            sanitized_sql = sanitize_history_string(data.sql) or data.sql
            sanitized_err = sanitize_history_string(data.error_message)

            entry = HistoryItem(
                id=entry_id,
                timestamp=ts,
                question=data.question,
                sql=sanitized_sql,
                profile_id=data.profile_id,
                profile_name=data.profile_name,
                engine_type=data.engine_type,
                status=data.status,
                row_count=data.row_count,
                execution_time_ms=data.execution_time_ms,
                error_message=sanitized_err,
                synthesized_answer=data.synthesized_answer,
                result_columns=data.result_columns,
                is_favorite=data.is_favorite,
                tags=list(data.tags or []),
                notes=data.notes,
            )

            self._entries[entry.id] = entry

            # Enforce max capacity clamp: evict oldest unstarred items
            if len(self._entries) > MAX_HISTORY_ENTRIES:
                unstarred = [e for e in self._entries.values() if not e.is_favorite]
                unstarred.sort(key=lambda e: e.timestamp)
                excess = len(self._entries) - MAX_HISTORY_ENTRIES
                for to_remove in unstarred[:excess]:
                    self._entries.pop(to_remove.id, None)

            self._save()
            return entry

    def list_entries(
        self,
        profile_id: Optional[str] = None,
        only_favorites: bool = False,
        search: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> List[HistoryItem]:
        """Return history entries matching optional filters, ordered chronologically newest first."""
        with self._lock:
            results = list(self._entries.values())

            if profile_id and profile_id != "all":
                results = [e for e in results if e.profile_id == profile_id]

            if only_favorites:
                results = [e for e in results if e.is_favorite]

            if tag and tag != "all":
                results = [e for e in results if tag in e.tags]

            if search and search.strip():
                query = search.strip().lower()
                results = [
                    e for e in results
                    if (e.question and query in e.question.lower())
                    or (e.sql and query in e.sql.lower())
                    or (e.profile_name and query in e.profile_name.lower())
                    or (e.notes and query in e.notes.lower())
                    or any(query in t.lower() for t in e.tags)
                ]

            results.sort(key=lambda e: e.timestamp, reverse=True)
            return results

    def toggle_favorite(self, entry_id: str) -> Optional[HistoryItem]:
        """Toggle is_favorite state of an existing entry."""
        with self._lock:
            entry = self._entries.get(entry_id)
            if not entry:
                return None

            new_fav = not entry.is_favorite
            updated = entry.model_copy(update={"is_favorite": new_fav})
            self._entries[entry_id] = updated
            self._save()
            return updated

    def delete_entry(self, entry_id: str) -> bool:
        """Delete a single history entry by ID."""
        with self._lock:
            if entry_id in self._entries:
                del self._entries[entry_id]
                self._save()
                return True
            return False

    def clear_entries(self, clear_favorites: bool = False) -> int:
        """Clear historical entries. Preserves starred favorites by default."""
        with self._lock:
            if clear_favorites:
                count = len(self._entries)
                self._entries.clear()
            else:
                to_keep = {k: v for k, v in self._entries.items() if v.is_favorite}
                count = len(self._entries) - len(to_keep)
                self._entries = to_keep

            self._save()
            return count

    def get_counts(self) -> tuple[int, int]:
        """Return (total_count, favorites_count)."""
        with self._lock:
            total = len(self._entries)
            favs = sum(1 for e in self._entries.values() if e.is_favorite)
            return total, favs
