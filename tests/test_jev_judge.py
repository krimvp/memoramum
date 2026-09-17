"""The System One judge (ADR-0019) behind `make_judge("jev")`: the one
choice question, the confidence gate, and fail-closed on every fault.
Unit tests fake the transport (urllib.request.urlopen); no database. The
live test runs only with MEMORAMUM_LIVE=1 and TYPESAFE_API_KEY set."""

from __future__ import annotations

import io
import json
import logging
import os
import urllib.error

import pytest

from memoramum import lifecycle
from memoramum.lifecycle import JEV_CONFIDENCE_GATE, JevJudge, make_judge

OLD = "dana prefers dark mode in the IDE"
NEW = "dana prefers light mode in the IDE"


def _answer(choice: str, confidence: float, probs: dict | None = None) -> dict:
    probs = probs or {k: (confidence if k == choice else (1 - confidence) / 2)
                      for k in ("duplicate", "contradiction", "unrelated")}
    return {"model": "jev-1.13.0",
            "answers": {"relation": {"type": "choice", "choice": choice,
                                     "confidence": confidence, "probabilities": probs}},
            "usage": {"input_tokens": 120, "output_tokens": 9}}


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _fake_urlopen(monkeypatch, payload, seen: list | None = None):
    def urlopen(req, timeout=None):
        if seen is not None:
            seen.append((req, timeout))
        if isinstance(payload, Exception):
            raise payload
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return _Resp(raw)
    monkeypatch.setattr(lifecycle.urllib.request, "urlopen", urlopen)


@pytest.fixture
def judge(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    j = make_judge("jev")
    assert isinstance(j, JevJudge)
    return j


def test_contradiction_above_gate(judge, monkeypatch):
    seen: list = []
    _fake_urlopen(monkeypatch, _answer("contradiction", 0.91), seen)
    assert judge.judge(NEW, OLD) == "contradiction"
    ev = judge.last_evidence
    assert ev["choice"] == "contradiction" and ev["confidence"] == 0.91
    assert set(ev["probabilities"]) == {"duplicate", "contradiction", "unrelated"}
    assert ev["model"] == "jev-1.13.0" and ev["usage"]["input_tokens"] == 120

    req, timeout = seen[0]
    assert timeout == lifecycle.JEV_TIMEOUT_SECONDS
    assert req.get_header("Authorization") == "Bearer test-key"
    body = json.loads(req.data)
    assert body["model"] == lifecycle.JEV_MODEL
    assert body["state"] == {"old_memory": OLD, "new_memory": NEW}
    q = body["questions"]["relation"]
    assert q["type"] == "choice" and set(q["criteria"]) == set(lifecycle.JEV_CRITERIA)


def test_contradiction_below_gate_is_unrelated(judge, monkeypatch):
    _fake_urlopen(monkeypatch, _answer("contradiction", JEV_CONFIDENCE_GATE - 0.01))
    assert judge.judge(NEW, OLD) == "unrelated"
    # The evidence still shows what the model said and why it did not count.
    assert judge.last_evidence["choice"] == "contradiction"
    assert judge.last_evidence["confidence"] < JEV_CONFIDENCE_GATE


def test_duplicate(judge, monkeypatch):
    _fake_urlopen(monkeypatch, _answer("duplicate", 0.88))
    assert judge.judge("dana likes the IDE in dark mode", OLD) == "duplicate"


@pytest.mark.parametrize("fault", [
    urllib.error.HTTPError("u", 429, "back off", {}, io.BytesIO(b"")),
    urllib.error.URLError("dns"),
    TimeoutError("timed out"),
    b"not json",
    {"model": "jev-1.13.0", "answers": {}, "usage": {}},                  # no answer
    _answer("maybe", 0.95),                                               # unknown choice
    _answer("contradiction", 1.5),                                        # bad confidence
    _answer("contradiction", 0.9, {"contradiction": 0.9}),                # partial probs
    {k: v for k, v in _answer("contradiction", 0.9).items() if k != "model"},
])
def test_fault_is_unrelated_with_warning(judge, monkeypatch, caplog, fault):
    _fake_urlopen(monkeypatch, fault)
    with caplog.at_level(logging.WARNING, logger="memoramum.lifecycle"):
        assert judge.judge(NEW, OLD) == "unrelated"
    assert any("jev judge unavailable" in r.message for r in caplog.records)
    assert judge.last_evidence is None


def test_missing_key_fails_at_construction(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="TYPESAFE_API_KEY"):
        make_judge("jev")


@pytest.mark.skipif(
    os.environ.get("MEMORAMUM_LIVE") != "1" or not os.environ.get("TYPESAFE_API_KEY"),
    reason="live System One call: set MEMORAMUM_LIVE=1 and TYPESAFE_API_KEY",
)
def test_live_contradiction():
    judge = make_judge("jev")
    assert judge.judge(NEW, OLD) == "contradiction"
    ev = judge.last_evidence
    assert ev["confidence"] >= JEV_CONFIDENCE_GATE and ev["model"].startswith("jev")
    assert judge.judge("dana's team ships on Fridays", OLD) == "unrelated"
