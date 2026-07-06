"""Thin wrappers over the `gh` CLI. GitHub itself is the source of truth for
the queue; taskloop keeps no persistent state of its own."""

from __future__ import annotations

import json
import subprocess

from .config import Config

ISSUE_FIELDS = "number,title,body,labels,url"


class GhError(RuntimeError):
    pass


def _gh(cfg: Config, *args: str) -> str:
    proc = subprocess.run(
        [cfg.gh_bin, *args],
        cwd=cfg.repo_root,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise GhError(f"gh {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def list_open_issues(cfg: Config, limit: int = 50) -> list[dict]:
    out = _gh(
        cfg, "issue", "list",
        "--label", cfg.label,
        "--state", "open",
        "--limit", str(limit),
        "--json", ISSUE_FIELDS,
    )
    return json.loads(out)


def get_issue(cfg: Config, number: int) -> dict:
    out = _gh(cfg, "issue", "view", str(number), "--json", ISSUE_FIELDS)
    return json.loads(out)


def comment(cfg: Config, number: int, body: str) -> None:
    _gh(cfg, "issue", "comment", str(number), "--body", body)


def create_pr(cfg: Config, head: str, title: str, body: str) -> str:
    """Open a PR for `head` against the base branch; returns the PR URL."""
    out = _gh(
        cfg, "pr", "create",
        "--head", head,
        "--base", cfg.base_branch,
        "--title", title,
        "--body", body,
    )
    return out.strip().splitlines()[-1] if out.strip() else ""
