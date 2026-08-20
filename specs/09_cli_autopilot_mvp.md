# Spec 09: PlanPatch CLI Autopilot (High-Impact Demo MVP)

## Objective

Build a lightweight, command-line-driven PostgreSQL performance autopilot that monitors query performance, calculates accurate real-time interval metrics, simulates virtual indexes on the target database using `hypopg`, and automatically generates a draft GitHub Pull Request containing a reversible migration script.

This specification intentionally removes all non-essential web infrastructure, authentication systems, dashboards, and multi-tenancy so the implementation focuses exclusively on a reliable end-to-end engineering demonstration.

---

# System Architecture

```text
                  +----------------------+
                  |  Target PostgreSQL   |
                  |      Database        |
                  +----------+-----------+
                             |
                             | Poll pg_stat_statements
                             v
                +-----------------------------+
                | PlanPatch CLI / Daemon      |
                | Interval Delta Calculator   |
                +-------------+---------------+
                              |
                              | Candidate Queries
                              v
                +-----------------------------+
                | HypoPG Analyzer             |
                | Target-Side EXPLAIN         |
                +-------------+---------------+
                              |
                              | Cost Evaluation
                              v
                +-----------------------------+
                | Migration Generator         |
                +-------------+---------------+
                              |
                              | GitHub REST API
                              v
                +-----------------------------+
                | GitHub Repository           |
                | Draft Pull Request          |
                +-----------------------------+
```

---

# Goals

The CLI should automatically:

1. Monitor `pg_stat_statements`
2. Compute interval (not cumulative) metrics
3. Detect expensive frequently executed queries
4. Simulate indexes using HypoPG
5. Measure planner cost reduction
6. Generate reversible PostgreSQL migrations
7. Create a Git branch
8. Open a Draft Pull Request

---

# Project Layout

```text
planpatch/

├── daemon/
│   ├── analyzer.py
│   ├── delta_calculator.py
│   ├── github_automation.py
│   ├── migration_builder.py
│   ├── parser.py
│   └── watcher.py
│
├── db/
│   └── migrations/
│
├── specs/
│   ├── 09_cli_autopilot_mvp.md
│   └── 10_cli_autopilot_complete.md
│
├── .env
├── .planpatch_state.json
├── main.py
├── requirements.txt
└── README.md
```

---

# Requirement 1 — Interval Delta Calculator

**File**

```text
daemon/delta_calculator.py
```

## Background

`pg_stat_statements` exposes cumulative counters.

Never evaluate performance directly from cumulative values.

Instead compute metrics only between two polling intervals.

---

## Persist State

Store the previous snapshot locally using either:

- `.planpatch_state.json`
- SQLite

Minimum fields:

```json
{
  "queryid": {
    "calls": 140,
    "total_exec_time": 9214.1,
    "rows": 9010
  }
}
```

---

## Delta Formula

For every polling cycle:

```text
delta_calls =
current.calls - previous.calls

delta_time_ms =
current.total_exec_time -
previous.total_exec_time

avg_exec_time_ms =
delta_time_ms /
delta_calls
```

---

## Reset Handling

If any counter decreases:

```text
current.calls < previous.calls

OR

current.total_exec_time <
previous.total_exec_time
```

Assume:

- pg_stat_statements reset
- PostgreSQL restart

Behavior:

- discard interval
- reset baseline
- do not generate recommendations

---

## Candidate Filter

Only continue with queries satisfying:

```text
delta_calls >= 1

AND

avg_exec_time_ms > 50
```

---

# Requirement 2 — Target-Side HypoPG Analyzer

**File**

```text
daemon/analyzer.py
```

---

## Database Connection

Connect using

```env
TARGET_DATABASE_URL
```

---

## Query Parsing

Determine:

- target table
- WHERE columns
- JOIN columns

A lightweight SQL parser is acceptable.

---

## Baseline Plan

Run

```sql
EXPLAIN (FORMAT JSON)
<query>;
```

Extract:

```text
Total Cost
```

Also determine whether a sequential scan exists.

Only continue when a Seq Scan is present.

---

## Virtual Index

Create:

```sql
SELECT *
FROM hypopg_create_index(
'CREATE INDEX ON table_name(column_name)'
);
```

---

## Re-run EXPLAIN

Again execute:

```sql
EXPLAIN (FORMAT JSON)
<query>;
```

Extract new planner cost.

---

## Cost Reduction

Formula:

```text
cost_reduction_pct =
(
(cost_before - cost_after)
/
cost_before
)
* 100
```

---

## Cleanup

Always execute:

```sql
SELECT hypopg_reset();
```

Even if an exception occurs.

---

## Recommendation Threshold

Only recommend when:

```text
cost_reduction_pct >= 30.0
```

---

# Requirement 3 — Migration Builder

**File**

```text
daemon/migration_builder.py
```

Generate two files.

---

## Up Migration

```text
db/migrations/

<timestamp>_add_<table>_<column>_idx.sql
```

Contents:

```sql
-- PlanPatch Auto-Generated Migration
-- Target Table: {table_name}
-- Estimated Cost Reduction: {cost_reduction_pct}%

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_{table}_{column}
ON {table} ({column});
```

---

## Down Migration

```text
db/migrations/

<timestamp>_add_<table>_<column>_idx.down.sql
```

Contents:

```sql
DROP INDEX CONCURRENTLY IF EXISTS idx_{table}_{column};
```

---

## Requirements

Migration names must be deterministic.

Migrations must be:

- reversible
- idempotent
- safe for repeated execution

---

# Requirement 4 — GitHub PR Automation

**File**

```text
daemon/github_automation.py
```

Authentication:

```env
GITHUB_PAT
```

Repository:

```env
GITHUB_REPO
```

---

## Workflow

### Step 1

Fetch latest commit SHA from:

```text
main
```

---

### Step 2

Create branch

```text
planpatch/optimize-{table}-{short_hash}
```

---

### Step 3

Commit:

```text
db/migrations/
```

---

### Step 4

Open Draft Pull Request

Target:

```text
main
```

---

## Pull Request Body

Include:

### Flagged Query

```sql
SELECT ...
```

---

### Interval Metrics

```text
Calls

Total Execution Time

Average Latency
```

---

### HypoPG Results

```text
Planner Cost Before

Planner Cost After

Reduction %
```

---

### Migration

Include both:

#### Up

```sql
CREATE INDEX ...
```

#### Down

```sql
DROP INDEX ...
```

---

# Requirement 5 — CLI

**File**

```text
main.py
```

Use:

```python
rich
```

for terminal output.

---

## Demo Mode

```bash
python main.py --demo
```

Behavior:

- connect
- poll once
- analyze
- generate migration
- open PR
- exit

---

## Watch Mode

```bash
python main.py --watch --interval 60
```

Behavior:

continuous polling.

---

# Terminal Output

Expected logging:

```text
[+] Connecting to target database... Connected.

[+] Polling pg_stat_statements...
14 active queries analyzed.

[!] Slow Query Detected:
SELECT * FROM orders
WHERE customer_id = $1

[*] Running HypoPG target-side simulation...

[✔] Virtual Index Tested:
idx_orders_customer_id

[✔] Cost Reduction:
84.2%
(Cost: 1250.0 -> 197.5)

[+] Generating migration...

[✔] Migration created:
db/migrations/20260808_add_orders_customer_id_idx.sql

[🚀] Opening Draft Pull Request...

[SUCCESS]
PR Created:

https://github.com/username/repo/pull/12
```

---

# Environment Configuration

Create:

```text
.env
```

Contents:

```env
TARGET_DATABASE_URL=postgresql://user:password@host:5432/dbname

GITHUB_PAT=ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

GITHUB_REPO=owner/repository

POLL_INTERVAL_SECONDS=60
```

---

# Dependencies

Minimum Python packages:

```text
psycopg
python-dotenv
requests
rich
sqlparse
```

---

# Verification Protocol

## Database Setup

Ensure:

```sql
CREATE EXTENSION IF NOT EXISTS hypopg;
```

Install:

```sql
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
```

---

## Seed Data

Create:

```text
orders
```

Insert:

```text
20,000+
```

rows.

Ensure:

```text
customer_id
```

is **not indexed**.

---

## Generate Workload

Execute repeated queries:

```sql
SELECT *
FROM orders
WHERE customer_id = 123;
```

until statistics accumulate.

---

## Execute CLI

Run:

```bash
python main.py --demo
```

---

## Verify

### Interval Metrics

Confirm:

- delta values are calculated
- cumulative history is ignored

---

### HypoPG

Confirm:

- virtual index created
- planner cost decreases
- reduction percentage is correctly calculated

---

### Migration

Confirm:

Generated files exist:

```text
db/migrations/
```

---

### GitHub

Confirm:

- branch created
- migration committed
- draft PR opened

---

# Completion Artifact

After successful verification, generate:

```text
specs/10_cli_autopilot_complete.md
```

This document must include:

- Date and time of execution
- Environment information
- PostgreSQL version
- HypoPG version
- Poll interval
- Detected query
- Delta metrics
- Planner cost before
- Planner cost after
- Cost reduction percentage
- Generated migration filenames
- Git branch name
- Draft Pull Request URL
- Complete terminal output
- Verification checklist
- Final implementation summary

---

# Acceptance Criteria

The implementation is considered complete only if all of the following are true:

- Interval metrics are calculated using delta snapshots rather than cumulative statistics.
- Counter resets are detected and handled without generating false recommendations.
- HypoPG virtual indexes are created and cleaned up automatically.
- Planner cost reduction is accurately calculated.
- Recommendations are emitted only when planner cost reduction is at least 30%.
- Idempotent up/down PostgreSQL migrations are generated.
- A Git branch is created automatically.
- Migration files are committed to the repository.
- A Draft Pull Request is opened using the GitHub REST API.
- Demo mode completes the full workflow in a single execution.
- Watch mode continuously polls at the configured interval.
- `specs/10_cli_autopilot_complete.md` is generated after successful verification documenting the end-to-end execution.