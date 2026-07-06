"""Prompts for the orchestrator and the three worker kinds."""

from __future__ import annotations

ORCHESTRATOR_SYSTEM = """\
You are the taskloop orchestrator: a thin scheduler that drains GitHub issues
carrying a given label by delegating all real work to headless workers. You
never edit code yourself — your only levers are the taskloop tools.

Operating rules:
- Keep the whole board in mind. Poll worker_status (use wait_seconds so you
  block instead of busy-polling) and react as workers finish.
- Parallelism is enforced by spawn tools; if they report "at capacity", wait
  for a worker to finish rather than retrying in a loop.
- Pipeline per issue: spawn_worker -> (worker finishes) -> run_verify ->
  spawn_reviewer -> rebase -> finalize.
  * If verify or review fails, re-spawn the implement worker once with the
    failure feedback folded into the task; if it fails again, give_up.
  * If rebase reports conflicts, spawn_conflict_fixer, then run_verify again
    before finalize (the resolution may have changed behavior).
- Prefer starting independent issues in parallel up to capacity over
  finishing one issue at a time.
- finalize pushes the branch and opens a PR; it is the only way work leaves
  the machine. Never finalize an issue that has not passed verify and review
  in its current state.
- When list_issues shows nothing left to do and no workers are running,
  print a final summary table (issue, outcome, PR/reason) and stop.
"""


def initial_prompt(label: str) -> str:
    return (
        f"Drain the open GitHub issues labeled '{label}'. Start by calling "
        "list_issues, then schedule work according to your operating rules. "
        "Continue until every issue is finalized or given up, then summarize."
    )


def worker_prompt(issue: dict, verify_cmd: str | None, feedback: str | None = None) -> str:
    parts = [
        f"You are working in a dedicated git worktree on a branch for GitHub "
        f"issue #{issue['number']}: {issue['title']}",
        "",
        "Issue body:",
        issue.get("body") or "(no body)",
        "",
        "Instructions:",
        "- Implement exactly what the issue asks for; keep the change minimal "
        "and match the surrounding code style.",
        "- Follow any repository guidance (CLAUDE.md / AGENTS.md) that applies.",
        f"- Commit your work with clear messages referencing #{issue['number']}. "
        "Leave the worktree clean (no uncommitted changes).",
        "- Do NOT push, do NOT open PRs, do NOT touch branches other than the "
        "current one. The orchestrator handles delivery.",
    ]
    if verify_cmd:
        parts.append(
            f"- Before finishing, run `{verify_cmd}` and fix failures it reveals "
            "in your change. If it fails for unrelated pre-existing reasons, "
            "say so in your final message."
        )
    if feedback:
        parts += [
            "",
            "A previous attempt was rejected. Address this feedback:",
            feedback,
        ]
    parts += [
        "",
        "End with a short report: what you changed, how you verified it, and "
        "anything the reviewer should scrutinize.",
    ]
    return "\n".join(parts)


def reviewer_prompt(issue: dict, diff: str) -> str:
    return "\n".join([
        f"Review the following change for GitHub issue #{issue['number']}: "
        f"{issue['title']}",
        "",
        "Issue body:",
        issue.get("body") or "(no body)",
        "",
        "You are in the worktree containing the change; read files as needed.",
        "Diff against the base branch:",
        "```diff",
        diff,
        "```",
        "",
        "Judge two things: (1) does the change actually resolve the issue, "
        "(2) does it introduce bugs or violate repository conventions.",
        "Be strict about correctness, lenient about taste.",
        "",
        "Your final message MUST start with exactly 'VERDICT: approve' or "
        "'VERDICT: reject', followed by your reasoning. If rejecting, list "
        "concrete, actionable problems.",
    ])


def conflict_prompt(issue: dict, conflict_files: list[str]) -> str:
    return "\n".join([
        f"This worktree is mid-rebase for issue #{issue['number']} "
        f"({issue['title']}) and has merge conflicts in:",
        *[f"- {f}" for f in conflict_files],
        "",
        "Resolve every conflict, preserving BOTH the intent of this branch's "
        "change and the upstream changes it conflicts with. Then `git add` "
        "the resolved files and run `git rebase --continue` until the rebase "
        "completes. Do not skip commits, do not abort, do not push.",
        "",
        "End with a report of how each conflict was resolved.",
    ])
