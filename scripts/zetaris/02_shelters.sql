-- =============================================================================
-- Source: FEMA ESF#6 National Shelter System (NSS), ArcGIS REST FeatureServer.
--   https://gis.fema.gov/arcgis/rest/services/NSS/FEMA_NSS/FeatureServer/0/query
-- Open shelters only are also published at NSS/OpenShelters.
--
-- Licence: FEMA open data, US Government work.
-- Format: ArcGIS REST JSON. Attributes arrive under features[].attributes, so
-- the view explodes `features` and reads one level down.
-- Limits: no key required; 4000 records per request. Filtered to the five
-- Florida counties in scope.
--
-- VERIFY BEFORE THE DEMO: ArcGIS field names differ between NSS layers. Check
-- the live payload and adjust the SELECT if a column comes back null:
--   curl -s "https://gis.fema.gov/arcgis/rest/services/NSS/FEMA_NSS/FeatureServer/0/query?where=1%3D1&outFields=*&resultRecordCount=1&f=json"
-- Falls back to the committed extract (03_shelters_fallback.sql) if the service
-- is unavailable, which it was for part of 2025.
-- =============================================================================

CREATE LIGHTNING REST TABLE shelters_nss_raw FROM PULSAR5_REST REQUEST(
    endpoint "https://gis.fema.gov/arcgis/rest/services/NSS/FEMA_NSS/FeatureServer/0/query?where=state%3D%27FL%27&outFields=*&f=json",
    method "get",
    response_type "json",
    http_encoding "URLENCODED"
) HEADER (
) BODY ();

CREATE SCHEMASTORE VIEW shelters WITH CONTAINER pulsar5 AS
SELECT
    feature.attributes.shelter_id            AS shelter_id,
    feature.attributes.shelter_name          AS shelter_name,
    feature.attributes.county                AS county,
    feature.attributes.evacuation_capacity   AS capacity,
    feature.attributes.total_population      AS current_occupancy,
    feature.attributes.shelter_status         AS status,
    feature.attributes.latitude              AS lat,
    feature.attributes.longitude             AS lon,
    feature.attributes.last_updated          AS updated_at
FROM shelters_nss_raw
LATERAL VIEW explode(features) AS feature
WHERE feature.attributes.county IN ('Lee', 'Charlotte', 'Sarasota', 'Pinellas', 'Hillsborough');
