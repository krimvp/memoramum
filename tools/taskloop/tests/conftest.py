from __future__ import annotations

import os
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

from taskloop.config import Config


def _write_exe(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + textwrap.dedent(body))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def fake_bins(tmp_path: Path) -> Path:
    """Directory containing fake `gh` and `claude` executables."""
    bins = tmp_path / "bin"
    bins.mkdir()
    _write_exe(bins / "gh", """\
        # echoes canned JSON for `issue list`, records everything else
        echo "$@" >> "$FAKE_GH_LOG"
        case "$1 $2" in
          "issue list") cat "$FAKE_GH_ISSUES" ;;
          "issue view") echo '{"number": 7, "title": "t", "body": "b", "labels": [], "url": "u"}' ;;
          "pr create") echo "https://example.test/pr/1" ;;
        esac
        """)
    _write_exe(bins / "claude", """\
        # pretends to be `claude -p`: sleeps briefly, emits a JSON result
        sleep "${FAKE_CLAUDE_SLEEP:-0.2}"
        echo '{"result": "fake worker done", "is_error": false, "session_id": "s", "total_cost_usd": 0.0}'
        exit "${FAKE_CLAUDE_EXIT:-0}"
        """)
    return bins


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A throwaway git repo with an initial commit on `main`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True,
                       capture_output=True)
    git("init", "-b", "main")
    git("config", "user.email", "t@example.test")
    git("config", "user.name", "t")
    (repo / "hello.txt").write_text("hello\n")
    git("add", ".")
    git("commit", "-m", "init")
    return repo


@pytest.fixture
def cfg(git_repo: Path, fake_bins: Path, tmp_path: Path, monkeypatch) -> Config:
    issues_file = tmp_path / "issues.json"
    issues_file.write_text(
        '[{"number": 7, "title": "Add greeting", "body": "say hi", '
        '"labels": [{"name": "agent-task"}], "url": "https://example.test/7"}]'
    )
    monkeypatch.setenv("FAKE_GH_ISSUES", str(issues_file))
    monkeypatch.setenv("FAKE_GH_LOG", str(tmp_path / "gh.log"))
    monkeypatch.setenv("PATH", f"{fake_bins}{os.pathsep}{os.environ['PATH']}")
    return Config(
        label="agent-task",
        repo_root=git_repo,
        base_branch="main",
        max_parallel=2,
        workdir=tmp_path / "work",
        claude_bin=str(fake_bins / "claude"),
        gh_bin=str(fake_bins / "gh"),
    )
