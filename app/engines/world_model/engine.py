"""Meterless World Model engine — "keep parallel agents in sync".

One shared state that every agent reads and writes: zones, shelters, supplies,
beliefs with provenance, and decisions with dependencies. Two guarantees the
rest of Pulsar 5 leans on:

1. **Concurrent writes do not lose each other.** Writes are per-key upserts
   under a re-entrant lock, so an Analyst writing shelter facts and a Belief
   Reviser writing advisory facts both survive. Same-key collisions keep the
   newer `as_of` and record the loser in `conflicts`, instead of silently
   dropping one.
2. **Nothing is stated without provenance.** A belief carries the Zetaris table
   and query that produced it plus an `as_of` timestamp, which is what lets the
   Critic flag stale facts and the Planner cite evidence ids.

Decisions declare which belief ids they depend on. That graph is what makes a
targeted replan possible: when a belief changes, only the decisions downstream
of it are invalidated.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from typing import Any, Iterable

from app.schemas import utc_now_iso

ENGINE_NAME = "world_model"


@dataclass(frozen=True)
class Provenance:
    source_table: str
    query: str | None = None
    as_of: str = field(default_factory=utc_now_iso)
    retrieved_at: str = field(default_factory=utc_now_iso)
    data_source: str = "zetaris"  # "zetaris" | "sample_cache" | "event"

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_table": self.source_table,
            "query": self.query,
            "as_of": self.as_of,
            "retrieved_at": self.retrieved_at,
            "data_source": self.data_source,
        }


@dataclass(frozen=True)
class Belief:
    """One fact about the world, addressable by id and always sourced."""

    fact_id: str
    subject: str
    predicate: str
    value: Any
    provenance: Provenance
    version: int = 1
    written_by: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "subject": self.subject,
            "predicate": self.predicate,
            "value": self.value,
            "version": self.version,
            "written_by": self.written_by,
            "provenance": self.provenance.to_dict(),
        }


@dataclass(frozen=True)
class Decision:
    decision_id: str
    kind: str
    target: str
    detail: str
    depends_on: tuple[str, ...] = ()
    status: str = "active"  # active | invalidated | committed | escalated
    made_by: str = "Planner"
    confidence: float = 0.5
    created_at: str = field(default_factory=utc_now_iso)
    invalidated_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "kind": self.kind,
            "target": self.target,
            "detail": self.detail,
            "depends_on": list(self.depends_on),
            "status": self.status,
            "made_by": self.made_by,
            "confidence": self.confidence,
            "created_at": self.created_at,
            "invalidated_reason": self.invalidated_reason,
        }


@dataclass
class BeliefDelta:
    fact_id: str
    field_name: str
    before: Any
    after: Any

    def to_dict(self) -> dict[str, Any]:
        return {"fact_id": self.fact_id, "field": self.field_name, "before": self.before, "after": self.after}


class WorldModel:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._beliefs: dict[str, Belief] = {}
        self._entities: dict[str, dict[str, Any]] = {}
        self._relations: list[dict[str, str]] = []
        self._decisions: dict[str, Decision] = {}
        self._conflicts: list[dict[str, Any]] = []
        self._revision = 0
        self._writes = 0

    # --------------------------------------------------------------- entities

    def put_entity(self, entity_id: str, kind: str, **attrs: Any) -> dict[str, Any]:
        with self._lock:
            existing = self._entities.get(entity_id, {})
            merged = {**existing, **attrs, "entity_id": entity_id, "kind": kind}
            self._entities[entity_id] = merged
            self._revision += 1
            self._writes += 1
            return dict(merged)

    def entities(self, kind: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            values = list(self._entities.values())
        return [dict(e) for e in values if kind is None or e.get("kind") == kind]

    def entity(self, entity_id: str) -> dict[str, Any] | None:
        with self._lock:
            found = self._entities.get(entity_id)
            return dict(found) if found else None

    def relate(self, source: str, relation: str, target: str) -> None:
        with self._lock:
            edge = {"source": source, "relation": relation, "target": target}
            if edge not in self._relations:
                self._relations.append(edge)
                self._revision += 1

    def related(self, source: str, relation: str | None = None) -> list[str]:
        with self._lock:
            return [
                e["target"]
                for e in self._relations
                if e["source"] == source and (relation is None or e["relation"] == relation)
            ]

    # ---------------------------------------------------------------- beliefs

    def assert_belief(
        self,
        fact_id: str,
        subject: str,
        predicate: str,
        value: Any,
        provenance: Provenance,
        *,
        written_by: str = "unknown",
    ) -> tuple[Belief, list[BeliefDelta]]:
        """Upsert one belief. Returns the stored belief and what changed.

        A same-key write with an older `as_of` than the stored belief is kept as
        a conflict and does not overwrite — late-arriving stale data must not
        clobber fresher facts.
        """
        with self._lock:
            previous = self._beliefs.get(fact_id)
            self._writes += 1

            if previous is not None and provenance.as_of < previous.provenance.as_of:
                self._conflicts.append(
                    {
                        "fact_id": fact_id,
                        "kept_as_of": previous.provenance.as_of,
                        "rejected_as_of": provenance.as_of,
                        "rejected_value": value,
                        "rejected_by": written_by,
                        "reason": "stale_write",
                    }
                )
                return previous, []

            deltas: list[BeliefDelta] = []
            if previous is None:
                belief = Belief(fact_id, subject, predicate, value, provenance, 1, written_by)
                deltas.append(BeliefDelta(fact_id, "value", None, value))
            else:
                if previous.value != value:
                    deltas.append(BeliefDelta(fact_id, "value", previous.value, value))
                if previous.provenance.as_of != provenance.as_of:
                    deltas.append(
                        BeliefDelta(fact_id, "as_of", previous.provenance.as_of, provenance.as_of)
                    )
                belief = replace(
                    previous,
                    value=value,
                    provenance=provenance,
                    version=previous.version + 1,
                    written_by=written_by,
                )

            self._beliefs[fact_id] = belief
            self._revision += 1
            return belief, deltas

    def assert_many(
        self, items: Iterable[dict[str, Any]], *, written_by: str = "unknown"
    ) -> tuple[list[Belief], list[BeliefDelta]]:
        beliefs: list[Belief] = []
        deltas: list[BeliefDelta] = []
        for item in items:
            belief, item_deltas = self.assert_belief(
                item["fact_id"],
                item["subject"],
                item["predicate"],
                item["value"],
                item["provenance"],
                written_by=item.get("written_by", written_by),
            )
            beliefs.append(belief)
            deltas.extend(item_deltas)
        return beliefs, deltas

    def belief(self, fact_id: str) -> Belief | None:
        with self._lock:
            return self._beliefs.get(fact_id)

    def beliefs(self, *, subject: str | None = None, predicate: str | None = None) -> list[Belief]:
        with self._lock:
            values = list(self._beliefs.values())
        return [
            b
            for b in values
            if (subject is None or b.subject == subject) and (predicate is None or b.predicate == predicate)
        ]

    def belief_value(self, fact_id: str, default: Any = None) -> Any:
        belief = self.belief(fact_id)
        return belief.value if belief else default

    def stale_beliefs(self, cutoff_as_of: str) -> list[Belief]:
        """Beliefs older than the newest advisory — what the Critic flags."""
        return [b for b in self.beliefs() if b.provenance.as_of < cutoff_as_of]

    # -------------------------------------------------------------- decisions

    def record_decision(
        self,
        decision_id: str,
        kind: str,
        target: str,
        detail: str,
        *,
        depends_on: Iterable[str] = (),
        made_by: str = "Planner",
        confidence: float = 0.5,
    ) -> Decision:
        with self._lock:
            decision = Decision(
                decision_id=decision_id,
                kind=kind,
                target=target,
                detail=detail,
                depends_on=tuple(depends_on),
                made_by=made_by,
                confidence=confidence,
            )
            self._decisions[decision_id] = decision
            self._revision += 1
            self._writes += 1
            return decision

    def decisions(self, *, status: str | None = None) -> list[Decision]:
        with self._lock:
            values = list(self._decisions.values())
        return [d for d in values if status is None or d.status == status]

    def depends_on_any(self, fact_ids: Iterable[str]) -> list[Decision]:
        """Active decisions that cite at least one of these belief ids."""
        wanted = set(fact_ids)
        return [d for d in self.decisions(status="active") if wanted & set(d.depends_on)]

    def invalidate(self, decision_ids: Iterable[str], reason: str) -> list[Decision]:
        changed: list[Decision] = []
        with self._lock:
            for decision_id in decision_ids:
                existing = self._decisions.get(decision_id)
                if existing is None or existing.status == "invalidated":
                    continue
                updated = replace(existing, status="invalidated", invalidated_reason=reason)
                self._decisions[decision_id] = updated
                self._revision += 1
                changed.append(updated)
        return changed

    def set_decision_status(self, decision_id: str, status: str) -> Decision | None:
        with self._lock:
            existing = self._decisions.get(decision_id)
            if existing is None:
                return None
            updated = replace(existing, status=status)
            self._decisions[decision_id] = updated
            self._revision += 1
            return updated

    # ------------------------------------------------------------------ state

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    @property
    def write_count(self) -> int:
        with self._lock:
            return self._writes

    def conflicts(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(c) for c in self._conflicts]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "revision": self._revision,
                "entities": {k: dict(v) for k, v in self._entities.items()},
                "relations": [dict(r) for r in self._relations],
                "beliefs": {k: v.to_dict() for k, v in self._beliefs.items()},
                "decisions": {k: v.to_dict() for k, v in self._decisions.items()},
                "conflicts": [dict(c) for c in self._conflicts],
            }

    def digest(self) -> dict[str, int]:
        """Small fixed-size summary — this is what Markovian carries between steps."""
        with self._lock:
            return {
                "revision": self._revision,
                "entities": len(self._entities),
                "beliefs": len(self._beliefs),
                "active_decisions": sum(1 for d in self._decisions.values() if d.status == "active"),
                "invalidated_decisions": sum(1 for d in self._decisions.values() if d.status == "invalidated"),
                "conflicts": len(self._conflicts),
            }
