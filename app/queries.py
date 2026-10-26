"""Named Lightning SQL queries the Analyst runs against Zetaris.

Each name is also the SAMPLE_MODE cache filename
(`data/sample_cache/sql/<name>.json`), so a judge can read the SQL here and the
recorded rows next to it. Registration DDL lives in `scripts/zetaris/*.sql`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NamedQuery:
    """One statement, plus a declaration of what it filters on.

    The predicate fields are not a second query language: they state what the
    SQL above already does, so SAMPLE_MODE can replay a recorded superset
    through the same conditions. Without them a cache keyed only by query name
    would hand a two-county request all five counties' rows, and every replay
    hour the same advisory — SAMPLE_MODE would quietly stop matching live mode.
    """

    name: str
    table: str
    purpose: str
    sql: str
    where_in: tuple[tuple[str, tuple[str, ...]], ...] = ()
    where_lte: tuple[tuple[str, str], ...] = ()
    where_eq: tuple[tuple[str, str], ...] = ()
    order_desc: str | None = None
    limit: int | None = None

    def predicates(self) -> dict[str, object]:
        return {
            "where_in": {col: list(values) for col, values in self.where_in},
            "where_lte": dict(self.where_lte),
            "where_eq": dict(self.where_eq),
            "order_desc": self.order_desc,
            "limit": self.limit,
        }


def _counties(region: list[str]) -> str:
    return ", ".join("'" + c.replace("'", "''") + "'" for c in region)


def storm_advisory_latest(replay_hour: str) -> NamedQuery:
    return NamedQuery(
        "storm_advisory_latest",
        "storm_advisories",
        "latest forecast position and cone at or before the replay hour",
        f"""
        SELECT advisory_number, issued_at, center_lat, center_lon, max_wind_kt,
               forecast_landfall_county, cone_polygon
        FROM pulsar5.storm_advisories
        WHERE issued_at <= '{replay_hour}'
        ORDER BY issued_at DESC
        LIMIT 1
        """.strip(),
        where_lte=(("issued_at", replay_hour),),
        order_desc="issued_at",
        limit=1,
    )


def shelters_by_county(region: list[str]) -> NamedQuery:
    return NamedQuery(
        "shelters_by_county",
        "shelters",
        "FEMA ESF#6 shelters with capacity and occupancy in the region",
        f"""
        SELECT shelter_id, shelter_name, county, capacity, current_occupancy,
               status, lat, lon, updated_at
        FROM pulsar5.shelters
        WHERE county IN ({_counties(region)})
        ORDER BY county, shelter_id
        """.strip(),
        where_in=(("county", tuple(region)),),
    )


def hospitals_by_county(region: list[str]) -> NamedQuery:
    return NamedQuery(
        "hospitals_by_county",
        "hospitals",
        "HIFLD open hospitals, used as fixed high-dependency sites",
        f"""
        SELECT hospital_id, hospital_name, county, beds, trauma_level, lat, lon
        FROM pulsar5.hospitals
        WHERE county IN ({_counties(region)})
        ORDER BY county, hospital_id
        """.strip(),
        where_in=(("county", tuple(region)),),
    )


def zone_vulnerability(region: list[str]) -> NamedQuery:
    return NamedQuery(
        "zone_vulnerability",
        "acs_vulnerability",
        "Census ACS social vulnerability inputs that drive evacuation priority",
        f"""
        SELECT zone_id, county, population, pop_65_plus, households_no_vehicle,
               disability_count, poverty_rate, lat, lon
        FROM pulsar5.acs_vulnerability
        WHERE county IN ({_counties(region)})
        ORDER BY zone_id
        """.strip(),
        where_in=(("county", tuple(region)),),
    )


def staging_sites(region: list[str]) -> NamedQuery:
    return NamedQuery(
        "staging_sites",
        "staging_sites",
        "candidate supply staging sites (state logistics points)",
        f"""
        SELECT site_id, site_name, county, lat, lon, truck_capacity
        FROM pulsar5.staging_sites
        WHERE county IN ({_counties(region)})
        ORDER BY site_id
        """.strip(),
        where_in=(("county", tuple(region)),),
    )


def road_events_open(replay_hour: str) -> NamedQuery:
    return NamedQuery(
        "road_events_open",
        "road_events",
        "open road closures affecting evacuation routes (synthetic source)",
        f"""
        SELECT event_id, route, county, status, lat, lon, reported_at
        FROM pulsar5.road_events
        WHERE reported_at <= '{replay_hour}' AND status = 'closed'
        ORDER BY reported_at DESC
        """.strip(),
        where_lte=(("reported_at", replay_hour),),
        where_eq=(("status", "closed"),),
        order_desc="reported_at",
    )


def disaster_declarations(region: list[str]) -> NamedQuery:
    return NamedQuery(
        "disaster_declarations",
        "disaster_declarations",
        "OpenFEMA declarations, used to validate the region is in scope",
        f"""
        SELECT disaster_number, declaration_date, incident_type, county
        FROM pulsar5.disaster_declarations
        WHERE county IN ({_counties(region)}) AND incident_type = 'Hurricane'
        ORDER BY declaration_date DESC
        LIMIT 10
        """.strip(),
        where_in=(("county", tuple(region)),),
        where_eq=(("incident_type", "Hurricane"),),
        order_desc="declaration_date",
        limit=10,
    )


# The Analyst's tool menu. The Fast lane chooses from these names; the
# deterministic fallback runs the whole list in this order.
QUERY_MENU: dict[str, str] = {
    "storm_advisory_latest": "Latest storm advisory: center, wind, forecast landfall county, cone polygon.",
    "zone_vulnerability": "Census zones with population and social vulnerability inputs.",
    "shelters_by_county": "Shelters with capacity, current occupancy and location.",
    "staging_sites": "Candidate supply staging sites with truck capacity.",
    "hospitals_by_county": "Hospitals with bed counts and location.",
    "road_events_open": "Open road closures that may block evacuation routes.",
    "disaster_declarations": "FEMA disaster declarations for the region.",
}


def build(name: str, *, region: list[str], replay_hour: str) -> NamedQuery:
    builders = {
        "storm_advisory_latest": lambda: storm_advisory_latest(replay_hour),
        "shelters_by_county": lambda: shelters_by_county(region),
        "hospitals_by_county": lambda: hospitals_by_county(region),
        "zone_vulnerability": lambda: zone_vulnerability(region),
        "staging_sites": lambda: staging_sites(region),
        "road_events_open": lambda: road_events_open(replay_hour),
        "disaster_declarations": lambda: disaster_declarations(region),
    }
    if name not in builders:
        raise KeyError(f"unknown query {name!r}; known: {sorted(builders)}")
    return builders[name]()
