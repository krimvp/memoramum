"""The agentic orchestrator.

One Claude Agent SDK session whose ONLY tools are the thin scheduling layer
below. The model keeps the whole board in mind; the tools do the mechanics
(worktrees, subprocesses, capacity, git, gh) deterministically.
"""

from __future__ import annotations

import anyio
from dataclasses import dataclass, field
from typing import Any

from . import github, gitops, prompts
from .config import Config
from .workers import AtCapacity, WorkerPool


@dataclass
class LoopState:
    cfg: Config
    pool: WorkerPool
    issues: dict[int, dict] = field(default_factory=dict)  # number -> gh issue
    attempts: dict[int, int] = field(default_factory=dict)
    done: dict[int, str] = field(default_factory=dict)  # number -> PR url / note
    failed: dict[int, str] = field(default_factory=dict)  # number -> reason

    def issue(self, number: int) -> dict:
        if number not in self.issues:
            self.issues[number] = github.get_issue(self.cfg, number)
        return self.issues[number]


def _text(msg: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}]}


def build_server(state: LoopState):
    """Create the in-process MCP server exposing the scheduling tools."""
    from claude_agent_sdk import create_sdk_mcp_server, tool

    cfg = state.cfg

    @tool("list_issues", "List open issues carrying the configured label, with "
          "current taskloop state for each.", {})
    async def list_issues(args: dict[str, Any]) -> dict[str, Any]:
        fetched = github.list_open_issues(cfg)
        state.issues.update({i["number"]: i for i in fetched})
        lines = []
        for i in fetched:
            n = i["number"]
            w = state.pool.worker_for(n)
            if n in state.done:
                st = f"done ({state.done[n]})"
            elif n in state.failed:
                st = f"given up ({state.failed[n]})"
            elif w and w.running:
                st = f"{w.kind} worker running ({w.elapsed}s)"
            elif w:
                st = f"{w.kind} worker finished (exit {w.exit_code})"
            else:
                st = "unclaimed"
            lines.append(f"#{n} [{st}] {i['title']}")
        if not lines:
            lines = ["(no open issues with this label)"]
        lines.append(f"\ncapacity: {state.pool.running_count()}/{cfg.max_parallel} workers running")
        return _text("\n".join(lines))

    @tool("spawn_worker", "Start an implementation worker for an issue in its "
          "own worktree/branch. Pass feedback when re-attempting after a "
          "failed verify/review.", {"issue_number": int, "feedback": str})
    async def spawn_worker(args: dict[str, Any]) -> dict[str, Any]:
        n = int(args["issue_number"])
        feedback = args.get("feedback") or None
        attempts = state.attempts.get(n, 0)
        if attempts >= cfg.max_attempts:
            return _text(f"issue #{n} already used {attempts} attempts; give_up instead")
        issue = state.issue(n)
        try:
            wt = gitops.add_worktree(cfg, n)
            worker = state.pool.spawn(
                n, "implement",
                prompts.worker_prompt(issue, cfg.verify_cmd, feedback),
                cwd=wt,
            )
        except AtCapacity as e:
            return _text(str(e))
        state.attempts[n] = attempts + 1
        return _text(f"implement worker started for #{n} (attempt "
                     f"{state.attempts[n]}/{cfg.max_attempts}) in {worker.cwd}")

    @tool("worker_status", "Snapshot of all workers. Set wait_seconds > 0 to "
          "block until a running worker finishes (or timeout) instead of "
          "polling in a loop.", {"wait_seconds": int})
    async def worker_status(args: dict[str, Any]) -> dict[str, Any]:
        wait = int(args.get("wait_seconds") or 0)
        if wait > 0 and state.pool.running_count() > 0:
            await anyio.to_thread.run_sync(
                lambda: state.pool.wait_for_change(timeout_s=wait)
            )
        snaps = state.pool.status()
        if not snaps:
            return _text("no workers")
        lines = [
            f"#{s['issue']} {s['kind']}: {s['state']} ({s['elapsed_s']}s)"
            for s in snaps
        ]
        return _text("\n".join(lines))

    @tool("worker_report", "Final report of a finished worker for an issue "
          "(its closing message, or log tail on failure).", {"issue_number": int})
    async def worker_report(args: dict[str, Any]) -> dict[str, Any]:
        n = int(args["issue_number"])
        w = state.pool.worker_for(n)
        if w is None:
            return _text(f"no worker for issue #{n}")
        if w.running:
            return _text(f"worker for #{n} still running ({w.elapsed}s)")
        res = w.result()
        commits = gitops.commit_count(cfg, n) if gitops.worktree_path(cfg, n).exists() else 0
        dirty = gitops.dirty_files(cfg, n) if gitops.worktree_path(cfg, n).exists() else []
        report = res.get("result") or str(res)
        extra = f"\n\n[branch has {commits} commit(s); "
        extra += f"uncommitted files: {', '.join(dirty)}]" if dirty else "worktree clean]"
        return _text(f"{w.kind} worker for #{n} "
                     f"{'FAILED' if res.get('is_error') else 'finished'}:\n"
                     f"{report}{extra}")

    @tool("run_verify", "Run the configured verify command in an issue's "
          "worktree; returns pass/fail with output tail.", {"issue_number": int})
    async def run_verify(args: dict[str, Any]) -> dict[str, Any]:
        n = int(args["issue_number"])
        if not cfg.verify_cmd:
            return _text("no verify command configured; treat as passed")
        wt = gitops.worktree_path(cfg, n)
        if not wt.exists():
            return _text(f"no worktree for #{n}")
        import subprocess
        proc = await anyio.to_thread.run_sync(
            lambda: subprocess.run(
                cfg.verify_cmd, shell=True, cwd=wt,
                capture_output=True, text=True, timeout=1800,
            )
        )
        out = (proc.stdout + proc.stderr)[-4000:]
        verdict = "PASSED" if proc.returncode == 0 else f"FAILED (exit {proc.returncode})"
        return _text(f"verify `{cfg.verify_cmd}` {verdict}\n{out}")

    @tool("spawn_reviewer", "Start a reviewer worker that judges an issue's "
          "diff against the issue. Its report starts with VERDICT: "
          "approve|reject.", {"issue_number": int})
    async def spawn_reviewer(args: dict[str, Any]) -> dict[str, Any]:
        n = int(args["issue_number"])
        issue = state.issue(n)
        diff = gitops.diff_against_base(cfg, n)
        if not diff.strip():
            return _text(f"issue #{n} has an empty diff against "
                         f"{cfg.base_branch}; nothing to review")
        try:
            state.pool.spawn(n, "review", prompts.reviewer_prompt(issue, diff),
                             cwd=gitops.worktree_path(cfg, n))
        except AtCapacity as e:
            return _text(str(e))
        return _text(f"reviewer started for #{n}")

    @tool("rebase", "Rebase an issue's branch onto fresh base. Reports clean "
          "or the list of conflicted files (worktree is left mid-rebase for "
          "the conflict fixer).", {"issue_number": int})
    async def rebase(args: dict[str, Any]) -> dict[str, Any]:
        n = int(args["issue_number"])
        res = await anyio.to_thread.run_sync(lambda: gitops.rebase_onto_base(cfg, n))
        if res.clean:
            return _text(f"rebase of #{n} onto {cfg.base_branch}: clean")
        return _text(f"rebase of #{n} has CONFLICTS in: "
                     f"{', '.join(res.conflict_files) or '(unknown)'}\n{res.detail}")

    @tool("spawn_conflict_fixer", "Start a worker to resolve the mid-rebase "
          "conflicts in an issue's worktree and finish the rebase.",
          {"issue_number": int})
    async def spawn_conflict_fixer(args: dict[str, Any]) -> dict[str, Any]:
        n = int(args["issue_number"])
        issue = state.issue(n)
        conflicts = gitops.dirty_files(cfg, n)
        try:
            state.pool.spawn(n, "fix-conflicts",
                             prompts.conflict_prompt(issue, conflicts),
                             cwd=gitops.worktree_path(cfg, n))
        except AtCapacity as e:
            return _text(str(e))
        return _text(f"conflict fixer started for #{n}")

    @tool("finalize", "Push an issue's branch, open a PR referencing the "
          "issue, comment on the issue, and mark it done. Only call after "
          "verify + review passed on the current state.", {"issue_number": int})
    async def finalize(args: dict[str, Any]) -> dict[str, Any]:
        n = int(args["issue_number"])
        issue = state.issue(n)
        if gitops.commit_count(cfg, n) == 0:
            return _text(f"refusing to finalize #{n}: branch has no commits")
        await anyio.to_thread.run_sync(lambda: gitops.push(cfg, n))
        note = f"pushed {gitops.branch_for(n)}"
        if cfg.open_pr:
            url = github.create_pr(
                cfg,
                head=gitops.branch_for(n),
                title=f"{issue['title']} (#{n})",
                body=f"Closes #{n}.\n\nAutomated change produced by taskloop; "
                     f"verified with `{cfg.verify_cmd or 'no verify cmd'}` and "
                     "an agent review.",
            )
            github.comment(cfg, n, f"taskloop opened {url} for this issue.")
            note = url or note
        state.done[n] = note
        gitops.remove_worktree(cfg, n)
        state.pool.active.pop(n, None)
        return _text(f"finalized #{n}: {note}")

    @tool("give_up", "Abandon an issue: comment the reason on GitHub, clean "
          "up its worktree, mark it failed.", {"issue_number": int, "reason": str})
    async def give_up(args: dict[str, Any]) -> dict[str, Any]:
        n = int(args["issue_number"])
        reason = args.get("reason") or "unspecified"
        try:
            github.comment(cfg, n, f"taskloop gave up on this issue: {reason}")
        except github.GhError as e:
            reason += f" (comment failed: {e})"
        gitops.abort_rebase(cfg, n)
        gitops.remove_worktree(cfg, n, delete_branch=True)
        state.pool.active.pop(n, None)
        state.failed[n] = reason
        return _text(f"gave up on #{n}: {reason}")

    tools = [list_issues, spawn_worker, worker_status, worker_report,
             run_verify, spawn_reviewer, rebase, spawn_conflict_fixer,
             finalize, give_up]
    return create_sdk_mcp_server(name="taskloop", version="1.0.0", tools=tools), [
        f"mcp__taskloop__{t}" for t in [
            "list_issues", "spawn_worker", "worker_status", "worker_report",
            "run_verify", "spawn_reviewer", "rebase", "spawn_conflict_fixer",
            "finalize", "give_up",
        ]
    ]


async def run_loop(cfg: Config) -> LoopState:
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, query

    state = LoopState(cfg=cfg, pool=WorkerPool(cfg))
    server, allowed = build_server(state)
    options = ClaudeAgentOptions(
        system_prompt=prompts.ORCHESTRATOR_SYSTEM,
        mcp_servers={"taskloop": server},
        allowed_tools=allowed,
        max_turns=cfg.orchestrator_max_turns,
        cwd=str(cfg.repo_root),
        **({"model": cfg.model} if cfg.model else {}),
    )
    try:
        async for message in query(prompt=prompts.initial_prompt(cfg.label),
                                   options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        print(f"[orchestrator] {block.text.strip()}", flush=True)
            elif isinstance(message, ResultMessage):
                cost = getattr(message, "total_cost_usd", None)
                if cost is not None:
                    print(f"[taskloop] orchestrator finished (cost ${cost:.2f})",
                          flush=True)
    finally:
        state.pool.terminate_all()
    return state
