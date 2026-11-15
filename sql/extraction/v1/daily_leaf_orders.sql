-- v1/daily_leaf_orders.sql
-- Query version: 2026-11-15.daily_leaf_orders.v1
--
-- Produces the canonical daily leaf target: date x leaf entity.
-- TOTAL is NEVER produced here; it is derived from leaves in Python.
--
-- Placeholders rendered by src/ingestion/extract.py (never raw string SQL):
--   {{start_date}}      -- YYYY-MM-DD, inclusive
--   {{end_date}}        -- YYYY-MM-DD, exclusive (half-open interval)
--   {{date_field}}      -- approved order date column (registered_date | created_date)
--   {{status_filter}}   -- approved status predicate
--   {{scope_filters}}   -- zero or more approved AND predicates, one per line
--
-- Unmapped orders are surfaced as entity_type = 'UNMAPPED' and entity_id = ''
-- so the caller can measure them. They are never silently dropped.
WITH order_base AS
(
    SELECT
        toDate(O.{{date_field}})  AS order_date,
        toUInt64(O.venture_id)    AS venture_id,
        O.id                      AS order_id
    FROM fms_credit.tbl_order AS O
    WHERE O.{{date_field}} >= toDate('{{start_date}}')
      AND O.{{date_field}} <  toDate('{{end_date}}')
      AND {{status_filter}}
      {{scope_filters}}
),
commercial_mapping AS
(
    SELECT
        toUInt64(venture_id) AS venture_id,
        any(vertical)        AS vertical,
        any(category)        AS category
    FROM minio_db.commercial_data
    GROUP BY venture_id
    HAVING count() = 1
),
classified_orders AS
(
    SELECT
        B.order_date,
        B.order_id,
        B.venture_id,
        M.vertical,
        M.category,
        multiIf(
            M.venture_id = 0, 'UNMAPPED',
            M.vertical = '{{venture_vertical_value}}' OR M.category = '{{venture_category_value}}',
                'VENTURE',
            M.vertical IN ({{vertical_list}}),
                'VERTICAL',
            'UNMAPPED'
        ) AS entity_type
    FROM order_base AS B
    LEFT JOIN commercial_mapping AS M
        ON B.venture_id = M.venture_id
)
SELECT
    order_date AS date,
    entity_type,
    if(entity_type = 'VENTURE', toString(venture_id), vertical) AS entity_id,
    uniqExact(order_id) AS successful_orders
FROM classified_orders
GROUP BY date, entity_type, entity_id
ORDER BY date, entity_type, entity_id
