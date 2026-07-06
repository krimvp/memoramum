from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    label: str
    repo_root: Path
    base_branch: str = "main"
    max_parallel: int = 2
    verify_cmd: str | None = None
    model: str | None = None
    workdir: Path | None = None  # where worktrees live; default <repo>/.taskloop
    max_attempts: int = 2  # retries per issue before giving up
    open_pr: bool = True
    skip_permissions: bool = True  # workers run with --dangerously-skip-permissions
    dry_run: bool = False
    claude_bin: str = "claude"
    gh_bin: str = "gh"
    orchestrator_max_turns: int = 200
    extra_worker_args: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.repo_root = Path(self.repo_root).resolve()
        if self.workdir is None:
            self.workdir = self.repo_root / ".taskloop"
        self.workdir = Path(self.workdir).resolve()

    @property
    def worktrees_dir(self) -> Path:
        return self.workdir / "worktrees"

    @property
    def logs_dir(self) -> Path:
        return self.workdir / "logs"

    def ensure_dirs(self) -> None:
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)


def detect_base_branch(repo_root: Path) -> str:
    """Best-effort default branch detection; falls back to 'main'."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "symbolic-ref", "refs/remotes/origin/HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        return out.rsplit("/", 1)[-1] or "main"
    except (subprocess.CalledProcessError, OSError):
        return "main"
