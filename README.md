# media-pipeline — ورکر ویدیو روی گیت‌هاب اکشنز

جای اسپیس داکر هاگینگ‌فیس (که حالا PRO می‌خواد). سرور همیشه‌روشن نداره: هر وقت توی صف اپ کاری باشه، سرور اپ با
`workflow_dispatch` یه اجرا روشن می‌کنه؛ اجرا کار برمی‌داره، انجام می‌ده، کار بعدی رو برمی‌داره و وقتی چیزی نموند (یا ~۵ ساعت گذشت) خاموش می‌شه.

مخزن **عمومی**ه (دقیقه‌های اکشنز رایگان و نامحدود برای مخزن عمومی). پس لاگ‌ها رو همه می‌بینن و هیچ رازی نباید توشون باشه؛ پایین‌تر توضیح داده شده.

## هر کار

1. `claim` از سرور اپ: شمارهٔ کار، توکن Write هاگینگ‌فیس مقصد، مخزن، کلید و IV ویدیو، مسیر `videos/<uuid>`، برچسب پیام تلگرام.
   ربات اپ همون لحظه فایل رو (با file_id، بدون سقف حجم) توی چت خصوصی حساب ورکر با کپشن `#qj…` می‌فرسته.
2. **قفل تلگرام** از سرور (`lease`): یه نشست تلگرام هم‌زمان از چند IP باعث `AUTH_KEY_DUPLICATED` و باطل شدن نشست می‌شه؛
   پس فقط یه اجرا در لحظه به حساب ورکر وصله. دانلود (تا ۲ گیگ، Telethon + cryptg) ← پاک کردن پیام کمکی ← قطع ← آزاد کردن قفل.
   ساخت کیفیت‌ها و آپلود موازی بین اجراها انجام می‌شه.
3. HLS با AES-128 خود HLS (کلید از سرور، توی پلی‌لیست فقط `qkey://<keyId>`):
   - `v0` اصل: اگه ویدیو H.264 سازگار (8 بیت، yuv420p، بدون چرخش، حداکثر 1080p) باشه **بدون تبدیل** (`-c copy`)؛ صدای غیر AAC فقط صداش AAC می‌شه.
     وگرنه 720p با libx264.
   - `v1` 480p با libx264 `veryfast` (اگه منبع بلندتر از 480 باشه).
   - تکه‌های ~۶ ثانیه MPEG-TS، `master.m3u8` با BANDWIDTH/RESOLUTION/CODECS.
4. `upload_folder` روی مخزن dataset عمومی (`videos/<uuid>/`)، یه commit.
5. `done` با پلی‌لیست‌ها و حجم کیفیت‌ها (سرور خودش پلی‌لیست‌ها رو سرو می‌کنه). خطا = `error`؛ سرور با فاصلهٔ نمایی دوباره صف می‌کنه (حداکثر ۵ بار).

هر ۱۵ ثانیه `progress` (و هر ۴۵ ثانیه حتی وسط آپلود): کاری که ۲۰ دقیقه بی‌خبر بمونه دوباره صف می‌شه. اگه ادمین لغو کنه، جواب progress
`cancel` می‌شه و ران‌ر ffmpeg رو می‌کشه و کار رو ول می‌کنه. نوع دوم کار، `move`، یه ویدیو رو به مسیر/مخزن تازه کپی و قبلی رو پاک می‌کنه.

## پروتکل

همهٔ درخواست‌ها `POST {APP_API}/worker`. پلتفرم tgcloud فقط درخواستِ با initData معتبر رو قبول می‌کنه، پس سرآیند
`Authorization: TMA base64(initData)` حساب ورکر (از `messages.requestWebView` روی ربات) لازمه. روی اون بدنه امضای HMAC داره:

```
{ "payload": "<json>", "ts": "…", "nonce": "…", "sig": hex(HMAC-SHA256(WORKER_SECRET, ts\nnonce\nPOST\n/api/worker\npayload)) }
```

op ها: `hello` · `claim` · `lease {on}` · `progress` · `done` · `error` · `init` (initData تازه برای اجراهای بعدی) · `bye` ·
`session` (فقط از `login.py push`). همتای سرور: `tgcloud/endpoints/worker.js` و `tgcloud/lib/worker.js`.

ورودی‌های dispatch: `nonce` (شناسهٔ اجرا) و `boot`: جعبهٔ `h1.` رمزشده با WORKER_SECRET (HMAC در حالت شمارنده + برچسب HMAC؛
`pipeline/crypto.py` ← `seal_boot` و `tgcloud/lib/hmac.js` ← `sealBoot`) شامل api_id/api_hash، نشست رمزشدهٔ v1، یوزرنیم ربات،
آدرس اپ، آخرین initData و زمان انقضا. اسکریپت ورودی رو از فایل رویداد می‌خونه نه env (تا توی سرآیند لاگ نیاد).

## لاگ عمومی

- هر راز رسیده موقع اجرا (توکن هاگینگ‌فیس، کلید/IV، نشست، initData، api_hash، ورودی boot) قبل از هر چیز با `::add-mask::` مخفی می‌شه.
- خود کد فقط شمارهٔ کار، مرحله، مدت و اسم کلاس خطا لاگ می‌کنه؛ متن کامل خطا فقط به سرور اپ می‌ره. نه عنوان، نه اسم فایل، نه اسم دبیر.
- لاگ telethon/httpx/huggingface_hub خاموش، نوار پیشرفت هاب خاموش، ffmpeg با `-loglevel error` و خروجیش فقط توی حافظه.
- `run-name` ثابته؛ `permissions: contents: read`؛ فقط صاحب مخزن می‌تونه dispatch کنه و fork ها به سکرت‌ها دسترسی ندارن.

## راه‌اندازی (یه بار)

```bash
pip install -r requirements-tools.txt

# ۱) مخزن + کد + سکرت‌ها (WORKER_SECRET و APP_API) — توکنی که این‌جا می‌دی فقط روی سیستم خودته
export GH_TOKEN=…            # fine-grained: Administration (برای --create)، Contents، Workflows، Secrets، Actions
export GH_REPO=owner/media-pipeline
export WORKER_SECRET=…       # همونی که توی پنل اپ هست (≥۳۲ کاراکتر)
export APP_API=https://<app-host>/api
python3 setup.py --create

# ۲) ورود حساب تلگرام جدای ورکر (کد به همون حساب میاد)
python3 login.py start --phone +1…
python3 login.py code --code 12345          # اگه خروجی ۱۰ بود:
TG_PASSWORD=… python3 login.py password
#    نشست رمزشده (AES-GCM با کلید مشتق از WORKER_SECRET) + initData → اپ
BOT=<bot-username> APP_URL=https://<app-host>/ python3 login.py push
shred -u tg.session

# ۳) پنل اپ ← فضا و آرشیو ← ورکر ویدیو ← تنظیمات: مخزن گیت‌هاب، یه توکن fine-grained جدا فقط برای همین مخزن
#    با «Actions: Read and write» (برای dispatch و دیدن اجراها)، تعداد اجرای هم‌زمان (پیش‌فرض ۴).
```

api_id/api_hash پیش‌فرض شناسهٔ عمومی تلگرام دسکتاپه (همون پیش‌فرض اپ). اگه توی پنل عوضش کردی، `login.py` رو هم با
`API_ID`/`API_HASH` همون اجرا کن. اگه WORKER_SECRET عوض بشه، نشست دوباره باید `push` بشه (رمزش با همون کلیده).

## آزمون محلی (بدون شبکه)

```bash
pip install -r requirements.txt
python3 -m tests.test_hls     # HLS (copy / تبدیل / فقط صدا)، جعبهٔ boot، کل خط لوله با تلگرام/سرور/هاب قلابی، لغو وسط کار
```

## محدودیت‌ها

- ران‌ر عمومی: ۴ هسته، ۱۶ گیگ رم؛ دیسک: هر کدوم از `/mnt` و `RUNNER_TEMP` که جای بیشتری داره. هر کار ~۲٫۴ برابر حجم فایل جا می‌خواد.
- هر اجرا حداکثر ۳۵۰ دقیقه؛ بعد از ۴٫۷۵ ساعت کار تازه برنمی‌داره.
- دانلودها نوبتی‌اند (قفل تلگرام)؛ با ۴ اجرا، دانلود یکی و تبدیل/آپلود بقیه هم‌زمانه.
