-- =============================================================================
-- Pulsar 5 — Lightning database and schemastore container.
-- Run first. Every other file in this directory assumes these exist.
--
-- Pattern copied from the Zetaris getting-started PokeAPI recipe:
--   ../getting-started/open_data/rest_apis/sql/non_rate_limited/02_pokeapi_create.sql
--
-- Send one statement at a time. scripts/register_sources.py does that for you.
-- =============================================================================

CREATE LIGHTNING DATABASE PULSAR5_REST DESCRIBE BY "Pulsar 5 disaster response REST sources";

CREATE SCHEMASTORE CONTAINER pulsar5;
