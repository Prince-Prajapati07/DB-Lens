# Spec 01: Project Bootstrapping and Asynchronous Telemetry Daemon

## 1. Objective
Initialize a clean, monorepo workspace for `DB-Lens` (Database Intelligence Platform). Implement a lightweight, background Python telemetry daemon that connects to a target database, systematically polls performance snapshots from `pg_stat_statements`, and stores them over time into a central metadata PostgreSQL database managed via Prisma ORM.

---

## 2. Technical Stack & Environment
- **Workspace Strategy:** Standard decoupled folder layout (`/daemon`, `/backend`, `/specs`).
- **Telemetry Ingestion Worker:** Python 3.12+, `psycopg` (binary distribution), and `schedule` library.
- **Central Storage Layer:** PostgreSQL database managed via Node.js/TypeScript and Prisma ORM.
- **Target Instance Requirements:** A standard PostgreSQL instance with `pg_stat_statements` pre-installed and activated.

---

## 3. Reference Material Context
You have a separate, fully operational open-source clone of `OptimizeQL` on this local machine. 
- Inspect the connection utilities inside `OptimizeQL/backend/connectors/` to see how it initializes clean connection pools to target databases.
- Inspect `OptimizeQL/backend/core/` to understand standard credential loading mechanics.
*Note: Do not clone or copy their direct system. OptimizeQL is a reactive, manual paste-and-click tool; we are building an automated, asynchronous telemetry pipeline.*

---

## 4. Implementation Steps

### Step 4.1: Workspace & Relational Schema Setup
1. Create a `backend/` folder and initialize a standard Node.js/TypeScript configuration with Prisma.
2. Define the core database models inside `backend/prisma/schema.prisma` to track targeted infrastructures and metric intervals:

```prisma
datasource db {
  provider = "postgresql"
  url      = env("DATABASE_URL")
}

generator client {
  provider = "prisma-client-js"
}

model TargetDatabase {
  id             String           @id @default(uuid())
  name           String
  connectionUri  String           // Will be encrypted in later phases
  createdAt      DateTime         @default(now())
  metricsHistory MetricsHistory[]
}

model MetricsHistory {
  id               String         @id @default(uuid())
  databaseId       String
  database         TargetDatabase @relation(fields: [databaseId], references: [id], onDelete: Cascade)
  queryHash        String         // The queryid token extracted from pg_stat_statements
  normalizedQuery  String         // Stored structural query string
  callCount        BigInt
  totalExecTimeMs  Float
  rowsReturned     BigInt
  sharedBlksHit    BigInt
  sharedBlksRead   BigInt
  snapshotTime     DateTime       @default(now())

  @@index([queryHash, snapshotTime])
}

### Step 4.2: The Python Telemetry Daemon (`/daemon`)

Create a `/daemon` folder containing a structured `main.py` heartbeat file and a `requirements.txt` file.

In `requirements.txt`, declare these explicit dependencies:

- `psycopg[binary]`
- `schedule`
- `python-dotenv`

In `main.py`, orchestrate a stable execution loop that wakes up automatically every **60 seconds**.

On every operational tick, the daemon must execute the following automated steps:

1. Establish a secure database connection to the designated target database.

2. Run the exact extraction scan block below:

```sql
SELECT 
  queryid::text as query_hash,
  query,
  calls,
  total_exec_time,
  rows,
  shared_blks_hit,
  shared_blks_read
FROM pg_stat_statements
ORDER BY total_exec_time DESC
LIMIT 50;
```

3. Connect immediately to the DB-Lens Central Metadata Database using native `psycopg` drivers.

4. Safely check for or map the matching `TargetDatabase` ID, and batch insert the incoming performance intervals into the `MetricsHistory` database table.

5. Prevent app crashes: Enclose all database requests inside explicit `try-except` blocks. If either database goes offline, capture the warning, log it cleanly to stdout, and gracefully exit the context to await the next 60-second processing marker.

---

### Step 4.3: Local Infrastructure Integration (`docker-compose.yml`)

Create a single, absolute root-level `docker-compose.yml` to set up the development local sandbox.

**Service 1 (`dblens-meta-db`)**

A standard PostgreSQL engine container acting as the platform database.

Automatically push the Prisma schema migrations to it on deployment startup.

**Service 2 (`simulated-prod-db`)**

An isolated PostgreSQL engine container representing a simulated live database environment.

Use an entrypoint setup script (`.sql` or `.sh`) to ensure that when it boots, it automatically runs:

```sql
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
```

**Service 3 (`telemetry-daemon`)**

Mounts your `/daemon` directory workspace and automatically launches the continuous background Python data telemetry engine loop.

---

## 5. Verification Gate Criteria

To ensure this specification phase is complete, you must verify the following parameters:

- Executing `docker-compose up --build` boots all three system containers without dropping processing or configuration parameters.

- The telemetry loop correctly establishes data read states and systematically dumps metrics into the central `MetricsHistory` logging table.

- Accessing the metadata engine manually confirms that live performance statistics are accumulating over time from the tracking database.

---

## 6. Token Memory Directive

When you complete the full engineering execution and pass the validation loops for this specification file:

- Do **not** proceed to write AI analyzers or dashboards yet.

- Summarize every system decision, internal tooling adjustments, and package parameters directly into a new file named:

```text
specs/02_daemon_complete_ledger.md
```

so that future prompt files inherit a perfectly managed framework.