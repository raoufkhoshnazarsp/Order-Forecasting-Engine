-- 03b_deletion_behavior.sql
SELECT
    status,
    countIf(deleted_at IS NOT NULL) AS deleted_records,
    count()                         AS total_records,
    round(countIf(deleted_at IS NOT NULL) / count() * 100, 2) AS deleted_pct
FROM fms_credit.tbl_order
WHERE registered_date >= toDate('{{start_date}}')
  AND registered_date <  toDate('{{end_date}}')
GROUP BY status
ORDER BY total_records DESC;
