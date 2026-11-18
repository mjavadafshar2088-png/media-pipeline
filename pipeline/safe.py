"""
لاگ امن برای اجرای عمومی گیت‌هاب اکشنز.

لاگ‌های مخزن عمومی رو همه می‌بینن؛ پس:
- هر مقدار رازی که موقع اجرا می‌رسه (توکن هاگینگ‌فیس، کلید، نشست، initData، api_hash) قبل از هر کاری
  با ::add-mask:: به گیت‌هاب معرفی می‌شه تا اگه جایی چاپ شد *** بشه.
- خود کد هم فقط شمارهٔ کار، مرحله و اسم کلاس خطا رو لاگ می‌کنه؛ نه اسم فایل، نه عنوان، نه آدرس.
- لاگ کتابخونه‌ها (telethon، huggingface_hub، httpx) خاموش یا در حد WARNING.
"""
import logging
import os
import sys

_masked: set[str] = set()
IN_GHA = os.environ.get("GITHUB_ACTIONS") == "true"


def mask(*values) -> None:
    """هر رشتهٔ غیرخالی رو (و تکه‌های چندخطی‌ش رو) توی لاگ گیت‌هاب مخفی کن"""
    for v in values:
        if v is None:
            continue
        if isinstance(v, (dict, list, tuple)):
            mask(*(v.values() if isinstance(v, dict) else v))
            continue
        s = str(v)
        for part in [s, *s.splitlines()]:
            part = part.strip()
            if len(part) < 4 or part in _masked:
                continue
            _masked.add(part)
            if IN_GHA:
                # فرمان روی stdout؛ خود این خط توی لاگ نمایش داده نمی‌شه (٪ طبق قاعدهٔ فرمان‌های گیت‌هاب escape)
                esc = part.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
                sys.stdout.write(f"::add-mask::{esc}\n")
                sys.stdout.flush()


def setup_logging() -> logging.Logger:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    for name in ("telethon", "httpx", "httpcore", "huggingface_hub", "urllib3", "filelock", "hf_xet"):
        logging.getLogger(name).setLevel(logging.ERROR)
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("HF_HUB_VERBOSITY", "error")
    return logging.getLogger("worker")


def err_name(e: BaseException) -> str:
    """فقط اسم کلاس خطا برای لاگ عمومی؛ متن کامل فقط به سرور اپ می‌ره"""
    return type(e).__name__
