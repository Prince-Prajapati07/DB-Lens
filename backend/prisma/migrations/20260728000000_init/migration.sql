CREATE TABLE "TargetDatabase" (
    "id" TEXT NOT NULL,
    "name" TEXT NOT NULL,
    "connectionUri" TEXT NOT NULL,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "TargetDatabase_pkey" PRIMARY KEY ("id")
);

CREATE TABLE "MetricsHistory" (
    "id" TEXT NOT NULL,
    "databaseId" TEXT NOT NULL,
    "queryHash" TEXT NOT NULL,
    "normalizedQuery" TEXT NOT NULL,
    "callCount" BIGINT NOT NULL,
    "totalExecTimeMs" DOUBLE PRECISION NOT NULL,
    "rowsReturned" BIGINT NOT NULL,
    "sharedBlksHit" BIGINT NOT NULL,
    "sharedBlksRead" BIGINT NOT NULL,
    "snapshotTime" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "MetricsHistory_pkey" PRIMARY KEY ("id")
);

CREATE INDEX "MetricsHistory_queryHash_snapshotTime_idx" ON "MetricsHistory"("queryHash", "snapshotTime");

ALTER TABLE "MetricsHistory" ADD CONSTRAINT "MetricsHistory_databaseId_fkey"
FOREIGN KEY ("databaseId") REFERENCES "TargetDatabase"("id") ON DELETE CASCADE ON UPDATE CASCADE;
