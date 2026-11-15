-- 02_date_fields.sql
-- Phase 0 / 5.2 — Compare candidate date fields.
-- Half-open window via parameters.
SELECT
    count()                                                    AS rows_total,
    countIf(registered_date IS NULL)                           AS registered_date_null,
    countIf(created_date IS NULL)                              AS created_date_null,
    countIf(registered_at IS NULL)                             AS registered_at_null,
    countIf(created_at IS NULL)                                AS created_at_null,
    countIf(toDate(registered_at) != registered_date)          AS registered_at_vs_date_mismatch,
    countIf(toDate(created_at) != created_date)                AS created_at_vs_date_mismatch,
    countIf(created_date != registered_date)                   AS created_vs_registered_mismatch,
    min(registered_date)                                       AS min_registered_date,
    max(registered_date)                                       AS max_registered_date,
    min(created_date)                                          AS min_created_date,
    max(created_date)                                          AS max_created_date
FROM fms_credit.tbl_order
WHERE registered_date >= toDate('{{start_date}}')
  AND registered_date <  toDate('{{end_date}}');
