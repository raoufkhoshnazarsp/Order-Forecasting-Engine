-- 05_commercial_mapping.sql
-- Phase 0 / 5.4 — Commercial mapping validation.

-- 5a. Duplicate venture_id rows (must be empty before joining).
SELECT
    venture_id,
    count() AS mapping_rows
FROM minio_db.commercial_data
GROUP BY venture_id
HAVING mapping_rows > 1
ORDER BY mapping_rows DESC;

-- 5b. Available Vertical / Category values.
SELECT
    vertical,
    category,
    count()           AS rows,
    uniqExact(venture_id) AS ventures
FROM minio_db.commercial_data
GROUP BY vertical, category
ORDER BY vertical, category;
