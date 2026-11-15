-- v1/daily_leaf_users.sql
-- Query version: 2026-11-15.daily_leaf_users.v1
--
-- Daily user features per leaf entity.
-- NOTE: user counts are NOT additive across entities (one wallet may transact
-- in several entities). Total-level user metrics must be computed over the
-- whole population in Python, never by summing leaves.
WITH order_base AS
(
    SELECT
        toDate(O.{{date_field}}) AS order_date,
        toUInt64(O.venture_id)   AS venture_id,
        O.id                     AS order_id,
        O.wallet_id              AS wallet_id,
        if(isNull(O.voucher_discount_amount) OR O.voucher_discount_amount = 0, 1, 0) AS is_organic
    FROM fms_credit.tbl_order AS O
    WHERE O.{{date_field}} >= toDate('{{start_date}}')
      AND O.{{date_field}} <  toDate('{{end_date}}')
      AND {{status_filter}}
      {{scope_filters}}
),
commercial_mapping AS
(
    SELECT toUInt64(venture_id) AS venture_id, any(vertical) AS vertical, any(category) AS category
    FROM minio_db.commercial_data
    GROUP BY venture_id
    HAVING count() = 1
),
classified AS
(
    SELECT
        B.*,
        multiIf(
            M.venture_id = 0, 'UNMAPPED',
            M.vertical = '{{venture_vertical_value}}' OR M.category = '{{venture_category_value}}', 'VENTURE',
            M.vertical IN ({{vertical_list}}), 'VERTICAL',
            'UNMAPPED'
        ) AS entity_type
    FROM order_base AS B
    LEFT JOIN commercial_mapping AS M ON B.venture_id = M.venture_id
),
first_seen AS
(
    SELECT wallet_id, min(order_date) AS first_order_date
    FROM classified
    WHERE entity_type IN ('VERTICAL', 'VENTURE')
    GROUP BY wallet_id
)
SELECT
    C.order_date AS date,
    C.entity_type,
    if(C.entity_type = 'VENTURE', toString(C.venture_id), C.vertical) AS entity_id,
    uniqExact(C.wallet_id)                                        AS active_users,
    uniqExactIf(C.wallet_id, F.first_order_date = C.order_date)    AS new_users,
    uniqExactIf(C.wallet_id, F.first_order_date < C.order_date)    AS returning_users,
    uniqExactIf(C.order_id, C.is_organic = 1)                      AS organic_orders,
    uniqExactIf(C.order_id, C.is_organic = 0)                      AS non_organic_orders
FROM classified AS C
JOIN first_seen AS F ON C.wallet_id = F.wallet_id
GROUP BY date, entity_type, entity_id
ORDER BY date, entity_type, entity_id
