-- Create the dedicated read-only database user for the TeslaMate MCP server.
--
-- Run ONCE against the TeslaMate database, as the database owner (the same
-- user TeslaMate itself connects with), e.g.:
--
--   psql -U teslamate -d teslamate -f create_readonly_user.sql
--
-- Replace the placeholder password below with a long random value
-- (openssl rand -hex 24) and put the SAME value in the MCP server's .env as
-- TESLAMATE_DB_PASSWORD. Never commit the real password to git.

CREATE USER teslamate_mcp WITH PASSWORD 'CHANGE_ME';

GRANT CONNECT ON DATABASE teslamate TO teslamate_mcp;
GRANT USAGE ON SCHEMA public TO teslamate_mcp;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO teslamate_mcp;

-- Keep future tables (created by TeslaMate upgrades) readable too.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT ON TABLES TO teslamate_mcp;
