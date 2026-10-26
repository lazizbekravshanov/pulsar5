-- =============================================================================
-- Source: hospitals.
--
-- LIVE ATTEMPT (kept for the record, not used): HIFLD Open via the NASA NCCS
-- mirror. HIFLD Open was deactivated on 26 Aug 2025 and the mirror would not
-- register as a REST table on 2026-10-26.
--
-- IN USE: the committed facilities extract, as for shelters. SYNTHETIC and
-- labelled. Hospitals are an OPTIONAL source in app/agents/analyst.py, so a
-- run degrades rather than abstains without them.
-- =============================================================================

CREATE SCHEMASTORE VIEW hospitals WITH CONTAINER pulsar5 AS
SELECT
    hospital.hospital_id   AS hospital_id,
    hospital.hospital_name AS hospital_name,
    hospital.county        AS county,
    hospital.beds          AS beds,
    hospital.trauma_level  AS trauma_level,
    hospital.lat           AS lat,
    hospital.lon           AS lon
FROM PULSAR5_REST.facilities_raw
LATERAL VIEW explode(hospitals) AS hospital;
