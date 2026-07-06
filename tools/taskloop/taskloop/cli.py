from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import github
from .config import Config, detect_base_branch


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="taskloop",
        description="Drain GitHub issues with a given label using an agentic "
                    "orchestrator and parallel headless Claude Code workers.",
    )
    p.add_argument("--label", required=True, help="issue label to pick up")
    p.add_argument("--repo-root", default=".", help="path to the git repo (default: cwd)")
    p.add_argument("--base", default=None, help="base branch (default: auto-detect)")
    p.add_argument("--max-parallel", type=int, default=2, help="max concurrent workers")
    p.add_argument("--verify-cmd", default=None,
                   help="shell command that must pass in a worktree (e.g. 'make test')")
    p.add_argument("--model", default=None, help="model for orchestrator and workers")
    p.add_argument("--workdir", default=None,
                   help="where worktrees/logs live (default: <repo>/.taskloop)")
    p.add_argument("--max-attempts", type=int, default=2,
                   help="implementation attempts per issue before giving up")
    p.add_argument("--no-pr", action="store_true",
                   help="push branches but do not open PRs")
    p.add_argument("--no-skip-permissions", action="store_true",
                   help="run workers with --permission-mode acceptEdits instead of "
                        "--dangerously-skip-permissions")
    p.add_argument("--dry-run", action="store_true",
                   help="list matching issues and the plan, then exit")
    p.add_argument("--claude-bin", default="claude")
    p.add_argument("--gh-bin", default="gh")
    return p


def config_from_args(argv: list[str] | None = None) -> Config:
    args = build_parser().parse_args(argv)
    repo_root = Path(args.repo_root).resolve()
    return Config(
        label=args.label,
        repo_root=repo_root,
        base_branch=args.base or detect_base_branch(repo_root),
        max_parallel=args.max_parallel,
        verify_cmd=args.verify_cmd,
        model=args.model,
        workdir=Path(args.workdir) if args.workdir else None,
        max_attempts=args.max_attempts,
        open_pr=not args.no_pr,
        skip_permissions=not args.no_skip_permissions,
        dry_run=args.dry_run,
        claude_bin=args.claude_bin,
        gh_bin=args.gh_bin,
    )


def main(argv: list[str] | None = None) -> None:
    cfg = config_from_args(argv)

    if not (cfg.repo_root / ".git").exists():
        sys.exit(f"taskloop: {cfg.repo_root} is not a git repository root")

    issues = github.list_open_issues(cfg)
    print(f"[taskloop] {len(issues)} open issue(s) labeled '{cfg.label}' "
          f"in {cfg.repo_root} (base: {cfg.base_branch}, "
          f"max-parallel: {cfg.max_parallel})")
    for i in issues:
        print(f"  #{i['number']} {i['title']}")

    if cfg.dry_run:
        print("[taskloop] dry run — plan: one worktree+branch per issue, "
              f"verify with {cfg.verify_cmd or '(none)'}, reviewer agent, "
              f"{'PR per issue' if cfg.open_pr else 'push only'}")
        return
    if not issues:
        return

    import anyio

    from .orchestrator import run_loop

    state = anyio.run(run_loop, cfg)
    print(f"[taskloop] done={sorted(state.done)} failed={sorted(state.failed)}")
    for n, note in state.done.items():
        print(f"  #{n}: {note}")
    for n, reason in state.failed.items():
        print(f"  #{n}: gave up — {reason}")


if __name__ == "__main__":
    main()
