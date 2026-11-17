# برگهٔ درخواست داده — پیش‌بینی سفارش BNPL

این برگه را به تیم داده بدهید. هر چیزی که اینجا نیامده، لازم نیست؛ هر چیزی
که خلاف اینجا باشد، مدل را متوقف می‌کند. مرجع فنی کامل: `specs/data-contract.md`.

## ۱. بازه

- **از ۴۰۰ روز پیش از دیروز تا دیروز**؛ کمتر از ۱۲۰ روز، هشدار کیفیت و
  اعتبار بک‌تست پایین می‌آورد.
- پهنای بازه نیم‌باز: `registered_date >= start` و `registered_date < end`.
- **همهٔ روزها** باید باشند؛ روز بدون فروش با ردیف صفر می‌آید، نه حذف.
- امروز داخل فایل نباشد؛ روز ناقص، آموزش مدل را خراب می‌کند.

## ۲. ستون‌ها (به همین نام و همین ترتیب)

| # | سرستون | الزامی | نوع | نمونه |
| --- | --- | --- | --- | --- |
| ۱ | `date` | ✅ | تاریخ `YYYY-MM-DD` | `2026-11-16` |
| ۲ | `vertical` | ✅ | متن | `DIGITAL` |
| ۳ | `category` | ✅ | متن | `Laptop` |
| ۴ | `payment_method_type` | ➖ | متن | `INSTALLMENT` |
| ۵ | `orders` | ✅ | عدد صحیح | `3711` |
| ۶ | `gmv` | ✅ | عدد به تومان | `28436156909` |
| ۷ | `active_merchants` | ➖ | عدد صحیح | `42` |

- یک ردیف = یک روز × یک ورتیکال × یک دسته × یک روش پرداخت.
- `orders` تعداد سفارش موفق است، نه مبلغ و نه تعداد آیتم.
- `gmv` جمع `amount` همان سفارش‌هاست، تقسیم بر ۱۰ (ریال → تومان).
- ستون اضافه نگذارید؛ ستون‌های `Year` و `Month` لازم نیستند.

## ۳. مقادیر مجاز

**ورتیکال:** `DIGITAL`، `FASHION`، `FMCG`، `GOLD`، `H&B`، `Home`،
`Services`، و `Snapp-Venture` جدا.

**دسته:** نام دستهٔ دیتابیس؛ برای `Snapp-Venture` مقدار `ventures`.

**روش پرداخت:** `INSTALLMENT`، `POSTPAID`، `FINANCING`، `PAY_NOW`، `WALLET`،
`PAY_PRO`. خالی را `UNKNOWN` بگذارید و حذف نکنید.

**قواعد کسب‌وکار که در کوئری اعمال می‌شود:**

- `status = 'SETTLE'` فقط.
- `merchant_type = 'ONLINE'` فقط.
- `deleted_at IS NULL` (بدون سفارش حذف‌شده).
- سفارش‌های برگشتی/ریفاند (`REVERSED`, `REFUNDED`, `REFUND`, `REVERSAL`) بیرون.
- تاریخ مبنا: `registered_date`.

## ۴. چیزهایی که فایل را رد می‌کند

1. عدد منفی در `orders` یا `gmv` (خطای مسدودکننده).
2. حتی یک ردیف با تاریخ خالی یا نامفهوم (خطای مسدودکننده).
3. نبودن یکی از ستون‌های `date`، `vertical`، `category`، `orders`، `gmv`.
4. `gmv` ریالی به‌جای تومانی (خروجی هم ریالی می‌ماند و بی‌معنا می‌شود).
5. دو ستون `gmv` یا دو ستون تاریخ (اولی برنده می‌شود، دومی بی‌صدا نادیده).

کلید تکراری فایل را رد نمی‌کند ولی هشدار می‌دهد و ردیف‌ها جمع می‌شوند؛ اگر
مجموع عمدی است (چند روش پرداخت)، اشکالی ندارد.

## ۵. شکل تحویل

- یک فایل `.xlsx` با **یک برگه**. CSV پذیرفته نمی‌شود.
- نام برگه مهم نیست؛ سرستون‌ها شناسایی می‌شوند.
- فایل را با همین نام تحویل دهید: `bnpl_orders_daily_<start>_<end>.xlsx`.

## ۶. کنترل‌های قبل از تحویل

```sql
-- نگاشت چندپهلو (باید خالی باشد)
SELECT toUInt64(venture_id) AS venture_id, count() AS rows
FROM minio_db.commercial_data
WHERE venture_id IS NOT NULL
GROUP BY venture_id
HAVING rows > 1;

-- سفارش بدون ورتیکال (باید نزدیک صفر باشد)
SELECT count() AS unmapped_orders
FROM fms_credit.tbl_order AS O
LEFT JOIN minio_db.commercial_data AS CD
    ON toUInt64(CD.venture_id) = toUInt64(O.venture_id)
WHERE O.registered_date >= toDate('{{start_date}}')
  AND O.registered_date <  toDate('{{end_date}}')
  AND O.status = 'SETTLE'
  AND O.merchant_type = 'ONLINE'
  AND O.deleted_at IS NULL
  AND (CD.venture_id = '' OR CD.venture_id IS NULL);

-- روش پرداخت (نباید مقدار ناشناخته داشته باشد)
SELECT payment_method_type, count() AS orders
FROM fms_credit.tbl_order
WHERE registered_date >= toDate('{{start_date}}')
  AND registered_date <  toDate('{{end_date}}')
  AND status = 'SETTLE'
GROUP BY payment_method_type
ORDER BY orders DESC;
```

## ۷. کوئری تولید فایل

منبع واحد: `specs/data-contract.md` بخش ۳ (همین کوئری، با همان قواعد).

```sql
WITH order_base AS
(
    SELECT
        toDate(O.registered_date)      AS order_date,
        toUInt64(O.venture_id)         AS venture_id,
        O.id                           AS order_id,
        O.payment_method_type          AS payment_method,
        O.amount                       AS amount_rial
    FROM fms_credit.tbl_order AS O
    WHERE O.registered_date >= toDate('{{start_date}}')
      AND O.registered_date <  toDate('{{end_date}}')
      AND O.status = 'SETTLE'
      AND O.status NOT IN ('REVERSED', 'REFUNDED', 'REFUND', 'REVERSAL')
      AND O.merchant_type = 'ONLINE'
      AND O.deleted_at IS NULL
      AND O.venture_id IS NOT NULL   -- بدون ونچر، نگاشت ورتیکال ممکن نیست
),
commercial_mapping AS
(
    -- یک ردیف برای هر venture_id. نگاشت چندپهلو حذف نمی‌شود تا سکوت نکند،
    -- ولی به یک انتخاب دلبخواه هم تبدیل نمی‌شود: در آن حالت ردیف کنار می‌رود
    -- و باید در کوئری کنترل کیفیت شمرده شود.
    SELECT
        toUInt64(venture_id) AS venture_id,
        any(vertical)        AS vertical,
        any(category)        AS category
    FROM minio_db.commercial_data
    WHERE venture_id IS NOT NULL AND vertical IS NOT NULL AND category IS NOT NULL
    GROUP BY venture_id
    HAVING count() = 1
),
mapped AS
(
    SELECT
        B.order_date                  AS date,
        M.vertical                    AS vertical,
        M.category                    AS category,
        B.payment_method              AS payment_method_type,
        B.order_id                    AS order_id,
        B.amount_rial                 AS amount_rial
    FROM order_base AS B
    INNER JOIN commercial_mapping AS M
        ON B.venture_id = M.venture_id
),
daily AS
(
    SELECT
        date, vertical, category, payment_method_type,
        uniqExact(order_id)              AS orders,
        round(sum(amount_rial) / 10, 0)  AS gmv
    FROM mapped
    GROUP BY date, vertical, category, payment_method_type
),
-- شبکهٔ کامل: هر ترکیب ورتیکال×دسته×پرداخت که در بازه دیده شده، برای هر روز
-- حاضر است. بدون این شبکه، روزی که یک دسته صفر فروش داشته اصلاً ردیفی
-- ندارد و موتور آن را «شکاف» می‌بیند، نه «صفر».
leaf_combos AS
(
    -- از خروجی تجمیعی، نه از رکورد خام: ارزان‌تر و همان مجموعه را می‌دهد.
    SELECT DISTINCT vertical, category, payment_method_type FROM daily
),
days AS
(
    SELECT toDate('{{start_date}}') + number AS date
    FROM numbers(greatest(dateDiff('day',
                             toDate('{{start_date}}'),
                             toDate('{{end_date}}')), 0))
),
grid AS
(
    SELECT D.date AS date, C.vertical AS vertical,
           C.category AS category, C.payment_method_type AS payment_method_type
    FROM days AS D
    CROSS JOIN leaf_combos AS C
)
SELECT
    G.date                AS date,
    G.vertical            AS vertical,
    G.category            AS category,
    G.payment_method_type AS payment_method_type,
    coalesce(H.orders, 0) AS orders,
    coalesce(H.gmv, 0)    AS gmv
FROM grid AS G
LEFT JOIN daily AS H
    ON  H.date = G.date
    AND H.vertical = G.vertical
    AND H.category = G.category
    AND H.payment_method_type = G.payment_method_type
ORDER BY date, vertical, category, payment_method_type;
```

نتیجهٔ این کوئری را مستقیم به `.xlsx` بدهید: ستون‌هایش همان بخش ۲ است (به‌جز
`active_merchants`). حجم تقریبی: ۴۰۰ روز × ~۱۳۰ ترکیب ≈ ۵۰ هزار ردیف؛ اکسل
به‌راحتی جا می‌دهد.

