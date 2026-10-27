"""Deterministic simulation of a plan against the World Model.

This is plain code, not a model call. The Planner proposes; this decides
whether the proposal survives contact with capacity, geography and the supply
budget. Every failure comes back as a structured `Violation` with the observed
value and the limit it broke, never as prose, so the Critic and the Planner can
act on it mechanically.
"""

from __future__ import annotations

from typing import Any

from app.engines.world_model.engine import WorldModel
from app.schemas import Plan, SimulationResult, Violation
from app.sim.capacity import usable_headroom
from app.sim.geo import distance_to_nearest, point_in_polygon

# An evacuation order for a zone sends this share of its population to shelters.
# Conservative public-planning rule of thumb, stated in the README limitations.
SHELTER_SEEKING_RATE = 0.18
MAX_STAGING_DISTANCE_MILES = 45.0
HIGH_VULNERABILITY_THRESHOLD = 0.6
EVACUEES_PER_TRUCK = 1200


def _coords(entity: dict[str, Any]) -> tuple[float, float] | None:
    lat, lon = entity.get("lat"), entity.get("lon")
    if lat is None or lon is None:
        return None
    return (float(lat), float(lon))


def simulate(plan: Plan, world: WorldModel, constraints: dict[str, Any]) -> SimulationResult:
    violations: list[Violation] = []

    zones = {z["entity_id"]: z for z in world.entities("zone")}
    shelters = {s["entity_id"]: s for s in world.entities("shelter")}
    staging_sites = {s["entity_id"]: s for s in world.entities("staging_site")}

    opened = [a for a in plan.actions if a.kind == "open_shelter"]
    staged = [a for a in plan.actions if a.kind == "stage_supplies"]
    evacuations = [a for a in plan.actions if a.kind == "evacuate_zone"]

    # ---------------------------------------------------- 1. shelter capacity
    evacuating_zone_ids = [a.target for a in evacuations]
    expected_evacuees = sum(
        float(zones.get(z, {}).get("population", 0)) * SHELTER_SEEKING_RATE for z in evacuating_zone_ids
    )

    opened_capacity = 0.0
    for action in opened:
        shelter = shelters.get(action.target)
        if shelter is None:
            violations.append(
                Violation(
                    code="unknown_shelter",
                    severity="high",
                    message=f"Plan opens {action.target}, which is not in the World Model",
                    action_id=action.action_id,
                )
            )
            continue
        capacity = float(shelter.get("capacity", 0))
        occupancy = float(shelter.get("current_occupancy", 0))
        headroom = usable_headroom(shelter)
        opened_capacity += headroom
        if headroom <= 0:
            violations.append(
                Violation(
                    code="shelter_at_capacity",
                    severity="high",
                    message=f"Shelter {action.target} is already at capacity and cannot take evacuees",
                    action_id=action.action_id,
                    observed=occupancy,
                    limit=capacity,
                )
            )

    if expected_evacuees > opened_capacity:
        # Can the Planner fix this by opening more, or has the region simply run
        # out of shelter space? Those are different findings: the first goes back
        # to the Planner, the second is a resource gap only a human can answer.
        regional_capacity = sum(
            usable_headroom(s) for s in shelters.values() if s.get("status") != "closed"
        )
        shortfall = expected_evacuees - opened_capacity
        if expected_evacuees > regional_capacity:
            violations.append(
                Violation(
                    code="regional_capacity_shortfall",
                    severity="high",
                    message=(
                        f"Region cannot shelter this evacuation: {expected_evacuees:.0f} expected "
                        f"evacuees against {regional_capacity:.0f} spaces in every open shelter "
                        f"combined (short by {expected_evacuees - regional_capacity:.0f}). "
                        "Opening more shelters cannot close this gap."
                    ),
                    observed=expected_evacuees,
                    limit=regional_capacity,
                )
            )
        else:
            violations.append(
                Violation(
                    code="insufficient_shelter_capacity",
                    severity="high",
                    message=(
                        f"Expected {expected_evacuees:.0f} shelter-seeking evacuees against "
                        f"{opened_capacity:.0f} open spaces (short by {shortfall:.0f}); "
                        f"{regional_capacity:.0f} spaces exist in the region"
                    ),
                    observed=expected_evacuees,
                    limit=opened_capacity,
                )
            )

    # ------------------------------------------------- 2. hazard zone coverage
    cone = world.belief_value("hazard:cone_polygon", []) or []
    cone_points = [(float(p[0]), float(p[1])) for p in cone if isinstance(p, (list, tuple)) and len(p) >= 2]

    zones_in_hazard: list[str] = []
    for zone_id, zone in zones.items():
        point = _coords(zone)
        if point and cone_points and point_in_polygon(point, cone_points):
            zones_in_hazard.append(zone_id)

    covered = set(evacuating_zone_ids)
    for zone_id in zones_in_hazard:
        if zone_id in covered:
            continue
        zone = zones[zone_id]
        vulnerability = float(zone.get("vulnerability_score", 0))
        high = vulnerability >= HIGH_VULNERABILITY_THRESHOLD
        violations.append(
            Violation(
                code="high_vulnerability_zone_uncovered" if high else "hazard_zone_uncovered",
                severity="high" if high else "medium",
                message=(
                    f"Zone {zone_id} ({zone.get('county','?')}) sits in the forecast cone with "
                    f"vulnerability {vulnerability:.2f} and has no evacuation action"
                ),
                zone_id=zone_id,
                observed=vulnerability,
                limit=HIGH_VULNERABILITY_THRESHOLD,
            )
        )

    # ------------------------------------------------- 3. staging site budget
    max_sites = int(constraints.get("staging_sites_max", 4))
    if len(staged) > max_sites:
        violations.append(
            Violation(
                code="too_many_staging_sites",
                severity="medium",
                message=f"Plan stages at {len(staged)} sites, limit is {max_sites}",
                observed=float(len(staged)),
                limit=float(max_sites),
            )
        )

    trucks = int(constraints.get("supply_trucks", 12))
    trucks_needed = expected_evacuees / EVACUEES_PER_TRUCK if expected_evacuees else 0.0
    if trucks_needed > trucks:
        violations.append(
            Violation(
                code="insufficient_supply_trucks",
                severity="medium",
                message=(
                    f"{expected_evacuees:.0f} evacuees need about {trucks_needed:.1f} truckloads, "
                    f"{trucks} available"
                ),
                observed=trucks_needed,
                limit=float(trucks),
            )
        )

    # --------------------------------------- 4. staging reachability of shelters
    staging_points = [p for p in (_coords(staging_sites.get(a.target, {})) for a in staged) if p]
    for action in staged:
        if action.target not in staging_sites:
            violations.append(
                Violation(
                    code="unknown_staging_site",
                    severity="medium",
                    message=f"Plan stages at {action.target}, which is not in the World Model",
                    action_id=action.action_id,
                )
            )

    far_shelters: list[str] = []
    for action in opened:
        shelter_point = _coords(shelters.get(action.target, {}))
        if not shelter_point or not staging_points:
            continue
        distance = distance_to_nearest(shelter_point, staging_points)
        if distance > MAX_STAGING_DISTANCE_MILES:
            far_shelters.append(action.target)
            violations.append(
                Violation(
                    code="shelter_out_of_staging_range",
                    severity="medium",
                    message=(
                        f"Shelter {action.target} is {distance:.0f} mi from the nearest staging site "
                        f"(limit {MAX_STAGING_DISTANCE_MILES:.0f} mi)"
                    ),
                    action_id=action.action_id,
                    observed=round(distance, 1),
                    limit=MAX_STAGING_DISTANCE_MILES,
                )
            )

    # ------------------------------------------------------------ 5. staleness
    if not opened and evacuations:
        violations.append(
            Violation(
                code="evacuation_without_shelter",
                severity="high",
                message="Plan orders evacuations but opens no shelters",
            )
        )

    metrics = {
        "expected_evacuees": round(expected_evacuees, 1),
        "open_shelter_capacity": round(opened_capacity, 1),
        "regional_shelter_capacity": round(
            sum(usable_headroom(s) for s in shelters.values() if s.get("status") != "closed"), 1
        ),
        "capacity_margin": round(opened_capacity - expected_evacuees, 1),
        "zones_in_hazard": float(len(zones_in_hazard)),
        "zones_covered": float(len([z for z in zones_in_hazard if z in covered])),
        "coverage_rate": round(
            len([z for z in zones_in_hazard if z in covered]) / len(zones_in_hazard), 3
        )
        if zones_in_hazard
        else 1.0,
        "high_vulnerability_uncovered": float(
            len([v for v in violations if v.code == "high_vulnerability_zone_uncovered"])
        ),
        "staging_sites_used": float(len(staged)),
        "trucks_needed": round(trucks_needed, 2),
        "shelters_out_of_range": float(len(far_shelters)),
    }

    return SimulationResult(
        ok=not any(v.severity == "high" for v in violations),
        violations=violations,
        metrics=metrics,
    )
