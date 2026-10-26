-- =============================================================================
-- Source: HIFLD Open hospitals. HIFLD Open was deactivated on 26 Aug 2025, so
-- this reads the NASA NCCS mirror:
--   https://maps.nccs.nasa.gov/mapping/rest/services/hifld_open/public_health/FeatureServer
--
-- Licence: HIFLD Open was public domain; the NCCS mirror republishes it.
-- Format: ArcGIS REST JSON, features[].attributes.
-- If the mirror fails, commit a small Florida extract and note the licence.
-- =============================================================================

CREATE LIGHTNING REST TABLE hospitals_raw FROM PULSAR5_REST REQUEST(
    endpoint "https://maps.nccs.nasa.gov/mapping/rest/services/hifld_open/public_health/FeatureServer/0/query?where=STATE%3D%27FL%27&outFields=*&f=json",
    method "get",
    response_type "json",
    http_encoding "URLENCODED"
) HEADER (
) BODY ();

CREATE SCHEMASTORE VIEW hospitals WITH CONTAINER pulsar5 AS
SELECT
    feature.attributes.ID         AS hospital_id,
    feature.attributes.NAME       AS hospital_name,
    feature.attributes.COUNTY     AS county,
    feature.attributes.BEDS       AS beds,
    feature.attributes.TRAUMA     AS trauma_level,
    feature.attributes.LATITUDE   AS lat,
    feature.attributes.LONGITUDE  AS lon
FROM hospitals_raw
LATERAL VIEW explode(features) exploded AS feature
WHERE feature.attributes.COUNTY IN ('LEE', 'CHARLOTTE', 'SARASOTA', 'PINELLAS', 'HILLSBOROUGH');
