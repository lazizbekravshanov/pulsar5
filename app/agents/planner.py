"""Planner (Deep lane) — proposes the plan, then revises it under instruction.

The Planner is the only agent allowed to author actions. It must cite World
Model fact ids for every claim, so each action carries `evidence_ids` pointing
at beliefs the Analyst loaded from Zetaris.

Shelter openings are budgeted. Opening a shelter costs staff and cots, so the
first pass opens a small number and accepts that capacity may fall short; the
Simulator measures the shortfall and the Critic sends it back, and the revision
raises the budget for exactly the reason given. That is the planning trade-off
the loop exists to resolve, not a staged failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.agents.base import Agent
from app.schemas import Critique, Plan, PlanAction, SimulationResult, utc_now_iso
from app.sim.capacity import usable_headroom
from app.sim.checks import SHELTER_SEEKING_RATE
from app.sim.geo import distance_to_nearest, point_in_polygon

INITIAL_OPENING_BUDGET = 3
VULNERABILITY_EVAC_THRESHOLD = 0.55

# A forecast cone is not a boundary, so a highly vulnerable zone just outside it
# is still a candidate. "Just outside" has to mean something, though: without a
# buffer the Planner would evacuate every vulnerable zone in the region no
# matter how far from the storm, and a track shift would change nothing.
#
# 25 miles is deliberately tight. A major hurricane's damaging wind field
# reaches much further than that, so this under-covers on purpose and leaves
# the wider call to the operator; the README lists it as a known limitation.
EVAC_BUFFER_MILES = 25.0

_SYSTEM = """You are the Planner for a county emergency operations centre.
You receive the world state and must set planning parameters. You do not invent
facilities or numbers. Reply with JSON only:
{"opening_budget": int, "evacuate_zones": ["zone_id", ...], "staging_sites": int,
 "summary": "one sentence", "reasoning": "two sentences"}
opening_budget is how many shelters to open (1-12). evacuate_zones lists zone ids
to evacuate from the candidates given, highest priority first."""


@dataclass
class PlanParams:
    opening_budget: int = INITIAL_OPENING_BUDGET
    forced_zones: list[str] = field(default_factory=list)
    max_staging: int = 4
    drop_staging: list[str] = field(default_factory=list)
    note: str = "initial proposal"


class Planner(Agent):
    name = "Planner"
    role = "Builds shelter openings, supply staging and evacuation priority, citing World Model evidence ids"
    lane = "deep"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self._counter = 0

    # ----------------------------------------------------------------- propose

    def propose(
        self,
        *,
        replay_hour: str,
        constraints: dict[str, Any],
        lessons: list[str] | None = None,
    ) -> Plan:
        candidates = self._evacuation_candidates()
        params = PlanParams(max_staging=int(constraints.get("staging_sites_max", 4)))

        decided = self._ask_for_params(
            candidates=candidates, constraints=constraints, lessons=lessons or [], params=params
        )
        plan = self._build(decided, replay_hour=replay_hour, constraints=constraints, lessons=lessons or [])
        self.log(
            "propose_plan",
            output_summary=f"{len(plan.actions)} actions, opening_budget={decided.opening_budget}",
            extra={"opening_budget": decided.opening_budget, "note": decided.note},
        )
        self.ctx.tracer.handoff(self.name, "Simulator", plan.summary)
        return plan

    # ------------------------------------------------------------------ revise

    def revise(
        self,
        plan: Plan,
        simulation: SimulationResult,
        critique: Critique | None,
        *,
        replay_hour: str,
        constraints: dict[str, Any],
        lessons: list[str] | None = None,
    ) -> Plan:
        """Address the specific violation codes that came back, nothing else."""
        params = PlanParams(max_staging=int(constraints.get("staging_sites_max", 4)))
        params.opening_budget = len([a for a in plan.actions if a.kind == "open_shelter"])
        params.forced_zones = [a.target for a in plan.actions if a.kind == "evacuate_zone"]
        reasons: list[str] = []

        for violation in simulation.violations:
            if violation.code == "insufficient_shelter_capacity":
                shortfall = (violation.observed or 0) - (violation.limit or 0)
                extra = max(1, int(shortfall // 800) + 1)
                params.opening_budget += extra
                reasons.append(f"open {extra} more shelters for a {shortfall:.0f}-space shortfall")
            elif violation.code == "regional_capacity_shortfall":
                # Open everything available, then let it escalate. There is no
                # planning move that creates shelter space that does not exist.
                params.opening_budget = 12
                reasons.append("open every available shelter; the remaining gap needs a human decision")
            elif violation.code == "shelter_at_capacity":
                params.opening_budget += 1
                reasons.append("replace a shelter that is already full")
            elif violation.code in {"high_vulnerability_zone_uncovered", "hazard_zone_uncovered"}:
                if violation.zone_id and violation.zone_id not in params.forced_zones:
                    params.forced_zones.append(violation.zone_id)
                    reasons.append(f"cover {violation.zone_id}")
            elif violation.code == "too_many_staging_sites":
                params.max_staging = int(violation.limit or params.max_staging)
                reasons.append(f"cut staging to {params.max_staging} sites")
            elif violation.code == "shelter_out_of_staging_range":
                params.max_staging = min(params.max_staging + 1, int(constraints.get("staging_sites_max", 4)))
                reasons.append("add a staging site closer to the open shelters")
            elif violation.code == "evacuation_without_shelter":
                params.opening_budget = max(params.opening_budget, 2)
                reasons.append("open shelters to receive the ordered evacuations")

        # The Critic looks wider than the Simulator, so some findings exist only
        # in the critique. Without this the loop re-finds the same uncovered zone
        # every iteration, burns its whole budget and escalates something it could
        # simply have fixed.
        for finding in (critique.findings if critique else []):
            if finding.code == "high_vulnerability_zone_unaddressed" and finding.zone_id:
                if finding.zone_id not in params.forced_zones:
                    params.forced_zones.append(finding.zone_id)
                    reasons.append(f"cover {finding.zone_id}, flagged by the Critic")

        for instruction in (critique.instructions if critique else []):
            reasons.append(instruction)
            lowered = instruction.lower()
            if "more shelter" in lowered or "capacity" in lowered:
                params.opening_budget += 1

        params.note = "; ".join(reasons) or "no actionable findings"
        revised = self._build(params, replay_hour=replay_hour, constraints=constraints, lessons=lessons or [])
        self.log(
            "revise_plan",
            input_summary=f"{len(simulation.violations)} violations, "
            f"{len(critique.findings) if critique else 0} critic findings",
            output_summary=f"revision: {params.note}",
            extra={"opening_budget": params.opening_budget},
        )
        self.ctx.tracer.handoff(self.name, "Simulator", "revised plan")
        return revised

    # ------------------------------------------------------------------ params

    def _ask_for_params(
        self,
        *,
        candidates: list[dict[str, Any]],
        constraints: dict[str, Any],
        lessons: list[str],
        params: PlanParams,
    ) -> PlanParams:
        shelters = self.world.entities("shelter")
        summary = {
            "advisory": self.world.belief_value("hazard:advisory_number"),
            "forecast_landfall_county": self.world.belief_value("hazard:forecast_landfall_county"),
            "candidate_zones": [
                {
                    "zone_id": z["entity_id"],
                    "county": z.get("county"),
                    "population": z.get("population"),
                    "vulnerability": z.get("vulnerability_score"),
                    "in_cone": z.get("_in_cone"),
                }
                for z in candidates[:12]
            ],
            "shelters_available": len([s for s in shelters if _headroom(s) > 0]),
            "total_headroom": sum(_headroom(s) for s in shelters),
            "constraints": constraints,
            "memory_lessons": lessons,
        }

        result = self.ask(
            [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": f"World state:\n{summary}\n\nSet the planning parameters."},
            ],
            action="choose_plan_params",
            max_tokens=900,
        )
        parsed = result.json() if result.ok else None
        if isinstance(parsed, dict):
            budget = parsed.get("opening_budget")
            zones = parsed.get("evacuate_zones")
            if isinstance(budget, int) and 1 <= budget <= 12:
                params.opening_budget = budget
            if isinstance(zones, list):
                known = {c["entity_id"] for c in candidates}
                params.forced_zones = [str(z) for z in zones if str(z) in known]
            if isinstance(parsed.get("staging_sites"), int):
                params.max_staging = min(int(parsed["staging_sites"]), params.max_staging)
            params.note = str(parsed.get("reasoning", "model-set parameters"))[:300]
            return params

        self.log(
            "choose_plan_params_fallback",
            output_summary=f"deterministic parameters: budget={params.opening_budget}",
            status="fallback",
        )
        return params

    # ------------------------------------------------------------------- build

    def _evacuation_candidates(self) -> list[dict[str, Any]]:
        """Zones ranked by exposure, then vulnerability, then population.

        Each zone is tagged with whether it sits inside the current forecast
        cone and how far it is from that cone, so a track shift re-ranks the
        whole list rather than nudging priorities.
        """
        cone = self.world.belief_value("hazard:cone_polygon", []) or []
        cone_points = [(float(p[0]), float(p[1])) for p in cone if isinstance(p, (list, tuple)) and len(p) >= 2]

        rows: list[dict[str, Any]] = []
        for zone in self.world.entities("zone"):
            lat, lon = zone.get("lat"), zone.get("lon")
            enriched = dict(zone)
            if lat is None or lon is None or not cone_points:
                enriched["_in_cone"] = False
                enriched["_miles_from_cone"] = float("inf")
            else:
                point = (float(lat), float(lon))
                inside = point_in_polygon(point, cone_points)
                enriched["_in_cone"] = inside
                enriched["_miles_from_cone"] = 0.0 if inside else round(
                    distance_to_nearest(point, cone_points), 1
                )
            rows.append(enriched)

        rows.sort(
            key=lambda z: (
                z["_in_cone"],
                -float(z.get("_miles_from_cone") or 0),
                float(z.get("vulnerability_score") or 0),
                float(z.get("population") or 0),
            ),
            reverse=True,
        )
        return rows

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-{self._counter:03d}"

    def _build(
        self,
        params: PlanParams,
        *,
        replay_hour: str,
        constraints: dict[str, Any],
        lessons: list[str],
    ) -> Plan:
        candidates = self._evacuation_candidates()
        by_id = {c["entity_id"]: c for c in candidates}

        # ---- which zones to evacuate
        if params.forced_zones:
            chosen = [by_id[z] for z in params.forced_zones if z in by_id]
        else:
            chosen = [
                z
                for z in candidates
                if z["_in_cone"]
                or (
                    float(z.get("vulnerability_score") or 0) >= VULNERABILITY_EVAC_THRESHOLD
                    and float(z.get("_miles_from_cone") or float("inf")) <= EVAC_BUFFER_MILES
                )
            ]

        actions: list[PlanAction] = []
        for rank, zone in enumerate(chosen, start=1):
            zone_id = zone["entity_id"]
            population = float(zone.get("population") or 0)
            vulnerability = float(zone.get("vulnerability_score") or 0)
            evidence = [
                f"zone:{zone_id}:population",
                f"zone:{zone_id}:vulnerability",
                "hazard:cone_polygon",
                "hazard:advisory_number",
            ]
            actions.append(
                PlanAction(
                    action_id=self._next_id("ACT"),
                    kind="evacuate_zone",
                    target=zone_id,
                    detail=(
                        f"Evacuate zone {zone_id} ({zone.get('county')}): population "
                        f"{population:.0f}, vulnerability {vulnerability:.2f}, "
                        + (
                            "inside the forecast cone"
                            if zone.get("_in_cone")
                            else f"{float(zone.get('_miles_from_cone') or 0):.0f} mi outside the cone"
                        )
                    ),
                    priority=rank,
                    reversible=False,  # an evacuation order cannot be taken back
                    confidence=round(min(0.93, 0.55 + 0.4 * vulnerability), 2),
                    evidence_ids=[e for e in evidence if self.world.belief(e)],
                    rationale=(
                        "Inside the forecast cone"
                        if zone["_in_cone"]
                        else (
                            f"High social vulnerability within {EVAC_BUFFER_MILES:.0f} mi of the cone"
                        )
                    ),
                )
            )

        # ---- which shelters to open
        zone_points = [
            (float(z["lat"]), float(z["lon"])) for z in chosen if z.get("lat") is not None and z.get("lon") is not None
        ]
        expected = sum(float(z.get("population") or 0) for z in chosen) * SHELTER_SEEKING_RATE

        usable = [s for s in self.world.entities("shelter") if _headroom(s) > 0 and s.get("status") != "closed"]
        usable.sort(
            key=lambda s: (
                distance_to_nearest((float(s.get("lat") or 0), float(s.get("lon") or 0)), zone_points)
                if zone_points
                else 0.0,
                -_headroom(s),
            )
        )

        running = 0.0
        opened = 0
        for shelter in usable:
            if opened >= params.opening_budget:
                break
            shelter_id = shelter["entity_id"]
            headroom = _headroom(shelter)
            evidence = [f"shelter:{shelter_id}:capacity", f"shelter:{shelter_id}:occupancy"]
            actions.append(
                PlanAction(
                    action_id=self._next_id("ACT"),
                    kind="open_shelter",
                    target=shelter_id,
                    detail=(
                        f"Open {shelter.get('name') or shelter_id} ({shelter.get('county')}): "
                        f"{headroom:.0f} spaces free of {float(shelter.get('capacity') or 0):.0f}"
                    ),
                    priority=opened + 1,
                    reversible=True,
                    confidence=0.82,
                    evidence_ids=[e for e in evidence if self.world.belief(e)],
                    rationale="Nearest shelter with headroom to the evacuating zones",
                )
            )
            running += headroom
            opened += 1

        # ---- where to stage supplies
        shelter_points = [
            (float(s.get("lat") or 0), float(s.get("lon") or 0))
            for s in usable[:opened]
            if s.get("lat") is not None
        ]
        sites = [s for s in self.world.entities("staging_site") if s["entity_id"] not in params.drop_staging]
        sites.sort(
            key=lambda s: distance_to_nearest(
                (float(s.get("lat") or 0), float(s.get("lon") or 0)), shelter_points
            )
            if shelter_points
            else 0.0
        )
        trucks = int(constraints.get("supply_trucks", 12))
        site_limit = min(params.max_staging, int(constraints.get("staging_sites_max", 4)))
        for index, site in enumerate(sites[:site_limit]):
            site_id = site["entity_id"]
            share = trucks // max(1, min(len(sites), site_limit))
            actions.append(
                PlanAction(
                    action_id=self._next_id("ACT"),
                    kind="stage_supplies",
                    target=site_id,
                    detail=f"Stage {share} truckloads at {site.get('name') or site_id} ({site.get('county')})",
                    priority=index + 1,
                    reversible=True,
                    confidence=0.78,
                    evidence_ids=[
                        e for e in [f"staging:{site_id}:truck_capacity"] if self.world.belief(e)
                    ],
                    rationale="Closest logistics point to the shelters being opened",
                )
            )

        if not actions:
            actions.append(
                PlanAction(
                    action_id=self._next_id("ACT"),
                    kind="hold",
                    target="region",
                    detail="No zone meets the evacuation threshold at this hour; hold and re-evaluate on the next advisory",
                    reversible=True,
                    confidence=0.6,
                    evidence_ids=[e for e in ["hazard:advisory_number"] if self.world.belief(e)],
                    rationale="Hazard cone does not intersect a populated zone above threshold",
                )
            )

        advisory = self.world.belief_value("hazard:advisory_number", "?")
        summary = (
            f"Advisory {advisory}: evacuate {len([a for a in actions if a.kind=='evacuate_zone'])} zones, "
            f"open {opened} shelters ({running:.0f} spaces for ~{expected:.0f} expected evacuees), "
            f"stage at {len([a for a in actions if a.kind=='stage_supplies'])} sites"
        )
        if lessons:
            summary += f". Memory applied: {lessons[0][:90]}"

        plan = Plan(
            plan_id=f"plan-{utc_now_iso()}-{self._counter}",
            replay_hour=replay_hour,
            actions=actions,
            summary=summary,
            produced_by=self.name,
        )
        self._record_decisions(plan)
        return plan

    def _record_decisions(self, plan: Plan) -> None:
        """Mirror every action into the World Model decision graph with its deps."""
        for action in plan.actions:
            self.world.record_decision(
                action.action_id,
                action.kind,
                action.target,
                action.detail,
                depends_on=action.evidence_ids,
                made_by=self.name,
                confidence=action.confidence,
            )


# One shared definition, so the Planner and the Simulator never disagree about
# how big a shelter is. See app/sim/capacity.py.
_headroom = usable_headroom
