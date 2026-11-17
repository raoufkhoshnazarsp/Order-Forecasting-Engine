# قرارداد داده — ورودی و خروجی

این سند مرجع دقیقِ «چه فایلی می‌دهیم» و «چه فایلی می‌گیریم» است. هر عددی که
اینجا نیامده، جزء قرارداد نیست.

## ۱. گرید داده

| طرف | گرید |
| --- | --- |
| ورودی | یک ردیف = تاریخ × ورتیکال × دسته × روش پرداخت |
| خروجی | یک ردیف = تاریخ آینده × سطح × ورتیکال × دسته × روش پرداخت |

ورودی در گرید ریزتر از خروجی است؛ موتور روش پرداخت را جمع می‌زند تا مجموع
دسته را پیش‌بینی کند، بعد همان مجموع را با سهم تاریخی روش‌های پرداخت تقسیم
می‌کند (`sum`، هرگز میانگین).

## ۲. ستون‌های ورودی

فایل اکسل با برگه‌ای که این ستون‌ها را دارد، خودکار به‌عنوان
«مدل دادهٔ تجاری» (`daily_metrics`) شناخته می‌شود.

| سرستون | الزامی | نوع | نمونه | معنا |
| --- | --- | --- | --- | --- |
| `date` | ✅ | تاریخ | `2026-07-01` یا `۱۴۰۵-۰۴-۱۰` | روز ثبت سفارش (میلادی یا جلالی) |
| `vertical` | ✅ | متن | `FASHION`، `Snapp-Venture` | ورتیکال؛ مقدار ونچر باید `Snapp-Venture` باشد |
| `orders` | ✅ | عدد صحیح | `3711` | **تعداد** سفارش موفق آن روز (`order` مفرد هم پذیرفته است) |
| `gmv` | ✅ | عدد | `28436156909` | جمع مبلغ سفارش‌های موفق، **به تومان** |
| `category` | ➖ | متن | `Laptop`، `ventures` | دسته؛ برای ونچر باید `ventures` باشد |
| `payment_method_type` | ➖ | متن | `INSTALLMENT`، `PAY_NOW` | روش پرداخت؛ برای تفکیک خروجی |

### مواردی که باید رعایت شود

1. **`order` مفرد، اگر استفاده می‌کنید، باید ستون تعداد باشد.** موتور آن را
   در برگه‌ای که GMV دارد و ستون تعداد ندارد، تعداد سفارش می‌خواند. در برگه‌ای
   که ستون‌های سفارش خام را دارد (`order_id` و `venture_id`) همان نام، شناسهٔ
   سفارش است. هر دو حالت آزمایش شده‌اند؛ برای رفع ابهام، `orders` بگذارید.
2. **واحد GMV تومان باشد.** موتور تقسیم بر ۱۰ انجام نمی‌دهد؛ اگر عدد ریالی
   بدهید، خروجی هم ریالی می‌ماند. تبدیل باید در کوئری یا فایل انجام شود.
3. **دو ستون GMV نگذارید.** ستون تکراری نادیده گرفته می‌شود (اولی برنده است)
   و فقط سردرگمی می‌سازد.
4. **مقدار منفی ندهید.** سفارش یا GMV منفی کل برگه را رد می‌کند (خطای ۴۲۲).
5. **تاریخ خالی یا نامفهوم ندهید.** آن ردیف حذف می‌شود و در گزارش کیفیت
   به‌صورت هشدار می‌آید.
6. **حداقل ۱۲۰ روز تاریخ یکتا** لازم است؛ کمتر از آن هشدار کیفیت است و مدل
   اعتبار کافی ندارد. برای بک‌تست معنادار ۴۰۰ روز یا بیشتر بدهید (سقف توصیهٔ
   موتور). بازهٔ پیشنهادی: **سال کامل گذشته تا دیروز**.
7. **شکاف روزانه نداشته باشید.** شکاف بیش از ۵٪ روز-موجودیت، هشدار می‌دهد.
   روزی که یک دسته صفر فروش داشته باید **ردیف صریح با صفر** داشته باشد، نه
   حذف‌شده؛ کوئری بخش ۳ همین را می‌سازد.
8. **کلید تکراری نگذارید.** اگر یک ترکیب تاریخ × ورتیکال × دسته × روش پرداخت
   دو بار بیاید، جمع می‌شود؛ اگر تفکیک پرداخت ندارید، کلید مؤثر
   تاریخ × ورتیکال × دسته است.

## ۳. کوئری استخراج

پارامترها: `{{start_date}}` شامل، `{{end_date}}` **غیرشامل** (نیم‌باز).
قواعد کسب‌وکار از `configs/target.yaml`: `status = SETTLE`،
`merchant_type = ONLINE`، حذف سفارش‌های soft-delete، و کنارگذاشتن
سفارش‌های برگشتی/ریفاند.

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

نکته‌های کوئری:

- `INNER JOIN` عمداً است: ردیفی که ورتیکال/دسته ندارد، در فایل تجاری جای
  مشخصی ندارد و نباید بی‌صدا به یک ورتیکال حدسی چسبانده شود. اگر می‌خواهید
  اندازهٔ آن را ببینید، همین کوئری را با `LEFT JOIN` و
  `WHERE M.venture_id = 0` یک بار جدا اجرا کنید و تعداد را گزارش کنید.
- `uniqExact(order_id)` تعداد سفارش را می‌شمارد؛ چون گرید خروجی
  «روز × ورتیکال × دسته × پرداخت» است و هر سفارش یک `venture_id` و یک
  `payment_method_type` دارد، جمع این تعدادها روی روش‌های پرداخت همان
  تعداد سفارش ورتیکال می‌شود.
- `/ 10` تبدیل ریال به تومان است. اگر گزارش فعلی‌تان تومانی است، حذفش کنید.
- اگر جدول شما `commercial_data` را با نام دیگری دارد، فقط همان بخش را عوض
  کنید؛ بقیهٔ ساختار دست نمی‌خورد.
- اگر می‌خواهید فقط ورتیکال‌های دامنه را بگیرید، این شرط را به `order_base`
  اضافه کنید (لیست از `configs/target.yaml`):  
  `AND M.vertical IN (...)` — ولی چون `M` در آن مرحله نیست، به کوئری بیرونی
  به‌صورت `WHERE vertical IN ('DIGITAL', 'FASHION', 'FMCG', 'GOLD', 'H&B', 'Home', 'Services')`
  اضافه کنید. ردیف‌های `Snapp-Venture` را حذف نکنید؛ بلوک جداگانهٔ خروجی‌اند.
- اگر ستون اختیاری «مرچنت فعال» را هم می‌خواهید، به `daily` این را اضافه
  کنید: `uniqExact(venture_id) AS active_merchants` (نیازمند بردن
  `venture_id` به `mapped`). بدون آن هم مدل اجرا می‌شود.

### مقادیر مجاز (آنچه موتور می‌شناسد)

| ستون | مقادیر | قاعده |
| --- | --- | --- |
| `vertical` | `DIGITAL`، `FASHION`، `FMCG`، `GOLD`، `H&B`، `Home`، `Services`، `Snapp-Venture` | مقایسه بدون حساسیت به حروف بزرگ/کوچک و `_` است؛ ولی همین هفت نام را بدهید تا ورتیکال ناشناخته ساخته نشود |
| `category` | هر متنی | برای `Snapp-Venture` باید `ventures` باشد؛ برای بقیه، نام دستهٔ دیتابیس |
| `payment_method_type` | `INSTALLMENT`، `POSTPAID`، `FINANCING`، `PAY_NOW`، `WALLET`، `PAY_PRO` | همین شش مقدار را نگه دارید؛ مقدار خالی را `UNKNOWN` بگذارید و حذف نکنید |
| `orders` | عدد صحیح ≥ ۰ | صفر مجاز و معنادار است |
| `gmv` | عدد ≥ ۰ به **تومان** | صفر فقط وقتی `orders = 0`؛ GMV بدون سفارش یعنی داده معیوب |

### آزمایش روی دادهٔ خودتان

فایل `data-model.xlsx` با همین قالب روی موتور اجرا شد: قالب `daily_metrics`
تشخیص داده شد، ۹۶۰ ردیف برگ در بازهٔ ۲۰۲۶-۰۷-۰۱ تا ۲۰۲۶-۱۰-۲۸ ساخته شد،
گیت کیفیت خطای مسدودکننده ندارد (دو هشدار: کلید تکراری روی چند روش پرداخت،
و روز ناقص آخر). یعنی ساختار ستون‌ها و واحد مبلغ همین الان درست است؛ فقط
بازه کوتاه است (۱۲۰ روز).

### کنترل پیش از تحویل

این سه کوئری را قبل از تحویل فایل اجرا کنید؛ اگر ردیفی برگرداند، فایل هنوز
آماده نیست:

```sql
-- ۱) نگاشت چندپهلو در دیکشنری: ردیف‌هایش از فایل بیرون می‌مانند
SELECT toUInt64(venture_id) AS venture_id, count() AS rows
FROM minio_db.commercial_data
WHERE venture_id IS NOT NULL
GROUP BY venture_id
HAVING rows > 1
ORDER BY rows DESC;

-- ۲) سفارش بدون ورتیکال/دسته: این حجم باید نزدیک صفر باشد
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

-- ۳) روش پرداخت ناشناخته: باید صفر باشد، وگرنه ستون پرداخت را خالی ندهید
SELECT payment_method_type, count() AS orders
FROM fms_credit.tbl_order
WHERE registered_date >= toDate('{{start_date}}')
  AND registered_date <  toDate('{{end_date}}')
  AND status = 'SETTLE'
GROUP BY payment_method_type
ORDER BY orders DESC;
```

### شکل تحویل

- یک فایل `.xlsx` (یا `.xlsm`/`.xls`)؛ CSV پذیرفته نمی‌شود.
- یک برگه با سرستون‌های بخش ۲ کافی است؛ برگهٔ اضافه نادیده گرفته نمی‌شود و
  در گزارش کیفیت فهرست می‌شود، پس فقط همان یک برگه را بگذارید.
- نام برگه مهم نیست؛ تشخیص بر اساس سرستون‌ها است.
- بازهٔ پیشنهادی: **۴۰۰ روز پیش از دیروز تا دیروز** (حداقل ۱۸۰ روز). تاریخ
  را میلادی و به شکل `YYYY-MM-DD` بدهید؛ تاریخ شمسی هم پذیرفته و تبدیل
  می‌شود، ولی میلادی ابهام کمتری دارد.


## ۴. فرمت خروجی

ستون‌های فعلی فایل تحویل (`src/serving/delivery.py`):

| ستون | نوع | معنا |
| --- | --- | --- |
| `date` | تاریخ | روز آیندهٔ پیش‌بینی‌شده |
| `level` | متن | همیشه `vertical` |
| `vertical` | متن | ورتیکال، یا یکی از سه برچسب تجمیعی |
| `category` | متن/خالی | دسته؛ برای سطرهای تجمیعی خالی |
| `payment_method` | متن/خالی | روش پرداخت؛ برای سطرهای تجمیعی خالی، و `UNKNOWN` اگر ورودی ستون پرداخت نداشته باشد |
| `order` | عدد | تعداد سفارش پیش‌بینی‌شده |
| `GMV` | عدد | مبلغ پیش‌بینی‌شده، به همان واحد ورودی (تومان) |

قواعد ساخت:

1. `VERTICALS` مجموع دقیق هفت ورتیکال است (نه یک پیش‌بینی مستقل). موتور قبل
   از انتشار این تساوی را بررسی می‌کند و اگر برقرار نباشد فایل ساخته نمی‌شود.
2. `SNAPP-VENTURE` و `SNAPP-PAY` خواهرِ `VERTICALS` هستند، نه فرزندِ آن.
3. `SNAPP-PAY` همیشه در فایل هست؛ اگر داده‌اش نباشد صفر نوشته می‌شود تا
   قالب کامل بماند.
4. ترتیب سطرها: ورتیکال، بعد دسته، بعد روش پرداخت؛ سه سطر تجمیعی در پایان
   هر روز.
5. `order` گرد به ۶ رقم اعشار، `GMV` گرد به عدد صحیح.
6. **مجموع روش‌های پرداخت هر دسته دقیقاً برابر همان دسته است.** سهم‌ها از
   ۲۸ روز آخرِ همان فایل می‌آید؛ باقیماندهٔ گردکردن روی آخرین روش گذاشته
   می‌شود تا جمع دقیق بماند.
7. اگر فایل ورودی ستون روش پرداخت نداشته باشد، همهٔ سطرها `UNKNOWN` می‌گیرند
   و هیچ تفکیکی ساخته نمی‌شود — موتور عدد از خودش درنمی‌آورد.
