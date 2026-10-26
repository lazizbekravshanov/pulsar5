-- =============================================================================
-- Source: OpenFEMA DisasterDeclarationsSummaries.
--   https://www.fema.gov/api/open/v2/DisasterDeclarationsSummaries
-- Used for context and to validate that the region is in scope. Ian is DR-4673.
--
-- Licence: FEMA open data, US Government work. No key required.
-- Format: JSON object with a DisasterDeclarationsSummaries array.
-- =============================================================================

CREATE LIGHTNING REST TABLE fema_declarations_raw FROM PULSAR5_REST REQUEST(
    endpoint "https://www.fema.gov/api/open/v2/DisasterDeclarationsSummaries?%24filter=state%20eq%20%27FL%27%20and%20incidentType%20eq%20%27Hurricane%27&%24top=1000",
    method "get",
    response_type "json",
    http_encoding "URLENCODED"
) HEADER (
) BODY ();

CREATE SCHEMASTORE VIEW disaster_declarations WITH CONTAINER pulsar5 AS
SELECT
    declaration.disasterNumber    AS disaster_number,
    declaration.declarationDate   AS declaration_date,
    declaration.incidentType      AS incident_type,
    declaration.designatedArea    AS county
FROM fema_declarations_raw
LATERAL VIEW explode(DisasterDeclarationsSummaries) AS declaration;
