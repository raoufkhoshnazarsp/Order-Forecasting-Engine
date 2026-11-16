# استقرار و مانیتورینگ

این سند همان چیزی است که برای بالا نگه‌داشتن سرویس در عمل لازم است: یک اجرای
روزانهٔ خودکار، هشدار وقتی چیزی خراب است، و راه برگشت به نسخهٔ قبلی مدل.

## معماری استقرار

| مؤلفه | جایگاه | نقش |
| --- | --- | --- |
| `forecast-studio` | `127.0.0.1:8000` | FastAPI + UI، پشت nginx روی `8443` |
| `static-report` | `127.0.0.1:8001` | گزارش استاتیک ساخته‌شده از همان image |
| nginx | `0.0.0.0:8443` | تنها ورودی عمومی |

هر دو کانتینر از یک image (`bnpl-forecast-engine:latest`) بالا می‌آیند تا استقرار
فقط یک build لازم داشته باشد.

## استقرار

```bash
./scripts/deploy.sh            # pull main، rebuild، restart، ساخت گزارش استاتیک
./scripts/deploy.sh --no-pull  # فقط rebuild همان checkout
```

اسکریپت در پایان `docker compose ps` و `/healthz` را بررسی می‌کند؛ اگر سالم نبود،
با کد غیرصفر خارج می‌شود.

> **نکتهٔ عملی:** اگر Docker Hub در دسترس نبود (`network is unreachable`)، یک بار
> `docker pull python:3.12-slim` را روی سرور تکرار کنید و سپس build را دوباره بزنید.
> لایه‌های قبلی cache می‌مانند.

## اجرای روزانه

```bash
python scripts/scheduled_run.py --workbook uploads/data-model.xlsx --horizon 30
```

این اسکریپت کل زنجیره را می‌زند: `run_pipeline` → بررسی سلامت → ثبت نسخهٔ مدل →
ثبت اجرا در `artifacts/monitoring/job_runs.json`. خروجی‌ها در
`artifacts/scheduled/<run_id>/` ذخیره می‌شوند (`summary.json`، `forecast.parquet`،
`mart.parquet`، `backtest.parquet`، `health.json`).

**کدهای خروج** — همین‌ها کانال هشدارند:

| کد | معنا | کار |
| --- | --- | --- |
| 0 | همه‌چیز سالم | — |
| 1 | خود پیش‌بینی شکست خورد | لاگ و ورودی را ببینید |
| 2 | پیش‌بینی انجام شد ولی سلامت `error` است | داده را تازه کنید |

cron فقط روی خروجی غیرصفر ایمیل می‌فرستد، پس روزی که داده کهنه است هم به گوش
کسی می‌رسد.

### نصب cron

```bash
python scripts/install_cron.py --dry-run   # فقط نمایش خط
python scripts/install_cron.py             # نصب (06:30 UTC = 10:00 تهران)
```

خط نصب‌شده با نشانگر `# bnpl-forecast-daily` علامت می‌خورد، پس اجرای دوباره
خط قبلی را جایگزین می‌کند نه اینکه تکرارش کند.

**ورودی باید در مسیری باشد که کانتینر می‌بیند.** فقط `artifacts/` و `reports/`
به کانتینر mount می‌شوند؛ فایل داخل `uploads/` برای کانتینر وجود ندارد. مسیر
پیش‌فرض `artifacts/inputs/data-model.xlsx` است. اسکریپت نصب مسیر کانتینر را به
مسیر میزبان نگاشت می‌کند و اگر فایل را نبیند نصب را رد می‌کند (با `--force`
می‌توان رد کرد) — خط cronی که هر روز صبح به فایل ناموجود اشاره کند، بی‌صدا شکست
می‌خورد.

پس از نصب، یک بار خودِ دستور را اجرا کنید؛ نصب‌شدنِ خط ثابت نمی‌کند که خط کار
می‌کند:

```bash
crontab -l | grep bnpl
cd /opt/order-forecasting-engine && docker compose exec -T forecast-studio \
  python scripts/scheduled_run.py --workbook /app/artifacts/inputs/data-model.xlsx
```

## مانیتورینگ

سه سیگنال، چون سه شکست مختلف وجود دارد:

| سیگنال | سؤال | آستانه |
| --- | --- | --- |
| تازگی داده | آخرین مشاهده چند روز عقب است؟ | ۲ روز هشدار، ۴ روز خطا |
| شکاف داده | چند روز-موجودیت خالی است؟ | بیش از ۵٪ هشدار |
| رانش مدل | پوشش ۹۰٪ و WAPE در مبدأهای اخیر | پوشش < ۸۰٪ یا WAPE > ۳۰٪ هشدار |
| سلامت کار | آخرین اجرای زمان‌بندی‌شده چند ساعت پیش؟ | بیش از ۳۶ ساعت هشدار |

سن کار از «آخرین اجرای کامل» حساب می‌شود، چه سالم چه ناسالم: اجرایی که انجام شده ولی
داده را کهنه یافته، ثابت می‌کند زمان‌بندی کار می‌کند. اگر این دو یکی گرفته شوند،
خرابیِ زمان‌بندی با خرابیِ داده قاطی می‌شود و هر کدام درمان دیگری می‌خواهد.

```bash
python scripts/monitor.py health                      # وضعیت کامل
python scripts/monitor.py health --prometheus         # قالب Prometheus
python scripts/monitor.py job                         # سن آخرین اجرای کامل
```

اندپوینت‌ها:

- `GET /api/health` — همان گزارش به‌صورت JSON، به‌همراه نسخهٔ فعال مدل و آخرین اجرا.
  عمداً خط لوله را دوباره اجرا نمی‌کند؛ از آرتیفکت آخرین اجرا می‌خواند.
- `GET /api/health/metrics` — قالب متن Prometheus برای scrape بیرونی.
- `GET /healthz` — فقط liveness؛ زنده بودن پروسه.

## نسخهٔ مدل و rollback

```bash
python scripts/monitor.py registry                    # تاریخچه
python scripts/monitor.py promote --version <id>      # فعال‌کردن یک نسخه
python scripts/monitor.py rollback                    # بازگشت به نسخهٔ فعال قبلی
```

قاعدهٔ ارتقا ساده و صریح است: نسخهٔ تازه فقط وقتی خودکار فعال می‌شود که نسخهٔ فعالی
نباشد یا WAPE بک‌تستش بدتر نباشد. نسخهٔ بدتر به‌عنوان `candidate` می‌ماند تا دیده
شود، ولی جای مدل فعلی را نمی‌گیرد.

`rollback` فقط به نسخه‌ای برمی‌گردد که قبلاً واقعاً فعال بوده است؛ برگشتن به یک
candidate هرگز-فعال‌نشده یعنی عوض‌کردن یک مدل تست‌نشده با مدل تست‌نشدهٔ دیگر.

وزن‌های مدل ذخیره نمی‌شوند؛ از همان ورودی دوباره برازش می‌شوند. ذخیره‌کردن آن‌ها
مدل را بی‌صدا به دادهٔ قدیمی سنجاق می‌کرد.

## مسیر وضعیت

`FORECAST_STATE_DIR` محل `job_runs.json`، `model_registry.json` و `last_run.json` را
عوض می‌کند؛ پیش‌فرض `artifacts/` است که در git نیست.

## چک‌لیست بعد از استقرار

1. `curl -fsS http://127.0.0.1:8000/healthz` → `{"status":"ok"}`
2. `curl -s http://127.0.0.1:8000/api/health` → `status` و `alerts` را بخوانید
3. `docker compose ps` → هر دو کانتینر `healthy`
4. `python scripts/monitor.py job` → `state: ok`
