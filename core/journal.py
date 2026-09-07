import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional
from pydantic import BaseModel, Field

from core.state import TransactionState

logger = logging.getLogger("dsh.journal")


class JournalRecord(BaseModel):
    tx_id: str
    task_id: str
    base_commit: str
    worktree_path: str
    state: TransactionState = TransactionState.CREATED
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


def _get_git_dir(repo_path: Path) -> Path:
    repo = Path(repo_path).resolve()
    git_entry = repo / ".git"
    if git_entry.is_dir():
        return git_entry
    if git_entry.is_file():
        try:
            content = git_entry.read_text(encoding="utf-8").strip()
            if content.startswith("gitdir:"):
                p = Path(content[7:].strip())
                if not p.is_absolute():
                    p = (repo / p).resolve()
                return p
        except Exception:
            pass
    return git_entry


class TransactionJournal:
    """Persistent transaction journal to track active worktrees and recover from process crashes."""

    def __init__(self, repo_path: Path):
        self.repo_path = repo_path.resolve()
        self.journal_file = _get_git_dir(self.repo_path) / "dsh_journal.json"
        # In-process lock only; multi-process coordination is out of scope.
        self._lock = threading.Lock()
        # Opt 8.1: lazy-write pending states for update_state (in-memory only
        # until record_start/record_end/flush persists them). Guarded by the
        # same self._lock — no extra lock, no extra deadlock surface.
        self._pending_states: Dict[str, TransactionState] = {}

    def _read_records(self) -> Dict[str, JournalRecord]:
        if not self.journal_file.exists():
            return {}
        try:
            raw = self.journal_file.read_text(encoding="utf-8")
            if not raw.strip():
                return {}
            data = json.loads(raw)
            return {k: JournalRecord(**v) for k, v in data.items()}
        except Exception as e:
            logger.warning(f"Failed to parse transaction journal {self.journal_file}: {e}")
            return {}

    def _write_records(self, records: Dict[str, JournalRecord]):
        try:
            self.journal_file.parent.mkdir(parents=True, exist_ok=True)
            data = {k: v.model_dump(mode="json") for k, v in records.items()}
            content = json.dumps(data, indent=2)

            # Atomic write
            temp_file = self.journal_file.with_name(f".{self.journal_file.name}.tmp.{uuid.uuid4().hex}")
            temp_file.write_text(content, encoding="utf-8")
            os.replace(temp_file, self.journal_file)
        except Exception as e:
            logger.warning(f"Failed to persist transaction journal {self.journal_file}: {e}")

    def record_start(
        self,
        tx_id: str,
        task_id: str,
        base_commit: str,
        worktree_path: str,
        state: TransactionState = TransactionState.CREATED,
    ) -> JournalRecord:
        with self._lock:
            records = self._read_records()
            rec = JournalRecord(
                tx_id=tx_id,
                task_id=task_id,
                base_commit=base_commit,
                worktree_path=worktree_path,
                state=state,
                created_at=time.time(),
                updated_at=time.time(),
            )
            records[tx_id] = rec
            # Drop any stale pending state for a recycled tx_id; the freshly
            # created record carries the authoritative state.
            self._pending_states.pop(tx_id, None)
            self._write_records(records)
        return rec

    def update_state(self, tx_id: str, state: TransactionState):
        """Updates the record state in-memory only (lazy write, Opt 8.1).

        The change lives in self._pending_states and the in-memory record set
        until record_start/record_end/flush persists it. Crash-safety invariant
        is preserved: record_start always leaves the tx record on disk, so
        orphan detection never depends on intermediate states.
        """
        with self._lock:
            records = self._read_records()
            if tx_id in records:
                records[tx_id].state = state
                records[tx_id].updated_at = time.time()
                self._pending_states[tx_id] = state

    def flush(self) -> None:
        """Persists any pending state updates to disk and clears the pending set."""
        with self._lock:
            if not self._pending_states:
                return
            records = self._read_records()
            changed = False
            for tx_id, state in self._pending_states.items():
                if tx_id in records:
                    records[tx_id].state = state
                    records[tx_id].updated_at = time.time()
                    changed = True
            if changed:
                self._write_records(records)
            self._pending_states.clear()

    def record_end(self, tx_id: str):
        with self._lock:
            # Apply pending states for OTHER tx_ids before serializing, so a
            # full rewrite does not silently revert their lazy updates.
            if self._pending_states:
                records = self._read_records()
                for pending_id, state in self._pending_states.items():
                    if pending_id != tx_id and pending_id in records:
                        records[pending_id].state = state
                        records[pending_id].updated_at = time.time()
                if tx_id in records:
                    del records[tx_id]
                self._pending_states.pop(tx_id, None)
                self._write_records(records)
                return
            records = self._read_records()
            if tx_id in records:
                del records[tx_id]
                self._write_records(records)

    def _apply_pending_overlay(self, records: Dict[str, JournalRecord]) -> Dict[str, JournalRecord]:
        """Overlays in-memory pending states onto disk records for public API views.

        `_read_records` stays disk-only (lazy-write contract, Opt 8.1 R3b),
        while `list_active`/`get_orphaned` reflect the freshest in-memory
        state — preserving the pre-8.1 public API behavior (Bug 2.3 tests).
        """
        if not self._pending_states:
            return records
        with self._lock:
            for tx_id, state in self._pending_states.items():
                if tx_id in records:
                    records[tx_id].state = state
                    records[tx_id].updated_at = time.time()
        return records

    def list_active(self) -> List[JournalRecord]:
        records = self._apply_pending_overlay(self._read_records())
        return list(records.values())

    def get_orphaned(self) -> List[JournalRecord]:
        """Returns records that are still active/in-flight on disk."""
        records = self._apply_pending_overlay(self._read_records())
        orphaned = []
        for rec in records.values():
            wt_path = Path(rec.worktree_path)
            if wt_path.exists():
                orphaned.append(rec)
        return orphaned
