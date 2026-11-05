#!/bin/sh
# Runs once, when the database volume is first created.
#
# Migrations run as the OWNER (POSTGRES_USER). The running API connects as recon_app, which can
# read and write rows but cannot create, alter or drop tables, cannot TRUNCATE (which would
# bypass the audit log's no-edit trigger), and cannot disable that trigger.
set -e
psql -v ON_ERROR_STOP=1 -v app_pw="$APP_DB_PASSWORD" -v owner="$POSTGRES_USER" \
     --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
CREATE ROLE recon_app LOGIN PASSWORD :'app_pw' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
GRANT CONNECT ON DATABASE :"DBNAME" TO recon_app;
GRANT USAGE ON SCHEMA public TO recon_app;
-- Tables and sequences the migrations create later are covered automatically.
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO recon_app;
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO recon_app;
SQL
