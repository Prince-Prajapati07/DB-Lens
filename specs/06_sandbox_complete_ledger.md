# Spec 06: HypoPG Sandbox Completion Ledger

## Completed Scope

Spec 05 is implemented and verified.

The backend now contains a remediation engine that listens for `POSSIBLE_SEQ_SCAN` rule flags, extracts candidate table and predicate columns from the normalized SQL, generates candidate `CREATE INDEX` statements, and validates estimated cost reduction through a local HypoPG sandbox.

Implemented files:

- `docker/postgres-meta/Dockerfile`
- `backend/prisma/migrations/20260802000000_enable_hypopg/migration.sql`
- `backend/src/analyzer/remediation-engine.ts`
- Updated `backend/src/index.ts`
- Updated `docker-compose.yml`

## HypoPG Setup

The metadata database service now builds from a local PostgreSQL image that installs:

```text
postgresql-16-hypopg
```

The Prisma migration enables:

```sql
CREATE EXTENSION IF NOT EXISTS hypopg;
```

Verification result:

```text
extname | extversion
hypopg  | 1.4.3
```

## Remediation Engine Behavior

Implementation:

```text
backend/src/analyzer/remediation-engine.ts
```

The engine runs only when the deterministic rule output includes:

```text
POSSIBLE_SEQ_SCAN
```

It attempts to:

1. Parse the target table from the `FROM` clause.
2. Parse candidate columns from `WHERE` and `JOIN ... ON` predicates.
3. Generate a candidate `CREATE INDEX` statement.
4. Create a temporary local sandbox table in the metadata DB session.
5. Run `EXPLAIN (FORMAT JSON)` before the virtual index.
6. Create a virtual HypoPG index through `hypopg_create_index`.
7. Run `EXPLAIN (FORMAT JSON)` after the virtual index.
8. Calculate estimated cost reduction percentage.

The endpoint appends remediation data only when the engine can parse a credible index candidate and successfully simulate it.

Response shape:

```json
{
  "remediation": {
    "proposed_sql": "CREATE INDEX ...",
    "estimated_cost_reduction_percentage": 72.41,
    "estimated_cost_before": 847,
    "estimated_cost_after": 233.68,
    "sandbox_note": "HypoPG estimate was calculated on a local temporary sandbox table shaped from parsed query predicates."
  }
}
```

## API Update

Endpoint updated:

```text
GET /api/databases/:id/metrics
```

Existing fields are preserved. Metrics may now include a `remediation` object when a remediation exists.

BigInt fields still serialize as strings.

## Verification Commands

Build and restart:

```bash
npm run build
docker --context default compose up --build -d --remove-orphans
```

Health:

```bash
curl -sS http://localhost:3000/api/health
```

Result:

```json
{"ok":true,"database":"connected"}
```

HypoPG:

```bash
docker --context default compose exec -T dblens-meta-db \
  psql -U dblens -d dblens \
  -c "SELECT extname, extversion FROM pg_extension WHERE extname = 'hypopg';"
```

Result:

```text
hypopg | 1.4.3
```

## Sandbox Remediation Verification

A synthetic metadata metric was inserted for the existing `simulated-prod-db` target to verify the full remediation path with an indexable query:

```sql
SELECT * FROM orders WHERE customer_id = 42 /* seq_scan */
```

API verification:

```text
GET /api/databases/ededb422-b5f0-441b-b37b-f7b0195cf083/metrics
```

Observed summary:

```json
{
  "database": "simulated-prod-db",
  "metricCount": 1,
  "remediationCount": 1,
  "firstRemediation": {
    "queryHash": "sandbox-seq-scan-orders",
    "analysis": ["POSSIBLE_SEQ_SCAN"],
    "remediation": {
      "proposed_sql": "CREATE INDEX \"idx_dblens_orders_customer_id\" ON \"orders\" (\"customer_id\");",
      "estimated_cost_reduction_percentage": 72.41,
      "estimated_cost_before": 847,
      "estimated_cost_after": 233.68
    }
  }
}
```

## Real Target Behavior

The real Supabase target was also checked:

```text
GET /api/databases/4a7b5258-a0ff-49b9-9a23-b698b5e92987/metrics
```

Observed summary:

```json
{
  "database": "supabase-hepubqljjpdbzcsdrzol",
  "snapshotTime": "2026-08-02T12:22:28.428Z",
  "metricCount": 50,
  "ruleCounts": {
    "HIGH_LATENCY": 6,
    "POSSIBLE_SEQ_SCAN": 2,
    "HIGH_FREQUENCY_QUERY": 5
  },
  "remediationCount": 0
}
```

This is expected. The flagged real-target `POSSIBLE_SEQ_SCAN` queries are system/function-style queries where the parser cannot identify a credible table-and-column index target. The engine intentionally skips these instead of fabricating bad index advice.

## Engineering Notes

HypoPG state is session-scoped, so remediation simulations run sequentially inside the Prisma transaction. This avoids cross-query interference from `hypopg_reset()` and virtual index state.

The sandbox simulation is intentionally conservative. It validates the mechanics of proposed indexes, but it does not yet run against a schema clone of the real target database. Future versions should introspect real target schemas and build a closer local sandbox or run HypoPG directly against a safe read-only target replica.
