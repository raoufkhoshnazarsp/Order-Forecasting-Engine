-- 06_venture_coverage.sql
-- Phase 0 — Venture master coverage and unmapped measurement.
SELECT
    count()                            AS ventures_total,
    countIf(toUInt64(V.id) IN (SELECT toUInt64(venture_id) FROM minio_db.commercial_data)) AS mapped,
    countIf(toUInt64(V.id) NOT IN (SELECT toUInt64(venture_id) FROM minio_db.commercial_data)) AS unmapped
FROM fms_credit.tbl_venture AS V;
