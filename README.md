# DSH Pipeline: Deterministic Code Patch & Transaction Engine

[![Tests](https://img.shields.io/badge/tests-41%20passed-success)](tests/)
[![Python](https://img.shields.io/badge/python-3.12+-blue.svg)](https://python.org)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

> **Core Philosophy:**
> *"LLMs only propose patches. The deterministic system decides whether a patch is allowed to exist."*

---

## 🏗️ Architecture & Pipeline Phases

```text
                                USER GOAL / DAG
                                       │
                                       ▼
                       ┌───────────────────────────────┐
                       │       TASK ORCHESTRATOR       │
                       │ DAG, Cycle Detection & Locks  │
                       └───────────────┬───────────────┘
                                       │
                                ┌──────┴──────┐
                                ▼             ▼
                        Evidence Store    Workspace
                           (DuckDB)      (Git Checkpoint)
                                │             │
                                └──────┬──────┘
                                       ▼
                       ┌───────────────────────────────┐
                       │        CONTEXT BUILDER        │
                       │ Priority Ranking & Token Cap  │
                       └───────────────┬───────────────┘
                                       │
                                       ▼
                       ┌───────────────────────────────┐
                       │         MODEL ROUTER          │
                       │ Fast vs Reasoning (DeepSeek)  │
                       └───────────────┬───────────────┘
                                       │
                                PATCH PROPOSAL
                                       │
                                       ▼
                 ┌───────────────────────────────────────────┐
                 │       PRE-APPLY VALIDATION (T0 + T4)      │
                 │ • T0: Schema, Diff Budget, AST Guard (C#) │
                 │ • T4: Risk & Unsafe Memory Scan           │
                 └─────────────────────┬─────────────────────┘
                                       │
                                 PASS / REJECT
                                       │
                                       ▼
                       ┌───────────────────────────────┐
                       │       APPLY IN SANDBOX        │
                       └───────────────┬───────────────┘
                                       │
                                       ▼
                 ┌───────────────────────────────────────────┐
                 │       POST-APPLY VALIDATION (T1..T3)      │
                 │ • T1: Isolated Build (Compiler Errors)    │
                 │ • T2: Behavioral Smoke Tests              │
                 │ • T3: Regression Compatibility Suite      │
                 └─────────────────────┬─────────────────────┘
                                       │
                         ┌─────────────┴─────────────┐
                         ▼                           ▼
                       [FAIL]                      [PASS]
                         │                           │
                         ▼                           ▼
                 ┌───────────────┐           ┌───────────────┐
                 │ RECOVERY LOOP │           │  GIT COMMIT   │
                 │  3 Attempts   │           │ [TASK_ID] msg │
                 │ Fast/Reasoning│           └───────┬───────┘
                 │   Hard Halt   │                   │
                 └───────┬───────┘                   ▼
                         │                   ┌───────────────┐
                         ▼                   │EPISODIC MEMORY│
                   [ROLLBACK TAG]            │  Validated    │
                                             │  Episodes DB  │
                                             └───────────────┘
```

---

## 📦 Component Summary

1. **Phase 0 & 1 — Transaction Core (`core/`)**:
   - Clean status check, snapshot checkpoint tags (`ckpt_<task>_<hash>`), atomic git commits, and hard rollback (`reset --hard` + `clean -fd`).
2. **Phase 2 — AST Guard & Policy (`safety/`)**:
   - Tree-Sitter C# AST analyzer blocking method/class deletion, assertion removals, and early return bypasses (`return;`, `return null;`, `NotImplementedException`).
3. **Phase 3 — Recon & Evidence Layer (`recon/`, `context/`)**:
   - DuckDB storage for symbols, call graphs, cross-version mappings, and 6-tier token budget ranker.
4. **Phase 4 — Model Router & Structured Patch (`model/`)**:
   - Fast (`deepseek-chat`) vs Reasoning (`deepseek-reasoner`) router with JSON PatchProposal extraction.
5. **Phase 5 — Recovery Loop (`recovery/`)**:
   - 3-attempt escalation loop with structured failure history and Hard Halt failsafe.
6. **Phase 6 — Validation Pipeline (`validation/`)**:
   - 5-tier validation: T0 Structural, T1 Build, T2 Behavioral, T3 Regression, T4 Risk.
7. **Phase 7 — Task Orchestrator & DAG Engine (`task/`)**:
   - Kahn's topological sort, cyclic dependency detection, and file-level resource locks.
8. **Phase 8 — Episodic Memory (`memory/`)**:
   - Stores validated episodes, queries by symbol, and flags stale records across version shifts.

---

## 🚀 Quickstart & CLI

```bash
# 1. Activate virtual environment
source .venv/bin/activate

# 2. Run automated test suite (41/41 tests passing)
pytest -v tests/

# 3. Run dry-run transaction demo
python cli.py demo --dry-run

# 4. Execute a specific task on a target repository
python cli.py run --task task.json --patch patch.json --repo "/path/to/repo" --dry-run
```
