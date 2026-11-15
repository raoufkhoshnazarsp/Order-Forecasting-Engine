-- 04_scope_fields.sql
-- Phase 0 / 5.3-5.4 — merchant_type, payment_method_type and scope coverage.
SELECT
    merchant_type,
    payment_method_type,
    count() AS records,
    uniqExact(id) AS unique_orders
FROM fms_credit.tbl_order
WHERE registered_date >= toDate('{{start_date}}')
  AND registered_date <  toDate('{{end_date}}')
  AND status = 'SETTLE'
GROUP BY merchant_type, payment_method_type
ORDER BY merchant_type, records DESC;
