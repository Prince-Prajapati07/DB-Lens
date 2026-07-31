# Spec 04: API Completion Ledger

## Completed Scope

Spec 03 is implemented end-to-end.

The repository now includes a TypeScript Express API in `backend/` that reads daemon telemetry from the DB-Lens metadata PostgreSQL database through Prisma.

Implemented files:

- `backend/src/index.ts`
- `backend/src/lib/prisma.ts`
- `backend/src/rules/rule-engine.ts`
- Updated `backend/package.json`
- Updated `backend/Dockerfile`
- Updated root `docker-compose.yml`

## API Runtime

Service name:

```text
backend-api
```

Host port:

```text
3000
```

Container command:

```text
npm start
```

Database URL:

```text
postgresql://dblens:dblens@dblens-meta-db:5432/dblens?schema=public
```

The service depends on:

- `dblens-meta-db` healthy
- `prisma-migrate` completed successfully

The service has an HTTP healthcheck against:

```text
GET /api/health
```

## Endpoints

`GET /api/health`

Returns API status and Prisma metadata database connectivity.

Example response:

```json
{"ok":true,"database":"connected"}
```

`GET /api/databases`

Returns monitored target databases. Stored connection URIs are returned with passwords redacted.

Example response shape:

```json
{
  "databases": [
    {
      "id": "4a7b5258-a0ff-49b9-9a23-b698b5e92987",
      "name": "supabase-hepubqljjpdbzcsdrzol",
      "connectionUri": "postgresql://postgres.hepubqljjpdbzcsdrzol:REDACTED@aws-1-ap-northeast-2.pooler.supabase.com:5432/postgres",
      "createdAt": "2026-07-30T17:22:37.938Z"
    }
  ]
}
```

`GET /api/databases/:id/metrics`

Returns the latest snapshot of `MetricsHistory` rows for the requested database. Each metric includes an `analysis` array with deterministic rule flags.

Example summarized response:

```json
{
  "database": "supabase-hepubqljjpdbzcsdrzol",
  "snapshotTime": "2026-07-31T10:34:59.109Z",
  "metricCount": 50,
  "firstMetric": {
    "queryHash": "-2647655532108368607",
    "callCount": "21",
    "totalExecTimeMs": 9541.270563,
    "analysis": ["HIGH_LATENCY"]
  }
}
```

## Deterministic Rule Engine

Implemented in:

```text
backend/src/rules/rule-engine.ts
```

Rules:

`HIGH_LATENCY`

Triggers when:

```text
totalExecTimeMs / callCount > 100
```

`POSSIBLE_SEQ_SCAN`

Triggers when:

```text
normalizedQuery contains seq_scan
```

or when:

```text
normalizedQuery contains SELECT * FROM without WHERE or LIMIT
```

`HIGH_FREQUENCY_QUERY`

Triggers when:

```text
callCount > 1000
```

The live Supabase snapshot tested on 2026-07-31 produced:

```json
{
  "metricCount": 50,
  "ruleCounts": {
    "HIGH_LATENCY": 6,
    "POSSIBLE_SEQ_SCAN": 2
  }
}
```

No `HIGH_FREQUENCY_QUERY` flag appeared in the latest live snapshot because no query exceeded 1000 calls in that 60-second interval.

## Verification

Local checks:

```bash
npm install
npm run prisma:generate
npm run build
docker compose config
```

Docker startup:

```bash
docker --context default compose up --build -d --remove-orphans
```

Container status:

```text
backend-api        Up, healthy, 0.0.0.0:3000->3000/tcp
dblens-meta-db     Up, healthy, 0.0.0.0:55433->5432/tcp
telemetry-daemon   Up
```

Curl checks:

```bash
curl -sS http://localhost:3000/api/health
curl -sS http://localhost:3000/api/databases
curl -sS http://localhost:3000/api/databases/4a7b5258-a0ff-49b9-9a23-b698b5e92987/metrics
```

Observed results:

- `/api/health` returned `ok: true` and `database: connected`.
- `/api/databases` returned both known targets with redacted connection URI passwords.
- `/api/databases/:id/metrics` returned 50 metrics for the latest Supabase snapshot and attached deterministic rule flags.

## Handoff Notes

The API intentionally serializes PostgreSQL `BigInt` fields as strings so JSON responses remain valid.

The API intentionally redacts passwords in `connectionUri` responses because the current Spec 01 schema stores raw target URIs until a later encryption phase.

The backend Dockerfile now runs `npx prisma generate` after copying the Prisma schema. This is required so model types exist inside the Docker image before TypeScript compilation.
