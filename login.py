#!/usr/bin/env python3
"""
ورود حساب تلگرام ورکر (یه بار، از سندباکس یا لپ‌تاپ؛ نه روی گیت‌هاب) و فرستادن نشستش به اپ.

هیچ‌وقت نشست یا کد رو چاپ نمی‌کنه. نشست خام فقط توی فایل با دسترسی 600 نوشته می‌شه.

مرحله‌ای (برای وقتی کد رو باید از کس دیگه‌ای پرسید):
  python3 login.py start --phone +98912...      # کد فرستاده می‌شه؛ وضعیت توی .tglogin.json (600)
  python3 login.py code --code 12345            # اگه رمز دومرحله‌ای داشت خروجی ۱۰
  TG_PASSWORD=... python3 login.py password     # (یا بدون env، با پرسش پنهان)
یک‌جا:
  python3 login.py interactive
فرستادن به اپ (نشست با کلید مشتق از WORKER_SECRET رمز می‌شه؛ AES-GCM):
  WORKER_SECRET=... APP_API=https://<app-host>/api BOT=<bot-username> APP_URL=https://<app-host>/ \
    python3 login.py push --session-file tg.session
  (بعدش فایل نشست رو پاک کن: shred -u tg.session)

api_id/api_hash: پیش‌فرض شناسهٔ عمومی تلگرام دسکتاپ (همون پیش‌فرض اپ)؛ با API_ID/API_HASH عوض می‌شه.
"""
import argparse
import asyncio
import getpass
import json
import os
import sys

from telethon import TelegramClient
from telethon.errors import PasswordHashInvalidError, PhoneCodeExpiredError, PhoneCodeInvalidError, SessionPasswordNeededError
from telethon.sessions import StringSession

TD_API_ID = 2040
TD_API_HASH = "b18441a1ff607e10a989891a5462e627"
STATE = ".tglogin.json"


def api_pair():
    return int(os.environ.get("API_ID") or TD_API_ID), os.environ.get("API_HASH") or TD_API_HASH


def write_private(path: str, text: str):
    """فایل با دسترسی 600 از همون اول (نه بعد از نوشتن)"""
    if os.path.exists(path):
        os.remove(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def client(sess: str = "") -> TelegramClient:
    api_id, api_hash = api_pair()
    return TelegramClient(StringSession(sess), api_id, api_hash, device_model="Desktop", system_version="Linux x86_64", app_version="5.6.3 x64", lang_code="fa", system_lang_code="fa-IR", receive_updates=False)


async def finish(c: TelegramClient, out: str, bot: str | None):
    me = await c.get_me()
    write_private(out, c.session.save())
    if bot:  # تا ربات بتونه فایل‌ها رو توی همین چت بفرسته
        try:
            await c.send_message(bot.lstrip("@"), "/start")
        except Exception:
            print("هشدار: /start به ربات فرستاده نشد؛ دستی بفرست", file=sys.stderr)
    if os.path.exists(STATE):
        os.remove(STATE)
    print(f"وارد شد: id={me.id}{' @' + me.username if me.username else ''} — نشست توی {out} (600)")


async def cmd_start(a):
    c = client()
    await c.connect()
    phone = "+" + "".join(ch for ch in a.phone if ch.isdigit())
    sent = await c.send_code_request(phone)
    write_private(STATE, json.dumps({"session": c.session.save(), "phone": phone, "hash": sent.phone_code_hash, "stage": "code"}))
    await c.disconnect()
    print("کد فرستاده شد؛ حالا: login.py code --code …")


def load_state():
    if not os.path.exists(STATE):
        sys.exit("اول login.py start")
    with open(STATE) as f:
        return json.load(f)


async def cmd_code(a):
    st = load_state()
    c = client(st["session"])
    await c.connect()
    try:
        await c.sign_in(st["phone"], "".join(ch for ch in a.code if ch.isdigit()), phone_code_hash=st["hash"])
    except SessionPasswordNeededError:
        st.update(stage="password", session=c.session.save())
        write_private(STATE, json.dumps(st))
        await c.disconnect()
        print("رمز دومرحله‌ای لازمه: login.py password")
        sys.exit(10)
    except PhoneCodeInvalidError:
        sys.exit("کد اشتباهه")
    except PhoneCodeExpiredError:
        os.remove(STATE)
        sys.exit("کد منقضی شده؛ دوباره start")
    await finish(c, a.out, a.bot)
    await c.disconnect()


async def cmd_password(a):
    st = load_state()
    pw = os.environ.get("TG_PASSWORD") or getpass.getpass("رمز دومرحله‌ای: ")
    c = client(st["session"])
    await c.connect()
    try:
        await c.sign_in(password=pw)
    except PasswordHashInvalidError:
        sys.exit("رمز اشتباهه")
    await finish(c, a.out, a.bot)
    await c.disconnect()


async def cmd_interactive(a):
    c = client()
    await c.start(phone=lambda: input("شماره (با کد کشور): "), code_callback=lambda: input("کد: "), password=lambda: getpass.getpass("رمز دومرحله‌ای: "))
    await finish(c, a.out, a.bot)
    await c.disconnect()


async def cmd_push(a):
    """نشست رمزشده + initData تازه → POST {APP_API}/worker op=session (امضای HMAC)"""
    for k in ("WORKER_SECRET", "APP_API", "BOT", "APP_URL"):
        if not os.environ.get(k):
            sys.exit(f"{k} لازمه")
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from pipeline.app import App
    from pipeline.crypto import open_session, seal_session
    from pipeline.tg import TG

    with open(a.session_file) as f:
        sess = f.read().strip()
    sealed = seal_session(sess)
    assert open_session(sealed) == sess
    api_id, api_hash = api_pair()
    cfg = {"apiId": api_id, "apiHash": api_hash, "session": sess, "bot": os.environ["BOT"], "appUrl": os.environ["APP_URL"]}
    async with TG(cfg) as t:
        try:
            await t.client.send_message(t.bot, "/start")
        except Exception:
            pass
        me = await t.client.get_me()
        init = await t.init_data()
    if not init:
        sys.exit("initData گرفته نشد (BOT و APP_URL درستن؟)")
    app = App(init=init, run="cli")
    try:
        await app.call("session", session=sealed, init=init, me={"id": me.id, "name": " ".join(x for x in [me.first_name, me.last_name] if x), "username": me.username})
    finally:
        await app.close()
    print(f"نشست حساب id={me.id} توی اپ ذخیره شد (رمزشده). حالا فایل {a.session_file} رو پاک کن.")


def main():
    p = argparse.ArgumentParser(description="ورود حساب تلگرام ورکر")
    sp = p.add_subparsers(dest="cmd", required=True)
    for name in ("start", "code", "password", "interactive"):
        s = sp.add_parser(name)
        s.add_argument("--out", default="tg.session")
        s.add_argument("--bot", default=os.environ.get("BOT"))
        if name == "start":
            s.add_argument("--phone", required=True)
        if name == "code":
            s.add_argument("--code", required=True)
    s = sp.add_parser("push")
    s.add_argument("--session-file", default="tg.session")
    a = p.parse_args()
    os.umask(0o077)
    asyncio.run({"start": cmd_start, "code": cmd_code, "password": cmd_password, "interactive": cmd_interactive, "push": cmd_push}[a.cmd](a))


if __name__ == "__main__":
    main()
