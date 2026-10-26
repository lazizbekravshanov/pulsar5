-- =============================================================================
-- Sources served from this repository's raw GitHub URL.
--
--   staging_sites  candidate supply staging points. SYNTHETIC.
--   road_events    road closures used for injected events. SYNTHETIC.
--
-- Both are clearly labelled synthetic in data/replay/ and in the README. They
-- exist because there is no public, stable, hour-resolution feed of Florida
-- evacuation-route closures for September 2022 that Zetaris can register.
-- Regenerate with: python scripts/build_replay.py
-- =============================================================================

CREATE LIGHTNING REST TABLE facilities_raw FROM PULSAR5_REST REQUEST(
    endpoint "${RAW_BASE}/data/replay/facilities.json",
    method "get",
    response_type "json",
    http_encoding "URLENCODED"
) HEADER (
) BODY ();

CREATE SCHEMASTORE VIEW staging_sites WITH CONTAINER pulsar5 AS
SELECT
    site.site_id        AS site_id,
    site.site_name      AS site_name,
    site.county         AS county,
    site.lat            AS lat,
    site.lon            AS lon,
    site.truck_capacity AS truck_capacity
FROM PULSAR5_REST.facilities_raw
LATERAL VIEW explode(staging_sites) AS site;

CREATE LIGHTNING REST TABLE road_events_raw FROM PULSAR5_REST REQUEST(
    endpoint "${RAW_BASE}/data/replay/road_events.json",
    method "get",
    response_type "json",
    http_encoding "URLENCODED"
) HEADER (
) BODY ();

CREATE SCHEMASTORE VIEW road_events WITH CONTAINER pulsar5 AS
SELECT
    event_id,
    route,
    county,
    status,
    lat,
    lon,
    reported_at
FROM PULSAR5_REST.road_events_raw;
