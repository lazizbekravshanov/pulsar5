"""Analyst (Fast lane) — tool-calling loop over Zetaris.

The loop is real: the Fast lane is shown the goal, the query menu and what has
already been gathered, and picks the next queries to run. It stops when it says
`done` or when the round cap is hit. Every row it gets back becomes a World
Model belief carrying the Zetaris table, the exact SQL and an `as_of`
timestamp, which is what lets the Planner cite evidence and the Critic flag
stale facts.

With no model available the Analyst runs the full menu in dependency order.
That is a weaker policy, not a fake one: the facts and provenance are
identical, so SAMPLE_MODE exercises the same downstream logic.
"""

from __future__ import annotations

import json
from typing import Any

from app.adapters.zetaris import ZetarisUnavailable
from app.agents.base import Agent
from app.engines.world_model.engine import Provenance
from app.queries import QUERY_MENU, NamedQuery, build
from app.schemas import utc_now_iso

MAX_TOOL_ROUNDS = 4

# Without these there is no plan to make, and guessing past them would be
# exactly the invention this system exists to avoid: the run abstains instead.
# The rest add context, so losing one degrades the plan rather than killing it.
REQUIRED_QUERIES = frozenset(
    {"storm_advisory_latest", "zone_vulnerability", "shelters_by_county", "staging_sites"}
)

_SYSTEM = """You are the Analyst in a disaster-response planning system.
You gather facts by choosing SQL queries from a fixed menu. You never invent data.
Reply with JSON only: {"queries": ["name", ...], "done": false, "why": "short reason"}
Choose only names from the menu. Choose nothing already gathered unless it is stale.
Set done=true when you have what a planner needs: the storm advisory, zone
vulnerability, shelters and staging sites at minimum."""


class Analyst(Agent):
    name = "Analyst"
    role = "Writes Lightning SQL, runs it through Zetaris, and loads facts with provenance into the World Model"
    lane = "fast"

    def gather(self, *, region: list[str], replay_hour: str, goal: str) -> dict[str, Any]:
        gathered: list[str] = []
        fact_ids: list[str] = []
        rounds = 0

        while rounds < MAX_TOOL_ROUNDS:
            rounds += 1
            choice = self._choose(goal=goal, gathered=gathered, round_index=rounds)
            wanted = [n for n in choice["queries"] if n in QUERY_MENU and n not in gathered]

            if not wanted:
                self.log(
                    "tool_loop_stop",
                    output_summary=f"round {rounds}: nothing new to fetch ({choice['why']})",
                )
                break

            for name in wanted:
                query = build(name, region=region, replay_hour=replay_hour)
                try:
                    new_ids = self._run_and_load(query, replay_hour=replay_hour)
                except ZetarisUnavailable as exc:
                    if name in REQUIRED_QUERIES:
                        self.log(
                            "required_source_unavailable",
                            output_summary=f"{name} is required and unavailable: {exc}",
                            status="error",
                        )
                        raise
                    self.log(
                        "optional_source_unavailable",
                        output_summary=f"{name} unavailable, planning without it: {exc}",
                        status="degraded",
                    )
                    gathered.append(name)
                    continue
                fact_ids.extend(new_ids)
                gathered.append(name)

            if choice["done"]:
                self.log("tool_loop_stop", output_summary=f"round {rounds}: Analyst reports done")
                break

        self.ctx.tracer.handoff(self.name, "Planner", f"{len(fact_ids)} facts from {len(gathered)} queries")
        fact_ids.extend(self._derive_exposure())
        return {"queries_run": gathered, "fact_ids": fact_ids, "rounds": rounds}

    def _derive_exposure(self) -> list[str]:
        """`zone:<id>:in_cone` for every zone, from the cone just gathered, so an
        evacuation can depend on its zone's membership rather than the polygon."""
        from app.sim.exposure import derive_zone_exposure

        cone = self.world.belief("hazard:cone_polygon")
        if cone is None:
            return []
        deltas = derive_zone_exposure(self.world, cone.value, cone.provenance, written_by=self.name)
        return sorted({d.fact_id for d in deltas})

    # ---------------------------------------------------------------- choosing

    def _choose(self, *, goal: str, gathered: list[str], round_index: int) -> dict[str, Any]:
        menu = "\n".join(f"- {name}: {desc}" for name, desc in QUERY_MENU.items())
        result = self.ask(
            [
                {"role": "system", "content": _SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"Goal: {goal}\n\nQuery menu:\n{menu}\n\n"
                        f"Already gathered: {gathered or 'nothing yet'}\n"
                        f"Round {round_index} of {MAX_TOOL_ROUNDS}. What next?"
                    ),
                },
            ],
            action="choose_queries",
        )

        parsed = result.json() if result.ok else None
        if isinstance(parsed, dict) and isinstance(parsed.get("queries"), list):
            return {
                "queries": [str(q) for q in parsed["queries"]],
                "done": bool(parsed.get("done", False)),
                "why": str(parsed.get("why", ""))[:200],
            }

        # Deterministic policy: everything the planner needs, in dependency order.
        remaining = [n for n in QUERY_MENU if n not in gathered]
        self.log(
            "choose_queries_fallback",
            output_summary=f"deterministic query policy, {len(remaining)} queries queued",
            status="fallback",
        )
        return {"queries": remaining, "done": True, "why": "deterministic full-menu policy"}

    # ----------------------------------------------------------------- loading

    def _run_and_load(self, query: NamedQuery, *, replay_hour: str) -> list[str]:
        result = self.ctx.zetaris.run_sql(
            query.sql,
            query_name=query.name,
            agent_name=self.name,
            predicates=query.predicates(),
        )
        loader = {
            "storm_advisory_latest": self._load_advisory,
            "shelters_by_county": self._load_shelters,
            "hospitals_by_county": self._load_hospitals,
            "zone_vulnerability": self._load_zones,
            "staging_sites": self._load_staging,
            "road_events_open": self._load_road_events,
            "disaster_declarations": self._load_declarations,
        }[query.name]

        def provenance_for(as_of: str) -> Provenance:
            return Provenance(
                source_table=query.table,
                query=query.sql,
                as_of=as_of,
                retrieved_at=utc_now_iso(),
                data_source=result.source,
            )
        fact_ids = loader(result.rows, provenance_for, replay_hour)
        self.log(
            "load_facts",
            engine="world_model",
            output_summary=f"{query.name}: {len(fact_ids)} facts into the World Model",
            extra={"query_name": query.name, "rows": result.row_count},
        )
        return fact_ids

    def _assert(self, fact_id: str, subject: str, predicate: str, value: Any, provenance: Provenance) -> str:
        self.world.assert_belief(fact_id, subject, predicate, value, provenance, written_by=self.name)
        return fact_id

    def _load_advisory(self, rows, provenance_for, replay_hour) -> list[str]:
        if not rows:
            return []
        row = rows[0]
        as_of = str(row.get("issued_at") or replay_hour)
        provenance = provenance_for(as_of)
        advisory = str(row.get("advisory_number", "?"))
        cone = row.get("cone_polygon")
        if isinstance(cone, str):
            try:
                cone = json.loads(cone)
            except json.JSONDecodeError:
                cone = []

        ids = [
            self._assert("hazard:advisory_number", "hazard", "advisory_number", advisory, provenance),
            self._assert("hazard:issued_at", "hazard", "issued_at", as_of, provenance),
            self._assert(
                "hazard:center",
                "hazard",
                "center",
                {"lat": row.get("center_lat"), "lon": row.get("center_lon")},
                provenance,
            ),
            self._assert("hazard:max_wind_kt", "hazard", "max_wind_kt", row.get("max_wind_kt"), provenance),
            self._assert(
                "hazard:forecast_landfall_county",
                "hazard",
                "forecast_landfall_county",
                row.get("forecast_landfall_county"),
                provenance,
            ),
            self._assert("hazard:cone_polygon", "hazard", "cone_polygon", cone or [], provenance),
        ]
        self.world.put_entity("hazard", "hazard", advisory_number=advisory, issued_at=as_of)
        return ids

    def _load_shelters(self, rows, provenance_for, replay_hour) -> list[str]:
        ids = []
        for row in _newest_per(rows, key="shelter_id", stamp="updated_at", at_or_before=replay_hour):
            shelter_id = str(row.get("shelter_id"))
            as_of = str(row.get("updated_at") or replay_hour)
            provenance = provenance_for(as_of)
            self.world.put_entity(
                shelter_id,
                "shelter",
                name=row.get("shelter_name"),
                county=row.get("county"),
                capacity=row.get("capacity"),
                current_occupancy=row.get("current_occupancy"),
                status=row.get("status"),
                lat=row.get("lat"),
                lon=row.get("lon"),
            )
            ids.append(
                self._assert(
                    f"shelter:{shelter_id}:capacity",
                    f"shelter:{shelter_id}",
                    "capacity",
                    row.get("capacity"),
                    provenance,
                )
            )
            ids.append(
                self._assert(
                    f"shelter:{shelter_id}:occupancy",
                    f"shelter:{shelter_id}",
                    "current_occupancy",
                    row.get("current_occupancy"),
                    provenance,
                )
            )
        return ids

    def _load_hospitals(self, rows, provenance_for, replay_hour) -> list[str]:
        ids = []
        for row in rows:
            hospital_id = str(row.get("hospital_id"))
            provenance = provenance_for(replay_hour)
            self.world.put_entity(
                hospital_id,
                "hospital",
                name=row.get("hospital_name"),
                county=row.get("county"),
                beds=row.get("beds"),
                lat=row.get("lat"),
                lon=row.get("lon"),
            )
            ids.append(
                self._assert(
                    f"hospital:{hospital_id}:beds",
                    f"hospital:{hospital_id}",
                    "beds",
                    row.get("beds"),
                    provenance,
                )
            )
        return ids

    def _load_zones(self, rows, provenance_for, replay_hour) -> list[str]:
        ids = []
        for row in rows:
            zone_id = str(row.get("zone_id"))
            provenance = provenance_for(replay_hour)
            score = vulnerability_score(row)
            self.world.put_entity(
                zone_id,
                "zone",
                county=row.get("county"),
                population=row.get("population"),
                pop_65_plus=row.get("pop_65_plus"),
                households_no_vehicle=row.get("households_no_vehicle"),
                disability_count=row.get("disability_count"),
                poverty_rate=row.get("poverty_rate"),
                vulnerability_score=score,
                lat=row.get("lat"),
                lon=row.get("lon"),
            )
            ids.append(
                self._assert(
                    f"zone:{zone_id}:population",
                    f"zone:{zone_id}",
                    "population",
                    row.get("population"),
                    provenance,
                )
            )
            ids.append(
                self._assert(
                    f"zone:{zone_id}:vulnerability",
                    f"zone:{zone_id}",
                    "vulnerability_score",
                    score,
                    provenance,
                )
            )
        return ids

    def _load_staging(self, rows, provenance_for, replay_hour) -> list[str]:
        ids = []
        for row in rows:
            site_id = str(row.get("site_id"))
            provenance = provenance_for(replay_hour)
            self.world.put_entity(
                site_id,
                "staging_site",
                name=row.get("site_name"),
                county=row.get("county"),
                lat=row.get("lat"),
                lon=row.get("lon"),
                truck_capacity=row.get("truck_capacity"),
            )
            ids.append(
                self._assert(
                    f"staging:{site_id}:truck_capacity",
                    f"staging:{site_id}",
                    "truck_capacity",
                    row.get("truck_capacity"),
                    provenance,
                )
            )
        return ids

    def _load_road_events(self, rows, provenance_for, replay_hour) -> list[str]:
        ids = []
        for row in rows:
            event_id = str(row.get("event_id"))
            as_of = str(row.get("reported_at") or replay_hour)
            provenance = provenance_for(as_of)
            ids.append(
                self._assert(
                    f"road:{event_id}:status",
                    f"road:{event_id}",
                    "status",
                    row.get("status"),
                    provenance,
                )
            )
            self.world.put_entity(
                event_id,
                "road_event",
                route=row.get("route"),
                county=row.get("county"),
                status=row.get("status"),
                lat=row.get("lat"),
                lon=row.get("lon"),
            )
        return ids

    def _load_declarations(self, rows, provenance_for, replay_hour) -> list[str]:
        if not rows:
            return []
        provenance = provenance_for(replay_hour)
        counties = sorted({str(r.get("county")) for r in rows if r.get("county")})
        return [
            self._assert(
                "region:declared_counties", "region", "declared_counties", counties, provenance
            )
        ]


def _newest_per(
    rows: list[dict[str, Any]], *, key: str, stamp: str, at_or_before: str
) -> list[dict[str, Any]]:
    """Latest snapshot per entity at or before a cutoff.

    A feed that reports repeatedly (a shelter census, say) arrives as several
    rows per facility. Lightning SQL does this selection server side in live
    mode; this keeps SAMPLE_MODE honest about which snapshot the replay hour
    should see instead of taking whichever row came last.
    """
    newest: dict[str, dict[str, Any]] = {}
    for row in rows:
        identity = str(row.get(key))
        when = str(row.get(stamp) or "")
        if when and when > at_or_before:
            continue
        held = newest.get(identity)
        if held is None or when >= str(held.get(stamp) or ""):
            newest[identity] = row
    if newest:
        return list(newest.values())
    # Every snapshot post-dates the cutoff: fall back to the earliest per entity
    # rather than silently reporting no shelters at all.
    earliest: dict[str, dict[str, Any]] = {}
    for row in rows:
        identity = str(row.get(key))
        held = earliest.get(identity)
        if held is None or str(row.get(stamp) or "") < str(held.get(stamp) or ""):
            earliest[identity] = row
    return list(earliest.values())


def vulnerability_score(row: dict[str, Any]) -> float:
    """Composite social-vulnerability score in [0, 1].

    Equal weights on four ACS signals, each normalised against a per-signal
    reference share. Deliberately simple and auditable; the README says so.
    """
    population = float(row.get("population") or 0)
    if population <= 0:
        return 0.0

    share_65 = float(row.get("pop_65_plus") or 0) / population
    share_disability = float(row.get("disability_count") or 0) / population
    households = max(population / 2.4, 1.0)  # ACS average household size
    share_no_vehicle = float(row.get("households_no_vehicle") or 0) / households
    poverty = float(row.get("poverty_rate") or 0)
    poverty = poverty / 100 if poverty > 1 else poverty

    components = [
        min(share_65 / 0.35, 1.0),
        min(share_disability / 0.25, 1.0),
        min(share_no_vehicle / 0.20, 1.0),
        min(poverty / 0.30, 1.0),
    ]
    return round(sum(components) / len(components), 3)
