import type { MetricsHistory, Prisma } from "@prisma/client";

import type { RuleFlag } from "../rules/rule-engine.js";

export type Remediation = {
  proposed_sql: string;
  estimated_cost_reduction_percentage: number;
  estimated_cost_before: number;
  estimated_cost_after: number;
  sandbox_note: string;
};

type CandidateIndex = {
  tableName: string;
  columns: string[];
  proposedSql: string;
};

const RESERVED_WORDS = new Set([
  "and",
  "or",
  "not",
  "null",
  "true",
  "false",
  "select",
  "from",
  "where",
  "join",
  "on",
  "in",
  "is",
  "like",
]);

function normalizeSql(sql: string): string {
  return sql.replace(/\s+/g, " ").trim();
}

function quoteIdentifier(identifier: string): string {
  return `"${identifier.replace(/"/g, '""')}"`;
}

function sanitizeIdentifier(raw: string): string | null {
  const cleaned = raw.replaceAll('"', "").trim();
  if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(cleaned)) {
    return null;
  }
  if (RESERVED_WORDS.has(cleaned.toLowerCase())) {
    return null;
  }
  return cleaned;
}

function splitTableName(raw: string): string[] | null {
  const parts = raw
    .split(".")
    .map((part) => sanitizeIdentifier(part))
    .filter((part): part is string => Boolean(part));

  return parts.length > 0 && parts.length <= 2 ? parts : null;
}

function formatTableName(parts: string[]): string {
  return parts.map(quoteIdentifier).join(".");
}

function extractTargetTable(query: string): string[] | null {
  const normalized = normalizeSql(query);
  const match = normalized.match(/\bfrom\s+("?[\w]+"?(?:\."?[\w]+"?)?)/i);
  if (!match?.[1]) {
    return null;
  }

  const afterTable = normalized.slice(match.index! + match[0].length).trimStart();
  if (afterTable.startsWith("(")) {
    return null;
  }

  return splitTableName(match[1]);
}

function extractPredicateColumns(query: string): string[] {
  const normalized = normalizeSql(query);
  const columns = new Set<string>();
  const predicatePattern =
    /(?:^|[\s(])(?:(?:"?[A-Za-z_][A-Za-z0-9_]*"?)[.])?"?([A-Za-z_][A-Za-z0-9_]*)"?\s*(?:=|>=|<=|>|<|\bin\b|\blike\b|\bis\b)/gi;

  const predicateSections = [
    ...normalized.matchAll(/\bwhere\b(.+?)(?:\bgroup\s+by\b|\border\s+by\b|\blimit\b|$)/gi),
    ...normalized.matchAll(/\bon\b(.+?)(?:\bjoin\b|\bwhere\b|\bgroup\s+by\b|\border\s+by\b|\blimit\b|$)/gi),
  ];

  for (const section of predicateSections) {
    const text = section[1] ?? "";
    for (const match of text.matchAll(predicatePattern)) {
      const column = sanitizeIdentifier(match[1] ?? "");
      if (column) {
        columns.add(column);
      }
    }
  }

  return [...columns].slice(0, 3);
}

function buildCandidateIndex(metric: MetricsHistory): CandidateIndex | null {
  const tableParts = extractTargetTable(metric.normalizedQuery);
  const columns = extractPredicateColumns(metric.normalizedQuery);

  if (!tableParts || columns.length === 0) {
    return null;
  }

  const tableName = formatTableName(tableParts);
  const indexName = quoteIdentifier(
    `idx_dblens_${tableParts.at(-1)}_${columns.join("_")}`.slice(0, 62),
  );
  const columnSql = columns.map(quoteIdentifier).join(", ");

  return {
    tableName,
    columns,
    proposedSql: `CREATE INDEX ${indexName} ON ${tableName} (${columnSql});`,
  };
}

function extractTotalCost(explainResult: unknown): number | null {
  const rows = explainResult as Array<Record<string, unknown>>;
  const planPayload = rows[0]?.["QUERY PLAN"];
  const planRoot = Array.isArray(planPayload) ? planPayload[0] : null;
  const cost = (planRoot as { Plan?: { "Total Cost"?: unknown } } | null)?.Plan?.[
    "Total Cost"
  ];
  return typeof cost === "number" && Number.isFinite(cost) ? cost : null;
}

async function explainCost(
  tx: Prisma.TransactionClient,
  sql: string,
): Promise<number | null> {
  const result = await tx.$queryRawUnsafe(`EXPLAIN (FORMAT JSON) ${sql}`);
  return extractTotalCost(result);
}

function buildSandboxTableName(metric: MetricsHistory): string {
  return `dblens_sandbox_${metric.queryHash.replace(/[^a-zA-Z0-9]/g, "").slice(0, 18)}`;
}

async function runHypopgSimulation(
  tx: Prisma.TransactionClient,
  metric: MetricsHistory,
  candidate: CandidateIndex,
): Promise<Remediation | null> {
  const sandboxTable = quoteIdentifier(buildSandboxTableName(metric));
  const sandboxColumns = candidate.columns.map(quoteIdentifier);
  const firstColumn = sandboxColumns[0];
  const otherColumns = sandboxColumns.slice(1);
  const tableDefinition = sandboxColumns
    .map((column) => `${column} integer NOT NULL`)
    .join(", ");
  const insertColumns = sandboxColumns.join(", ");
  const selectValues = sandboxColumns
    .map((_column, index) =>
      index === 0 ? "(g % 250)" : `((g + ${index}) % 500)`,
    )
    .join(", ");

  const predicates = [
    `${firstColumn} = 42`,
    ...otherColumns.map((column, index) => `${column} = ${index + 7}`),
  ].join(" AND ");
  const sandboxSelect = `SELECT * FROM ${sandboxTable} WHERE ${predicates}`;
  const sandboxIndexSql = `CREATE INDEX ON ${sandboxTable} (${insertColumns})`;

  await tx.$executeRawUnsafe(`CREATE TEMP TABLE ${sandboxTable} (${tableDefinition})`);
  await tx.$executeRawUnsafe(
    `INSERT INTO ${sandboxTable} (${insertColumns}) SELECT ${selectValues} FROM generate_series(1, 50000) AS g`,
  );
  await tx.$executeRawUnsafe(`ANALYZE ${sandboxTable}`);
  await tx.$executeRawUnsafe("DO $$ BEGIN PERFORM hypopg_reset(); END $$");

  const beforeCost = await explainCost(tx, sandboxSelect);
  await tx.$queryRawUnsafe("SELECT * FROM hypopg_create_index($1)", sandboxIndexSql);
  const afterCost = await explainCost(tx, sandboxSelect);
  await tx.$executeRawUnsafe("DO $$ BEGIN PERFORM hypopg_reset(); END $$");

  if (!beforeCost || !afterCost || beforeCost <= 0) {
    return null;
  }

  const reduction = Math.max(0, ((beforeCost - afterCost) / beforeCost) * 100);
  return {
    proposed_sql: candidate.proposedSql,
    estimated_cost_reduction_percentage: Number(reduction.toFixed(2)),
    estimated_cost_before: Number(beforeCost.toFixed(2)),
    estimated_cost_after: Number(afterCost.toFixed(2)),
    sandbox_note:
      "HypoPG estimate was calculated on a local temporary sandbox table shaped from parsed query predicates.",
  };
}

export async function buildRemediation(
  tx: Prisma.TransactionClient,
  metric: MetricsHistory,
  analysis: RuleFlag[],
): Promise<Remediation | null> {
  if (!analysis.includes("POSSIBLE_SEQ_SCAN")) {
    return null;
  }

  const candidate = buildCandidateIndex(metric);
  if (!candidate) {
    return null;
  }

  try {
    return await runHypopgSimulation(tx, metric, candidate);
  } catch (error) {
    console.warn(
      `Remediation simulation skipped for metric ${metric.id}:`,
      error instanceof Error ? error.message : error,
    );
    return null;
  }
}
