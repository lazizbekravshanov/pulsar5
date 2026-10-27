"""Critic (Judge lane) — can reject the plan and send it back.

The Critic is a gate, not a commentator. It runs four evidence checks that do
not depend on a model being available:

  * every action cites at least one World Model fact id that actually exists
  * no cited fact is older than the newest advisory (staleness)
  * no high-severity simulation violation survives
  * no high-vulnerability zone in the cone is left without an action

On top of that, the Judge lane reviews the plan prose against the cited
evidence and may add findings of its own. Either source can set
`accepted=False`, which sends refinement instructions back to the Planner.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.agents.base import Agent
from app.agents.planner import EVAC_BUFFER_MILES
from app.schemas import Critique, CritiqueFinding, Plan, SimulationResult
from app.sim.checks import HIGH_VULNERABILITY_THRESHOLD
from app.sim.geo import distance_to_nearest, point_in_polygon

# Freshness is a property of the source, not of the clock. An operational feed
# (shelter census, road status, the advisory itself) goes stale in hours. A
# demographic baseline does not: ACS 5-year estimates are years old by
# construction and a hospital registry changes when a hospital is built. Judging
# those against a storm advisory would flag every plan forever.
STALENESS_TOLERANCE_HOURS: dict[str, float] = {
    "storm_advisories": 6,
    "shelters": 12,
    "road_events": 24,
}
TIMELESS_SOURCES = {"acs_vulnerability", "hospitals", "disaster_declarations", "staging_sites"}
DEFAULT_STALENESS_TOLERANCE_HOURS = 12

# Findings the Planner has no way to act on. They belong in unresolved issues
# and the human queue, not in another revise cycle.
#   regional_capacity_shortfall - no planning move invents shelter space
#   stale_evidence              - refreshing facts is the Analyst's job, and the
#                                 orchestrator already re-gathers on every
#                                 advisory change; what survives that is a real
#                                 data-currency gap for the operator to weigh
#   distant_vulnerable_zone     - a policy call about pre-positioning outside the
#                                 cone, which belongs to the operator
#   advisory:*                  - a model finding the Planner has no mechanism
#                                 to act on (see _model_findings)
UNFIXABLE_CODES = {"regional_capacity_shortfall", "stale_evidence", "distant_vulnerable_zone"}


def _is_unfixable(code: str) -> bool:
    return code in UNFIXABLE_CODES or code.startswith("advisory:")


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None

_SYSTEM = """You are the Critic verifying an emergency plan before it is committed.
You are given the plan actions, the evidence each one cites, and deterministic
simulation findings. Flag unsupported numbers, claims not backed by the cited
evidence, and missed high-vulnerability areas. Do not invent new facts.
Reply with JSON only:
{"accepted": bool, "confidence": 0.0-1.0,
 "findings": [{"code": "short_code", "message": "what is wrong", "action_id": "ACT-001 or null"}],
 "instructions": ["imperative fix for the planner", ...]}"""


class Critic(Agent):
    name = "Critic"
    role = "Verifies every claim against cited evidence, flags stale facts, and can reject the plan"
    lane = "judge"

    def review(self, plan: Plan, simulation: SimulationResult) -> Critique:
        findings: list[CritiqueFinding] = []
        instructions: list[str] = []

        # ---- 1. evidence exists
        for action in plan.actions:
            if action.kind == "hold":
                continue
            if not action.evidence_ids:
                findings.append(
                    CritiqueFinding(
                        code="uncited_action",
                        message=f"{action.kind} on {action.target} cites no evidence",
                        action_id=action.action_id,
                    )
                )
                instructions.append(f"Cite World Model fact ids for {action.action_id}")
                continue
            missing = [fid for fid in action.evidence_ids if self.world.belief(fid) is None]
            if missing:
                findings.append(
                    CritiqueFinding(
                        code="dangling_evidence",
                        message=f"{action.action_id} cites unknown fact ids: {', '.join(missing[:3])}",
                        action_id=action.action_id,
                    )
                )
                instructions.append(f"Replace the unknown fact ids on {action.action_id}")

        # ---- 2. staleness against the newest advisory, within tolerance
        advisory_as_of = self.world.belief_value("hazard:issued_at")
        advisory_ts = _parse_ts(advisory_as_of) if advisory_as_of else None
        if advisory_ts:
            for action in plan.actions:
                for fid in action.evidence_ids:
                    belief = self.world.belief(fid)
                    if belief is None:
                        continue
                    source = belief.provenance.source_table
                    if source in TIMELESS_SOURCES:
                        continue
                    tolerance = STALENESS_TOLERANCE_HOURS.get(source, DEFAULT_STALENESS_TOLERANCE_HOURS)
                    fact_ts = _parse_ts(belief.provenance.as_of)
                    if fact_ts is None or fact_ts >= advisory_ts - timedelta(hours=tolerance):
                        continue
                    age = (advisory_ts - fact_ts).total_seconds() / 3600
                    findings.append(
                        CritiqueFinding(
                            code="stale_evidence",
                            message=(
                                f"{action.action_id} relies on {fid} from {source}, dated "
                                f"{belief.provenance.as_of} — {age:.0f}h behind advisory "
                                f"{advisory_as_of} (tolerance {tolerance:.0f}h for this source)"
                            ),
                            action_id=action.action_id,
                        )
                    )
                    instructions.append(f"Re-query {source} for {fid}")
                    break

        # ---- 3. simulation violations
        for violation in simulation.violations:
            if violation.severity == "high":
                findings.append(
                    CritiqueFinding(
                        code=violation.code, message=violation.message, action_id=violation.action_id
                    )
                )
                instructions.append(violation.message)

        # ---- 4. missed high-vulnerability zones
        # The Simulator only inspects zones inside the forecast cone. A cone is a
        # forecast, not a boundary, so the Critic looks wider — but it separates
        # two different failures. A vulnerable zone the Planner should have
        # covered (inside the cone, or within its buffer) is a planning miss and
        # goes back for revision. A vulnerable zone far outside the cone is a
        # pre-positioning judgement call: the Planner is right not to order an
        # evacuation there, and the operator should decide. Conflating the two
        # sends the loop chasing something it cannot resolve.
        targeted = {a.target for a in plan.actions if a.kind == "evacuate_zone"}
        holding = any(a.kind == "hold" for a in plan.actions)
        if not holding:
            cone = self.world.belief_value("hazard:cone_polygon", []) or []
            cone_points = [
                (float(p[0]), float(p[1])) for p in cone if isinstance(p, (list, tuple)) and len(p) >= 2
            ]
            missed = sorted(
                (
                    z
                    for z in self.world.entities("zone")
                    if float(z.get("vulnerability_score") or 0) >= HIGH_VULNERABILITY_THRESHOLD
                    and z["entity_id"] not in targeted
                ),
                key=lambda z: float(z.get("vulnerability_score") or 0),
                reverse=True,
            )
            for zone in missed[:4]:
                zone_id = zone["entity_id"]
                score = float(zone.get("vulnerability_score") or 0)
                lat, lon = zone.get("lat"), zone.get("lon")

                miles = float("inf")
                if cone_points and lat is not None and lon is not None:
                    point = (float(lat), float(lon))
                    miles = 0.0 if point_in_polygon(point, cone_points) else distance_to_nearest(
                        point, cone_points
                    )

                if miles <= EVAC_BUFFER_MILES:
                    findings.append(
                        CritiqueFinding(
                            code="high_vulnerability_zone_unaddressed",
                            message=(
                                f"Zone {zone_id} ({zone.get('county')}) scores {score:.2f} on social "
                                f"vulnerability and sits {miles:.0f} mi from the cone, inside the "
                                f"{EVAC_BUFFER_MILES:.0f} mi planning buffer, with no action"
                            ),
                            zone_id=zone_id,
                        )
                    )
                    instructions.append(
                        f"Account for high-vulnerability zone {zone_id} or justify leaving it out"
                    )
                else:
                    findings.append(
                        CritiqueFinding(
                            code="distant_vulnerable_zone",
                            message=(
                                f"Zone {zone_id} ({zone.get('county')}) scores {score:.2f} on social "
                                f"vulnerability but lies {miles:.0f} mi outside the current cone. No "
                                f"evacuation is warranted yet; pre-positioning there is an operator call"
                            ),
                        )
                    )

        deterministic_findings = len(findings)

        # ---- 5. judge-lane review on top of the mechanical checks
        evidence_view = [
            {
                "action_id": a.action_id,
                "kind": a.kind,
                "target": a.target,
                "detail": a.detail,
                "cites": [
                    {"fact_id": fid, "value": self.world.belief_value(fid)} for fid in a.evidence_ids[:4]
                ],
            }
            for a in plan.actions[:14]
        ]
        chat = self.ask(
            [
                {"role": "system", "content": _SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"Plan summary: {plan.summary}\n\nActions and evidence:\n{evidence_view}\n\n"
                        f"Simulation ok={simulation.ok}, metrics={simulation.metrics}, "
                        f"violations={[v.model_dump() for v in simulation.violations[:8]]}\n\n"
                        "Review it."
                    ),
                },
            ],
            action="review_plan",
            max_tokens=1100,
        )

        model_confidence: float | None = None
        parsed = chat.json() if chat.ok else None
        if isinstance(parsed, dict):
            known_actions = {a.action_id for a in plan.actions}
            known_zones = {z["entity_id"] for z in self.world.entities("zone")}
            for raw in parsed.get("findings", []) or []:
                if not (isinstance(raw, dict) and raw.get("message")):
                    continue
                code = str(raw.get("code", "judge_finding"))[:48]
                action_id = str(raw["action_id"]) if raw.get("action_id") else None
                zone_id = str(raw["zone_id"]) if raw.get("zone_id") else None

                # A judge asked to review will almost always find *something*, so
                # an unbounded one never lets the loop converge and every plan
                # escalates — which teaches the operator to ignore the gate. A
                # model finding blocks only when it names an action or zone the
                # Planner can actually act on. Anything else is a real
                # observation for the human, not a revision instruction.
                actionable = (action_id in known_actions) or (zone_id in known_zones)
                findings.append(
                    CritiqueFinding(
                        code=code if actionable else f"advisory:{code}",
                        message=str(raw["message"])[:400],
                        action_id=action_id if action_id in known_actions else None,
                        zone_id=zone_id if zone_id in known_zones else None,
                    )
                )
            for raw in parsed.get("instructions", []) or []:
                if isinstance(raw, str) and raw.strip():
                    instructions.append(raw.strip()[:300])
            if isinstance(parsed.get("confidence"), (int, float)):
                model_confidence = max(0.0, min(1.0, float(parsed["confidence"])))

        fixable = [f for f in findings if not _is_unfixable(f.code)]
        unfixable = [f for f in findings if _is_unfixable(f.code)]

        # A plan is accepted when nothing remains that the Planner could fix.
        # Unfixable resource gaps still travel with the plan, as findings, and
        # the Risk agent turns them into a human decision.
        accepted = not fixable
        confidence = self._confidence(simulation, findings, model_confidence)

        critique = Critique(
            accepted=accepted,
            findings=findings,
            instructions=_dedupe([i for i in instructions if not any(u.message == i for u in unfixable)])[:8],
            confidence=confidence,
            reviewed_by_model=chat.model if chat.ok else None,
        )

        self.log(
            "accept_plan" if accepted else "reject_plan",
            input_summary=f"{len(plan.actions)} actions, {len(simulation.violations)} violations",
            output_summary=(
                (
                    "plan accepted"
                    + (
                        f" with {len(unfixable)} unresolved resource gap(s) for the human queue"
                        if unfixable
                        else ""
                    )
                )
                if accepted
                else f"{len(fixable)} fixable findings ({deterministic_findings} mechanical): "
                + "; ".join(f.message for f in fixable[:2])
            ),
            target_agent=None if accepted else "Planner",
            confidence=confidence,
            status="success" if accepted else "rejected",
            extra={"finding_codes": sorted({f.code for f in findings})},
        )
        return critique

    def _confidence(
        self,
        simulation: SimulationResult,
        findings: list[CritiqueFinding],
        model_confidence: float | None,
    ) -> float:
        """Score the plan on distinct problems, not on repetition.

        One stale source cited by twenty actions is one problem seen twenty
        times, not twenty problems. Counting instances let a single stale feed
        drive confidence to the floor and escalate an otherwise sound plan,
        which taught the operator to ignore the number. Distinct codes carry the
        weight; breadth adds a small, capped penalty on top.
        """
        base = 0.9

        codes = {f.code for f in findings}
        base -= 0.12 * len(codes)

        repeats = len(findings) - len(codes)
        base -= min(0.15, 0.01 * repeats)

        high_severity_codes = {v.code for v in simulation.violations if v.severity == "high"}
        base -= 0.1 * len(high_severity_codes)

        coverage = float(simulation.metrics.get("coverage_rate", 1.0))
        base -= 0.2 * (1.0 - coverage)

        deterministic = max(0.05, min(0.97, base))
        if model_confidence is None:
            return round(deterministic, 2)
        return round((deterministic + model_confidence) / 2, 2)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out
