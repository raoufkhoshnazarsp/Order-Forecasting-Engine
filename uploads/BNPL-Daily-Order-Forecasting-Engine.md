# **BNPL Daily Order Forecasting Engine**

## **Final Technical Architecture, Data Contract & Implementation Roadmap**

**Version:** 2.0  
**Architecture:** SQL-first Data Extraction \+ Python Forecasting  
**Source DWH:** ClickHouse  
**Development Environment:** Personal VPS → Company Server  
**Forecast Frequency:** Daily  
**Forecast Horizons:** D+1, D+7, D+30  
**Historical Data:** More than 3 years  
**Target:** Daily Successful & Finalized Orders

---

# **1\. Project Objective**

هدف، توسعه یک Forecasting Engine برای پیش‌بینی تعداد سفارش‌های موفق و نهایی‌شده کسب‌وکار BNPL در سطوح مختلف سازمان است.

این پروژه صرفاً یک مدل Machine Learning نیست؛ بلکه یک سیستم ماژولار شامل موارد زیر خواهد بود:

1. Data Extraction از ClickHouse  
2. Data Validation  
3. Daily Data Mart  
4. Feature Engineering  
5. Lifecycle Detection  
6. Statistical Forecasting  
7. Machine Learning Forecasting  
8. Hierarchical Reconciliation  
9. Prediction Intervals  
10. Explainability  
11. Scenario Analysis  
12. Backtesting & Model Selection  
13. Forecast Serving  
14. Monitoring & Model Governance

اصل طراحی این است که تمام مراحل قابل بازتولید، تست، نسخه‌بندی و انتقال به محیط Production باشند.

## **1.1. Forecast Requirements**

Engine باید:

* هر روز پس از نهایی‌شدن اطلاعات روز قبل اجرا شود.  
* برای ۳۰ روز آینده Forecast روزانه تولید کند.  
* مقادیر D+1، D+7 و D+30 را در اختیار قرار دهد.  
* Forecast را در سطح Total Business، هر Vertical و هر Venture ارائه دهد.  
* Prediction Interval داشته باشد.  
* مهم‌ترین Driverهای هر Forecast را نمایش دهد.  
* Actual و Forecast را در طول زمان مقایسه کند.  
* امکان اجرای سناریوهای مختلف کسب‌وکار را فراهم کند.

---

# **2\. Confirmed Business Structure**

## **2.1. Hierarchy**

ساختار کسب‌وکار شامل دو شاخه مستقل است.

```
                    TOTAL BUSINESS
                          |
             +------------+------------+
             |                         |
       VERTICAL PORTFOLIO        VENTURE PORTFOLIO
             |                         |
      +------+------+              +---+---+
      |      |      |              |       |
   DIGITAL FASHION FMCG          Venture A Venture B
      |      |      |              |       |
    GOLD    H&B   HOME           Venture C ...
      |
   SERVICES
```

هفت Vertical:

* DIGITAL  
* FASHION  
* FMCG  
* GOLD  
* H\&B  
* Home  
* Services

شاخه دوم:

* تمام Ventureها، هرکدام به‌عنوان یک Entity مستقل.

## **2.2. Aggregation Rule**

قاعده قطعی تجمیع:

```
Total Business Orders
=
Sum(7 Vertical Orders)
+
Sum(All Venture Orders)
```

Ventureها زیرمجموعه Verticalها نیستند.

در نتیجه:

* هر سفارش فقط باید در یکی از Leaf Entityها قرار بگیرد.  
* سفارش Venture نباید در Vertical نیز شمرده شود.  
* Total Business نباید از یک Query مستقل با فیلترهای متفاوت ساخته شود.  
* مرجع اصلی Total باید از همان Leaf Data ساخته شود.

## **2.3. Entity Types**

مقادیر مجاز:

```
TOTAL
VERTICAL
VENTURE
```

هر Entity باید مشخصات زیر را داشته باشد:

```
entity_id
entity_name
entity_type
parent_entity_id
is_active
valid_from
valid_to
```

برای تمام Verticalها و Ventureها باید شناسه ثابت داشته باشیم.

نام Entity نباید به‌عنوان کلید اصلی استفاده شود؛ زیرا ممکن است تغییر کند.

---

# **3\. Source Data Architecture**

## **3.1. Database**

منبع اصلی:

**ClickHouse**

استخراج داده از طریق SQL انجام می‌شود.

Python مسئول موارد زیر است:

* اتصال به ClickHouse  
* اجرای Queryهای نسخه‌بندی‌شده  
* دریافت خروجی Query  
* کنترل Schema  
* ساخت Featureها  
* آموزش مدل  
* Backtesting  
* تولید Forecast  
* ذخیره خروجی

مرز معماری:

```
CLICKHOUSE
    |
    | SQL
    v
DATA EXTRACTION
    |
    v
VALIDATED DATASET
    |
    | Python
    v
FEATURE ENGINEERING
    |
    v
FORECASTING ENGINE
```

## **3.2. Source Tables**

بر اساس راهنمای سازمان، منابع شناخته‌شده:

| Table | Role |
| ----- | ----- |
| fms\_credit.tbl\_order | سفارش‌ها |
| fms\_credit.tbl\_venture | اطلاعات Venture / Merchant |
| minio\_db.commercial\_data | نگاشت Commercial |
| fms\_credit.tbl\_cart | اطلاعات Cart |
| fms\_credit.tbl\_cart\_item | اطلاعات آیتم‌ها |
| app\_log.events | رویدادهای اپلیکیشن |
| app\_log.raw\_events | Raw Events |
| public.DimDate\_Persian | تقویم فارسی |
| fms\_credit\_warehoues.tbl\_registration | اطلاعات ثبت‌نام مرچنت |
| fms\_credit\_warehoues.tbl\_registration\_settlement\_policy | سیاست تسویه |
| fms\_credit\_warehoues.tbl\_settlement\_policy | جزئیات سیاست تسویه |

تمام این منابع الزاماً در MVP استفاده نمی‌شوند.

اولویت اولیه:

1. tbl\_order  
2. tbl\_venture  
3. commercial\_data  
4. Calendar Dimension

سپس User، Marketing، Merchant و Event Features اضافه شوند.

## **3.3. Important Source Fields**

### **fms\_credit.tbl\_order**

| Field | Usage |
| ----- | ----- |
| id | Order ID |
| venture\_id | Merchant / Venture ID |
| wallet\_id | User ID |
| status | Order status |
| merchant\_type | Merchant channel |
| payment\_method\_type | Payment method |
| registered\_at | Timestamp candidate |
| registered\_date | Date candidate |
| created\_at | Creation timestamp |
| created\_date | Creation date |
| amount | Gross amount |
| discount\_amount | Discount |
| voucher\_discount\_amount | Voucher cost |
| deleted\_at | Soft deletion |

### **fms\_credit.tbl\_venture**

| Field | Usage |
| ----- | ----- |
| id | Venture ID |
| name | Venture name |
| status | Current status |
| callback\_url | Technical / business metadata |
| created\_date | Creation date |
| pay\_now\_access | PayNow access |
| cashback\_percentage | Cashback configuration |

### **minio\_db.commercial\_data**

| Field | Usage |
| ----- | ----- |
| venture\_id | Mapping key |
| merchant\_name | Merchant name |
| vertical | Vertical classification |
| category | Business category |
| account\_manager | Account manager |
| source | Commercial source |

Join:

```
ON toUInt64(CD.venture_id) = toUInt64(O.venture_id)
```

Before using this join, the uniqueness of commercial\_data.venture\_id must be validated.

If multiple records exist for a Venture, use the latest valid record only after identifying the actual timestamp/version column.

Do not invent a timestamp column or use any() as a substitute for deterministic deduplication.

---

# **4\. Business Rules and Target Definition**

## **4.1. Known Rules**

Based on the organization's query guide:

* Successful order status: SETTLE  
* Raw monetary unit: Rial  
* Monetary reporting unit: Toman  
* Organic order: voucher\_discount\_amount \= 0 or NULL  
* Non-organic order: voucher\_discount\_amount \> 0  
* discount\_amount alone does not make an order non-organic.  
* Voucher cost is based on voucher\_discount\_amount.

Monetary conversion:

```
amount_toman = amount / 10
```

## **4.2. Target Definition**

Target:

```
successful_orders
```

Initial candidate definition:

```
uniqExact(O.id)
```

with:

```
O.status = 'SETTLE'
```

However, the final Target Definition must explicitly approve:

1. Whether only merchant\_type \= 'ONLINE' is included.  
2. Whether soft-deleted orders are excluded.  
3. Whether the order date is registered\_date, created\_date or another field.  
4. Whether all payment methods are included.  
5. Whether all Venture and Vertical order types are included.  
6. Whether a settled order later reversed or refunded remains in the historical Target.

The Target Definition must be stored as a versioned business rule.

## **4.3. Scope Filters**

The following filters are used for specific Commercial reporting scopes:

```
O.status = 'SETTLE'
AND O.merchant_type = 'ONLINE'
```

The following exclusions apply only when the requested scope is Commercial without Venture and excluded categories:

```
CD.vertical != 'Snapp-Venture'
AND CD.category NOT IN ('in_store', 'social', 'ventures')
```

These exclusions must not be applied automatically to the complete BNPL Forecasting Engine.

The Engine must have an explicit scope configuration.

Example:

```
target:
  status: SETTLE
  merchant_type: ONLINE
  exclude_deleted: null
  date_field: null
  payment_methods: all

scope:
  verticals:
    - DIGITAL
    - FASHION
    - FMCG
    - GOLD
    - H&B
    - Home
    - Services

  venture_portfolio:
    enabled: true

  excluded_categories: []
```

The null values are intentional until business approval.

---

# **5\. Data Discovery & SQL Validation**

Before building the Data Mart, execute a Data Discovery phase.

## **5.1. Schema Inspection**

Retrieve actual table schemas:

```
DESCRIBE TABLE fms_credit.tbl_order;
DESCRIBE TABLE fms_credit.tbl_venture;
DESCRIBE TABLE minio_db.commercial_data;
```

Validate:

* Column types  
* Nullable fields  
* DateTime precision  
* LowCardinality fields  
* Primary sorting key  
* Partitioning key  
* Engine type

Also inspect:

```
SHOW CREATE TABLE fms_credit.tbl_order;
```

This is important for partition pruning and query optimization.

## **5.2. Date Field Validation**

Compare these fields:

* registered\_date  
* created\_date  
* registered\_at  
* created\_at

Check:

* NULL rate  
* Differences between fields  
* Historical coverage  
* Timezone behavior  
* Relationship to settlement date  
* Relationship to the actual business reporting date

The approved date must be used consistently across all Target queries.

## **5.3. Status Validation**

Inspect all statuses:

```
SELECT
    status,
    count() AS records
FROM fms_credit.tbl_order
GROUP BY status
ORDER BY records DESC;
```

Inspect deleted orders:

```
SELECT
    countIf(deleted_at IS NOT NULL) AS deleted_records,
    count() AS total_records
FROM fms_credit.tbl_order;
```

The purpose is to establish the actual relationship between SETTLE and deletion/reversal behavior.

## **5.4. Commercial Mapping Validation**

```
SELECT
    venture_id,
    count() AS mapping_rows
FROM minio_db.commercial_data
GROUP BY venture_id
HAVING mapping_rows > 1
ORDER BY mapping_rows DESC;
```

Inspect the available Vertical and Category values:

```
SELECT
    vertical,
    category,
    count() AS rows,
    uniqExact(venture_id) AS ventures
FROM minio_db.commercial_data
GROUP BY vertical, category
ORDER BY vertical, category;
```

Important:

The mapping table may represent current Commercial classification rather than historical classification.

If a Venture changed Vertical, name or category over the last three years, historical mapping must be handled explicitly.

For historical modeling, use an effective-dated mapping or an approved fixed classification policy.

## **5.5. Required Discovery Outputs**

Before proceeding:

* Schema Report  
* Target Definition  
* Date Field Decision  
* Deletion Policy  
* Payment Method Scope  
* Vertical Mapping Report  
* Venture Mapping Report  
* Duplicate Mapping Report  
* Historical Coverage Report

---

# **6\. SQL Extraction Layer**

## **6.1. General Principles**

All source extraction must be implemented as SQL files.

Python must not contain large inline SQL strings.

Recommended pattern:

```
sql/
    discovery/
    extraction/
    validation/
    marts/
```

Use parameterized SQL templates.

Date ranges must use half-open intervals:

```
WHERE registered_date >= toDate('{{start_date}}')
  AND registered_date < toDate('{{end_date}}')
```

This prevents boundary overlap and simplifies incremental extraction.

## **6.2. Query Optimization Rules**

Apply the following rules:

1. Filter source tables before joining whenever possible.  
2. Always use partition-compatible date filters.  
3. Avoid SELECT \*.  
4. Avoid joining order-level facts to cart items unless needed.  
5. Validate dimension uniqueness before joining.  
6. Use uniqExact(O.id) when exact distinct order counts are required.  
7. Avoid accidental row multiplication.  
8. Avoid unnecessary FINAL.  
9. Use PREWHERE when it improves filtering for the actual table engine and sorting key.  
10. Inspect EXPLAIN indexes \= 1 for expensive queries.  
11. Use query limits and bounded date ranges for exploratory analysis.  
12. Avoid one enormous query that extracts every feature from every source.

For a large historical extraction, use monthly or quarterly partitions.

## **6.3. SQL Query 1 — Daily Leaf Orders**

This is the most important query in the Engine.

The query should return:

```
date
entity_id
entity_name
entity_type
successful_orders
```

Logical structure:

```
WITH order_base AS
(
    SELECT
        toDate(O.registered_date) AS order_date,
        toUInt64(O.venture_id) AS venture_id,
        O.id AS order_id
    FROM fms_credit.tbl_order AS O
    WHERE O.registered_date >= toDate('{{start_date}}')
      AND O.registered_date < toDate('{{end_date}}')
      AND O.status = 'SETTLE'
      -- Apply only the approved target filters.
),
commercial_mapping AS
(
    SELECT
        toUInt64(venture_id) AS venture_id,
        vertical,
        category,
        merchant_name
    FROM minio_db.commercial_data
    -- Replace with deterministic latest-valid-row logic
    -- once the actual mapping timestamp is confirmed.
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
            M.vertical = 'Snapp-Venture'
                OR M.category = 'ventures',
            'VENTURE',
            M.vertical IN (
                'DIGITAL', 'FASHION', 'FMCG',
                'GOLD', 'H&B', 'Home', 'Services'
            ),
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
    if(
        entity_type = 'VERTICAL',
        vertical,
        toString(venture_id)
    ) AS entity_id,
    uniqExact(order_id) AS successful_orders
FROM classified_orders
WHERE entity_type IN ('VERTICAL', 'VENTURE')
GROUP BY
    date,
    entity_type,
    entity_id
ORDER BY date, entity_type, entity_id;
```

**Important implementation warning:**

This is a logical template, not a production-ready query until the following are verified:

* Whether category \= 'ventures' is the complete Venture classification rule.  
* Whether vertical \= 'Snapp-Venture' identifies every Venture.  
* Whether these conditions overlap or conflict.  
* Which actual timestamp column determines the latest Commercial mapping.  
* Whether the target should filter ONLINE.  
* Whether deleted orders are excluded.

If the two Venture classification rules overlap, classify the order only once.

The UNMAPPED population must be measured and reported. Do not silently drop it in production.

The actual entity\_id for Ventures should be a stable Venture ID, not its name.

## **6.4. SQL Query 2 — Daily User Features**

Required output:

```
date
entity_id
active_users
new_users
organic_orders
non_organic_orders
```

Use wallet\_id for user identity according to the Commercial guide.

A user may appear in multiple entities on the same day. Therefore, user counts at Total Business level must be calculated using distinct users over the complete population, not by summing Vertical and Venture user counts.

The same rule applies to new and returning users.

## **6.5. SQL Query 3 — Merchant Features**

Required output:

```
date
entity_id
active_merchants
new_merchants
deactivated_merchants
merchant_growth_rate
```

Merchant activation should be defined using an approved business rule.

A candidate is the first successful order date, but this is not equivalent to operational activation unless confirmed.

## **6.6. SQL Query 4 — Marketing Features**

Required output:

```
date
entity_id
campaign_id
campaign_type
planned_budget
planned_discount
planned_impressions
actual_spend
actual_impressions
campaign_status
```

Store both the value and the time it became known.

Historical forecasts must only use campaign information available at the historical forecast origin.

## **6.7. SQL Query 5 — App Behavior**

Source:

```
app_log.events
app_log.raw_events
```

Extract only events that have an approved relationship with the order funnel.

Examples:

* Product view  
* Merchant view  
* Add to cart  
* Checkout started  
* Payment initiated  
* Payment successful

The actual event names must be discovered from the source.

Do not assume that every app event is a useful feature.

---

# **7\. Daily Data Mart**

## **7.1. Grain**

Primary modeling grain:

```
date × entity_id
```

The Data Mart should contain one row per date and Leaf Entity.

The hierarchy:

```
TOTAL
  |
  +-- VERTICAL entities
  |
  +-- VENTURE entities
```

Total should be derived from Leaf Entities.

## **7.2. Table Design**

Suggested logical schema:

```
fact_daily_entity_metrics
```

| Column | Type |
| ----- | ----- |
| date | Date |
| entity\_id | String |
| entity\_type | LowCardinality(String) |
| successful\_orders | UInt64 |
| active\_users | UInt64 |
| new\_users | UInt64 |
| returning\_users | UInt64 |
| organic\_orders | UInt64 |
| non\_organic\_orders | UInt64 |
| active\_merchants | UInt64 |
| new\_merchants | UInt64 |
| campaign\_spend | Float64 |
| campaign\_impressions | UInt64 |
| campaign\_clicks | UInt64 |
| data\_snapshot\_id | String |
| extraction\_timestamp | DateTime |

This is a logical contract. Physical types and table engine must be adjusted to the actual DWH and storage environment.

## **7.3. Missing Dates**

Every active Leaf Entity must have a complete daily calendar.

If an entity has no successful orders on a date, its Target should be zero only when the source data is complete and the entity was in scope.

Do not automatically turn missing source data into zero.

Distinguish:

* Genuine zero orders  
* Missing source partition  
* Entity not yet active  
* Entity deactivated  
* Entity mapping unavailable

## **7.4. Data Snapshots**

Every extraction must record:

* snapshot\_id  
* extraction\_start  
* extraction\_end  
* source\_max\_date  
* query\_version  
* extraction\_timestamp  
* record\_count  
* validation\_status

This makes the historical dataset reproducible.

---

# **8\. Feature Engineering**

Feature engineering should be implemented in Python using modular functions.

## **8.1. Calendar**

* weekday  
* weekend\_flag  
* Persian day  
* Persian month  
* Persian year  
* Gregorian month  
* Gregorian year  
* holiday\_flag  
* holiday\_type  
* days\_to\_holiday  
* days\_after\_holiday  
* days\_to\_month\_end  
* days\_to\_month\_start  
* month\_start  
* month\_end

Use a tested Persian calendar library.

The internal date key should remain Gregorian Date.

## **8.2. Lag Features**

Target lags:

```
lag_1
lag_2
lag_3
lag_7
lag_14
lag_21
lag_28
lag_30
lag_56
lag_90
lag_364
```

Lag 365 may also be tested, but should not automatically be treated as equivalent to the same Persian calendar date in the previous year.

## **8.3. Rolling Features**

```
rolling_mean_7
rolling_mean_14
rolling_mean_28
rolling_mean_30
rolling_mean_90

rolling_std_7
rolling_std_14
rolling_std_30

rolling_min_7
rolling_max_7
```

All rolling features must be calculated using historical observations only.

For target date t:

```
rolling_mean_7 = mean(y[t-7:t])
```

Never include y\[t\] in a feature used to predict y\[t\].

## **8.4. User Features**

* active\_users  
* new\_users  
* returning\_users  
* user\_growth\_7d  
* user\_growth\_30d  
* orders\_per\_active\_user  
* repeat\_user\_rate  
* user\_retention  
* average\_days\_between\_orders

## **8.5. Merchant Features**

* active\_merchants  
* new\_merchants  
* deactivated\_merchants  
* merchant\_growth\_30d  
* merchant\_productivity  
* merchant\_availability  
* merchant\_activation\_rate

## **8.6. Marketing Features**

* campaign\_active  
* campaign\_type  
* planned\_budget  
* planned\_discount  
* campaign\_duration  
* days\_to\_campaign  
* days\_after\_campaign  
* planned\_impressions  
* planned\_clicks  
* campaign\_intensity

Actual future spend, clicks and impressions must never be used as known future inputs.

## **8.7. Event Features**

* event\_flag  
* event\_type  
* days\_to\_event  
* days\_after\_event  
* event\_duration  
* event\_intensity  
* event\_campaign\_flag

## **8.8. Lifecycle Features**

* active\_user\_growth\_30d  
* active\_merchant\_growth\_30d  
* order\_growth\_30d  
* order\_growth\_90d  
* merchant\_productivity\_growth  
* repeat\_purchase\_trend  
* vertical\_expansion\_flag  
* venture\_launch\_flag  
* policy\_change\_flag  
* order\_volatility\_30d

---

# **9\. Feature Availability Contract**

Every feature must be assigned one availability category.

| Category | Description |
| ----- | ----- |
| FUTURE\_KNOWN | Known at forecast origin |
| FUTURE\_ESTIMATED | Requires an independent forecast |
| SCENARIO\_BASED | Provided as a scenario assumption |
| FUTURE\_UNKNOWN | Not available for forecasting |

Example:

| Feature | Availability |
| ----- | ----- |
| Weekday | Future Known |
| Holiday Calendar | Future Known |
| Confirmed Campaign Budget | Future Known |
| Future Active Users | Future Estimated |
| Future Actual Spend | Future Unknown |
| Unconfirmed Campaign | Scenario Based |
| Historical Orders | Historical |

## **9.1. Point-in-Time Correctness**

For every historical Forecast Origin, the system must reconstruct the information available at that point in time.

This applies to:

* Campaign plans  
* Merchant status  
* Venture mapping  
* Marketing budget  
* User estimates  
* Product launches  
* Business policies

A value updated after the forecast date must not appear in historical backtests.

---

# **10\. Forecasting Model Architecture**

## **10.1. Baseline Models**

Build the following first:

* Naive  
* Seasonal Naive  
* Moving Average  
* ETS  
* AutoARIMA

These are mandatory benchmarks.

## **10.2. Main Machine Learning Model**

Primary model:

**LightGBM \+ MLForecast**

Why:

* Suitable for tabular time-series features  
* Handles nonlinear relationships  
* Supports shared learning across entities  
* Works with exogenous features  
* Supports horizon-specific modeling  
* Can be combined with SHAP

Challenger:

**CatBoost**

Optional later challenger:

XGBoost.

## **10.3. Global and Separate Models**

Evaluate three strategies:

### **Strategy A — Global Model**

One model across all entities.

### **Strategy B — Separate Models**

One model per Entity or Entity group.

### **Strategy C — Hybrid**

Shared model for similar entities and separate models for unusual or very large entities.

Initial setup:

* Global model for the 7 Verticals.  
* Global model for the Venture Portfolio.  
* Independent benchmarks for large or unusual Ventures.  
* Total Business derived from Leaf forecasts.

The final model selection must be based on rolling backtests.

## **10.4. Forecasting Horizons**

The system must produce a daily forecast path for D+1 through D+30.

Use direct multi-horizon forecasting as the initial strategy.

Do not use a simple recursive strategy as the only production model, because recursive forecast errors can accumulate.

Evaluate:

* D+1 accuracy  
* D+7 accuracy  
* D+30 accuracy  
* Full 30-day path accuracy

---

# **11\. Hierarchical Reconciliation**

## **11.1. Objective**

All Forecasts must be mathematically consistent.

```
Total Forecast
=
Sum(Vertical Forecasts)
+
Sum(Venture Forecasts)
```

## **11.2. Methods**

Benchmark:

1. Bottom-Up  
2. Top-Down  
3. MinTrace

Initial recommendation:

* Bottom-Up as the guaranteed coherent baseline.  
* MinTrace as the main challenger.  
* Top-Down as a comparison method.

Use the HierarchicalForecast library.

## **11.3. Summing Matrix**

Construct the Summing Matrix dynamically from the Entity Master.

Do not hardcode a fixed number of Ventures.

The system must support:

* Adding a Venture  
* Deactivating a Venture  
* Changing Entity metadata  
* Historical Entity changes  
* Rebuilding the hierarchy

## **11.4. Count Constraints**

Orders are non-negative integer counts.

Forecasting models may produce decimal values.

Use a controlled rounding procedure that preserves aggregation consistency.

Do not independently round every Entity and then sum them without reconciliation.

The rounded child forecasts must sum exactly to the rounded parent forecast.

---

# **12\. Lifecycle Detection Engine**

Purpose:

Identify structural changes in business growth.

Candidate regimes:

* Launch  
* Early Growth  
* Scale-up  
* Maturity  
* Structural Transformation

Methods:

* PELT  
* Bayesian Change Point Detection  
* Hidden Markov Models

MVP:

ruptures with PELT.

Use independent business indicators such as:

* User growth  
* Merchant growth  
* Merchant productivity  
* Repeat purchase trend  
* Vertical expansion

Lifecycle detection must be point-in-time correct.

A future structural change must not influence a historical forecast.

Do not define lifecycle exclusively through the Target Orders series.

---

# **13\. Backtesting Framework**

## **13.1. Mandatory Method**

Rolling Origin / Expanding Window.

Random Train/Test Split is prohibited.

Example:

```
Origin 1:
Train → Forecast 30 Days → Compare Actual

Origin 2:
Expand Training Window → Forecast 30 Days → Compare Actual

Origin 3:
Expand Training Window → Forecast 30 Days → Compare Actual
```

Every historical run must simulate the actual daily forecasting process.

## **13.2. Evaluation**

Evaluate separately for:

* Total Business  
* Each Vertical  
* Each Venture  
* D+1  
* D+7  
* D+30  
* Full daily forecast path

Also evaluate:

* Holidays  
* Campaign days  
* Event periods  
* High-growth periods  
* Low-volume entities

## **13.3. Metrics**

Primary:

```
WAPE = SUM(ABS(Actual - Forecast)) / SUM(Actual)
```

Bias:

```
Bias = SUM(Forecast - Actual) / SUM(Actual)
```

Secondary:

* MAE  
* RMSE  
* MASE  
* RMSSE

MAPE must not be the primary metric.

For Total Business, WAPE can conceal poor performance in small entities; therefore entity-level metrics must always be reported.

## **13.4. Model Selection**

A model is promoted only if:

* It beats the baseline on the approved evaluation criteria.  
* Bias is within the approved tolerance.  
* It is stable across forecast origins.  
* It does not substantially degrade important entities.  
* It passes data leakage tests.  
* It passes reconciliation tests.

Set WAPE and Bias acceptance thresholds after measuring baseline performance.

---

# **14\. Prediction Intervals**

The Engine must return:

* Point Forecast  
* Lower 80%  
* Upper 80%  
* Lower 90%  
* Upper 90%

Recommended first approach:

Conformal Prediction.

Alternative:

Quantile Regression.

Evaluate coverage by:

* Horizon  
* Entity Type  
* Entity  
* Volume segment

For reconciled forecasts, ensure uncertainty estimates remain meaningful after reconciliation.

---

# **15\. Explainability**

Use SHAP for Tree-based models.

Output:

* Top positive drivers  
* Top negative drivers  
* Driver contribution  
* Comparison against baseline  
* Historical driver behavior

SHAP values are model attributions, not proof of causality.

Marketing ROI and causal impact require a separate causal inference project.

---

# **16\. Scenario Engine**

The Engine should support What-if scenarios.

Examples:

* Campaign budget \+20%  
* Active Merchants \+10%  
* New Venture launch  
* User growth \+15%  
* Discount increase  
* Campaign duration change

Each scenario must include:

```
scenario_id
scenario_name
forecast_origin
assumptions
changed_features
model_version
output_forecast
```

Scenario Forecast must never overwrite the Base Forecast.

A scenario output is conditional on its assumptions; it does not establish causal impact.

---

# **17\. Technology Stack**

| Layer | Technology |
| ----- | ----- |
| DWH | ClickHouse |
| SQL Extraction | SQL |
| Python Connection | clickhouse-connect |
| Data Processing | Pandas / Polars |
| Forecasting Framework | MLForecast |
| Statistical Models | StatsForecast |
| Main ML Model | LightGBM |
| Challenger | CatBoost |
| Reconciliation | HierarchicalForecast |
| Change Point Detection | ruptures |
| Explainability | SHAP |
| Experiment Tracking | MLflow |
| Data Validation | Pandera |
| API | FastAPI |
| Testing | pytest |
| Scheduling | Cron initially / Airflow later |
| Deployment | Docker |
| Environment | VPS → Company Server |

Use a minimal MVP stack. Do not introduce Airflow, a dedicated Feature Store or Kubernetes unless operational complexity justifies them.

---

# **18\. Repository Structure**

```
bnpl-forecast-engine/
│
├── configs/
│   ├── base.yaml
│   ├── database.yaml
│   ├── target.yaml
│   ├── hierarchy.yaml
│   ├── features.yaml
│   ├── models.yaml
│   └── monitoring.yaml
│
├── sql/
│   ├── discovery/
│   ├── validation/
│   ├── extraction/
│   └── marts/
│
├── src/
│   ├── ingestion/
│   │   ├── clickhouse_client.py
│   │   └── extract.py
│   │
│   ├── validation/
│   │   ├── schema.py
│   │   ├── data_quality.py
│   │   └── leakage.py
│   │
│   ├── data_mart/
│   │   ├── orders.py
│   │   ├── users.py
│   │   ├── merchants.py
│   │   ├── marketing.py
│   │   ├── calendar.py
│   │   └── entity_master.py
│   │
│   ├── features/
│   │   ├── calendar.py
│   │   ├── lags.py
│   │   ├── rolling.py
│   │   ├── user.py
│   │   ├── merchant.py
│   │   ├── marketing.py
│   │   ├── events.py
│   │   └── lifecycle.py
│   │
│   ├── lifecycle/
│   │   ├── change_points.py
│   │   └── regimes.py
│   │
│   ├── models/
│   │   ├── baselines.py
│   │   ├── global_lightgbm.py
│   │   ├── global_catboost.py
│   │   ├── venture_models.py
│   │   └── model_factory.py
│   │
│   ├── evaluation/
│   │   ├── backtest.py
│   │   ├── metrics.py
│   │   └── model_selection.py
│   │
│   ├── reconciliation/
│   │   ├── hierarchy.py
│   │   ├── summing_matrix.py
│   │   └── reconcile.py
│   │
│   ├── explainability/
│   │   └── shap.py
│   │
│   ├── scenarios/
│   │   └── engine.py
│   │
│   ├── serving/
│   │   ├── api.py
│   │   └── service.py
│   │
│   └── monitoring/
│       ├── data_monitor.py
│       ├── model_monitor.py
│       └── alerts.py
│
├── tests/
│   ├── test_sql_contract.py
│   ├── test_data_quality.py
│   ├── test_target.py
│   ├── test_hierarchy.py
│   ├── test_features.py
│   ├── test_leakage.py
│   ├── test_backtesting.py
│   └── test_reconciliation.py
│
├── scripts/
│   ├── discover_sources.py
│   ├── extract_history.py
│   ├── build_data_mart.py
│   ├── train.py
│   ├── backtest.py
│   └── daily_forecast.py
│
├── artifacts/
├── reports/
├── notebooks/
├── tests/
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

The duplicate tests/ entry in this illustrative tree should be consolidated into one directory during repository setup.

---

# **19\. Configuration Management**

All environment-specific configuration must be externalized.

Example:

```
project:
  name: bnpl_forecast_engine
  timezone: Asia/Tehran
  frequency: D

data:
  source: clickhouse
  history_years: 3
  target: successful_orders

forecast:
  horizons: [1, 7, 30]
  full_daily_path: true
  interval_levels: [80, 90]

hierarchy:
  levels:
    - total
    - vertical
    - venture

  aggregation:
    total:
      - vertical
      - venture

models:
  baseline:
    - seasonal_naive
    - ets
    - auto_arima

  primary:
    - lightgbm

  challenger:
    - catboost

evaluation:
  method: rolling_origin
  horizons: [1, 7, 30]

training:
  retraining_frequency: weekly
  production_promotion_requires_backtest: true
```

Database credentials must never be stored in YAML, source code or Git.

Use environment variables or a secret manager.

---

# **20\. Database Connection**

Use clickhouse-connect as the initial Python driver.

Example:

```
import os
import clickhouse_connect

client = clickhouse_connect.get_client(
    host=os.environ["CLICKHOUSE_HOST"],
    port=int(os.environ["CLICKHOUSE_PORT"]),
    username=os.environ["CLICKHOUSE_USER"],
    password=os.environ["CLICKHOUSE_PASSWORD"],
    database=os.environ.get("CLICKHOUSE_DATABASE", "default"),
    secure=os.environ.get("CLICKHOUSE_SECURE", "true").lower() == "true"
)
```

Connection settings must be configurable for:

* Personal VPS  
* Company environment

The same codebase must work in both environments.

Use read-only credentials for the extraction process.

Do not expose database credentials to coding agents or commit them to Git.

---

# **21\. Forecast Output Contract**

Every Forecast Run must include:

```
forecast_run_id
forecast_origin
target_date
entity_id
entity_type
horizon
model_name
model_version
feature_version
data_snapshot_id
yhat
lower_80
upper_80
lower_90
upper_90
reconciled_yhat
scenario_id
```

Example:

```
{
  "forecast_run_id": "run_20261114_001",
  "forecast_origin": "2026-11-14",
  "target_date": "2026-11-15",
  "entity_id": "FASHION",
  "entity_type": "VERTICAL",
  "horizon": 1,
  "yhat": 15200,
  "lower_90": 13900,
  "upper_90": 16700,
  "model_version": "lightgbm_v1"
}
```

This is an illustrative output contract, not a real forecast.

---

# **22\. VPS and Production Deployment**

## **22.1. Development Environment**

The initial development environment is a personal VPS.

Requirements:

* Linux  
* Python virtual environment  
* Git  
* Docker  
* Environment variables  
* Secure ClickHouse connectivity  
* Persistent artifact storage  
* Logging

## **22.2. Development / Production Separation**

Use separate configurations:

```
.env.development
.env.production
```

Do not copy production credentials to the development repository.

If production DWH access is restricted, use approved read-only credentials and network allowlisting.

## **22.3. Deployment Strategy**

```
PERSONAL VPS
     |
     | Develop
     v
Git Repository
     |
     | Tests + Docker Build
     v
Company Server
     |
     | Production Configuration
     v
Daily Forecast Execution
```

Production deployment must not depend on local paths or developer-specific settings.

## **22.4. Scheduling**

For MVP, a daily Cron job is sufficient.

Later, Airflow can be introduced if there are multiple dependent pipelines or more advanced scheduling needs.

---

# **23\. Development Roadmap**

The roadmap is divided into implementation phases with explicit deliverables and exit criteria.

## **Phase 0 — Source Discovery and Business Contract**

**Deliverables:**

* Source schema report  
* Target definition  
* Date field decision  
* Commercial mapping validation  
* Entity classification  
* Data coverage report  
* Data quality report

**Tasks:**

1. Inspect source schemas.  
2. Compare date fields.  
3. Inspect statuses.  
4. Validate deletion behavior.  
5. Validate ONLINE scope.  
6. Validate payment methods.  
7. Inspect Vertical and Venture mappings.  
8. Identify mapping history.  
9. Calculate daily order counts for sample periods.  
10. Reconcile SQL outputs with existing Commercial reports.

**Exit criteria:**

No unresolved ambiguity in Target Definition or Entity Classification.

## **Phase 1 — SQL Extraction Layer**

**Deliverables:**

* Versioned SQL files  
* ClickHouse connection module  
* Parameterized extraction scripts  
* Historical extraction process  
* Query execution logs

**Tasks:**

1. Implement daily order extraction.  
2. Implement Entity mapping.  
3. Implement daily User aggregation.  
4. Implement Merchant aggregation.  
5. Implement calendar extraction.  
6. Add query-level validation.  
7. Add incremental extraction support.  
8. Add historical partition extraction.

**Exit criteria:**

The same extraction code works for historical and incremental data.

## **Phase 2 — Data Mart and Data Quality**

**Deliverables:**

* Daily Leaf Data Mart  
* Entity Master  
* Complete daily calendar  
* Data validation reports  
* Snapshot tracking

**Tasks:**

1. Create stable Entity IDs.  
2. Create hierarchy configuration.  
3. Generate daily entity rows.  
4. Distinguish missing data from zero orders.  
5. Validate duplicate orders.  
6. Validate unmapped Venture IDs.  
7. Validate Total aggregation.  
8. Save snapshots.

**Exit criteria:**

For every date:

```
Total Orders
=
Vertical Orders
+
Venture Orders
```

within the approved Target scope.

## **Phase 3 — Baseline Forecasting**

**Deliverables:**

* Seasonal Naive  
* Moving Average  
* ETS  
* AutoARIMA  
* Rolling Backtesting Framework  
* Baseline Scorecard

**Tasks:**

1. Build historical train/test windows.  
2. Implement forecast-origin simulation.  
3. Evaluate D+1, D+7 and D+30.  
4. Evaluate each Entity.  
5. Generate WAPE and Bias.  
6. Save backtest results.

**Exit criteria:**

Baseline performance is measurable and reproducible.

## **Phase 4 — Feature Engineering**

**Deliverables:**

* Calendar Features  
* Lag Features  
* Rolling Features  
* User Features  
* Merchant Features  
* Marketing Features  
* Event Features  
* Feature Availability Registry

**Tasks:**

1. Build feature functions.  
2. Add point-in-time availability rules.  
3. Implement missing-value handling.  
4. Add feature tests.  
5. Add leakage tests.  
6. Generate feature documentation.

**Exit criteria:**

All features can be recreated for historical Forecast Origins without future leakage.

## **Phase 5 — Machine Learning MVP**

**Deliverables:**

* Global LightGBM  
* CatBoost Challenger  
* Direct Multi-Horizon Forecasting  
* Model Comparison Report  
* Feature Importance

**Tasks:**

1. Build MLForecast pipeline.  
2. Train global Vertical model.  
3. Train global Venture model.  
4. Benchmark large Ventures independently.  
5. Compare direct horizon strategies.  
6. Tune model parameters.  
7. Evaluate against baselines.  
8. Register model artifacts.

**Exit criteria:**

At least one model outperforms the baseline according to approved criteria without unacceptable degradation in important entities.

## **Phase 6 — Hierarchical Reconciliation**

**Deliverables:**

* Summing Matrix  
* Bottom-Up  
* MinTrace  
* Top-Down Benchmark  
* Reconciliation Report

**Tasks:**

1. Build hierarchy from Entity Master.  
2. Generate base forecasts.  
3. Apply Bottom-Up.  
4. Apply MinTrace.  
5. Compare reconciled errors.  
6. Implement consistent rounding.  
7. Validate all aggregation constraints.

**Exit criteria:**

All forecasts reconcile exactly across the hierarchy.

## **Phase 7 — Prediction Intervals and Explainability**

**Deliverables:**

* 80% and 90% intervals  
* Conformal Prediction  
* SHAP Drivers  
* Forecast Explanation Report

**Tasks:**

1. Calibrate intervals.  
2. Evaluate coverage.  
3. Analyze interval width.  
4. Extract SHAP values.  
5. Generate positive and negative drivers.  
6. Separate model attribution from causal analysis.

**Exit criteria:**

Prediction intervals have measured empirical coverage and drivers can be traced to model features.

## **Phase 8 — Lifecycle and Scenario Engine**

**Deliverables:**

* Change Point Detection  
* Lifecycle Features  
* Scenario Configuration  
* What-if Forecasts

**Tasks:**

1. Implement PELT.  
2. Test change points on historical data.  
3. Add structural regime features.  
4. Build scenario input validation.  
5. Run scenario forecasts.  
6. Compare Base and Scenario outputs.

**Exit criteria:**

Scenario runs are reproducible and do not overwrite production Base Forecasts.

## **Phase 9 — Production Deployment**

**Deliverables:**

* Docker Image  
* Daily Scheduler  
* Forecast API  
* Monitoring  
* Model Registry  
* Deployment Documentation

**Tasks:**

1. Create production Docker image.  
2. Configure environment variables.  
3. Add daily job.  
4. Add failure alerts.  
5. Add model rollback.  
6. Add forecast persistence.  
7. Add API.  
8. Deploy to company server.

**Exit criteria:**

A full daily Forecast Run can execute automatically, produce validated output and be traced to its inputs and model version.

---

# **24\. Mandatory Testing**

The following tests are mandatory:

## **Data Tests**

* No duplicate order IDs in Target aggregation.  
* No duplicate Entity mappings.  
* No unexplained unmapped orders.  
* No missing source partitions.  
* No unexpected negative order counts.  
* No unexplained historical discontinuities.

## **Feature Tests**

* No future target values in features.  
* No future actual campaign spend.  
* Rolling windows exclude the target date.  
* Lag values match manually calculated examples.  
* Calendar features are correct.  
* Feature schemas are stable.

## **Hierarchy Tests**

* No Venture double-counting.  
* No missing active Entity.  
* No duplicate Leaf Entity.  
* Parent sums equal child sums.  
* Rounding preserves reconciliation.

## **Model Tests**

* Backtest uses rolling origin.  
* Model output is non-negative.  
* Forecast horizon is complete.  
* Forecast dates are correct.  
* Model artifacts are versioned.  
* Prediction intervals are ordered correctly.

---

# **25\. Production Monitoring**

Daily monitoring:

### **Data Monitoring**

* Source freshness  
* Data completeness  
* Unmapped orders  
* Entity mapping changes  
* Missing feature values  
* Daily order anomalies

### **Model Monitoring**

* WAPE  
* Bias  
* MAE  
* Forecast drift  
* Interval coverage  
* Error by Entity  
* Error by Horizon

### **Operational Monitoring**

* Job execution status  
* Query runtime  
* Model runtime  
* API availability  
* Storage usage  
* Last successful Forecast Run

A failed validation must block silent publication of an invalid Forecast.

---

# **26\. Coding Agent Operating Instructions**

The following rules should be provided to every AI coding agent working on this repository.

1. Read this architecture document before changing code.  
2. Do not invent source tables or database columns.  
3. Inspect the real ClickHouse schema before writing production SQL.  
4. Use the approved Target Definition.  
5. Keep SQL extraction separate from Python modeling.  
6. Never hardcode database credentials.  
7. Never use random train/test splitting.  
8. Never allow future data leakage.  
9. Keep every module independently testable.  
10. Write unit tests with each feature function.  
11. Use configuration files instead of hardcoded business rules.  
12. Never hardcode the number of Ventures.  
13. Build hierarchy dynamically from Entity Master.  
14. Preserve the distinction between Vertical and Venture.  
15. Never double-count Venture orders.  
16. Always validate Total against Leaf aggregation.  
17. Use Baseline models before complex models.  
18. Do not introduce Deep Learning without measurable benefit.  
19. Do not claim SHAP values are causal effects.  
20. Do not promote a model without backtest results.  
21. Preserve existing working code unless a change is justified.  
22. Document all assumptions.  
23. Report all unresolved schema or business ambiguities.  
24. Run tests before declaring a task complete.  
25. Provide a concise implementation report after every milestone.

## **Required Output from Each Coding Task**

Every task should return:

* Files created  
* Files changed  
* Technical decisions  
* Assumptions  
* Tests executed  
* Test results  
* Known limitations  
* Next implementation step

Do not ask the coding agent to build the entire Engine in one prompt.

---

# **27\. Definition of Done**

The Engine is considered production-ready when:

1. All approved Entities are represented.  
2. Historical data is available for the approved time range.  
3. Daily Data Mart is complete.  
4. Target definition is stable.  
5. SQL extraction is reproducible.  
6. Feature engineering is tested.  
7. Leakage prevention is implemented.  
8. Rolling backtesting is available.  
9. Baselines are benchmarked.  
10. Global ML models are trained and evaluated.  
11. Total, Vertical and Venture forecasts reconcile.  
12. Prediction intervals are validated.  
13. Forecast drivers are available.  
14. Model and data versions are traceable.  
15. Daily automation works.  
16. Production monitoring and rollback are available.

---

# **28\. Final Architecture Principles**

The implementation order must be:

```
BUSINESS CONTRACT
       |
       v
SOURCE DISCOVERY
       |
       v
SQL EXTRACTION
       |
       v
DATA MART
       |
       v
DATA QUALITY
       |
       v
FEATURE ENGINEERING
       |
       v
BASELINE FORECASTING
       |
       v
GLOBAL MACHINE LEARNING
       |
       v
HIERARCHICAL RECONCILIATION
       |
       v
PREDICTION INTERVALS
       |
       v
EXPLAINABILITY
       |
       v
SCENARIO ENGINE
       |
       v
PRODUCTION
```

The primary engineering priority is data reliability, not model complexity.

The system must be designed to support the addition of new Ventures, new Verticals, new features and new forecasting algorithms without rewriting its core architecture.

The first production objective is a reliable, explainable and reproducible daily forecast—not an unnecessarily complex AI platform.

