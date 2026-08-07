import cors from "cors";
import express from "express";
import type { MetricsHistory, TargetDatabase } from "@prisma/client";

import { prisma } from "./lib/prisma.js";
import { analyzeMetric } from "./rules/rule-engine.js";

const app = express();
const port = Number(process.env.PORT ?? 3000);

app.use(cors());
app.use(express.json());

function redactConnectionUri(connectionUri: string): string {
  try {
    const parsed = new URL(connectionUri);
    if (parsed.password) {
      parsed.password = "REDACTED";
    }
    return parsed.toString();
  } catch {
    return connectionUri.replace(/(:)[^:@/]+(@)/, "$1REDACTED$2");
  }
}

function serializeDatabase(database: TargetDatabase) {
  return {
    id: database.id,
    name: database.name,
    connectionUri: redactConnectionUri(database.connectionUri),
    createdAt: database.createdAt.toISOString(),
  };
}

function buildRemediation(metric: MetricsHistory) {
  if (!metric.proposedIndexSql || metric.costReductionPct === null) {
    return null;
  }

  return {
    proposed_sql: metric.proposedIndexSql,
    estimated_cost_reduction_percentage: metric.costReductionPct,
    sandbox_note:
      "HypoPG estimate was calculated on the target database using target schema and statistics.",
  };
}

function serializeMetric(metric: MetricsHistory) {
  const analysis = analyzeMetric(metric);
  const remediation = buildRemediation(metric);

  return {
    id: metric.id,
    databaseId: metric.databaseId,
    queryHash: metric.queryHash,
    normalizedQuery: metric.normalizedQuery,
    callCount: metric.callCount.toString(),
    totalExecTimeMs: metric.totalExecTimeMs,
    rowsReturned: metric.rowsReturned.toString(),
    sharedBlksHit: metric.sharedBlksHit.toString(),
    sharedBlksRead: metric.sharedBlksRead.toString(),
    snapshotTime: metric.snapshotTime.toISOString(),
    analysis,
    ...(remediation ? { remediation } : {}),
  };
}

app.get("/api/health", async (_req, res) => {
  try {
    await prisma.$queryRaw`SELECT 1`;
    res.status(200).json({
      ok: true,
      database: "connected",
    });
  } catch (error) {
    res.status(503).json({
      ok: false,
      database: "disconnected",
      error: error instanceof Error ? error.message : "Unknown database error",
    });
  }
});

app.get("/api/databases", async (_req, res, next) => {
  try {
    const databases = await prisma.targetDatabase.findMany({
      orderBy: { createdAt: "desc" },
    });
    res.json({
      databases: databases.map(serializeDatabase),
    });
  } catch (error) {
    next(error);
  }
});

app.get("/api/databases/:id/metrics", async (req, res, next) => {
  try {
    const database = await prisma.targetDatabase.findUnique({
      where: { id: req.params.id },
    });

    if (!database) {
      res.status(404).json({ error: "Target database not found" });
      return;
    }

    const latestMetric = await prisma.metricsHistory.findFirst({
      where: { databaseId: database.id },
      orderBy: { snapshotTime: "desc" },
      select: { snapshotTime: true },
    });

    if (!latestMetric) {
      res.json({
        database: serializeDatabase(database),
        snapshotTime: null,
        metrics: [],
      });
      return;
    }

    const metrics = await prisma.metricsHistory.findMany({
      where: {
        databaseId: database.id,
        snapshotTime: latestMetric.snapshotTime,
      },
      orderBy: { totalExecTimeMs: "desc" },
    });

    res.json({
      database: serializeDatabase(database),
      snapshotTime: latestMetric.snapshotTime.toISOString(),
      metrics: metrics.map(serializeMetric),
    });
  } catch (error) {
    next(error);
  }
});

app.use(
  (
    error: unknown,
    _req: express.Request,
    res: express.Response,
    _next: express.NextFunction,
  ) => {
    console.error(error);
    res.status(500).json({
      error: error instanceof Error ? error.message : "Internal server error",
    });
  },
);

const server = app.listen(port, "0.0.0.0", () => {
  console.log(`DB-Lens backend API listening on port ${port}`);
});

async function shutdown() {
  server.close(async () => {
    await prisma.$disconnect();
    process.exit(0);
  });
}

process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
