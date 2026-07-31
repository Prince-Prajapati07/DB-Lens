# Spec 02: Daemon Completion Ledger

## Completed Scope

Spec 01 is implemented end-to-end for the DB-Lens telemetry foundation.

The repository now contains:

- `backend/` with a minimal Node.js/TypeScript Prisma project.
- `backend/prisma/schema.prisma` with `TargetDatabase` and `MetricsHistory`.
- `backend/prisma/migrations/20260728000000_init/migration.sql` for non-interactive deployment.
- `daemon/` with a Python 3.12 telemetry worker using `psycopg[binary]`, `schedule`, and `python-dotenv`.
- Root-level `docker-compose.yml` for the metadata database, simulated target database, Prisma migration job, and telemetry daemon.
- `docker/postgres/init-simulated-prod.sql` to initialize sample target data and create `pg_stat_statements`.

## System Decisions

The metadata schema is owned by Prisma, but the telemetry daemon writes with native `psycopg` as required by Spec 01. Because raw daemon inserts do not use Prisma Client's client-side `uuid()` behavior, the daemon generates UUID values explicitly for `TargetDatabase.id` and `MetricsHistory.id`.

The compose stack includes a short-lived `prisma-migrate` service. This keeps schema deployment separate from the Postgres container and avoids mixing migration logic into the database image. `telemetry-daemon` starts only after the migration service exits successfully.

The simulated production database is started with:

```text
shared_preload_libraries=pg_stat_statements
pg_stat_statements.track=all
```

The target init SQL also runs:

```sql
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
```

Host ports were set to `55433` for the metadata database and `55434` for the simulated target database because `5433` was already allocated on this development machine. Internal Docker service URLs still use port `5432`.

## Runtime Services

`dblens-meta-db`

- Image: `postgres:16`
- Database: `dblens`
- User/password: `dblens` / `dblens`
- Host access: `localhost:55433`
- Purpose: central DB-Lens metadata store.

`simulated-prod-db`

- Image: `postgres:16`
- Database: `appdb`
- User/password: `app` / `app`
- Host access: `localhost:55434`
- Purpose: simulated target PostgreSQL database with `pg_stat_statements`.

`prisma-migrate`

- Build context: `backend/`
- Command: `npx prisma migrate deploy`
- Purpose: applies Prisma migrations before telemetry starts.

`telemetry-daemon`

- Build context: `daemon/`
- Command: `python main.py`
- Poll interval: `60` seconds
- Purpose: reads top `pg_stat_statements` rows from the target and writes snapshots into `MetricsHistory`.

## Package Parameters

Backend package versions are locked in `backend/package-lock.json`.

Primary backend packages:

- `@prisma/client`
- `prisma`
- `typescript`

Daemon dependencies:

- `psycopg[binary]`
- `schedule`
- `python-dotenv`

The backend migration Docker image installs `openssl` and `ca-certificates` before `npm ci` so Prisma can detect OpenSSL cleanly in the container.

## Daemon Behavior

On startup, the daemon runs one immediate telemetry tick, then schedules a tick every 60 seconds.

Each tick:

1. Connects to the target database.
2. Executes the exact Spec 01 `pg_stat_statements` extraction query.
3. Connects to the metadata database.
4. Finds or creates the matching `TargetDatabase` row.
5. Batch inserts snapshot rows into `MetricsHistory`.

Target and metadata database operations are wrapped in `try-except` handling. If either database is unavailable, the daemon logs a warning and waits for the next scheduled tick instead of crashing.

## Verification Results

Commands executed successfully:

```bash
npm install
DATABASE_URL='postgresql://dblens:dblens@localhost:55433/dblens?schema=public' npx prisma validate
npm run prisma:generate
python3 -m py_compile daemon/main.py
docker compose config
docker --context default compose up --build -d
```

The local Docker default context was used because Docker Desktop's `desktop-linux` context was inactive.

Verified service state:

- `dblens-meta-db` running and healthy.
- `simulated-prod-db` running and healthy.
- `prisma-migrate` completed successfully.
- `telemetry-daemon` running.

Verified target database configuration:

```text
shared_preload_libraries = pg_stat_statements
pg_stat_rows = 35
```

Verified migration behavior:

```text
1 migration found in prisma/migrations
No pending migrations to apply.
```

Verified telemetry ingestion:

```text
Fetched 35 pg_stat_statements rows from target
Inserted 35 metrics history rows
Telemetry tick completed
```

Verified metadata rows:

```text
TargetDatabase count: 1
MetricsHistory count: 137
First snapshot: 2026-07-28 10:49:32.495
Latest snapshot: 2026-07-28 10:52:36.109
```

## Handoff Notes

No AI analyzers or dashboards were added in this phase.

Credentials are stored as plain `connectionUri` values for now because Spec 01 explicitly notes encryption is for a later phase.

Future specs should treat the telemetry pipeline as the first stable spine of DB-Lens: metadata schema, target polling, Dockerized local target, and raw interval history are now available.

## Real Target Verification Update

On 2026-07-30, the daemon was tested against a real Supabase PostgreSQL target using the Supabase IPv4 session pooler URL supplied through root `.env`.

Compose was adjusted so `telemetry-daemon` explicitly reads root `.env` through `env_file`, while still preserving defaults for local sandbox mode.

The direct Supabase database URL was not usable from this Docker environment because it resolved to IPv6 only. The IPv4 session pooler URL succeeded.

Verified daemon log sequence:

```text
Resolved target host to IPv4
Fetched 50 pg_stat_statements rows from target
Registered target database supabase-hepubqljjpdbzcsdrzol
Inserted 50 metrics history rows
Telemetry tick completed
```

Recurring scheduler verification:

```text
Supabase target metric rows: 200
Distinct snapshots: 4
Latest snapshot: 2026-07-30 17:25:50.004
```
