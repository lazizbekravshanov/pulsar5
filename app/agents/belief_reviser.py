"""Belief Reviser (Deep lane) — the change-of-mind machinery.

When the world changes mid-run (a new advisory shifts the track, a shelter
fills, a ramp closes) the wrong move is to replan from scratch — that throws
away sound decisions and makes the system look arbitrary. The right move is:

  1. write the event's facts into the World Model and capture the deltas,
  2. walk the decision dependency graph to find the decisions that actually
     cited a changed belief,
  3. invalidate exactly those and leave the rest standing,
  4. hand the Planner a targeted replan.

Step 2 is why every action carries `evidence_ids`. The Deep lane is asked to
explain the revision for the operator; it never decides what gets invalidated.
"""

from __future__ import annotations

import json
from typing import Any

from app.agents.base import Agent
from app.engines.world_model.engine import BeliefDelta, Provenance
from app.schemas import BeliefChange, Event, Plan, PlanDiff, utc_now_iso

_SYSTEM = """You explain to an emergency manager why a plan changed.
You are given the facts that changed and the decisions that depended on them.
Two sentences, plain words, no new facts. Reply with JSON only:
{"explanation": "two sentences"}"""


class BeliefReviser(Agent):
    name = "BeliefReviser"
    role = "Diffs the World Model on new events, invalidates only dependent decisions, triggers a targeted replan"
    lane = "deep"

    def apply_event(self, event: Event) -> dict[str, Any]:
        handler = {
            "advisory_update": self._advisory_update,
            "shelter_capacity": self._shelter_capacity,
            "road_closure": self._road_closure,
        }.get(event.type)
        if handler is None:
            raise ValueError(f"unsupported event type {event.type!r}")

        before_revision = self.world.revision
        # A belief re-asserted with a newer as_of and the same value was
        # confirmed, not changed; only a changed value can unsettle a decision.
        deltas = [d for d in handler(event.payload) if d.field_name == "value"]
        changed_ids = sorted({d.fact_id for d in deltas})

        dependents = self.world.depends_on_any(changed_ids)
        dependent_ids = [d.decision_id for d in dependents]
        preserved = [d.decision_id for d in self.world.live_decisions() if d.decision_id not in dependent_ids]

        invalidated = self.world.invalidate(
            dependent_ids, reason=f"{event.type}: depended on {', '.join(changed_ids[:4])}"
        )

        self.log(
            "revise_beliefs",
            engine="world_model",
            input_summary=f"{event.type} payload keys: {sorted(event.payload)[:6]}",
            output_summary=(
                f"{len(changed_ids)} beliefs changed, {len(invalidated)} decisions invalidated, "
                f"{len(preserved)} preserved (world revision {before_revision} -> {self.world.revision})"
            ),
            target_agent="Planner" if invalidated else None,
            extra={
                "changed_fact_ids": changed_ids,
                "invalidated_decisions": [d.decision_id for d in invalidated],
                "preserved_decisions": preserved,
            },
        )

        explanation = self._explain(event, deltas, [d.decision_id for d in invalidated])

        return {
            "changed_beliefs": [
                BeliefChange(fact_id=d.fact_id, field=d.field_name, before=d.before, after=d.after)
                for d in deltas
            ],
            "invalidated_decisions": [d.decision_id for d in invalidated],
            "preserved_decisions": preserved,
            "explanation": explanation,
        }

    # ------------------------------------------------------------------ events

    def _provenance(self, source_table: str, as_of: str | None) -> Provenance:
        stamp = as_of or utc_now_iso()
        return Provenance(
            source_table=source_table,
            query=None,
            as_of=stamp,
            retrieved_at=utc_now_iso(),
            data_source="event",
        )

    def _advisory_update(self, payload: dict[str, Any]) -> list[BeliefDelta]:
        as_of = str(payload.get("issued_at") or utc_now_iso())
        provenance = self._provenance("storm_advisories", as_of)
        cone = payload.get("cone_polygon")
        if isinstance(cone, str):
            try:
                cone = json.loads(cone)
            except json.JSONDecodeError:
                cone = None

        updates = [
            ("hazard:advisory_number", "hazard", "advisory_number", payload.get("advisory_number")),
            ("hazard:issued_at", "hazard", "issued_at", as_of),
            (
                "hazard:center",
                "hazard",
                "center",
                {"lat": payload.get("center_lat"), "lon": payload.get("center_lon")},
            ),
            ("hazard:max_wind_kt", "hazard", "max_wind_kt", payload.get("max_wind_kt")),
            (
                "hazard:forecast_landfall_county",
                "hazard",
                "forecast_landfall_county",
                payload.get("forecast_landfall_county"),
            ),
        ]
        if cone is not None:
            updates.append(("hazard:cone_polygon", "hazard", "cone_polygon", cone))

        deltas: list[BeliefDelta] = []
        for fact_id, subject, predicate, value in updates:
            if value is None:
                continue
            _, item_deltas = self.world.assert_belief(
                fact_id, subject, predicate, value, provenance, written_by=self.name
            )
            deltas.extend(item_deltas)

        # A track shift changes which zones sit in the cone, so zone exposure
        # beliefs are re-derived rather than left to drift.
        if cone is not None:
            deltas.extend(self._reassess_zone_exposure(cone, provenance))
        return deltas

    def _reassess_zone_exposure(self, cone: Any, provenance: Provenance) -> list[BeliefDelta]:
        from app.sim.exposure import derive_zone_exposure

        return derive_zone_exposure(self.world, cone, provenance, written_by=self.name)

    def _shelter_capacity(self, payload: dict[str, Any]) -> list[BeliefDelta]:
        shelter_id = str(payload.get("shelter_id"))
        provenance = self._provenance("shelters", str(payload.get("updated_at") or utc_now_iso()))
        occupancy = payload.get("current_occupancy")
        capacity = payload.get("capacity")

        deltas: list[BeliefDelta] = []
        if occupancy is not None:
            _, d = self.world.assert_belief(
                f"shelter:{shelter_id}:occupancy",
                f"shelter:{shelter_id}",
                "current_occupancy",
                occupancy,
                provenance,
                written_by=self.name,
            )
            deltas.extend(d)
        if capacity is not None:
            _, d = self.world.assert_belief(
                f"shelter:{shelter_id}:capacity",
                f"shelter:{shelter_id}",
                "capacity",
                capacity,
                provenance,
                written_by=self.name,
            )
            deltas.extend(d)

        existing = self.world.entity(shelter_id) or {}
        self.world.put_entity(
            shelter_id,
            "shelter",
            current_occupancy=occupancy if occupancy is not None else existing.get("current_occupancy"),
            capacity=capacity if capacity is not None else existing.get("capacity"),
            status=payload.get("status", existing.get("status")),
        )
        return deltas

    def _road_closure(self, payload: dict[str, Any]) -> list[BeliefDelta]:
        event_id = str(payload.get("event_id") or payload.get("route") or "road-event")
        provenance = self._provenance("road_events", str(payload.get("reported_at") or utc_now_iso()))
        _, deltas = self.world.assert_belief(
            f"road:{event_id}:status",
            f"road:{event_id}",
            "status",
            payload.get("status", "closed"),
            provenance,
            written_by=self.name,
        )
        self.world.put_entity(
            event_id,
            "road_event",
            route=payload.get("route"),
            county=payload.get("county"),
            status=payload.get("status", "closed"),
            lat=payload.get("lat"),
            lon=payload.get("lon"),
        )

        # A closure near a staging site makes that site unusable for routing.
        affected = payload.get("affects_staging_site")
        if affected:
            site = self.world.entity(str(affected))
            if site:
                _, extra = self.world.assert_belief(
                    f"staging:{affected}:reachable",
                    f"staging:{affected}",
                    "reachable",
                    False,
                    provenance,
                    written_by=self.name,
                )
                deltas = list(deltas) + list(extra)
        return list(deltas)

    # ------------------------------------------------------------- explanation

    def _explain(self, event: Event, deltas: list[BeliefDelta], invalidated: list[str]) -> str:
        deterministic = (
            f"{event.type}: {len(deltas)} beliefs changed; "
            f"{len(invalidated)} decisions invalidated ({', '.join(invalidated[:4]) or 'none'})."
        )
        if not deltas:
            return deterministic

        chat = self.ask(
            [
                {"role": "system", "content": _SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"Event: {event.type}\n"
                        f"Changed facts: {[d.to_dict() for d in deltas[:8]]}\n"
                        f"Invalidated decisions: {invalidated[:8]}"
                    ),
                },
            ],
            action="explain_revision",
            max_tokens=400,
        )
        parsed = chat.json() if chat.ok else None
        if isinstance(parsed, dict) and parsed.get("explanation"):
            return str(parsed["explanation"])[:400]
        return deterministic


def diff_plans(previous: Plan, revised: Plan) -> PlanDiff:
    """Field-level diff of two plans, for the side-by-side in the UI."""
    before = previous.by_id()
    after = revised.by_id()

    # Actions are matched on (kind, target) rather than action_id, since a
    # revision mints new ids for the same real-world action.
    def key(action):
        return (action.kind, action.target)

    before_keys = {key(a): a for a in previous.actions}
    after_keys = {key(a): a for a in revised.actions}

    added = [a for k, a in after_keys.items() if k not in before_keys]
    removed = [a.action_id for k, a in before_keys.items() if k not in after_keys]
    changed: list[dict[str, Any]] = []
    for k, new_action in after_keys.items():
        old_action = before_keys.get(k)
        if old_action is None:
            continue
        for field_name in ("priority", "confidence", "reversible", "detail"):
            old_value = getattr(old_action, field_name)
            new_value = getattr(new_action, field_name)
            if old_value != new_value:
                changed.append(
                    {
                        "action_id": new_action.action_id,
                        "previous_action_id": old_action.action_id,
                        "kind": new_action.kind,
                        "target": new_action.target,
                        "field": field_name,
                        "before": old_value,
                        "after": new_value,
                    }
                )
    _ = (before, after)
    return PlanDiff(added=added, removed=removed, changed=changed)
