import os
import time
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
import httpx
from pydantic import BaseModel, Field

from task.schema import TaskDefinition, PatchProposal, TransactionResult
from core.runtime import DSHRuntime
from model.providers import OpenAICompatibleProvider
from context.builder import ContextBuilder

logger = logging.getLogger("dsh.cloud_worker")


class CloudTaskItem(BaseModel):
    id: str
    title: str
    target_repo: str = "/home/justargldude/Bản tải về/ngoc-rong-online-linux"
    allowed_files: List[str] = Field(default_factory=list)
    instruction: str = ""
    status: str = "PENDING"  # PENDING, RUNNING, COMPLETED, FAILED
    result: Optional[Dict[str, Any]] = None
    created_at: Optional[str] = None


class CloudQueueWorker:
    def __init__(
        self,
        cloud_api_url: str,
        auth_token: Optional[str] = None,
        poll_interval_seconds: int = 5,
    ):
        self.cloud_api_url = cloud_api_url.rstrip("/")
        self.auth_token = auth_token
        self.poll_interval = poll_interval_seconds
        self.headers = {"Content-Type": "application/json"}
        if self.auth_token:
            self.headers["Authorization"] = f"Bearer {self.auth_token}"

    def fetch_pending_tasks(self) -> List[CloudTaskItem]:
        """Polls cloud queue for pending tasks."""
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.get(f"{self.cloud_api_url}/api/tasks?status=PENDING", headers=self.headers)
                if resp.status_code == 200:
                    data = resp.json()
                    tasks = data if isinstance(data, list) else data.get("tasks", [])
                    return [CloudTaskItem(**t) for t in tasks if t.get("status") == "PENDING"]
        except Exception as e:
            logger.debug(f"Cloud queue poll failed or empty: {e}")
        return []

    def update_task_status(
        self,
        task_id: str,
        status: str,
        result_details: Optional[Dict[str, Any]] = None,
    ):
        """Updates task execution status in the cloud queue."""
        payload = {"status": status}
        if result_details:
            payload["result"] = result_details

        try:
            with httpx.Client(timeout=10) as client:
                client.patch(
                    f"{self.cloud_api_url}/api/tasks/{task_id}",
                    json=payload,
                    headers=self.headers,
                )
        except Exception as e:
            logger.error(f"Failed to update task {task_id} in cloud queue: {e}")

    def process_single_task(self, cloud_task: CloudTaskItem) -> bool:
        logger.info(f"==> [CLOUD WORKER] Processing offline task: [{cloud_task.id}] {cloud_task.title}")
        self.update_task_status(cloud_task.id, "RUNNING")

        repo_path = Path(cloud_task.target_repo).resolve()
        if not repo_path.exists():
            self.update_task_status(
                cloud_task.id,
                "FAILED",
                {"error": f"Target repo '{cloud_task.target_repo}' does not exist on this machine."},
            )
            return False

        runtime = DSHRuntime(repo_path, dry_run=False)
        provider = OpenAICompatibleProvider()
        context_builder = ContextBuilder()

        task_def = TaskDefinition(
            task_id=cloud_task.id,
            title=cloud_task.title,
            allowed_files=cloud_task.allowed_files,
        )

        res: TransactionResult = runtime.execute_with_recovery(
            task=task_def,
            provider=provider,
            context_builder=context_builder,
        )

        status_str = "COMPLETED" if res.success else "FAILED"
        result_dict = {
            "success": res.success,
            "commit_hash": res.commit_hash,
            "failure_type": res.failure_type,
            "error_message": res.error_message,
            "events": res.events,
        }
        self.update_task_status(cloud_task.id, status_str, result_dict)
        return res.success

    def run_worker_loop(self, max_iterations: Optional[int] = None):
        """Continuous polling loop on PC boot."""
        logger.info(f"DSH Cloud Queue Worker started. Polling: {self.cloud_api_url}")
        iterations = 0

        while True:
            try:
                pending = self.fetch_pending_tasks()
                for task in pending:
                    self.process_single_task(task)
            except Exception as e:
                logger.error(f"Error in worker loop: {e}")

            iterations += 1
            if max_iterations and iterations >= max_iterations:
                break

            time.sleep(self.poll_interval)
