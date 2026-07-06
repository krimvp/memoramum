# taskloop

A tiny agentic loop that drains GitHub issues carrying a label: it picks them
up, works on them in parallel, verifies the results, resolves conflicts, and
delivers each one as a pull request.

> Incubating inside the memoramum repo under `tools/`; deliberately
> self-contained (own `pyproject.toml`, no imports from `memoramum`) so it can
> be extracted into its own repository later. It is **not** part of the
> memory-service design surface described in the repo's docs.

## Design

**Thin agentic orchestrator, fat workers.** One Claude Agent SDK session is
the scheduler. It has *no* Bash/Edit/file tools — its entire world is ten
scheduling tools, so it can't drift into doing the work itself, and it keeps
the whole board (every issue, every worker, capacity) in one context. All
mechanics are deterministic Python:

| concern | enforced by |
|---|---|
| max parallelism | `WorkerPool.spawn` refuses over capacity — never model memory |
| isolation | one git worktree + `taskloop/issue-N` branch per issue |
| work | headless `claude -p` subprocess per issue, log per run |
| verification | your `--verify-cmd` (e.g. `make test`) **and** a reviewer agent (`VERDICT: approve|reject`) |
| conflicts | orchestrator rebases; if dirty, a conflict-fixer agent finishes the rebase |
| delivery | push + PR per issue (`gh pr create`), comment on the issue |
| retries | bounded per issue (`--max-attempts`), then `give_up` with a comment |

Per-issue pipeline: `spawn_worker → run_verify → spawn_reviewer → rebase
(→ spawn_conflict_fixer → run_verify) → finalize`.

GitHub is the queue (no state files); worker logs land in
`<workdir>/logs/`, worktrees in `<workdir>/worktrees/` (default
`<repo>/.taskloop/` — add it to `.gitignore`).

## Requirements

- `git`, an authenticated `gh` CLI, and a logged-in `claude` CLI on PATH
- `pip install -e 'tools/taskloop[dev]'` (deps: `claude-agent-sdk`, `anyio`)

## Usage

```sh
taskloop --label agent-task --verify-cmd "make test" --max-parallel 3
```

Common flags:

```
--label LABEL          issue label to drain (required)
--repo-root PATH       repo to operate on (default: cwd)
--base BRANCH          base branch (default: auto-detect origin/HEAD)
--max-parallel N       concurrent workers (default: 2)
--verify-cmd CMD       must pass in each worktree before delivery
--max-attempts N       implementation retries per issue (default: 2)
--model NAME           model for orchestrator + workers
--no-pr                push branches without opening PRs
--no-skip-permissions  workers use acceptEdits instead of skipping permissions
--dry-run              print the matching issues and plan, then exit
```

⚠️ By default workers run `claude -p --dangerously-skip-permissions`
inside their worktree — the worktree is the isolation boundary. Use
`--no-skip-permissions` if you want workers gated to `acceptEdits`.

## Testing

`pytest` from `tools/taskloop/`. The tests stub `gh` and `claude` with fake
executables and run the git plumbing against a throwaway repo, so no network,
credentials, or API tokens are needed.
