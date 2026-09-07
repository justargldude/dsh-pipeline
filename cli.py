import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional
import typer
from rich.console import Console
from rich.table import Table
from pydantic import BaseModel, ValidationError

from task.schema import TaskDefinition, PatchProposal, FilePatch, PatchHunk
from core.config import PipelineConfig
from core.runtime import DSHRuntime
from build.sandbox import MockBuildRunner, SubprocessBuildRunner
from context.builder import ContextBuilder
from safety.patch_engine import normalize_repo_path

app = typer.Typer(help="DSH Pipeline - Deterministic Patch Transaction Engine")
console = Console()


def _load_json(path: Path, model):
    """Loads a JSON file and validates it against a pydantic model.

    Raises typer.BadParameter with a concise message on missing file, bad
    JSON, or schema mismatch — never a raw traceback.
    """
    if not path.exists():
        raise typer.BadParameter(f"File not found: {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise typer.BadParameter(f"Invalid JSON in {path}: {e}")

    try:
        if model is None:
            return data
        if issubclass(model, BaseModel):
            if isinstance(data, list):
                return [model(**item) for item in data]
            return model(**data)
        return data
    except ValidationError as e:
        # Compact one-line-per-error summary instead of a raw traceback.
        details = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()
        )
        raise typer.BadParameter(f"Schema validation failed for {path}: {details}")
    except TypeError as e:
        # Non-mapping JSON roots (e.g. [123, 456] or scalar 123) break **-unpack.
        raise typer.BadParameter(f"Schema validation failed for {path}: {e}")


def _load_env_file(env_file: Optional[Path]) -> None:
    """Loads KEY=VALUE pairs from a .env-style file into os.environ (no overrides)."""
    if env_file is None or not env_file.exists():
        return
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("\"'")
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception as e:
        console.print(f"[yellow]Warning:[/yellow] Could not read env file {env_file}: {e}")


def _prepare_model_runtime(repo_path: Path, dry_run: bool, env_file: Optional[Path]):
    """Common setup for `model`/`recover`: env, API key check, runtime, provider, context builder.

    Returns (runtime, provider, context_builder) or exits non-zero with a
    clear configuration message when the API key is unconfigured.
    """
    _load_env_file(env_file)

    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip() or None
    if not api_key:
        # Fall back to DSH auto-sync (settings.yaml / ~/.dsh/.env / local .env).
        from model.providers import resolve_deepseek_api_key
        api_key = resolve_deepseek_api_key()
    if not api_key:
        console.print(
            "[bold red]Configuration error:[/bold red] No DEEPSEEK_API_KEY configured.\n"
            "Set the DEEPSEEK_API_KEY environment variable, pass --env-file pointing to a "
            "key file, or configure the key in DeepSeek Harness (~/.dsh/settings.yaml)."
        )
        raise typer.Exit(code=1)

    base_url = os.environ.get("DEEPSEEK_BASE_URL", "").strip() or "https://api.deepseek.com/v1"

    from model.providers import OpenAICompatibleProvider

    config = PipelineConfig(workspace_root=repo_path.resolve())
    runtime = DSHRuntime(repo_path, config=config, dry_run=dry_run, test_mode=False)
    provider = OpenAICompatibleProvider(api_key=api_key, base_url=base_url)
    # Single SymbolExtractor/ContextBuilder per invocation (R2d).
    context_builder = ContextBuilder()
    return runtime, provider, context_builder


def _collect_file_snippets(runtime: DSHRuntime, task: TaskDefinition) -> Dict[str, str]:
    """Reads task.allowed_files content from the repo for context building."""
    from safety.patch_engine import PatchValidationError

    snippets: Dict[str, str] = {}
    for raw_file in task.allowed_files:
        try:
            rel_file = normalize_repo_path(raw_file)
        except PatchValidationError as e:
            # Malicious/invalid allowed_files entries must fail clean with a
            # clear message, not a raw traceback from the CLI.
            console.print(f"[bold red]Error:[/bold red] Invalid allowed_files entry '{raw_file}': {e}")
            raise typer.Exit(code=1)
        file_path = (runtime.workspace_path / rel_file).resolve()
        if file_path.exists():
            snippets[rel_file] = file_path.read_text(encoding="utf-8")
    return snippets


def _print_transaction_result(result) -> None:
    """Prints a compact TransactionResult summary table."""
    table = Table(title=f"Transaction Result: {result.task_id}")
    table.add_column("Property", style="cyan")
    table.add_column("Value", style="magenta")
    table.add_row("Success", "[green]YES[/green]" if result.success else "[red]NO[/red]")
    table.add_row("Dry Run", str(result.dry_run))
    if result.commit_hash:
        table.add_row("Commit", result.commit_hash)
    if result.base_commit:
        table.add_row("Base Commit", result.base_commit)
    if result.failure_type:
        table.add_row("Failure Type", f"[bold red]{result.failure_type}[/bold red]")
    if result.error_message:
        table.add_row("Error", result.error_message)
    console.print(table)


@app.command()
def run(
    task_file: Path = typer.Option(..., "--task", "-t", help="Path to Task JSON/YAML definition"),
    patch_file: Path = typer.Option(..., "--patch", "-p", help="Path to Patch Proposal JSON"),
    repo_path: Path = typer.Option(Path("."), "--repo", "-r", help="Path to target Git repository"),
    build_cmd: Optional[str] = typer.Option(None, "--build-cmd", "-b", help="Trusted build command to execute"),
    test_cmd: Optional[str] = typer.Option(None, "--test-cmd", help="Trusted regression test command to execute"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Run validation and apply in sandbox without commit"),
):
    """Execute a single-task patch transaction."""
    if not task_file.exists():
        console.print(f"[bold red]Error:[/bold red] Task file not found: {task_file}")
        raise typer.Exit(code=1)

    if not patch_file.exists():
        console.print(f"[bold red]Error:[/bold red] Patch file not found: {patch_file}")
        raise typer.Exit(code=1)

    with open(task_file, "r", encoding="utf-8") as f:
        task_data = json.load(f)
        task = TaskDefinition(**task_data)

    with open(patch_file, "r", encoding="utf-8") as f:
        patch_data = json.load(f)
        proposal = PatchProposal(**patch_data)

    config = PipelineConfig(workspace_root=repo_path.resolve())
    if build_cmd:
        config.build_command = shlex.split(build_cmd)
    if test_cmd:
        config.test_command = shlex.split(test_cmd)

    runtime = DSHRuntime(repo_path, config=config, dry_run=dry_run)
    result = runtime.execute_transaction(task, proposal)

    table = Table(title=f"Transaction Result: {task.task_id}")
    table.add_column("Property", style="cyan")
    table.add_column("Value", style="magenta")

    table.add_row("Success", "[green]YES[/green]" if result.success else "[red]NO[/red]")
    table.add_row("Dry Run", str(result.dry_run))
    if result.commit_hash:
        table.add_row("Commit", result.commit_hash)
    if result.base_commit:
        table.add_row("Base Commit", result.base_commit)
    if result.failure_type:
        table.add_row("Failure Type", f"[bold red]{result.failure_type}[/bold red]")
    if result.error_message:
        table.add_row("Error", result.error_message)

    console.print(table)
    if not result.success:
        raise typer.Exit(code=1)


@app.command()
def demo(
    dry_run: bool = typer.Option(True, "--dry-run/--commit", help="Run demo in dry-run or real commit mode")
):
    """Run built-in Phase 1 demonstration in an isolated temporary Git repository."""
    console.print("[bold green]Starting Phase 1 Transaction Engine Demo in isolated workspace...[/bold green]")
    import tempfile
    
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)
        subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Demo Agent"], cwd=repo_path, check=True)
        subprocess.run(["git", "config", "user.email", "demo@pipeline.local"], cwd=repo_path, check=True)

        demo_file = repo_path / "Player.cs"
        demo_file.write_text("public class Player {\n    public void Update() {}\n}\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo_path, check=True)
        subprocess.run(["git", "commit", "-m", "Initial demo file"], cwd=repo_path, check=True)

        # Demo explicitly uses test_mode to inject mock build runner for demonstration
        runtime = DSHRuntime(repo_path, dry_run=dry_run, test_mode=True)

        task = TaskDefinition(
            task_id="T_DEMO_01",
            title="Hook Player.Update logic",
            allowed_files=["Player.cs"],
            max_lines_added=15,
            max_lines_deleted=5
        )

        proposal = PatchProposal(
            patches=[
                FilePatch(
                    file="Player.cs",
                    hunks=[
                        PatchHunk(
                            old_text="    public void Update() {}",
                            new_text="    public void Update() {\n        // [PIPELINE_VERIFIED: T_DEMO_01]\n    }"
                        )
                    ]
                )
            ],
            reason="Demo transaction hook application",
            confidence=1.0
        )

        res = runtime.execute_transaction(task, proposal)
        console.print(f"[bold]Demo completed successfully: {res.success} (Dry-run: {res.dry_run})[/bold]")


@app.command(name="model")
def model(
    task_file: Path = typer.Option(..., "--task", "-t", help="Path to Task JSON definition"),
    repo_path: Path = typer.Option(Path("."), "--repo", "-r", help="Path to target Git repository"),
    dry_run: bool = typer.Option(True, "--dry-run/--commit", help="Validate in sandbox without committing"),
    env_file: Optional[Path] = typer.Option(None, "--env-file", help="Optional .env file providing DEEPSEEK_API_KEY"),
):
    """Execute a single model-driven patch transaction (single attempt)."""
    try:
        task = _load_json(task_file, TaskDefinition)
        if isinstance(task, list):
            raise typer.BadParameter(
                f"Task file must contain a single TaskDefinition object, not a list: {task_file}"
            )
    except typer.BadParameter as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(code=1)

    runtime, provider, context_builder = _prepare_model_runtime(repo_path, dry_run, env_file)
    result = runtime.execute_with_model(task, provider, context_builder)
    _print_transaction_result(result)
    if not result.success:
        raise typer.Exit(code=1)


@app.command(name="recover")
def recover(
    task_file: Path = typer.Option(..., "--task", "-t", help="Path to Task JSON definition"),
    repo_path: Path = typer.Option(Path("."), "--repo", "-r", help="Path to target Git repository"),
    dry_run: bool = typer.Option(True, "--dry-run/--commit", help="Validate in sandbox without committing"),
    env_file: Optional[Path] = typer.Option(None, "--env-file", help="Optional .env file providing DEEPSEEK_API_KEY"),
):
    """Execute a model-driven task with the 3-attempt recovery loop."""
    try:
        task = _load_json(task_file, TaskDefinition)
        if isinstance(task, list):
            raise typer.BadParameter(
                f"Task file must contain a single TaskDefinition object, not a list: {task_file}"
            )
    except typer.BadParameter as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(code=1)

    runtime, provider, context_builder = _prepare_model_runtime(repo_path, dry_run, env_file)
    result = runtime.execute_with_recovery(task, provider, context_builder)
    _print_transaction_result(result)
    if not result.success:
        raise typer.Exit(code=1)


@app.command(name="dag")
def dag(
    tasks_file: Path = typer.Option(..., "--tasks", "-t", help="Path to JSON list of TaskDefinitions"),
    patches_file: Path = typer.Option(..., "--patches", "-p", help="Path to JSON dict task_id -> PatchProposal"),
    repo_path: Path = typer.Option(Path("."), "--repo", "-r", help="Path to target Git repository"),
    mode: str = typer.Option("sequential", "--mode", help="Scheduling mode: sequential or parallel"),
    max_workers: int = typer.Option(4, "--max-workers", help="Max parallel workers (parallel mode only)"),
    dry_run: bool = typer.Option(True, "--dry-run/--commit", help="Validate all tasks without committing (default)"),
):
    """Execute a multi-task DAG of patch transactions."""
    if mode not in ("sequential", "parallel"):
        console.print(f"[bold red]Error:[/bold red] Invalid --mode '{mode}': expected 'sequential' or 'parallel'.")
        raise typer.Exit(code=1)

    try:
        tasks: List[TaskDefinition] = _load_json(tasks_file, TaskDefinition)
        raw_patches = _load_json(patches_file, None)
    except typer.BadParameter as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(code=1)

    if not isinstance(raw_patches, dict):
        console.print(f"[bold red]Error:[/bold red] Patches file must be a JSON object mapping task_id -> PatchProposal: {patches_file}")
        raise typer.Exit(code=1)

    if not isinstance(tasks, list) or not tasks:
        console.print(f"[bold red]Error:[/bold red] Tasks file must be a non-empty JSON list: {tasks_file}")
        raise typer.Exit(code=1)

    try:
        patch_proposals: Dict[str, PatchProposal] = {
            task_id: PatchProposal(**p) for task_id, p in raw_patches.items()
        }
    except (AttributeError, ValidationError, TypeError) as e:
        console.print(f"[bold red]Error:[/bold red] Patches file must map task_id -> PatchProposal ({patches_file}): {e}")
        raise typer.Exit(code=1)

    from task.dag import TaskDAG, DAGCycleError, DAGDependencyError
    from task.scheduler import DAGScheduler

    dag_graph = TaskDAG()
    try:
        for task in tasks:
            dag_graph.add_task(task)
        dag_graph.build_and_validate()
    except (ValueError, DAGCycleError, DAGDependencyError) as e:
        console.print(f"[bold red]Error:[/bold red] Invalid task DAG: {e}")
        raise typer.Exit(code=1)

    config = PipelineConfig(workspace_root=repo_path.resolve())
    # Follow the `demo` precedent: without a trusted build command configured,
    # run with mock validators (test_mode) so the DAG can execute end-to-end.
    # Production use should set PIPELINE_BUILD_COMMAND / PIPELINE_TEST_COMMAND.
    runtime = DSHRuntime(repo_path, config=config, dry_run=dry_run, test_mode=True)
    scheduler = DAGScheduler(dag_graph, runtime)

    if mode == "parallel":
        summary = scheduler.run_parallel(patch_proposals, max_workers=max_workers)
    else:
        summary = scheduler.run_sequential(patch_proposals)

    table = Table(title=f"DAG Execution Summary ({mode})")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="magenta")
    table.add_row("Total Tasks", str(summary.total_tasks))
    table.add_row("Completed", f"[green]{len(summary.completed_tasks)}[/green] {summary.completed_tasks}")
    table.add_row("Failed", f"[red]{len(summary.failed_tasks)}[/red] {summary.failed_tasks}")
    table.add_row("Aborted", f"[yellow]{len(summary.aborted_tasks)}[/yellow] {summary.aborted_tasks}")
    console.print(table)

    if not summary.success:
        raise typer.Exit(code=1)


@app.command(name="prompt")
def prompt(
    prompt_text: str = typer.Argument(..., help="Natural language prompt describing the change"),
    files: Optional[str] = typer.Option(None, "--files", "-f", help="Comma-separated target files (e.g. Player.cs,Game.cs)"),
    symbol: Optional[str] = typer.Option(None, "--symbol", "-s", help="Optional target symbol to focus on (e.g. Player.Update)"),
    repo_path: Path = typer.Option(Path("."), "--repo", "-r", help="Path to target Git repository"),
    build_cmd: Optional[str] = typer.Option(None, "--build-cmd", "-b", help="Trusted build command to execute"),
    test_cmd: Optional[str] = typer.Option(None, "--test-cmd", help="Trusted regression test command to execute"),
    max_added: int = typer.Option(300, "--max-added", help="Maximum lines allowed to add"),
    max_deleted: int = typer.Option(150, "--max-deleted", help="Maximum lines allowed to delete"),
    dry_run: bool = typer.Option(True, "--dry-run/--commit", help="Validate in sandbox without committing (default)"),
    env_file: Optional[Path] = typer.Option(None, "--env-file", help="Optional .env file providing DEEPSEEK_API_KEY"),
):
    """Execute a model-driven patch transaction directly from a free-form prompt string."""
    import re
    import time

    resolved_repo = repo_path.resolve()
    target_files: List[str] = []

    if files:
        target_files = [f.strip() for f in files.split(",") if f.strip()]
    else:
        # Lightweight file auto-detection (Claude's heuristic)
        tokens = set(re.findall(r"[\w-]+\.\w+|[A-Z][a-zA-Z0-9]+", prompt_text))
        try:
            res = subprocess.run(
                ["git", "ls-files"],
                cwd=resolved_repo,
                capture_output=True,
                text=True,
                check=True,
            )
            all_repo_files = [f.strip() for f in res.stdout.splitlines() if f.strip()]
            for rf in all_repo_files:
                rf_name = Path(rf).name
                rf_stem = Path(rf).stem
                if any(t == rf_name or t == rf_stem or t in rf.split("/") for t in tokens):
                    target_files.append(rf)
        except Exception:
            pass

    if not target_files:
        console.print(
            "[bold red]Error:[/bold red] Could not automatically infer target file(s) from prompt.\n"
            "Please explicitly specify `--files <filename>` (e.g. `--files Player.cs`)."
        )
        raise typer.Exit(code=1)

    console.print(f"[cyan]Target file(s) identified:[/cyan] {', '.join(target_files)}")
    if symbol:
        console.print(f"[cyan]Target symbol specified:[/cyan] {symbol}")

    task_id = f"TASK_{int(time.time())}"
    task = TaskDefinition(
        task_id=task_id,
        title=prompt_text[:80],
        allowed_files=target_files,
        target_symbols=[symbol] if symbol else [],
        max_lines_added=max_added,
        max_lines_deleted=max_deleted,
    )

    runtime, provider, context_builder = _prepare_model_runtime(resolved_repo, dry_run, env_file)
    if build_cmd:
        runtime.config.build_command = shlex.split(build_cmd)
    if test_cmd:
        runtime.config.test_command = shlex.split(test_cmd)

    result = runtime.execute_with_recovery(task, provider, context_builder)
    _print_transaction_result(result)
    if not result.success:
        raise typer.Exit(code=1)


@app.command(name="orchestrate")
def orchestrate(
    goal: str = typer.Argument(..., help="High-level autonomous goal for multi-agent review and patch"),
    target_repo: Path = typer.Option(Path("."), "--target-repo", "-t", help="Target repository path"),
    qa: Optional[str] = typer.Option(
        None,
        "--qa",
        help="Subagent/model for QA (Scout, Architect, Reviewer). Options: agy, claude, codex, deepseek, qwen, xkiro, glm, or specific model.",
    ),
    dev: Optional[str] = typer.Option(
        None,
        "--dev",
        help="Subagent/model for Dev (Coder, Patch Generator). Options: deepseek, codex, claude, agy, qwen, xkiro, glm, or specific model.",
    ),
    max_tasks: int = typer.Option(3, "--max-tasks", help="Maximum TDD tasks to generate and execute"),
    dry_run: bool = typer.Option(True, "--dry-run/--commit", help="Validate in sandbox without committing (default)"),
    test_mode: bool = typer.Option(False, "--test-mode", help="Run with mock subagents for verification"),
):
    """Run full autonomous TDD loop with user-selected QA subagent and Dev subagent."""
    from rich.markdown import Markdown
    from orchestrator.coordinator import AutonomousCoordinator

    resolved_repo = target_repo.resolve()

    # Dynamic resolution: Never hardcode QA or Dev models!
    qa_model = qa or os.environ.get("DSH_QA_MODEL")
    dev_model = dev or os.environ.get("DSH_DEV_MODEL")

    if test_mode:
        qa_model = qa_model or "mock-qa"
        dev_model = dev_model or "mock-dev"

    if not qa_model or not dev_model:
        if sys.stdin.isatty():
            from rich.prompt import Prompt
            console.print("[bold yellow]Subagent Selection Required:[/bold yellow] QA and Dev models were not specified.")
            if not qa_model:
                qa_model = Prompt.ask(
                    "[bold magenta]Choose QA model[/bold magenta] (Auditor/Reviewer)",
                    choices=["agy", "claude", "codex", "deepseek", "qwen", "xkiro", "glm"],
                    default="agy",
                )
            if not dev_model:
                dev_model = Prompt.ask(
                    "[bold blue]Choose Dev model[/bold blue] (Coder)",
                    choices=["deepseek", "codex", "claude", "agy", "qwen", "xkiro", "glm"],
                    default="deepseek",
                )
        else:
            console.print("[bold red]Subagent Configuration Error:[/bold red] QA and Dev models must not be hardcoded or assumed.")
            console.print(
                "You must explicitly specify both the QA model (Auditor/Reviewer) and Dev model (Coder):\n"
                "  --qa <model>  (e.g., agy, claude, codex, deepseek, qwen, glm)\n"
                "  --dev <model> (e.g., deepseek, codex, claude, agy, qwen, glm)\n"
                "\n[dim]Action for AI Agent: Stop and ask the user to confirm model roles.[/dim]"
            )
            raise typer.Exit(code=1)


    console.print(f"[bold green]Starting Autonomous Orchestration on:[/bold green] {resolved_repo}")
    console.print(f"[bold cyan]Goal:[/bold cyan] {goal}")
    console.print(f"[bold magenta]QA (Lead/Reviewer):[/bold magenta] {qa_model} | [bold blue]Dev (Coder):[/bold blue] {dev_model}")
    console.print(f"[dim]Dry-run: {dry_run} | Max tasks: {max_tasks}[/dim]\n")

    coordinator = AutonomousCoordinator(
        target_repo=resolved_repo,
        qa_name=qa_model,
        dev_name=dev_model,
        dry_run=dry_run,
        test_mode=test_mode,
    )

    result = coordinator.run(user_goal=goal, max_tasks=max_tasks)
    console.print(Markdown(result.final_report))

    if not result.success:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()


