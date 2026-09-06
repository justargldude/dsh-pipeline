import fnmatch
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from pydantic import BaseModel, Field

from task.schema import TaskDefinition
from recon.evidence import EvidenceService
from context.budget import ContextComplexity, TokenBudgetManager, ContextBudgetExceededError
from context.ranking import ContextItem, ContextRanker, PriorityLevel
from context.extractor import SymbolExtractor, ExtractedSourceResult
from memory.episodic import EpisodeRecord, EpisodeStatus


class ContextManifest(BaseModel):
    total_budget: int
    available_budget: int
    used_tokens: int
    mandatory_sections: List[str] = Field(default_factory=list)
    optional_sections_included: List[str] = Field(default_factory=list)
    omitted_sections: List[Dict[str, Any]] = Field(default_factory=list)
    target_source_truncated: bool = False
    extraction_method: str = "full_source"


class ContextBuilder:
    def __init__(self, evidence_service: Optional[EvidenceService] = None):
        self.evidence_service = evidence_service
        self.symbol_extractor = SymbolExtractor()
        self.last_manifest: Optional[ContextManifest] = None

    def build_context_with_manifest(
        self,
        task: TaskDefinition,
        file_snippets: Dict[str, str],
        evidence: Optional[Dict[str, Any]] = None,
        previous_failure: Optional[str] = None,
        advisory_episodes: Optional[List[EpisodeRecord]] = None,
        caller_context: Optional[Dict[str, Any]] = None,
        complexity: ContextComplexity = ContextComplexity.NORMAL,
    ) -> Tuple[str, ContextManifest]:
        """Deterministically constructs model prompt context while guaranteeing mandatory items
        (task definition, target source, target symbols) are protected from optional items.
        """
        total_budget = TokenBudgetManager.get_budget(complexity)
        available_budget = TokenBudgetManager.compute_available_budget(complexity)

        mandatory_items: List[ContextItem] = []
        optional_items: List[ContextItem] = []

        # 1. MANDATORY: Task Definition
        symbols_str = f"Target Symbols: {', '.join(task.target_symbols)}\n" if task.target_symbols else ""
        task_header_content = (
            f"# TASK OBJECTIVE: [{task.task_id}] {task.title}\n"
            f"Allowed Files: {', '.join(task.allowed_files)}\n"
            f"{symbols_str}"
            f"Line Budget: +{task.max_lines_added} / -{task.max_lines_deleted}\n"
            f"Risk Level: {task.risk.value.upper()}\n"
            "---"
        )
        task_item = ContextItem(
            priority=PriorityLevel.TASK_DEFINITION,
            category="TASK_DEFINITION",
            content=task_header_content,
        )
        mandatory_items.append(task_item)

        # 2. MANDATORY: Target Source Files / Symbol Extractions
        target_source_truncated = False
        extraction_method = "none"

        # Calculate max tokens reserved for target source files
        reserved_source_budget = max(400, available_budget - task_item.token_cost - 300)

        FENCE_MAP = {
            ".cs": "csharp",
            ".py": "python",
            ".js": "javascript",
            ".jsx": "javascript",
            ".ts": "typescript",
            ".tsx": "typescript",
            ".cpp": "cpp",
            ".cc": "cpp",
            ".c": "c",
            ".h": "cpp",
            ".hpp": "cpp",
        }

        # Anti-reward-hacking: Never leak holdout tests to the Dev Agent
        holdout_names = {task.holdout_test_file} if task.holdout_test_file else set()

        for fname, snippet in file_snippets.items():
            if fname in holdout_names or any(fnmatch.fnmatch(fname, pat) for pat in ["*holdout*", "*secret_test*"]):
                continue

            ext = Path(fname).suffix.lower()
            lang_fence = FENCE_MAP.get(ext, "csharp")

            ext_res: ExtractedSourceResult = self.symbol_extractor.extract_relevant_source(
                source_code=snippet,
                target_symbols=task.target_symbols if task.target_symbols else None,
                max_tokens=reserved_source_budget,
                file_name=fname,
            )
            if ext_res.is_truncated:
                target_source_truncated = True
            extraction_method = ext_res.extraction_method

            source_item = ContextItem(
                priority=PriorityLevel.TARGET_SOURCE,
                category=f"TARGET_SOURCE:{fname}",
                content=f"### [SOURCE FILE: {fname}]\n```{lang_fence}\n{ext_res.extracted_code}\n```\n",
            )
            mandatory_items.append(source_item)

        # Include Visible Test Specification if provided (without holdouts)
        if task.visible_test_code:
            ext = Path(task.holdout_test_file or "test.cs").suffix.lower()
            spec_fence = FENCE_MAP.get(ext, "csharp")
            mandatory_items.append(
                ContextItem(
                    priority=PriorityLevel.TASK_DEFINITION,
                    category="TEST_SPECIFICATION",
                    content=f"### [TEST SPECIFICATION (BEHAVIORAL REQUIREMENTS)]\n```{spec_fence}\n{task.visible_test_code}\n```\n",
                )
            )

        # Check if mandatory items exceed available budget
        mandatory_cost = sum(it.token_cost for it in mandatory_items)
        if mandatory_cost > available_budget:
            raise ContextBudgetExceededError(
                f"Context budget exceeded: Mandatory items require {mandatory_cost} tokens, "
                f"but available budget is {available_budget} (Total: {total_budget})."
            )

        # 3. IMPORTANT: Caller & Callee Context
        if caller_context:
            optional_items.append(
                ContextItem(
                    priority=PriorityLevel.DIRECT_CALLERS_CALLEES,
                    category="CALLER_CALLEE_GRAPH",
                    content=f"### [CALL GRAPH CONTEXT]\n```json\n{json.dumps(caller_context, indent=2)}\n```\n",
                )
            )

        # 4. IMPORTANT: Recon Evidence / Facts
        if evidence:
            optional_items.append(
                ContextItem(
                    priority=PriorityLevel.RELEVANT_EVIDENCE,
                    category="RECON_EVIDENCE",
                    content=f"### [RECON EVIDENCE (FACTS)]\n```json\n{json.dumps(evidence, indent=2)}\n```\n",
                )
            )

        # 5. OPTIONAL: Failure History
        if previous_failure:
            optional_items.append(
                ContextItem(
                    priority=PriorityLevel.FAILURE_HISTORY,
                    category="FAILURE_HISTORY",
                    content=f"### [PREVIOUS FAILURE HISTORY]\n{previous_failure}\n",
                )
            )

        # 6. OPTIONAL: Advisory Memory (Validated historical episodes)
        if advisory_episodes:
            # Filter only validated episodes
            validated_eps = [ep for ep in advisory_episodes if ep.status == EpisodeStatus.VALIDATED]
            if validated_eps:
                ep_texts = []
                for ep in validated_eps:
                    # Concise patch summary instead of full raw json dump
                    modified_files = [p.file for p in ep.solution_patch.patches]
                    hunk_count = sum(len(p.hunks) for p in ep.solution_patch.patches)
                    ep_texts.append(
                        f"- Task {ep.task_id} (Version: {ep.version}, Confidence: {ep.confidence:.2f})\n"
                        f"  Target Symbol: {ep.symbol}\n"
                        f"  Summary: Patched {len(modified_files)} file(s) ({', '.join(modified_files)}), {hunk_count} hunk(s)\n"
                        f"  Reason: {ep.solution_patch.reason or 'Passed validation'}\n"
                        f"  Validation: Build=Pass, Behavior=Pass, Regression=Pass"
                    )
                optional_items.append(
                    ContextItem(
                        priority=PriorityLevel.ADVISORY_MEMORY,
                        category="ADVISORY_MEMORY",
                        content="### [ADVISORY MEMORY (HISTORICAL PASSED EPISODES)]\n" + "\n\n".join(ep_texts) + "\n",
                    )
                )

        # Combine all items and rank/trim
        all_items = mandatory_items + optional_items
        selected_items, omitted_items = ContextRanker.rank_and_trim_detailed(
            items=all_items,
            token_budget=available_budget,
        )

        # Assemble final context string
        context_parts = [it.content for it in selected_items]
        final_context = "\n".join(context_parts)

        # Build manifest
        manifest = ContextManifest(
            total_budget=total_budget,
            available_budget=available_budget,
            used_tokens=sum(it.token_cost for it in selected_items),
            mandatory_sections=[it.category for it in mandatory_items],
            optional_sections_included=[it.category for it in selected_items if it.priority.value > 3],
            omitted_sections=[
                {
                    "category": it.category,
                    "priority": it.priority.name,
                    "token_cost": it.token_cost,
                    "reason": "budget_exhausted",
                }
                for it in omitted_items
            ],
            target_source_truncated=target_source_truncated,
            extraction_method=extraction_method,
        )
        self.last_manifest = manifest
        return final_context, manifest

    def build_context(
        self,
        task: TaskDefinition,
        file_snippets: Dict[str, str],
        evidence: Optional[Dict[str, Any]] = None,
        previous_failure: Optional[str] = None,
        advisory_episodes: Optional[List[EpisodeRecord]] = None,
        complexity: ContextComplexity = ContextComplexity.NORMAL,
    ) -> str:
        """Standard entry point returning the prompt context string."""
        context_str, manifest = self.build_context_with_manifest(
            task=task,
            file_snippets=file_snippets,
            evidence=evidence,
            previous_failure=previous_failure,
            advisory_episodes=advisory_episodes,
            complexity=complexity,
        )
        return context_str
