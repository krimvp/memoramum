"""Principals and the flow context (doc 01 §4, doc 04 preamble).

Every call is authenticated as a principal pair (agent, optional
on_behalf_of user) and carries a flow context from which the service —
never the agent — resolves the scope chain.
"""

from __future__ import annotations

from dataclasses import dataclass, field

PRINCIPAL_KINDS = ("user", "agent", "system")


class PrincipalError(ValueError):
    pass


def validate_principal(principal: str) -> str:
    kind, _, rest = principal.partition(":")
    if kind not in PRINCIPAL_KINDS or not rest:
        raise PrincipalError(
            f"invalid principal {principal!r}: expected 'user:<id>', 'agent:<id>' or 'system:<id>'"
        )
    return principal


@dataclass(frozen=True)
class Principal:
    """The acting pair. `actor` is who acts (agent:sage, user:dana,
    system:consolidator); `on_behalf_of` is the user an agent acts for —
    access checks apply the intersection of both (doc 05 §4.1)."""

    actor: str
    on_behalf_of: str | None = None

    def __post_init__(self) -> None:
        validate_principal(self.actor)
        if self.on_behalf_of is not None:
            validate_principal(self.on_behalf_of)
            if not self.on_behalf_of.startswith("user:"):
                raise PrincipalError("on_behalf_of must be a user principal")

    @property
    def kind(self) -> str:
        return self.actor.partition(":")[0]

    @property
    def effective_user(self) -> str | None:
        """The human whose visibility bounds a read: the on-behalf-of user,
        or the actor itself when a user acts directly."""
        if self.on_behalf_of:
            return self.on_behalf_of
        return self.actor if self.kind == "user" else None


@dataclass(frozen=True)
class Flow:
    """Where the call is happening. Agents describe where they are; the
    service decides what that makes visible (doc 04)."""

    surface: str | None = None            # 'slack', 'gitlab', 'ide'
    container: str | None = None          # scope id of the innermost container, e.g. 'thread/…' or 'channel/C0DEP'
    participants: tuple[str, ...] = field(default_factory=tuple)
    session_id: str | None = None
    project: str | None = None            # project container scope the session works against — dev-time flows (container is a 'devsession', not an MR) name it explicitly (ADR-0012)
    touched_paths: tuple[str, ...] = field(default_factory=tuple)   # MR diff paths / dev-session touched files, mapped onto module scopes (ADR-0010)
