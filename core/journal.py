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


class TransactionJournal:
    """Persistent transaction journal to track active worktrees and recover from process crashes."""

    def __init__(self, repo_path: Path):
        self.repo_path = repo_path.resolve()
        self.journal_file = self.repo_path / ".git" / "dsh_journal.json"
        # In-process lock only; multi-process coordination is out of scope.
        self._lock = threading.Lock()

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
            self._write_records(records)
        return rec

    def update_state(self, tx_id: str, state: TransactionState):
        with self._lock:
            records = self._read_records()
            if tx_id in records:
                records[tx_id].state = state
                records[tx_id].updated_at = time.time()
                self._write_records(records)

    def record_end(self, tx_id: str):
        with self._lock:
            records = self._read_records()
            if tx_id in records:
                del records[tx_id]
                self._write_records(records)

    def list_active(self) -> List[JournalRecord]:
        records = self._read_records()
        return list(records.values())

    def get_orphaned(self) -> List[JournalRecord]:
        """Returns records that are still active/in-flight on disk."""
        records = self._read_records()
        orphaned = []
        for rec in records.values():
            wt_path = Path(rec.worktree_path)
            if wt_path.exists():
                orphaned.append(rec)
        return orphaned
