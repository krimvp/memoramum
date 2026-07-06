"""Headless Claude Code workers.

Each worker is one `claude -p` subprocess running inside an issue's worktree.
The pool enforces max parallelism mechanically — the orchestrator agent asks,
the pool says yes or "at capacity" — so scheduling limits never depend on the
model remembering them.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config

WorkerKind = str  # "implement" | "review" | "fix-conflicts"


@dataclass
class Worker:
    issue_number: int
    kind: WorkerKind
    cwd: Path
    log_path: Path
    proc: subprocess.Popen
    started_at: float = field(default_factory=time.monotonic)

    @property
    def running(self) -> bool:
        return self.proc.poll() is None

    @property
    def exit_code(self) -> int | None:
        return self.proc.poll()

    @property
    def elapsed(self) -> int:
        return int(time.monotonic() - self.started_at)

    def snapshot(self) -> dict:
        return {
            "issue": self.issue_number,
            "kind": self.kind,
            "state": "running" if self.running else ("ok" if self.exit_code == 0 else "failed"),
            "exit_code": self.exit_code,
            "elapsed_s": self.elapsed,
            "log": str(self.log_path),
        }

    def result(self) -> dict:
        """Parse the worker's final JSON (from `--output-format json`).

        Returns {"result": str, "is_error": bool, ...} or a synthetic error
        dict when the output is unparseable.
        """
        if self.running:
            return {"is_error": True, "result": "worker still running"}
        text = self.log_path.read_text(errors="replace") if self.log_path.exists() else ""
        # stdout is a single JSON object; stderr noise may precede it, so scan
        # for the last line that parses as a JSON object.
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
        return {
            "is_error": self.exit_code != 0,
            "result": f"no JSON result in log (exit {self.exit_code}); tail:\n{text[-2000:]}",
        }

    def tail(self, max_chars: int = 4000) -> str:
        if not self.log_path.exists():
            return ""
        return self.log_path.read_text(errors="replace")[-max_chars:]


class AtCapacity(RuntimeError):
    pass


class WorkerPool:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.active: dict[int, Worker] = {}  # one live worker per issue
        self.history: list[Worker] = []

    def running_count(self) -> int:
        return sum(1 for w in self.active.values() if w.running)

    def capacity_left(self) -> int:
        return max(0, self.cfg.max_parallel - self.running_count())

    def worker_for(self, issue_number: int) -> Worker | None:
        return self.active.get(issue_number)

    def _claude_cmd(self, prompt: str) -> list[str]:
        cmd = [self.cfg.claude_bin, "-p", prompt, "--output-format", "json"]
        if self.cfg.skip_permissions:
            cmd.append("--dangerously-skip-permissions")
        else:
            cmd += ["--permission-mode", "acceptEdits"]
        if self.cfg.model:
            cmd += ["--model", self.cfg.model]
        cmd += self.cfg.extra_worker_args
        return cmd

    def spawn(self, issue_number: int, kind: WorkerKind, prompt: str, cwd: Path) -> Worker:
        existing = self.active.get(issue_number)
        if existing and existing.running:
            raise AtCapacity(f"issue #{issue_number} already has a running {existing.kind} worker")
        if self.capacity_left() <= 0:
            raise AtCapacity(
                f"at max parallelism ({self.cfg.max_parallel}); "
                "wait for a worker to finish before spawning another"
            )
        self.cfg.ensure_dirs()
        log_path = self.cfg.logs_dir / f"issue-{issue_number}-{kind}-{int(time.time())}.log"
        log = open(log_path, "w")
        proc = subprocess.Popen(
            self._claude_cmd(prompt),
            cwd=cwd,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )
        worker = Worker(issue_number=issue_number, kind=kind, cwd=cwd,
                        log_path=log_path, proc=proc)
        if existing:
            self.history.append(existing)
        self.active[issue_number] = worker
        return worker

    def status(self) -> list[dict]:
        return [w.snapshot() for w in self.active.values()]

    def wait_for_change(self, timeout_s: int = 300, poll_s: float = 2.0) -> bool:
        """Block until any running worker exits (or timeout). Returns True if
        something finished. Lets the orchestrator sleep instead of busy-poll."""
        running = {n: w for n, w in self.active.items() if w.running}
        if not running:
            return False
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if any(not w.running for w in running.values()):
                return True
            time.sleep(poll_s)
        return False

    def terminate_all(self) -> None:
        for w in self.active.values():
            if w.running:
                w.proc.terminate()
