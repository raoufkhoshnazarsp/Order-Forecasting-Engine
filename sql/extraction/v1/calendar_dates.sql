-- v1/calendar_dates.sql
-- Query version: 2026-11-15.calendar_dates.v1
-- Persian calendar dimension over an explicit half-open range.
SELECT
    toDate(D.date)   AS date,
    D.persian_year   AS persian_year,
    D.persian_month  AS persian_month,
    D.persian_day    AS persian_day
FROM public.DimDate_Persian AS D
WHERE toDate(D.date) >= toDate('{{start_date}}')
  AND toDate(D.date) <  toDate('{{end_date}}')
ORDER BY date
