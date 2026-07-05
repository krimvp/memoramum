"""Learning policy: layered, default-deny, strictest-wins (doc 05 §1–2).

P2 carries the evaluation skeleton and a built-in org layer that enables
exactly the P2 write surface: `explicit_user_ask` writes are allowed,
`llm_inferred` hot-path writes route to the staged tier (ADR-0003),
credentials/secrets are denied at the floor, and everything else is
denied until the phase that gives it semantics (doc 07 §6). The full
engine — surface/agent/user-preference layers loaded from versioned
policy documents, routing, `ask` flows — lands in P3.
"""

from __future__ import annotations

from dataclasses import dataclass, field

VERDICTS = ("allow", "stage", "ask", "deny")
_STRICTNESS = {"deny": 3, "ask": 2, "stage": 1, "allow": 0}


@dataclass(frozen=True)
class Strategy:
    """One row of the strategy allowlist (doc 05 §1). Empty tuple = match any."""

    name: str
    decision: str
    kinds: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    origin_kinds: tuple[str, ...] = ()
    reason: str = ""

    def matches(self, *, kind: str, categories: list[str], origin_kind: str) -> bool:
        if self.kinds and kind not in self.kinds:
            return False
        if self.origin_kinds and origin_kind not in self.origin_kinds:
            return False
        if self.categories and not set(self.categories) & set(categories):
            return False
        return True


@dataclass(frozen=True)
class PolicyLayer:
    name: str                       # 'org' | 'surface' | 'agent' | 'user_pref'
    version: str
    strategies: tuple[Strategy, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Verdict:
    decision: str
    rule_id: str                    # winning '<layer>/<strategy>' id
    reason: str
    layer_verdicts: dict            # per-layer rule id → decision, for the event


P2_ORG_LAYER = PolicyLayer(
    name="org",
    version="p2-builtin-1",
    strategies=(
        Strategy(
            name="secrets",
            decision="deny",
            categories=("credentials", "secrets"),
            reason="credentials and secrets are never memorized (org floor)",
        ),
        Strategy(
            name="explicit-asks",
            decision="allow",
            origin_kinds=("explicit_user_ask",),
            reason="the user said 'remember this' — skips staging (doc 03 §2)",
        ),
        Strategy(
            name="inferred-stage",
            decision="stage",
            origin_kinds=("llm_inferred",),
            reason="agent judgment mid-conversation: useful immediately, trusted later (doc 03 §2)",
        ),
        Strategy(
            name="p2-default",
            decision="deny",
            reason="agent_observed extraction, consolidated and imported writes arrive in P4 (doc 07 §6)",
        ),
    ),
)


def evaluate(
    layers: list[PolicyLayer],
    *,
    kind: str,
    categories: list[str],
    origin_kind: str,
) -> Verdict:
    """First match wins within a layer; layers combine strictest-wins
    (deny > ask > stage > allow). No match anywhere → deny (default deny)."""
    winning: Verdict | None = None
    layer_verdicts: dict = {}
    for layer in layers:
        for strategy in layer.strategies:
            if strategy.matches(kind=kind, categories=categories, origin_kind=origin_kind):
                rule_id = f"{layer.name}/{strategy.name}@{layer.version}"
                layer_verdicts[rule_id] = strategy.decision
                if winning is None or _STRICTNESS[strategy.decision] > _STRICTNESS[winning.decision]:
                    winning = Verdict(strategy.decision, rule_id, strategy.reason, {})
                break
    if winning is None:
        winning = Verdict(
            "deny", "default-deny", "no learning policy matched this write (default deny)", {}
        )
        layer_verdicts["default-deny"] = "deny"
    return Verdict(winning.decision, winning.rule_id, winning.reason, layer_verdicts)
