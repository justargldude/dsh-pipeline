import json
from typing import Any, Dict, List, Optional
from task.schema import TaskDefinition
from recon.evidence import EvidenceService
from context.budget import ContextComplexity, TokenBudgetManager
from context.ranking import ContextItem, ContextRanker, PriorityLevel
from memory.episodic import EpisodeRecord


class ContextBuilder:
    def __init__(self, evidence_service: Optional[EvidenceService] = None):
        self.evidence_service = evidence_service

    def build_context(
        self,
        task: TaskDefinition,
        file_snippets: Dict[str, str],
        evidence: Optional[Dict[str, Any]] = None,
        previous_failure: Optional[str] = None,
        advisory_episodes: Optional[List[EpisodeRecord]] = None,
        complexity: ContextComplexity = ContextComplexity.NORMAL,
    ) -> str:
        budget = TokenBudgetManager.get_budget(complexity)
        raw_items: List[ContextItem] = []

        # 1. Target symbol evidence (Priority 1)
        if evidence:
            raw_items.append(
                ContextItem(
                    priority=PriorityLevel.TARGET_SYMBOL_EVIDENCE,
                    category="EVIDENCE_FACTS",
                    content=f"### [RECON EVIDENCE (FACTS)]\n```json\n{json.dumps(evidence, indent=2)}\n```\n",
                )
            )

        # 2. File snippets / Target code (Priority 2)
        for fname, snippet in file_snippets.items():
            raw_items.append(
                ContextItem(
                    priority=PriorityLevel.DIRECT_CALLERS_CALLEES,
                    category="TARGET_SOURCE",
                    content=f"### [SOURCE FILE: {fname}]\n```csharp\n{snippet}\n```\n",
                )
            )

        # 3. Advisory Episodes from Memory (Priority 6)
        if advisory_episodes:
            ep_texts = []
            for ep in advisory_episodes:
                status_tag = f"[{ep.status.value}]"
                ep_texts.append(
                    f"- Historical Task: {ep.task_id} ({status_tag}) for symbol '{ep.symbol}'\n"
                    f"  Version: {ep.version}, Confidence: {ep.confidence}\n"
                    f"  Patch:\n```json\n{ep.solution_patch.model_dump_json(indent=2)}\n```"
                )
            raw_items.append(
                ContextItem(
                    priority=PriorityLevel.PREVIOUS_VALIDATED_FIX,
                    category="ADVISORY_MEMORY",
                    content="### [ADVISORY MEMORY (HISTORICAL PASSED EPISODES)]\n" + "\n".join(ep_texts) + "\n",
                )
            )

        # 4. Previous failure / Recovery details (Priority 5)
        if previous_failure:
            raw_items.append(
                ContextItem(
                    priority=PriorityLevel.RELEVANT_DIFF,
                    category="FAILURE_HISTORY",
                    content=f"### [PREVIOUS FAILURE HISTORY]\n{previous_failure}\n",
                )
            )

        # Rank and trim to fit budget
        selected = ContextRanker.rank_and_trim(raw_items, token_budget=budget)

        # Assemble final context string
        context_parts = [
            f"# TASK OBJECTIVE: [{task.task_id}] {task.title}",
            f"Allowed Files: {', '.join(task.allowed_files)}",
            f"Line Budget: +{task.max_lines_added} / -{task.max_lines_deleted}",
            "---",
        ]
        for it in selected:
            context_parts.append(it.content)

        return "\n".join(context_parts)
