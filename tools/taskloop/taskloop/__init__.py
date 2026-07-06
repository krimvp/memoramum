"""taskloop — a thin agentic loop that drains labeled GitHub issues.

An orchestrator agent (Claude Agent SDK) schedules headless Claude Code
workers, one git worktree + branch per issue, with capacity enforced by the
tool layer rather than the model. Incubating inside the memoramum repo; not
part of the memory service itself.
"""

__version__ = "0.1.0"
