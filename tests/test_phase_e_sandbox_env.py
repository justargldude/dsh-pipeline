"""Phase E (v2.3) — SAFE_ENV_ALLOWLIST replaces os.environ.copy() in the sandbox.

Contracts:
1. build/sandbox.py defines SAFE_ENV_ALLOWLIST (PATH, DOTNET_*, PYTHON*,
   NODE*, HOME basics) and DANGEROUS env patterns.
2. run_hardened_command builds the child env from the ALLOWLIST ONLY: parent
   secrets (API keys, tokens, credentials) NEVER propagate into sandboxed
   subprocesses even when present in os.environ.
3. Explicit `env` overrides still work; GIT_TERMINAL_PROMPT/LC_ALL forced.
4. Allowlist semantics: anything NOT matching the allowlist is dropped
   (fail-closed), not a blocklist.
"""
import pytest

from build.sandbox import run_hardened_command, SAFE_ENV_ALLOWLIST, build_child_env


class TestSafeEnvAllowlist:
    def test_allowlist_constant_exists(self):
        assert isinstance(SAFE_ENV_ALLOWLIST, (list, tuple))
        assert "PATH" in SAFE_ENV_ALLOWLIST

    def test_allowlist_covers_toolchains(self):
        import fnmatch
        pats = list(SAFE_ENV_ALLOWLIST)
        # Dotnet / python / node toolchains must be usable inside the sandbox
        assert any(fnmatch.fnmatch("DOTNET_ROOT", p) for p in pats)
        assert any(fnmatch.fnmatch("DOTNET_CLI_TELEMETRY_OPTOUT", p) for p in pats)
        assert any(fnmatch.fnmatch("PYTHONPATH", p) for p in pats)
        assert any(fnmatch.fnmatch("PYTHONUNBUFFERED", p) for p in pats)
        assert any(fnmatch.fnmatch("NODE_PATH", p) for p in pats)
        assert any(fnmatch.fnmatch("NODE_OPTIONS", p) for p in pats)

    def test_build_child_env_drops_secrets(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-123")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-456")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
        monkeypatch.setenv("MY_STUFF", "hello")
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        child = build_child_env()
        assert "sk-secret-123" not in str(child.values())
        assert "sk-openai-456" not in str(child.values())
        assert "ghp_secret" not in str(child.values())
        assert "aws-secret" not in str(child.values())
        assert "MY_STUFF" not in child, "non-allowlisted vars must be dropped"
        assert child.get("PATH") == "/usr/bin:/bin", "PATH must survive"

    def test_build_child_env_keeps_allowlisted(self, monkeypatch):
        monkeypatch.setenv("DOTNET_ROOT", "/usr/share/dotnet")
        monkeypatch.setenv("PYTHONPATH", "/opt/py")
        monkeypatch.setenv("NODE_OPTIONS", "--max-old-space-size=512")
        child = build_child_env()
        assert child.get("DOTNET_ROOT") == "/usr/share/dotnet"
        assert child.get("PYTHONPATH") == "/opt/py"
        assert child.get("NODE_OPTIONS") == "--max-old-space-size=512"

    def test_build_child_env_explicit_override(self):
        child = build_child_env(env={"MY_TASK_VAR": "x1"})
        assert child.get("MY_TASK_VAR") == "x1"

    def test_build_child_env_forces_deterministic(self):
        child = build_child_env(env={"LC_ALL": "vi_VN"})
        assert child["GIT_TERMINAL_PROMPT"] == "0"
        assert child["LC_ALL"] == "C"

    def test_run_hardened_command_never_leaks_parent_secrets(self, tmp_path, monkeypatch):
        """End-to-end: a sandboxed subprocess must not see parent secrets."""
        import os
        import sys
        monkeypatch.setenv("DSH_FAKE_API_KEY", "leak-me-if-you-can")
        script = (
            "import os, sys;"
            "sys.stdout.write(os.environ.get('DSH_FAKE_API_KEY', 'NOT_PRESENT'))"
        )
        rc, out, timed_out, sig, trunc = run_hardened_command(
            cmd=[sys.executable, "-c", script],
            cwd=tmp_path,
            timeout_seconds=30,
        )
        assert rc == 0
        assert "leak-me-if-you-can" not in out
        assert "NOT_PRESENT" in out

    def test_run_hardened_command_path_still_works(self, tmp_path):
        """The sandbox must remain functional: plain commands resolve and run."""
        import sys
        script = "import sys; sys.stdout.write('ok')"
        rc, out, _tt, _s, _tr = run_hardened_command(
            cmd=[sys.executable, "-c", script], cwd=tmp_path, timeout_seconds=30
        )
        assert rc == 0 and "ok" in out
