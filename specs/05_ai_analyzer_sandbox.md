# Spec 05: HypoPG Sandbox & Automated SQL Remediation

## Objective
Implement a virtual index sandbox using the `hypopg` PostgreSQL extension on the local metadata/sandbox database, and build backend logic to generate and test indexing strategies for flagged queries.

## Core Requirements

### 1. Database Sandbox Preparation
* Update `dblens-meta-db` initialization/Docker setup to install the `hypopg` extension.
* Ensure the Express API can execute virtual index commands (`SELECT * FROM hypopg_create_index(...)`) against this database.

### 2. Remediation Engine (`backend/src/analyzer/`)
* Create a service that listens for `POSSIBLE_SEQ_SCAN` flags from the Deterministic Rule Engine.
* Parse the `normalizedQuery` to identify the target table and `WHERE`/`JOIN` clause columns.
* Generate a candidate `CREATE INDEX` SQL string.

### 3. Verification Pipeline
* Execute an `EXPLAIN` statement on the query *before* creating the virtual index.
* Create the virtual index using `hypopg`.
* Execute an `EXPLAIN` statement *after* creating the virtual index.
* Calculate the percentage reduction in estimated query execution cost.

### 4. API Update
* Modify `GET /api/databases/:id/metrics` so that if a remediation exists for a query, append a `remediation` object containing `proposed_sql` and `estimated_cost_reduction_percentage`.

## Verification Protocol
1. Rebuild and restart the Docker stack.
2. Verify `hypopg` is active on the metadata database.
3. Call the API metrics endpoint and confirm the JSON payload contains the proposed `CREATE INDEX` string and cost reduction metrics.

## Handoff
Upon successful verification, generate `specs/06_sandbox_complete_ledger.md` documenting the engine's performance and `curl` test results.