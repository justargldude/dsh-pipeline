import pytest
from pathlib import Path
from safety.languages import (
    get_driver_for_file,
    get_driver_for_language,
    get_all_registered_drivers,
    ASTViolationError,
)
from safety.languages.python import PythonDriver
from safety.languages.javascript import JavascriptDriver
from safety.languages.cpp import CppDriver
from safety.scope_guard import ScopeGuard, ScopeViolationError
from task.schema import TaskDefinition, PatchProposal, FilePatch, PatchHunk


def test_driver_registry_resolution():
    drivers = get_all_registered_drivers()
    lang_ids = {d.language_id for d in drivers}
    assert {"csharp", "python", "javascript", "cpp"}.issubset(lang_ids)

    assert get_driver_for_file("Foo.cs").language_id == "csharp"
    assert get_driver_for_file("bar.py").language_id == "python"
    assert get_driver_for_file("app.ts").language_id == "javascript"
    assert get_driver_for_file("server.js").language_id == "javascript"
    assert get_driver_for_file("kernel.cpp").language_id == "cpp"
    assert get_driver_for_file("native.c").language_id == "cpp"


def test_python_driver_syntax_and_deletions():
    driver = PythonDriver()

    # Valid transition
    old_py = "def calculate(x):\n    return x * 2\n"
    new_py = "def calculate(x):\n    return x * 3\n"
    driver.validate_transition(old_py, new_py, file_path="calc.py")

    # Syntax error
    bad_py = "def calculate(x)\n    return x * 3\n"
    with pytest.raises(ASTViolationError) as exc:
        driver.validate_transition(old_py, bad_py, file_path="calc.py")
    assert "syntax error" in str(exc.value)

    # Function deletion
    deleted_py = "def other():\n    pass\n"
    with pytest.raises(ASTViolationError) as exc:
        driver.validate_transition(old_py, deleted_py, file_path="calc.py")
    assert "Disallowed function deletion" in str(exc.value)


def test_python_driver_purity_checks():
    driver = PythonDriver()

    pure_code = "def add(a, b):\n    return a + b\n"
    rep = driver.check_purity(pure_code, file_path="core/math.py")
    assert rep.is_pure
    assert len(rep.violations) == 0

    impure_code = "def add(a, b):\n    with open('log.txt', 'w') as f:\n        f.write('x')\n    return a + b\n"
    rep = driver.check_purity(impure_code, file_path="core/math.py")
    assert not rep.is_pure
    assert any("File I/O" in v for v in rep.violations)


def test_javascript_driver_validation_and_purity():
    driver = JavascriptDriver()

    old_js = "function add(a, b) { return a + b; }\n"
    new_js = "function add(a, b) { return (a + b) | 0; }\n"
    driver.validate_transition(old_js, new_js, file_path="math.js")

    # Impure check
    impure_js = "function getSeed() { return Math.random() + Date.now(); }\n"
    rep = driver.check_purity(impure_js, file_path="core/rng.js")
    assert not rep.is_pure
    assert any("random" in v.lower() for v in rep.violations)


def test_cpp_driver_validation_and_purity():
    driver = CppDriver()

    old_cpp = "int multiply(int a, int b) { return a * b; }\n"
    new_cpp = "int multiply(int a, int b) { return a * b * 1; }\n"
    driver.validate_transition(old_cpp, new_cpp, file_path="math.cpp")

    # Impure check
    impure_cpp = "int log_and_add(int a, int b) { FILE* f = fopen(\"a.txt\", \"r\"); return a + b; }\n"
    rep = driver.check_purity(impure_cpp, file_path="core/log.cpp")
    assert not rep.is_pure
    assert any("File System I/O" in v for v in rep.violations)


def test_scope_guard_fcis_enforcement_in_core(tmp_path: Path):
    """ScopeGuard must block impure calls in Core / Domain directories,
    but allow them in Imperative Shell directories."""
    guard = ScopeGuard()

    # Core file: purity enforced
    core_dir = tmp_path / "Core"
    core_dir.mkdir(parents=True, exist_ok=True)
    core_file = core_dir / "Accounting.cs"
    core_file.write_text("public class Accounting {\n    public int Compute() => 42;\n}\n", encoding="utf-8")

    task = TaskDefinition(
        task_id="T_FCIS_CORE",
        title="Tweak Accounting",
        allowed_files=["Core/Accounting.cs"],
    )

    # Dev tries to introduce file I/O in Functional Core
    impure_patch = PatchProposal(
        patches=[
            FilePatch(
                file="Core/Accounting.cs",
                hunks=[
                    PatchHunk(
                        old_text="    public int Compute() => 42;\n",
                        new_text="    public int Compute() { System.IO.File.WriteAllText(\"leak.txt\", \"data\"); return 42; }\n",
                    )
                ]
            )
        ]
    )

    with pytest.raises(ScopeViolationError) as exc:
        guard.validate(task, impure_patch, repo_root=tmp_path)
    assert "FCIS Purity violation" in str(exc.value)

    # Shell file: I/O permitted in Imperative Shell
    shell_dir = tmp_path / "Shell"
    shell_dir.mkdir(parents=True, exist_ok=True)
    shell_file = shell_dir / "FileStorage.cs"
    shell_file.write_text("public class FileStorage {\n    public void Save() {}\n}\n", encoding="utf-8")

    shell_task = TaskDefinition(
        task_id="T_FCIS_SHELL",
        title="Tweak Shell Storage",
        allowed_files=["Shell/FileStorage.cs"],
    )
    shell_patch = PatchProposal(
        patches=[
            FilePatch(
                file="Shell/FileStorage.cs",
                hunks=[
                    PatchHunk(
                        old_text="    public void Save() {}\n",
                        new_text="    public void Save() { System.IO.File.WriteAllText(\"ok.txt\", \"data\"); }\n",
                    )
                ]
            )
        ]
    )

    # Shell file is allowed to do I/O!
    guard.validate(shell_task, shell_patch, repo_root=tmp_path)
