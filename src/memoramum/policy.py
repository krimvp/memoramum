"""The learning-policy engine: layered, default-deny, strictest-wins (doc 05).

P3 (doc 07 §6) makes this the full engine: policy documents ("YAML in,
canonical JSON out", doc 05 §5) parsed into layers — org > surface >
agent > user-preference — evaluated per write with first-match-wins
inside a layer and strictest-wins across layers (deny > ask > stage >
allow). Strategies are the allowlist: no match anywhere → deny. The
engine also owns routing (policy, not the agent, decides where a write
lands), the promotion gates of doc 05 §3, the read-side attribute rules
of doc 05 §4.2 (sensitivity ceilings, trust floors, category deny-lists),
per-category retention overrides, and simulation support (doc 05 §5).

The org layer falls back to a built-in baseline when no org policy
document is stored: secrets denied at the floor, and every origin kind
carrying its doc 03 §2 entry semantics — `explicit_user_ask` allowed,
`llm_inferred` and `agent_observed` staged (ADR-0003), `consolidated`
allowed (the write path computes the weakest-input entry tier),
`imported` staged. Anything else stays default-deny.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

VERDICTS = ("allow", "stage", "ask", "deny")
_STRICTNESS = {"deny": 3, "ask": 2, "stage": 1, "allow": 0}

LAYERS = ("org", "surface", "agent", "user_pref")
SENSITIVITIES = ("public", "internal", "confidential", "restricted")
_SENSITIVITY_RANK = {s: i for i, s in enumerate(SENSITIVITIES)}
ROUTES = ("source", "subject")
CONFIRMERS = ("flow_user", "scope_member", "subject")


class PolicyError(ValueError):
    pass


def sensitivity_rank(sensitivity: str) -> int:
    if sensitivity not in _SENSITIVITY_RANK:
        raise PolicyError(f"unknown sensitivity {sensitivity!r}")
    return _SENSITIVITY_RANK[sensitivity]


def stricter(a: str, b: str) -> str:
    return a if _STRICTNESS[a] >= _STRICTNESS[b] else b


def parse_ttl(value) -> float | None:
    """'365d' / '12h' / int days → days."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([dh])", str(value).strip())
    if not m:
        raise PolicyError(f"unparseable ttl {value!r} (expected e.g. '90d' or '12h')")
    n = float(m.group(1))
    return n if m.group(2) == "d" else n / 24.0


@dataclass(frozen=True)
class Candidate:
    """The classified write (doc 05 §2's pipeline input): what strategy
    matching is structural over — kind / category / origin / scope-class
    set membership, plus subjects and flow context."""

    kind: str
    categories: tuple[str, ...] = ()
    origin_kind: str = "llm_inferred"
    subjects: tuple[str, ...] = ()
    participants: tuple[str, ...] = ()
    surface: str | None = None
    scope_class: str = "internal_public"   # trust_class of the scope written to


@dataclass(frozen=True)
class Strategy:
    """One row of the strategy allowlist (doc 05 §1). Empty tuple = match any."""

    name: str
    decision: str
    kinds: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    origin_kinds: tuple[str, ...] = ()
    subjects: str | None = None            # 'participants': only about people in the flow
    surfaces: tuple[str, ...] = ()         # user-pref tightening per surface (doc 05 §2)
    scope_classes: tuple[str, ...] = ()    # trust_class set membership (doc 05 §2)
    route_scope: str = "source"            # 'source' (default) | 'subject' (doc 05 §1)
    ttl_days: float | None = None
    sensitivity_floor: str | None = None
    reason: str = ""

    def matches(self, c: Candidate) -> bool:
        if self.kinds and c.kind not in self.kinds:
            return False
        if self.origin_kinds and c.origin_kind not in self.origin_kinds:
            return False
        if self.categories and not set(self.categories) & set(c.categories):
            return False
        if self.subjects == "participants" and (
            not c.subjects or not set(c.subjects) <= set(c.participants)
        ):
            return False
        if self.surfaces and c.surface not in self.surfaces:
            return False
        if self.scope_classes and c.scope_class not in self.scope_classes:
            return False
        return True


@dataclass(frozen=True)
class PolicyLayer:
    name: str                              # 'org' | 'surface' | 'agent' | 'user_pref'
    version: str
    applies_to: str | None = None
    strategies: tuple[Strategy, ...] = field(default_factory=tuple)
    procedural_writes: str | None = None   # 'ask' | 'deny': kind=procedural minimum verdict
    promotion: dict = field(default_factory=dict)
    retention: tuple[dict, ...] = field(default_factory=tuple)
    read: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Verdict:
    decision: str
    rule_id: str                           # winning '<layer>/<strategy>@<version>' id
    reason: str
    layer_verdicts: dict                   # per-layer rule id → decision, for the event
    route_scope: str = "source"            # from the winning strategy
    ttl_days: float | None = None
    sensitivity_floor: str | None = None   # max floor across matched strategies


BUILTIN_ORG_LAYER = PolicyLayer(
    name="org",
    version="builtin-1",
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
            name="observed-stage",
            decision="stage",
            origin_kinds=("agent_observed",),
            reason="background extraction over third-party messages is the main"
                   " poisoning surface — staged by default (doc 03 §2, ADR-0003)",
        ),
        Strategy(
            name="consolidated-procedural",
            decision="ask",
            kinds=("procedural",),
            origin_kinds=("consolidated",),
            reason="reflection-produced procedural candidates default to ask —"
                   " they steer behavior (doc 07 §2)",
        ),
        Strategy(
            name="consolidated",
            decision="allow",
            origin_kinds=("consolidated",),
            reason="consolidator output; the entry tier and trust inherit from"
                   " the weakest input (doc 03 §2, doc 06 §3)",
        ),
        Strategy(
            name="imported-stage",
            decision="stage",
            origin_kinds=("imported",),
            reason="bulk imports are unaudited by definition — staged by default (doc 03 §2)",
        ),
    ),
)


# ---------- document parsing (doc 05 §1 shape; §5: YAML in, canonical JSON out) ----------

_STRATEGY_KEYS = {"name", "kinds", "categories", "origin_kinds", "subjects", "surfaces",
                  "scope_classes", "route", "decision", "ttl", "sensitivity_floor", "reason"}


def parse_document(document) -> dict:
    """Validate a policy document (dict, or a YAML string) and return its
    canonical JSON-shaped form. Raises PolicyError on anything malformed —
    policy is reviewable configuration, not best-effort input."""
    if isinstance(document, str):
        import yaml  # the YAML-in edge of doc 05 §5

        document = yaml.safe_load(document)
    if not isinstance(document, dict):
        raise PolicyError("a policy document must be a mapping")
    doc = dict(document)
    name = doc.get("policy")
    layer = doc.get("layer")
    applies_to = doc.get("applies_to")
    if not name or not isinstance(name, str):
        raise PolicyError("policy documents need a 'policy' name")
    if layer not in LAYERS:
        raise PolicyError(f"unknown layer {layer!r} (expected one of {LAYERS})")
    if not applies_to or not isinstance(applies_to, str):
        raise PolicyError("policy documents need 'applies_to'")

    canonical = {"policy": name, "layer": layer, "applies_to": applies_to}
    learning = doc.get("learning") or {}
    if learning.get("default", "deny") != "deny":
        raise PolicyError("learning.default can only be 'deny' (doc 05 §1: default deny)")
    strategies = []
    for raw in learning.get("strategies") or []:
        unknown = set(raw) - _STRATEGY_KEYS
        if unknown:
            raise PolicyError(f"unknown strategy keys {sorted(unknown)} in {raw.get('name')!r}")
        if raw.get("decision") not in VERDICTS:
            raise PolicyError(f"strategy {raw.get('name')!r} needs a decision in {VERDICTS}")
        route = raw.get("route") or {}
        if isinstance(route, str):
            route = {"scope": route}
        if route.get("scope", "source") not in ROUTES:
            raise PolicyError(f"unknown route {route!r} (expected scope: source|subject)")
        if raw.get("subjects") not in (None, "participants"):
            raise PolicyError("strategy 'subjects' only supports 'participants'")
        floor = raw.get("sensitivity_floor")
        if floor is not None:
            sensitivity_rank(floor)
        parse_ttl(raw.get("ttl"))
        if not raw.get("name"):
            raise PolicyError("every strategy needs a name")
        strategies.append({k: raw[k] for k in _STRATEGY_KEYS if raw.get(k) is not None})
    procedural = learning.get("procedural_writes")
    if procedural not in (None, "ask", "deny"):
        raise PolicyError("procedural_writes can only tighten: 'ask' or 'deny'")
    canonical["learning"] = {"default": "deny", "strategies": strategies}
    if procedural:
        canonical["learning"]["procedural_writes"] = procedural

    promotion = doc.get("promotion") or {}
    for key, allowed in (("to_shared_scope", ("ask", "deny")),
                         ("to_subject_scope", ("ask", "deny")),
                         ("from_private_scope", ("deny",))):
        if key in promotion and promotion[key] not in allowed:
            raise PolicyError(f"promotion.{key} can only tighten the default: {allowed}")
    confirmers = promotion.get("confirmers", [])
    if not set(confirmers) <= {"scope_member", "subject"}:
        raise PolicyError("promotion.confirmers supports 'scope_member' and 'subject'")
    if promotion:
        canonical["promotion"] = promotion

    retention = (doc.get("retention") or {}).get("overrides") or []
    for o in retention:
        if not o.get("categories"):
            raise PolicyError("retention overrides match on categories")
        parse_ttl(o.get("ttl"))
        if o.get("expiry_mode", "archive") not in ("archive", "tombstone"):
            raise PolicyError("expiry_mode is 'archive' or 'tombstone'")
    if retention:
        canonical["retention"] = {"overrides": retention}

    read = doc.get("read") or {}
    if "sensitivity_ceiling" in read:
        sensitivity_rank(read["sensitivity_ceiling"])
    if not set(read) <= {"include_staged", "trust_floor", "sensitivity_ceiling", "deny_categories"}:
        raise PolicyError(f"unknown read-policy keys {sorted(set(read) - {'include_staged', 'trust_floor', 'sensitivity_ceiling', 'deny_categories'})}")
    if read:
        canonical["read"] = read
    return canonical


def layer_from_document(doc: dict, version: int | str) -> PolicyLayer:
    strategies = tuple(
        Strategy(
            name=s["name"],
            decision=s["decision"],
            kinds=tuple(s.get("kinds") or ()),
            categories=tuple(s.get("categories") or ()),
            origin_kinds=tuple(s.get("origin_kinds") or ()),
            subjects=s.get("subjects"),
            surfaces=tuple(s.get("surfaces") or ()),
            scope_classes=tuple(s.get("scope_classes") or ()),
            route_scope=(s.get("route") or {}).get("scope", "source")
            if isinstance(s.get("route"), dict) else (s.get("route") or "source"),
            ttl_days=parse_ttl(s.get("ttl")),
            sensitivity_floor=s.get("sensitivity_floor"),
            reason=s.get("reason", ""),
        )
        for s in doc["learning"]["strategies"]
    )
    return PolicyLayer(
        name=doc["layer"],
        version=f"{doc['policy']}@v{version}",
        applies_to=doc["applies_to"],
        strategies=strategies,
        procedural_writes=doc["learning"].get("procedural_writes"),
        promotion=doc.get("promotion") or {},
        retention=tuple((doc.get("retention") or {}).get("overrides") or ()),
        read=doc.get("read") or {},
    )


# ---------- write evaluation (doc 05 §2) ----------

def evaluate(layers: list[PolicyLayer], candidate: Candidate) -> Verdict:
    """First match wins within a layer; layers combine strictest-wins
    (deny > ask > stage > allow). No match anywhere → deny (default deny).
    A layer that matches nothing contributes nothing — the per-document
    `default: deny` is the global default restated, not a per-layer veto.

    Routing is its own axis (doc 05 §1: "route decouples what the agent
    asked from where it lands", and the §6 worked row where an
    `explicit-asks` allow still routes to the subject scope): the first
    *matching* strategy with a non-source route supplies the route (and
    its TTL, when the deciding strategy has none), even when a different
    strategy supplied the decision. Sensitivity floors aggregate to the
    strongest across matched strategies."""
    winning: Verdict | None = None
    layer_verdicts: dict = {}
    floors: list[str] = []
    ttls: list[float] = []
    route_scope = "source"
    for layer in layers:
        deciding = None
        for strategy in layer.strategies:
            if not strategy.matches(candidate):
                continue
            if deciding is None:
                deciding = strategy
            if strategy.sensitivity_floor:
                floors.append(strategy.sensitivity_floor)
            if strategy.route_scope != "source":
                if route_scope == "source":
                    route_scope = strategy.route_scope
                if strategy.ttl_days is not None:
                    ttls.append(strategy.ttl_days)
        if deciding is None:
            continue
        if deciding.ttl_days is not None:
            ttls.append(deciding.ttl_days)  # a shorter TTL is a tightening
        decision = deciding.decision
        rule_id = f"{layer.name}/{deciding.name}@{layer.version}"
        reason = deciding.reason
        if layer.procedural_writes and candidate.kind == "procedural":
            tightened = stricter(decision, layer.procedural_writes)
            if tightened != decision:
                decision = tightened
                rule_id = f"{layer.name}/procedural-writes@{layer.version}"
                reason = "kind=procedural always asks (it steers behavior, doc 05 §1)"
        layer_verdicts[rule_id] = decision
        if winning is None or _STRICTNESS[decision] > _STRICTNESS[winning.decision]:
            winning = Verdict(decision, rule_id, reason, {})
    if winning is None:
        winning = Verdict(
            "deny", "default-deny", "no learning policy matched this write (default deny)", {}
        )
        layer_verdicts["default-deny"] = "deny"
    floor = max(floors, key=sensitivity_rank) if floors else None
    return Verdict(winning.decision, winning.rule_id, winning.reason, layer_verdicts,
                   route_scope=route_scope, ttl_days=min(ttls) if ttls else None,
                   sensitivity_floor=floor)


# ---------- promotion gates (doc 05 §3) ----------

@dataclass(frozen=True)
class PromotionGate:
    decision: str
    rule_id: str
    reason: str
    confirmer: str | None = None       # who may confirm an 'ask'


def promotion_gate(
    layers: list[PolicyLayer], *, crossing: str, source_trust_class: str,
) -> PromotionGate:
    """The doc 05 §3 defaults, tightened (never loosened) by the layers'
    `promotion:` sections. `crossing` is 'narrowing' (target is a
    descendant of the source), 'subject' (target is a subject scope) or
    'shared' (any other move — up the tree or sideways)."""
    if source_trust_class == "private":
        return PromotionGate(
            "deny", "org/promotion-private-floor",
            "nothing leaves a private scope automatically (org floor, doc 05 §3)",
        )
    if source_trust_class == "shared_external":
        return PromotionGate(
            "deny", "org/promotion-shared-external",
            "promotions out of shared_external scopes are denied by default (doc 05 §3)",
        )
    if crossing == "narrowing":
        return PromotionGate(
            "allow", "org/promotion-narrowing",
            "broader → narrower needs no ceremony (doc 01 §3.2)",
        )
    key = "to_subject_scope" if crossing == "subject" else "to_shared_scope"
    decision, rule_id = "ask", f"org/promotion-{crossing}-default"
    reason = (
        "container → subject crossings ask the subject (doc 05 §3)"
        if crossing == "subject"
        else "crossing into a shared scope asks a member of the source scope (doc 05 §3)"
    )
    for layer in layers:
        tightened = stricter(decision, layer.promotion.get(key, decision))
        if tightened != decision:
            decision, rule_id = tightened, f"{layer.name}/promotion-{key}@{layer.version}"
            reason = f"promotion.{key} tightened to {tightened} by the {layer.name} layer"
    confirmer = "subject" if crossing == "subject" else "scope_member"
    return PromotionGate(decision, rule_id, reason, confirmer if decision == "ask" else None)


# ---------- read policy (doc 05 §1 `read:`, §4.2 attribute rules) ----------

@dataclass(frozen=True)
class ReadPolicy:
    include_staged: bool = True
    trust_floor: float | None = None            # None → the settings default
    sensitivity_ceiling: str = "restricted"     # no ceiling
    deny_categories: tuple[str, ...] = ()


def read_policy(layers: list[PolicyLayer]) -> ReadPolicy:
    """Merge the layers' `read:` sections, strictest-wins: staged excluded
    if any layer excludes it, the highest trust floor, the lowest
    sensitivity ceiling, the union of category deny-lists."""
    include_staged = True
    floor: float | None = None
    ceiling = "restricted"
    deny: list[str] = []
    for layer in layers:
        r = layer.read
        if r.get("include_staged") is False:
            include_staged = False
        if r.get("trust_floor") is not None:
            floor = r["trust_floor"] if floor is None else max(floor, r["trust_floor"])
        if r.get("sensitivity_ceiling") is not None:
            if sensitivity_rank(r["sensitivity_ceiling"]) < sensitivity_rank(ceiling):
                ceiling = r["sensitivity_ceiling"]
        deny += [c for c in r.get("deny_categories", ()) if c not in deny]
    return ReadPolicy(include_staged, floor, ceiling, tuple(deny))


# ---------- retention overrides (doc 05 §1 `retention:`) ----------

def retention_override(layers: list[PolicyLayer], categories: list[str]) -> dict | None:
    """The strictest per-category override: the shortest TTL, and
    tombstone beats archive (doc 03 §5: categories that demand hard
    deletion at expiry)."""
    ttl: float | None = None
    mode = "archive"
    matched = False
    for layer in layers:
        for override in layer.retention:
            if not set(override["categories"]) & set(categories):
                continue
            matched = True
            o_ttl = parse_ttl(override.get("ttl"))
            if o_ttl is not None and (ttl is None or o_ttl < ttl):
                ttl = o_ttl
            if override.get("expiry_mode") == "tombstone":
                mode = "tombstone"
    return {"ttl_days": ttl, "expiry_mode": mode} if matched else None
