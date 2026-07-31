import type { MetricsHistory } from "@prisma/client";

export type RuleFlag =
  | "HIGH_LATENCY"
  | "POSSIBLE_SEQ_SCAN"
  | "HIGH_FREQUENCY_QUERY";

const AVG_EXECUTION_THRESHOLD_MS = 100;
const HIGH_FREQUENCY_THRESHOLD = 1000n;

function hasPossibleSelectStarScan(query: string): boolean {
  const normalized = query.replace(/\s+/g, " ").trim().toLowerCase();
  return (
    /\bselect\s+\*\s+from\b/.test(normalized) &&
    !/\bwhere\b/.test(normalized) &&
    !/\blimit\b/.test(normalized)
  );
}

export function analyzeMetric(metric: MetricsHistory): RuleFlag[] {
  const flags: RuleFlag[] = [];
  const callCount = metric.callCount;

  if (
    callCount > 0n &&
    metric.totalExecTimeMs / Number(callCount) > AVG_EXECUTION_THRESHOLD_MS
  ) {
    flags.push("HIGH_LATENCY");
  }

  if (
    /\bseq_scan\b/i.test(metric.normalizedQuery) ||
    hasPossibleSelectStarScan(metric.normalizedQuery)
  ) {
    flags.push("POSSIBLE_SEQ_SCAN");
  }

  if (callCount > HIGH_FREQUENCY_THRESHOLD) {
    flags.push("HIGH_FREQUENCY_QUERY");
  }

  return flags;
}
