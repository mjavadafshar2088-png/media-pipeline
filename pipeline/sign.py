"""
امضای HMAC بین ورکر و سرور اپ.

متن امضا (هر دو طرف یکسان):  ts \n nonce \n METHOD \n path \n body
- ts: ثانیهٔ یونیکس؛ بیشتر از ۵ دقیقه اختلاف = رد
- nonce: رشتهٔ تصادفی؛ تکراری توی ۱۰ دقیقهٔ اخیر = رد (ضد تکرار درخواست)
- امضا: hex(HMAC-SHA256(WORKER_SECRET, متن))
سرآیندها: X-Q-Ts، X-Q-Nonce، X-Q-Sig
"""
import hashlib
import hmac
import os
import secrets
import time

SECRET = os.environ.get("WORKER_SECRET", "").encode()  # راز مشترک با سرور اپ (سکرت مخزن)
WINDOW = 300
_seen: dict[str, float] = {}


def _msg(ts: str, nonce: str, method: str, path: str, body: str) -> bytes:
    return f"{ts}\n{nonce}\n{method.upper()}\n{path}\n{body}".encode()


def sign(method: str, path: str, body: str) -> dict:
    """سرآیندهای امضا برای درخواستی که ورکر به سرور می‌فرسته"""
    ts = str(int(time.time()))
    nonce = secrets.token_hex(12)
    sig = hmac.new(SECRET, _msg(ts, nonce, method, path, body), hashlib.sha256).hexdigest()
    return {"ts": ts, "nonce": nonce, "sig": sig}


def verify(ts: str | None, nonce: str | None, sig: str | None, method: str, path: str, body: str) -> bool:
    """درخواست رسیده از سرور اپ درسته؟"""
    if not SECRET or not ts or not nonce or not sig:
        return False
    try:
        t = int(ts)
    except ValueError:
        return False
    now = time.time()
    if abs(now - t) > WINDOW:
        return False
    # پاک‌سازی nonce های کهنه
    for k in [k for k, v in _seen.items() if now - v > WINDOW * 2]:
        _seen.pop(k, None)
    if nonce in _seen:
        return False
    good = hmac.new(SECRET, _msg(ts, nonce, method, path, body), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(good, sig.lower()):
        return False
    _seen[nonce] = now
    return True
