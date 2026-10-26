#!/usr/bin/env python3
"""Regenerate the Hurricane Ian replay data and the SAMPLE_MODE cache.

    python scripts/build_replay.py

Writes:
  data/replay/ian_advisories.json     advisory track + forecast cones
  data/replay/road_events.json        SYNTHETIC road closures
  data/replay/zones.json              SYNTHETIC ACS-shaped vulnerability zones
  data/replay/facilities.json         SYNTHETIC shelters / hospitals / staging sites
  data/sample_cache/sql/*.json        recorded rows per named query

Provenance, stated plainly:

* **Advisory track** — centre positions, intensities and the Tampa-then-south
  forecast shift follow the public NHC advisory archive for Ian (Sept 2022).
  Positions are rounded to 0.1 degrees and the cones are simplified polygons,
  so this is a faithful *shape* of the forecast history, not the official
  product. Live mode reads the real alerts from Zetaris instead.
* **Everything else is synthetic**, generated deterministically from the seed
  below. Facility names, capacities, occupancies and tract statistics are
  invented to ACS- and FEMA-shaped distributions. No real shelter occupancy or
  patient data is used anywhere in this repository.

The replay exists so SAMPLE_MODE can run the real agent logic with no network.
In live mode every one of these tables is registered in Zetaris and queried
through its REST API; nothing here is read at request time.
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.agents.analyst import vulnerability_score  # noqa: E402

REPLAY = ROOT / "data" / "replay"
CACHE = ROOT / "data" / "sample_cache" / "sql"
SEED = 20220928  # Ian's Florida landfall date, used only to make this reproducible

COUNTY_FIPS = {"Lee": "071", "Charlotte": "015", "Sarasota": "115", "Pinellas": "103", "Hillsborough": "057"}
COUNTY_CENTRES = {
    "Lee": (26.64, -81.87),
    "Charlotte": (26.95, -82.05),
    "Sarasota": (27.31, -82.53),
    "Pinellas": (27.85, -82.72),
    "Hillsborough": (27.95, -82.35),
}

# Forecast cone at advisory 15 (Sept 26): the track pointed at Tampa Bay.
CONE_TAMPA = [
    [28.60, -83.30], [28.60, -82.10], [27.80, -81.90],
    [27.10, -82.10], [27.05, -83.00], [27.90, -83.40],
]
# Forecast cone at advisory 23 (Sept 27): shifted south toward Lee County.
CONE_FORT_MYERS = [
    [27.45, -82.75], [27.45, -81.55], [26.60, -81.40],
    [26.10, -81.70], [26.15, -82.40], [26.90, -82.80],
]

ADVISORIES = [
    # advisory, issued_at,            lat,  lon,    kt, forecast landfall county, cone
    (14, "2022-09-26T03:00Z", 20.8, -83.1, 85, "Hillsborough", CONE_TAMPA),
    (15, "2022-09-26T09:00Z", 21.5, -83.5, 100, "Hillsborough", CONE_TAMPA),
    (16, "2022-09-26T15:00Z", 22.1, -83.7, 105, "Hillsborough", CONE_TAMPA),
    (17, "2022-09-26T21:00Z", 22.8, -83.9, 110, "Pinellas", CONE_TAMPA),
    (19, "2022-09-27T09:00Z", 23.9, -83.6, 115, "Sarasota", CONE_TAMPA),
    (21, "2022-09-27T15:00Z", 24.6, -83.2, 120, "Sarasota", CONE_FORT_MYERS),
    (23, "2022-09-27T21:00Z", 25.4, -82.9, 130, "Lee", CONE_FORT_MYERS),
    (25, "2022-09-28T09:00Z", 26.3, -82.4, 140, "Lee", CONE_FORT_MYERS),
]

SHELTER_KINDS = ["High School", "Middle School", "Elementary School", "Community Center", "Civic Arena"]
STREETS = ["Palmetto", "Cypress", "Manatee", "Gulfview", "Pine Island", "Estero", "Caloosa", "Myakka"]


def build_advisories() -> list[dict]:
    return [
        {
            "advisory_number": number,
            "issued_at": issued_at,
            "center_lat": lat,
            "center_lon": lon,
            "max_wind_kt": wind,
            "forecast_landfall_county": county,
            "cone_polygon": cone,
            "source": "shape follows the public NHC advisory archive for Hurricane Ian (2022)",
        }
        for number, issued_at, lat, lon, wind, county, cone in ADVISORIES
    ]


def build_zones(rng: random.Random) -> list[dict]:
    """SYNTHETIC census-tract-shaped zones, three per county."""
    zones: list[dict] = []
    for county, (lat, lon) in COUNTY_CENTRES.items():
        fips = COUNTY_FIPS[county]
        for index in range(3):
            # Spread tracts around the county centre, coastal ones first.
            offset_lat = (index - 1) * 0.16 + rng.uniform(-0.05, 0.05)
            offset_lon = (index - 1) * -0.14 + rng.uniform(-0.05, 0.05)
            population = rng.randrange(3200, 11800, 100)
            # Coastal tract (index 0) skews older and poorer: that is the real
            # evacuation problem on this coast.
            frailty = {0: 0.92, 1: 0.58, 2: 0.34}[index] * rng.uniform(0.82, 1.12)
            zone = {
                "zone_id": f"FL-{fips}-{500 + index * 7:04d}",
                "county": county,
                "population": population,
                "pop_65_plus": int(population * (0.14 + 0.22 * frailty)),
                "households_no_vehicle": int((population / 2.4) * (0.03 + 0.14 * frailty)),
                "disability_count": int(population * (0.08 + 0.13 * frailty)),
                "poverty_rate": round((0.06 + 0.17 * frailty) * 100, 1),
                "lat": round(lat + offset_lat, 4),
                "lon": round(lon + offset_lon, 4),
            }
            zones.append(zone)
    return zones


def build_facilities(rng: random.Random, zones: list[dict]) -> dict[str, list[dict]]:
    shelters: list[dict] = []
    hospitals: list[dict] = []
    staging: list[dict] = []

    for county, (lat, lon) in COUNTY_CENTRES.items():
        fips = COUNTY_FIPS[county]

        for index in range(3):
            capacity = rng.randrange(700, 3200, 50)
            # Pre-landfall occupancy: coastal counties start loading first.
            fill = rng.uniform(0.05, 0.45) if county in {"Lee", "Charlotte"} else rng.uniform(0.0, 0.2)
            shelter_id = f"S-{fips}-{index + 1}"
            name = f"{rng.choice(STREETS)} {rng.choice(SHELTER_KINDS)}"
            slat = round(lat + rng.uniform(-0.22, 0.22), 4)
            slon = round(lon + rng.uniform(-0.22, 0.22), 4)
            # Two census snapshots, as a real shelter feed reports: one well
            # before landfall and one the evening before. The Analyst picks the
            # newest snapshot at or before the replay hour, so re-querying after
            # an advisory shift genuinely returns fresher numbers.
            for snapshot_at, loading in (("2022-09-26T06:00Z", 1.0), ("2022-09-27T18:00Z", 2.1)):
                shelters.append(
                    {
                        "shelter_id": shelter_id,
                        "shelter_name": name,
                        "county": county,
                        "capacity": capacity,
                        "current_occupancy": min(capacity, int(capacity * fill * loading)),
                        "status": "open",
                        "lat": slat,
                        "lon": slon,
                        "updated_at": snapshot_at,
                    }
                )

        for index in range(2):
            hospitals.append(
                {
                    "hospital_id": f"H-{fips}-{index + 1}",
                    "hospital_name": f"{county} Regional {'Medical Center' if index == 0 else 'Hospital'}",
                    "county": county,
                    "beds": rng.randrange(120, 620, 10),
                    "trauma_level": rng.choice(["I", "II", "III"]),
                    "lat": round(lat + rng.uniform(-0.15, 0.15), 4),
                    "lon": round(lon + rng.uniform(-0.15, 0.15), 4),
                }
            )

        staging.append(
            {
                "site_id": f"ST-{fips}",
                "site_name": f"{county} County Logistics Point",
                "county": county,
                "lat": round(lat + rng.uniform(-0.1, 0.1), 4),
                "lon": round(lon + rng.uniform(-0.1, 0.1), 4),
                "truck_capacity": rng.randrange(6, 20, 2),
            }
        )

    # One shelter starts effectively full, so the capacity loop has something
    # real to resolve rather than a staged failure.
    for row in shelters:
        if row["county"] == "Lee" and row["shelter_id"].endswith("-1"):
            row["current_occupancy"] = row["capacity"]
    _ = zones
    return {"shelters": shelters, "hospitals": hospitals, "staging_sites": staging}


def build_road_events() -> list[dict]:
    return [
        {
            "event_id": "RD-001",
            "route": "I-75 southbound ramp at Corkscrew Rd",
            "county": "Lee",
            "status": "closed",
            "lat": 26.4361,
            "lon": -81.7795,
            "reported_at": "2022-09-27T18:00Z",
            "note": "SYNTHETIC event, generated for this demo",
        },
        {
            "event_id": "RD-002",
            "route": "Sanibel Causeway",
            "county": "Lee",
            "status": "closed",
            "lat": 26.4615,
            "lon": -82.0106,
            "reported_at": "2022-09-28T02:00Z",
            "note": "SYNTHETIC event, generated for this demo",
        },
    ]


def build_declarations() -> list[dict]:
    return [
        {
            "disaster_number": 4673,
            "declaration_date": "2022-09-29",
            "incident_type": "Hurricane",
            "county": county,
        }
        for county in COUNTY_CENTRES
    ]


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"  wrote {path.relative_to(ROOT)}")


def write_cache(name: str, rows: list[dict], note: str) -> None:
    write_json(
        CACHE / f"{name}.json",
        {
            "query_name": name,
            "recorded_from": "scripts/build_replay.py",
            "note": note,
            "columns": list(rows[0].keys()) if rows else [],
            "rows": rows,
        },
    )


def main() -> int:
    rng = random.Random(SEED)
    print("Building Ian replay data")

    advisories = build_advisories()
    zones = build_zones(rng)
    facilities = build_facilities(rng, zones)
    road_events = build_road_events()
    declarations = build_declarations()

    write_json(REPLAY / "ian_advisories.json", advisories)
    write_json(REPLAY / "zones.json", zones)
    write_json(REPLAY / "facilities.json", facilities)
    write_json(REPLAY / "road_events.json", road_events)

    print("Recording SAMPLE_MODE cache")
    # The cache holds the whole advisory track. The named query declares
    # `issued_at <= replay_hour ORDER BY issued_at DESC LIMIT 1`, so each replay
    # hour picks its own advisory out of this superset exactly as Zetaris would.
    write_cache(
        "storm_advisory_latest",
        [{k: v for k, v in a.items() if k != "source"} for a in advisories],
        "full advisory track; the query selects the newest at or before the replay hour",
    )
    write_cache("zone_vulnerability", zones, "SYNTHETIC ACS-shaped tract statistics")
    write_cache(
        "shelters_by_county",
        facilities["shelters"],
        "SYNTHETIC FEMA-NSS-shaped shelter records, two census snapshots per shelter",
    )
    write_cache("hospitals_by_county", facilities["hospitals"], "SYNTHETIC HIFLD-shaped hospital records")
    write_cache("staging_sites", facilities["staging_sites"], "SYNTHETIC state logistics points")
    write_cache("road_events_open", road_events, "SYNTHETIC closures; the query filters by replay hour")
    write_cache("disaster_declarations", declarations, "OpenFEMA DR-4673 county list for Hurricane Ian")
    write_cache("health_select_1", [{"one": 1}], "connectivity probe")

    computed = sorted(
        ((vulnerability_score(z), z["zone_id"], z["county"]) for z in zones), reverse=True
    )
    print("\nMost vulnerable zones:")
    for score, zone_id, county in computed[:5]:
        print(f"  {zone_id} {county:<13} {score:.3f}")
    print(f"\n{len(zones)} zones, {len(facilities['shelters'])} shelters, "
          f"{len(facilities['staging_sites'])} staging sites, {len(advisories)} advisories")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
