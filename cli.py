import json
import subprocess
from pathlib import Path
from typing import Optional
import typer
from rich.console import Console
from rich.table import Table

from task.schema import TaskDefinition, PatchProposal, FilePatch, PatchHunk
from core.runtime import DSHRuntime
from build.sandbox import MockBuildRunner, SubprocessBuildRunner

app = typer.Typer(help="DSH Pipeline - Deterministic Patch Transaction Engine")
console = Console()


@app.command()
def run(
    task_file: Path = typer.Option(..., "--task", "-t", help="Path to Task JSON/YAML definition"),
    patch_file: Path = typer.Option(..., "--patch", "-p", help="Path to Patch Proposal JSON"),
    repo_path: Path = typer.Option(Path("."), "--repo", "-r", help="Path to target Git repository"),
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

    runtime = DSHRuntime(repo_path, dry_run=dry_run)
    result = runtime.execute_transaction(task, proposal)

    table = Table(title=f"Transaction Result: {task.task_id}")
    table.add_column("Property", style="cyan")
    table.add_column("Value", style="magenta")

    table.add_row("Success", "[green]YES[/green]" if result.success else "[red]NO[/red]")
    table.add_row("Dry Run", str(result.dry_run))
    if result.commit_hash:
        table.add_row("Commit", result.commit_hash)
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

        runtime = DSHRuntime(repo_path, dry_run=dry_run)

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


if __name__ == "__main__":
    app()
