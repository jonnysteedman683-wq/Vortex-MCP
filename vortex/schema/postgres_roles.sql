-- Least-privilege runtime role for the Vortex server: row access in the Vortex schema, no DDL.
-- Created NOLOGIN; the owner grants a login role membership (or LOGIN with its own password) in Supabase.
-- {schema} is substituted by vortex.migrate after the name is validated.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vortex_writer') THEN
        CREATE ROLE vortex_writer NOLOGIN;
    END IF;
END
$$;
GRANT USAGE ON SCHEMA "{schema}" TO vortex_writer;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "{schema}" TO vortex_writer;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA "{schema}" TO vortex_writer;
ALTER DEFAULT PRIVILEGES IN SCHEMA "{schema}" GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO vortex_writer;
ALTER DEFAULT PRIVILEGES IN SCHEMA "{schema}" GRANT USAGE, SELECT ON SEQUENCES TO vortex_writer;
