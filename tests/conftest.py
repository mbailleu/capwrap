"""Shared fixtures.

Tests are split by what they need: most run anywhere, while those marked
``@pytest.mark.sandbox`` require a working bwrap and are skipped with an
explanation rather than failing on a host that cannot sandbox.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from capwrap.runtime import probe


@pytest.fixture
def state_dir(tmp_path, monkeypatch) -> Path:
    """Point capwrap's state root at a temp directory for the whole test.

    Torn down with `force_rmtree` rather than left to pytest, because overlay
    work directories are mode 000 and pytest's own cleanup cannot remove them.
    """
    from capwrap.paths import force_rmtree

    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setenv("CAPWRAP_STATE", str(root))
    yield root
    force_rmtree(root)


@pytest.fixture
def git_repo(tmp_path) -> Path:
    """A small git repo with one commit on `main`."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=repo, check=True,
            capture_output=True, text=True,
        ).stdout.strip()

    git("init", "--quiet", "--initial-branch=main")
    git("config", "user.email", "test@capwrap.local")
    git("config", "user.name", "capwrap tests")
    (repo / "README.md").write_text("hello\n")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("x = 1\n")
    git("add", "-A")
    git("commit", "--quiet", "-m", "initial")
    return repo


@pytest.fixture(scope="session")
def bwrap_report():
    return probe.run_all()


@pytest.fixture
def require_sandbox(bwrap_report):
    """Skip a test when this host cannot actually run a sandbox."""
    check = bwrap_report.get("bwrap can create namespaces")
    if check is None or not check.ok:
        detail = check.detail if check else "no bwrap"
        pytest.skip(f"sandboxing unavailable: {detail}")
    return bwrap_report


@pytest.fixture
def run_in_sandbox(require_sandbox):
    """Run a shell command inside a prepared container, returning its output."""
    from capwrap.paths import ContainerPaths
    from capwrap.runtime import bwrap as bwrap_mod
    from capwrap.runtime import fsprep

    def run(config, script: str, timeout: int = 60) -> subprocess.CompletedProcess:
        paths = ContainerPaths(config.name)
        prepared = fsprep.prepare(
            config, paths,
            overlay_backend=require_sandbox.overlay_backend or "kernel",
        )
        config.runtime.command = ["/bin/bash", "-c", script]
        argv = bwrap_mod.build_argv(
            config, prepared, paths, bwrap=probe.find_bwrap() or "bwrap"
        )
        try:
            return subprocess.run(
                argv, capture_output=True, text=True, timeout=timeout, check=False
            )
        finally:
            prepared.cleanup()

    return run
