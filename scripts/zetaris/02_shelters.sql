-- =============================================================================
-- Source: shelters.
--
-- LIVE ATTEMPT (kept for the record, not used): FEMA ESF#6 National Shelter
-- System, https://gis.fema.gov/arcgis/rest/services/NSS/FEMA_NSS/FeatureServer/0/query
-- Registered successfully on 2026-10-26 and returned HTTP 200, but with an
-- EMPTY feature set:
--     {"features":"[]","geometryType":"esriGeometryPoint",...}
-- The service answers, so the `where=state='FL'` predicate matches nothing —
-- that NSS layer does not carry a `state` field by that name. Resolving it
-- needs a field listing from the live service, which is a pre-demo task, not
-- something to guess at.
--
-- IN USE: the committed facilities extract, served from this repository's raw
-- GitHub URL and registered as a Lightning REST table, exactly like the storm
-- advisories. Still a source registered in Zetaris and queried through its
-- REST API. The data is SYNTHETIC and labelled as such in data/replay/ and in
-- the README; it was always synthetic in SAMPLE_MODE, so nothing is lost by
-- making live mode read the same rows.
--
-- Two census snapshots per shelter (06:00 on the 26th, 18:00 on the 27th);
-- the Analyst selects the newest at or before the replay hour.
-- =============================================================================

CREATE SCHEMASTORE VIEW shelters WITH CONTAINER pulsar5 AS
SELECT
    shelter.shelter_id        AS shelter_id,
    shelter.shelter_name      AS shelter_name,
    shelter.county            AS county,
    shelter.capacity          AS capacity,
    shelter.current_occupancy AS current_occupancy,
    shelter.status            AS status,
    shelter.lat               AS lat,
    shelter.lon               AS lon,
    shelter.updated_at        AS updated_at
FROM PULSAR5_REST.facilities_raw
LATERAL VIEW explode(shelters) AS shelter;
