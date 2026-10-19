-- DESIGN SKETCH ONLY. NOT RUN, NOT TESTED. This repository's code targets SQLite.
-- It shows how the same "migrations run with least privilege" idea is usually expressed
-- in PostgreSQL, where real roles and GRANTs exist. Names are fictional.

CREATE ROLE migrator_owner NOLOGIN;          -- owns the application schema objects
CREATE ROLE migrator_runner LOGIN PASSWORD NULL;  -- what the CI job connects as (use a secret store)
GRANT migrator_owner TO migrator_runner;

-- The runner may create and alter objects in its schema, but nothing else in the cluster.
GRANT USAGE, CREATE ON SCHEMA app TO migrator_owner;
REVOKE ALL ON SCHEMA public FROM migrator_runner;

-- History is append-only for everyone, including the runner.
GRANT SELECT, INSERT ON app.schema_migrations TO migrator_runner;
REVOKE UPDATE, DELETE, TRUNCATE ON app.schema_migrations FROM migrator_runner;

-- Destructive steps: run them from a separate, short-lived role that a human grants for one
-- deploy, instead of an in-file directive; the file's checksum still records the intent.
