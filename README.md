# DB-Lens

**Automated PostgreSQL database intelligence for engineering teams.**

DB-Lens is a B2B SaaS DevTool that continuously observes your production PostgreSQL workloads and turns raw query telemetry into actionable performance intelligence. Phase 1 delivers a production-ready telemetry pipeline: a background daemon polls `pg_stat_statements` from your live database, persists historical snapshots locally, and exposes them through a typed API backed by Prisma—all orchestrated with Docker Compose.

Stop guessing which queries are slowing you down. DB-Lens gives platform and backend teams a durable, query-level view of database health without manual EXPLAIN sessions or ad-hoc monitoring scripts.

---

## Architecture

```
┌─────────────────────┐     pg_stat_statements      ┌──────────────────────┐
│  Target PostgreSQL  │ ◄──── poll every 60s ────── │  Python Telemetry    │
│  (Supabase / prod)  │                             │  Daemon              │
└─────────────────────┘                             └──────────┬───────────┘
                                                                 │ write snapshots
                                                                 ▼
┌─────────────────────┐     Prisma ORM / migrations  ┌──────────────────────┐
│  Node.js Backend    │ ◄──────────────────────────── │  dblens-meta-db      │
│  API (:3000)        │                             │  (local PostgreSQL)  │
└─────────────────────┘                             └──────────────────────┘
         ▲
         │ docker compose
         └──────────────────────────────────────────────────────────────────
```

| Component | Role |
|-----------|------|
| **Python Telemetry Daemon** | Connects to the target database (e.g. Supabase via IPv4 session pooler), reads top queries from `pg_stat_statements`, and writes metrics snapshots to the metadata store. |
| **Prisma + Node.js Backend** | Owns the metadata schema (`TargetDatabase`, `MetricsHistory`), runs migrations on startup, and serves a REST API with health checks and rule-based query analysis. |
| **Docker Compose** | Boots `dblens-meta-db`, applies Prisma migrations, starts the API, and runs the daemon with configurable target connection settings. |

---

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and Docker Compose v2
- A PostgreSQL target with `pg_stat_statements` enabled (Supabase projects include this by default)
- For cloud targets on IPv4-only networks: use Supabase **Session mode** pooler connection string from **Project Settings → Database**

---

## Quick Start

### 1. Clone the repository

```bash
git clone https://github.com/Prince-Prajapati07/DB-Lens.git
cd DB-Lens
```

### 2. Configure environment files

Copy the example env files and fill in your target database credentials. **Never commit real `.env` files.**

**Root `.env`** (used by Docker Compose for the telemetry daemon):

```bash
cp .env.example .env
```

Edit `.env`:

```env
TARGET_DATABASE_URL=postgresql://postgres.[PROJECT_REF]:[PASSWORD]@[POOLER_HOST]:5432/postgres?sslmode=require
TARGET_DATABASE_NAME=my-production-db
TARGET_DATABASE_PREFER_IPV4=true
```

**Optional — local development outside Docker:**

```bash
cp backend/.env.example backend/.env
cp daemon/.env.example daemon/.env
```

| Variable | Description |
|----------|-------------|
| `TARGET_DATABASE_URL` | Connection string to the PostgreSQL instance being monitored |
| `TARGET_DATABASE_NAME` | Human-readable label stored in metadata |
| `TARGET_DATABASE_PREFER_IPV4` | Set `true` when resolving hostnames to IPv4 (required for some Supabase pooler setups) |
| `DATABASE_URL` | Metadata DB URL for Prisma (defaults to local `dblens-meta-db` in Compose) |

### 3. Start the stack

```bash
docker compose up -d --build
```

This will:

1. Start **dblens-meta-db** (metadata PostgreSQL on port `55433`)
2. Run **Prisma migrations** against the metadata database
3. Start the **backend API** on [http://localhost:3000](http://localhost:3000)
4. Start the **telemetry daemon**, polling every 60 seconds

Verify the API:

```bash
curl http://localhost:3000/api/health
```

### 4. Optional — local simulated target

To develop without a cloud database, enable the bundled simulated Postgres:

```bash
docker compose --profile local-target up -d --build
```

Point `.env` at the simulated instance (see `daemon/.env.example`) or rely on Compose defaults.

---

## Project Structure

```
DB-Lens/
├── daemon/           # Python telemetry daemon (pg_stat_statements poller)
├── backend/          # Node.js API + Prisma schema & migrations
├── docker/           # Postgres init scripts
├── specs/            # Design & implementation specs
├── docker-compose.yml
├── .env.example      # Root env template for Compose / daemon
└── README.md
```

---

## Phase 1 Status

- [x] Python daemon polling `pg_stat_statements` every 60s
- [x] Historical metrics persistence in `dblens-meta-db`
- [x] Prisma-managed schema and migrations
- [x] Docker Compose orchestration (meta DB, API, daemon)
- [x] Supabase / IPv4 session pooler connectivity

---

## License

Proprietary — All rights reserved.
