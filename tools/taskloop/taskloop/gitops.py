"""Git worktree and branch plumbing. One issue = one branch = one worktree."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config


class GitError(RuntimeError):
    pass


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc


def branch_for(issue_number: int) -> str:
    return f"taskloop/issue-{issue_number}"


def base_ref(cfg: Config) -> str:
    """Prefer the remote-tracking base so workers branch from fresh upstream."""
    proc = _git(cfg.repo_root, "rev-parse", "--verify", "--quiet",
                f"origin/{cfg.base_branch}", check=False)
    if proc.returncode == 0:
        return f"origin/{cfg.base_branch}"
    return cfg.base_branch


def fetch_base(cfg: Config) -> None:
    _git(cfg.repo_root, "fetch", "origin", cfg.base_branch, check=False)


def worktree_path(cfg: Config, issue_number: int) -> Path:
    return cfg.worktrees_dir / f"issue-{issue_number}"


def add_worktree(cfg: Config, issue_number: int) -> Path:
    """Create (or reuse) the worktree + branch for an issue, off fresh base."""
    path = worktree_path(cfg, issue_number)
    if path.exists():
        return path
    fetch_base(cfg)
    branch = branch_for(issue_number)
    exists = _git(cfg.repo_root, "rev-parse", "--verify", "--quiet", branch, check=False)
    if exists.returncode == 0:
        _git(cfg.repo_root, "worktree", "add", str(path), branch)
    else:
        _git(cfg.repo_root, "worktree", "add", "-b", branch, str(path), base_ref(cfg))
    return path


def remove_worktree(cfg: Config, issue_number: int, delete_branch: bool = False) -> None:
    path = worktree_path(cfg, issue_number)
    if path.exists():
        _git(cfg.repo_root, "worktree", "remove", "--force", str(path), check=False)
    if delete_branch:
        _git(cfg.repo_root, "branch", "-D", branch_for(issue_number), check=False)


def commit_count(cfg: Config, issue_number: int) -> int:
    """Commits on the issue branch that are not on base."""
    wt = worktree_path(cfg, issue_number)
    proc = _git(wt, "rev-list", "--count", f"{base_ref(cfg)}..HEAD")
    return int(proc.stdout.strip() or "0")


def diff_against_base(cfg: Config, issue_number: int, max_chars: int = 60_000) -> str:
    wt = worktree_path(cfg, issue_number)
    out = _git(wt, "diff", f"{base_ref(cfg)}...HEAD").stdout
    if len(out) > max_chars:
        out = out[:max_chars] + f"\n... [diff truncated at {max_chars} chars]"
    return out


def dirty_files(cfg: Config, issue_number: int) -> list[str]:
    wt = worktree_path(cfg, issue_number)
    out = _git(wt, "status", "--porcelain").stdout
    return [line[3:] for line in out.splitlines() if line.strip()]


@dataclass
class RebaseResult:
    clean: bool
    conflict_files: list[str] = field(default_factory=list)
    detail: str = ""


def rebase_onto_base(cfg: Config, issue_number: int) -> RebaseResult:
    """Rebase the issue branch onto fresh base. On conflict, the worktree is
    left mid-rebase so a conflict-fixer agent can resolve and continue."""
    fetch_base(cfg)
    wt = worktree_path(cfg, issue_number)
    proc = _git(wt, "rebase", base_ref(cfg), check=False)
    if proc.returncode == 0:
        return RebaseResult(clean=True, detail=proc.stdout.strip())
    status = _git(wt, "diff", "--name-only", "--diff-filter=U", check=False).stdout
    conflicts = [f for f in status.splitlines() if f.strip()]
    return RebaseResult(
        clean=False,
        conflict_files=conflicts,
        detail=(proc.stderr.strip() or proc.stdout.strip()),
    )


def abort_rebase(cfg: Config, issue_number: int) -> None:
    _git(worktree_path(cfg, issue_number), "rebase", "--abort", check=False)


def push(cfg: Config, issue_number: int) -> None:
    wt = worktree_path(cfg, issue_number)
    # force-with-lease: the branch may have been rewritten by a rebase
    _git(wt, "push", "--force-with-lease", "-u", "origin", branch_for(issue_number))
