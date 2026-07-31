# Spec 03: Core Express API & Deterministic Rule Engine

## Objective
Build a Node.js/Express backend API to serve the telemetry data collected by the daemon, and implement a Deterministic Rule Engine to analyze PostgreSQL execution metrics for obvious structural flaws.

## Core Requirements

### 1. Express API Setup (`backend/`)
* Install `express`, `cors`, and their respective TypeScript types.
* Configure a clean server entry point (`src/index.ts`) running on port `3000`.
* Initialize a singleton Prisma Client instance to interact with `dblens-meta-db`.

### 2. Deterministic Rule Engine (`backend/src/rules/`)
Create a rule engine that analyzes a `MetricsHistory` row and returns an array of structural violations.
Implement the following rules:
* **High Latency Rule:** If `totalExecTimeMs / callCount > 100` (Average execution time exceeds 100ms), flag as `HIGH_LATENCY`.
* **Sequential Scan Risk:** If the `normalizedQuery` contains the keyword `seq_scan` or `SELECT * FROM` without a `WHERE` or `LIMIT` clause, flag as `POSSIBLE_SEQ_SCAN`.
* **High Frequency Rule:** If `callCount > 1000` in a single 60-second snapshot, flag as `HIGH_FREQUENCY_QUERY`.

### 3. API Endpoints
Implement the following REST endpoints in the Express application:
* `GET /api/health`: Returns 200 OK and DB connection status.
* `GET /api/databases`: Returns a list of all monitored `TargetDatabase` records.
* `GET /api/databases/:id/metrics`: 
  * Fetches the latest `MetricsHistory` for the given database ID.
  * Passes each metric row through the Deterministic Rule Engine.
  * Returns a JSON payload containing raw metrics appended with an `analysis` array containing triggered rule flags.

### 4. Infrastructure Updates
* Update `docker-compose.yml` to include a new `backend-api` service.
* Build from the `backend/` directory using a Node.js Dockerfile.
* Expose port `3000` to the host machine.
* Ensure `backend-api` depends on `dblens-meta-db` being healthy.

## Verification Protocol
1. Run `docker compose up --build -d`.
2. Verify the `backend-api` container is running and healthy.
3. Execute `curl http://localhost:3000/api/databases` to confirm data retrieval.
4. Execute `curl http://localhost:3000/api/databases/<id>/metrics` to confirm rule flags are attached.

## Handoff
Upon successful verification, generate `specs/04_api_complete_ledger.md` documenting the endpoints, implemented rules, and `curl` test results.