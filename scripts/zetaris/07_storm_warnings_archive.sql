-- =============================================================================
-- Source: Iowa Environmental Mesonet (IEM) archive of NWS storm-based warnings.
--   https://mesonet.agron.iastate.edu/json/
--
-- Why this and not api.weather.gov/alerts: the NWS alerts API keeps roughly
-- seven days and requires a User-Agent header, which a Lightning REST table
-- cannot set. The IEM archive reaches back to ~2002 and needs no header, so it
-- is the right source for a 2022 replay. We do not bypass Zetaris to get it.
--
-- Licence: IEM data is freely redistributable; it republishes NWS products,
-- which are US Government works.
--
-- VERIFY BEFORE THE DEMO: confirm the exact service name and parameters at
-- https://mesonet.agron.iastate.edu/json/ — the endpoint below is the SBW
-- service shape and the field names should be checked against a live response.
-- Live mode uses this; SAMPLE_MODE does not depend on it.
-- =============================================================================

CREATE LIGHTNING REST TABLE iem_sbw_raw FROM PULSAR5_REST REQUEST(
    endpoint "https://mesonet.agron.iastate.edu/json/sbw_by_point.py?lat=26.64&lon=-81.87&sdate=2022-09-26&edate=2022-09-30",
    method "get",
    response_type "json",
    http_encoding "URLENCODED"
) HEADER (
) BODY ();

CREATE SCHEMASTORE VIEW storm_warnings_archive WITH CONTAINER pulsar5 AS
SELECT
    warning.wfo          AS office,
    warning.phenomena    AS phenomenon,
    warning.significance AS significance,
    warning.eventid      AS event_id,
    warning.issue        AS issued_at,
    warning.expire       AS expires_at
FROM iem_sbw_raw
LATERAL VIEW explode(sbws) AS warning;
