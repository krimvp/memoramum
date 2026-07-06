from __future__ import annotations

import subprocess
import time

import pytest

from taskloop import github, gitops, prompts
from taskloop.workers import AtCapacity, WorkerPool


def test_list_open_issues_parses_gh_json(cfg):
    issues = github.list_open_issues(cfg)
    assert issues == [{
        "number": 7, "title": "Add greeting", "body": "say hi",
        "labels": [{"name": "agent-task"}], "url": "https://example.test/7",
    }]


def test_create_pr_returns_url(cfg):
    url = github.create_pr(cfg, head="taskloop/issue-7", title="t", body="b")
    assert url == "https://example.test/pr/1"


def test_worktree_lifecycle(cfg):
    wt = gitops.add_worktree(cfg, 7)
    assert wt.exists()
    assert gitops.add_worktree(cfg, 7) == wt  # idempotent
    assert gitops.commit_count(cfg, 7) == 0

    (wt / "greet.txt").write_text("hi\n")
    subprocess.run(["git", "-C", str(wt), "add", "."], check=True)
    subprocess.run(["git", "-C", str(wt), "commit", "-m", "greet (#7)"],
                   check=True, capture_output=True)
    assert gitops.commit_count(cfg, 7) == 1
    assert "greet.txt" in gitops.diff_against_base(cfg, 7)
    assert gitops.dirty_files(cfg, 7) == []

    gitops.remove_worktree(cfg, 7, delete_branch=True)
    assert not wt.exists()


def test_rebase_reports_conflicts(cfg):
    wt = gitops.add_worktree(cfg, 7)
    # branch edits hello.txt
    (wt / "hello.txt").write_text("branch version\n")
    subprocess.run(["git", "-C", str(wt), "commit", "-am", "branch edit"],
                   check=True, capture_output=True)
    # main edits the same line
    (cfg.repo_root / "hello.txt").write_text("main version\n")
    subprocess.run(["git", "-C", str(cfg.repo_root), "commit", "-am", "main edit"],
                   check=True, capture_output=True)

    res = gitops.rebase_onto_base(cfg, 7)
    assert not res.clean
    assert res.conflict_files == ["hello.txt"]
    gitops.abort_rebase(cfg, 7)

    # a branch touching a different file rebases cleanly over the same main
    wt8 = gitops.add_worktree(cfg, 8)
    (wt8 / "other.txt").write_text("independent\n")
    subprocess.run(["git", "-C", str(wt8), "add", "."], check=True)
    subprocess.run(["git", "-C", str(wt8), "commit", "-m", "independent edit"],
                   check=True, capture_output=True)
    # main advances again underneath branch 8
    (cfg.repo_root / "hello.txt").write_text("main version 2\n")
    subprocess.run(["git", "-C", str(cfg.repo_root), "commit", "-am", "main edit 2"],
                   check=True, capture_output=True)
    assert gitops.rebase_onto_base(cfg, 8).clean
    assert gitops.commit_count(cfg, 8) == 1


def test_pool_enforces_capacity_and_parses_results(cfg, monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "1")
    pool = WorkerPool(cfg)
    w1 = pool.spawn(1, "implement", "p", cwd=tmp_path)
    pool.spawn(2, "implement", "p", cwd=tmp_path)
    with pytest.raises(AtCapacity):
        pool.spawn(3, "implement", "p", cwd=tmp_path)
    with pytest.raises(AtCapacity):  # one live worker per issue
        pool.spawn(1, "review", "p", cwd=tmp_path)

    assert pool.wait_for_change(timeout_s=10, poll_s=0.1)
    deadline = time.monotonic() + 10
    while pool.running_count() and time.monotonic() < deadline:
        time.sleep(0.1)

    res = w1.result()
    assert res["is_error"] is False
    assert res["result"] == "fake worker done"
    assert pool.capacity_left() == cfg.max_parallel


def test_failed_worker_result(cfg, monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "0")
    monkeypatch.setenv("FAKE_CLAUDE_EXIT", "1")
    pool = WorkerPool(cfg)
    w = pool.spawn(9, "implement", "p", cwd=tmp_path)
    w.proc.wait(timeout=10)
    # fake still prints a JSON result; is_error comes from the payload
    assert w.result()["result"] == "fake worker done"
    assert w.snapshot()["state"] == "failed"


def test_prompts_mention_the_contract():
    issue = {"number": 7, "title": "Add greeting", "body": "say hi"}
    wp = prompts.worker_prompt(issue, "make test", feedback="fix X")
    assert "#7" in wp and "make test" in wp and "fix X" in wp
    assert "Do NOT push" in wp
    rp = prompts.reviewer_prompt(issue, "diff-here")
    assert "VERDICT: approve" in rp and "diff-here" in rp
    cp = prompts.conflict_prompt(issue, ["a.py"])
    assert "a.py" in cp and "rebase --continue" in cp
