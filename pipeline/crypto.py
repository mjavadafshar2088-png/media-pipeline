"""
رمزنگاری فایل‌ها و نشست تلگرام.

فایل‌ها: AES-256-CTR. کلید ۳۲ بایتی و IV (شمارندهٔ اولیهٔ ۱۶ بایتی) برای هر فایل تصادفی‌اند
و سرور اپ می‌سازدشون (کلید هیچ‌وقت توی مخزن نمی‌ره). CTR یعنی مرورگر هر بازهٔ بایتی رو
مستقل رمزگشایی می‌کنه: شمارندهٔ بلوک n = IV + n (عدد ۱۲۸ بیتی big-endian، کل بلوک افزایش پیدا می‌کنه؛
همون رفتار Node و WebCrypto با length=128).

نشست تلگرام: AES-256-GCM با کلید مشتق‌شده از WORKER_SECRET؛ سرور فقط رشتهٔ رمزشده رو نگه می‌داره.
"""
import base64
import hashlib
import os

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

BLOCK = 16


def counter_at(iv: bytes, offset: int) -> bytes:
    """شمارندهٔ بلوکی که بایت offset توشه (offset باید مضرب ۱۶ باشه)"""
    if offset % BLOCK:
        raise ValueError("offset باید مضرب ۱۶ باشه")
    n = (int.from_bytes(iv, "big") + offset // BLOCK) % (1 << 128)
    return n.to_bytes(16, "big")


def ctr_encryptor(key: bytes, iv: bytes, offset: int = 0):
    """رمزکنندهٔ جریانی از بایت offset به بعد (برای ادامهٔ دانلود نیمه‌کاره)"""
    if len(key) != 32 or len(iv) != 16:
        raise ValueError("کلید ۳۲ و IV ۱۶ بایت")
    return Cipher(algorithms.AES(key), modes.CTR(counter_at(iv, offset))).encryptor()


def _session_key() -> bytes:
    secret = os.environ.get("WORKER_SECRET", "")
    return hashlib.sha256(("qafase-session|" + secret).encode()).digest()


def seal_session(s: str) -> str:
    """رشتهٔ نشست Telethon → متن رمزشده (base64)"""
    nonce = os.urandom(12)
    ct = AESGCM(_session_key()).encrypt(nonce, s.encode(), b"qafase-session")
    return "v1." + base64.urlsafe_b64encode(nonce + ct).decode()


def open_session(blob: str) -> str | None:
    """برعکس seal_session؛ اگه کلید عوض شده باشه یا خراب باشه None"""
    try:
        if not blob or not blob.startswith("v1."):
            return None
        raw = base64.urlsafe_b64decode(blob[3:].encode())
        return AESGCM(_session_key()).decrypt(raw[:12], raw[12:], b"qafase-session").decode()
    except Exception:
        return None


# ---------- جعبهٔ boot (ورودی workflow_dispatch) ----------
# سرور اپ crypto.subtle تضمینی نداره و فقط HMAC-SHA256 به JS خالص داره؛ پس رمز «HMAC در حالت شمارنده»:
#   کلید رمز = HMAC(secret, "qafase-boot|enc")   کلید امضا = HMAC(secret, "qafase-boot|mac")
#   بلوک i جریان = HMAC(کلید رمز, nonce || uint32be(i))   (۳۲ بایت)   متن رمز = متن XOR جریان
#   برچسب = HMAC(کلید امضا, "h1" || nonce || متن رمز)
#   خروجی = "h1." + base64url(nonce16 || متن رمز || برچسب32)
# همتای JS: tgcloud/lib/worker.js ← sealBoot
import hmac as _hmac  # noqa: E402


def _hm(key: bytes, msg: bytes) -> bytes:
    return _hmac.new(key, msg, hashlib.sha256).digest()


def _boot_keys(secret: str) -> tuple[bytes, bytes]:
    s = secret.encode()
    return _hm(s, b"qafase-boot|enc"), _hm(s, b"qafase-boot|mac")


def _stream(k: bytes, nonce: bytes, n: int) -> bytes:
    out = bytearray()
    i = 0
    while len(out) < n:
        out += _hm(k, nonce + i.to_bytes(4, "big"))
        i += 1
    return bytes(out[:n])


def seal_boot(text: str, secret: str | None = None) -> str:
    secret = os.environ.get("WORKER_SECRET", "") if secret is None else secret
    ek, mk = _boot_keys(secret)
    nonce = os.urandom(16)
    pt = text.encode()
    ct = bytes(a ^ b for a, b in zip(pt, _stream(ek, nonce, len(pt))))
    tag = _hm(mk, b"h1" + nonce + ct)
    return "h1." + base64.urlsafe_b64encode(nonce + ct + tag).decode().rstrip("=")


def open_boot(blob: str, secret: str | None = None) -> str | None:
    """برعکس seal_boot؛ امضای نادرست یا خراب = None"""
    secret = os.environ.get("WORKER_SECRET", "") if secret is None else secret
    try:
        if not secret or not blob or not blob.startswith("h1."):
            return None
        b = blob[3:]
        raw = base64.urlsafe_b64decode(b + "=" * (-len(b) % 4))
        if len(raw) < 48:
            return None
        nonce, ct, tag = raw[:16], raw[16:-32], raw[-32:]
        ek, mk = _boot_keys(secret)
        if not _hmac.compare_digest(tag, _hm(mk, b"h1" + nonce + ct)):
            return None
        return bytes(a ^ b for a, b in zip(ct, _stream(ek, nonce, len(ct)))).decode()
    except Exception:
        return None
