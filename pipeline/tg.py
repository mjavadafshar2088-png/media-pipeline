"""
حساب کاربری تلگرام ورکر (Telethon، نه ربات) روی ران‌ر گیت‌هاب.

- نشست (StringSession) رمزشده (v1، AES-GCM با کلید مشتق از WORKER_SECRET) از سرور اپ می‌رسه؛ این‌جا باز می‌شه.
- فقط وقتی «قفل تلگرام» سرور دست همین ران‌ره وصل می‌شیم و بلافاصله بعدش قطع می‌کنیم:
  یه نشست هم‌زمان از چند IP = خطای AUTH_KEY_DUPLICATED و باطل شدن نشست (یعنی ورود دوباره با کد).
- initData امضاشدهٔ تلگرام (از RequestWebView روی ربات) لازمه چون پلتفرم tgcloud فقط درخواستِ دارای
  initData معتبر رو به endpoint ها می‌رسونه.
"""
import base64
import os
import time
from urllib.parse import parse_qs, urlparse

from telethon import TelegramClient, functions
from telethon.sessions import StringSession

from .crypto import open_session
from .safe import mask

REQ = 512 * 1024  # اندازهٔ هر درخواست دانلود (حداکثر Telethon)


class NoSession(Exception):
    """نشست تلگرام ورکر نیست یا باز نمی‌شه (WORKER_SECRET عوض شده؟)"""


def session_string(blob: str) -> str:
    """نشست رمزشدهٔ v1 یا (برای آزمون محلی) خام"""
    blob = str(blob or "")
    s = open_session(blob) if blob.startswith("v1.") else blob
    if not s:
        raise NoSession("نشست تلگرام ورکر نیست یا با این WORKER_SECRET باز نمی‌شه؛ دوباره login.py")
    mask(blob, s)
    return s


class TG:
    def __init__(self, cfg: dict):
        """cfg: {apiId, apiHash, session, bot, appUrl}"""
        self.api_id = int(cfg.get("apiId") or 0)
        self.api_hash = str(cfg.get("apiHash") or "")
        self.sess = session_string(cfg.get("session"))
        self.bot = str(cfg.get("bot") or "").lstrip("@")
        self.app_url = str(cfg.get("appUrl") or "")
        mask(self.api_hash)
        self.client: TelegramClient | None = None
        self.me_id: int | None = None

    async def __aenter__(self):
        self.client = TelegramClient(
            StringSession(self.sess),
            self.api_id,
            self.api_hash,
            # هم‌خوان با تلگرام دسکتاپ چون api_id پیش‌فرض مال همونه
            device_model="Desktop",
            system_version="Linux x86_64",
            app_version="5.6.3 x64",
            lang_code="fa",
            system_lang_code="fa-IR",
            flood_sleep_threshold=180,
            request_retries=8,
            connection_retries=10,
            auto_reconnect=True,
            receive_updates=False,  # آپدیت لازم نداریم؛ ترافیک کمتر
        )
        await self.client.connect()
        if not await self.client.is_user_authorized():
            await self.client.disconnect()
            raise NoSession("نشست تلگرام ورکر باطل شده؛ دوباره login.py")
        me = await self.client.get_me()
        self.me_id = me.id
        return self

    async def __aexit__(self, *a):
        try:
            # نشست ممکنه کلید سرور (salt/dc) تازه گرفته باشه؛ ذخیره‌ش لازم نیست چون کلید احراز همونه
            await self.client.disconnect()
        except Exception:
            pass
        self.client = None

    # ---------- پیام فایل ----------
    async def find_message(self, src: dict):
        """src: {token} (ربات فایل رو با کپشن #token توی چت خصوصی ورکر فرستاده) یا {peer, msgId}"""
        c = self.client
        if src.get("msgId"):
            peer = src.get("peer")
            try:
                peer = int(peer)
            except (TypeError, ValueError):
                pass
            try:
                ent = await c.get_input_entity(peer)
            except ValueError:
                await c.get_dialogs(limit=None)
                ent = await c.get_input_entity(peer)
            m = await c.get_messages(ent, ids=int(src["msgId"]))
            return m if m and m.media else None
        token = str(src.get("token") or "")
        if not token or not self.bot:
            return None
        async for m in c.iter_messages(self.bot, limit=300):
            if m.media and token in (m.message or ""):
                return m
        return None

    async def download(self, m, dest: str, size: int, on_bytes):
        """دانلود تکه‌تکه با ادامه از جای قبلی (اگه فایل نیمه‌کاره هست)"""
        have = os.path.getsize(dest) if os.path.exists(dest) else 0
        if have > size:
            have = 0
        have -= have % REQ
        with open(dest, "ab" if have else "wb") as f:
            f.truncate(have)
            f.seek(have)
            if have < size:
                async for chunk in self.client.iter_download(m.document or m.media, offset=have, request_size=REQ, file_size=size):
                    f.write(chunk)
                    have += len(chunk)
                    await on_bytes(have)
            f.flush()
        if os.path.getsize(dest) != size:
            raise RuntimeError(f"دانلود ناقص ({os.path.getsize(dest)} از {size})")

    # ---------- initData ----------
    async def init_data(self) -> str | None:
        """initData امضاشدهٔ تلگرام برای حساب ورکر"""
        if not self.bot or not self.app_url:
            return None
        bot = await self.client.get_input_entity(self.bot)
        url = None
        try:
            r = await self.client(functions.messages.RequestWebViewRequest(peer=bot, bot=bot, platform="android", url=self.app_url, from_bot_menu=False))
            url = r.url
        except Exception:
            try:
                r = await self.client(functions.messages.RequestSimpleWebViewRequest(bot=bot, platform="android", url=self.app_url))
                url = r.url
            except Exception:
                return None
        mask(url)
        data = parse_qs(urlparse(url).fragment).get("tgWebAppData", [None])[0]
        if data:
            mask(data, tma(data))
        return data or None


def tma(init: str) -> str:
    """سرآیند Authorization همونی که telegram-web-app.js می‌فرسته"""
    return "TMA " + base64.b64encode(init.encode()).decode()


def init_age(init: str | None) -> float:
    """چند ثانیه از auth_date این initData گذشته (نامعلوم = خیلی)"""
    try:
        return time.time() - int(parse_qs(init or "").get("auth_date", ["0"])[0])
    except ValueError:
        return 1e12
