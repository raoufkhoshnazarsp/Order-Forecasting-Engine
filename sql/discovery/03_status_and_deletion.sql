-- 03_status_and_deletion.sql
-- Phase 0 / 5.3 — Status distribution and deletion behavior.
SELECT
    status,
    count() AS records,
    uniqExact(id) AS unique_orders
FROM fms_credit.tbl_order
WHERE registered_date >= toDate('{{start_date}}')
  AND registered_date <  toDate('{{end_date}}')
GROUP BY status
ORDER BY records DESC;
