-- =============================================================================
-- Source: Hurricane Ian forecast advisories (positions, intensity, cone).
-- Served as a small JSON file from this repository's raw GitHub URL, because
-- Zetaris cannot read a file on a laptop.
--
-- Provenance: the track and the Tampa-then-south forecast shift follow the
-- public NHC advisory archive for Ian (Sept 2022). Positions are rounded and
-- the cones are simplified polygons. Regenerate with:
--     python scripts/build_replay.py
--
-- Licence: NHC/NWS products are US Government works, public domain.
-- Format: JSON array of objects, one per advisory; cone_polygon is an array of
-- [lat, lon] pairs.
-- =============================================================================

CREATE LIGHTNING REST TABLE storm_advisories_raw FROM PULSAR5_REST REQUEST(
    endpoint "${RAW_BASE}/data/replay/ian_advisories.json",
    method "get",
    response_type "json",
    http_encoding "URLENCODED"
) HEADER (
) BODY ();

CREATE SCHEMASTORE VIEW storm_advisories WITH CONTAINER pulsar5 AS
SELECT
    advisory_number,
    issued_at,
    center_lat,
    center_lon,
    max_wind_kt,
    forecast_landfall_county,
    cone_polygon
FROM PULSAR5_REST.storm_advisories_raw;
