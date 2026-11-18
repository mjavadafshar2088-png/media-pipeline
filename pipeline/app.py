"""
گفت‌وگو با سرور اپ: POST {APP_API}/worker

پلتفرم tgcloud فقط درخواستِ دارای initData معتبر رو به endpoint می‌رسونه (سرآیند Authorization: TMA …).
روی اون، بدنه با HMAC امضا می‌شه:  { payload: "<json>", ts, nonce, sig }
متن امضا: ts\nnonce\nPOST\n/api/worker\npayload   (همون tgcloud/lib/hmac.js و signMsg سرور)
جواب پلتفرم: { ok: true, result } یا { ok: false, description }.
"""
import asyncio
import json
import os

import httpx

from .safe import mask
from .sign import sign
from .tg import tma

CB_PATH = "/api/worker"


class AppError(Exception):
    """سرور اپ درخواست رو رد کرد (EndpointError)"""


class Unauthorized(Exception):
    """initData کهنه یا نامعتبر (۴۰۱ پلتفرم)"""


class App:
    def __init__(self, api: str | None = None, init: str | None = None, run: str = ""):
        self.api = (api or os.environ.get("APP_API", "")).rstrip("/")
        self.init = init
        self.run = run
        self.refresh = None  # async () -> str | None  (initData تازه)
        self._h = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=20))
        if self.init:
            mask(self.init, tma(self.init))

    async def close(self):
        await self._h.aclose()

    async def _post(self, payload: str):
        s = sign("POST", CB_PATH, payload)
        return await self._h.post(
            self.api + "/worker",
            content=json.dumps({"payload": payload, **s}),
            headers={"Authorization": tma(self.init or ""), "Content-Type": "application/json"},
        )

    async def call(self, op: str, **data) -> dict:
        """یه op امضاشده؛ خطای شبکه چند بار دوباره، ۴۰۱ یه بار با initData تازه"""
        if not self.api:
            raise AppError("APP_API تنظیم نشده")
        payload = json.dumps({"op": op, "run": self.run, **data}, ensure_ascii=False, separators=(",", ":"))
        refreshed = False
        for attempt in range(6):
            try:
                r = await self._post(payload) if self.init else None
            except httpx.HTTPError:
                await asyncio.sleep(min(60, 5 * 2**attempt))
                continue
            if r is None or r.status_code == 401:
                if refreshed or not self.refresh:
                    raise Unauthorized("initData پذیرفته نشد")
                refreshed = True
                new = await self.refresh()
                if not new:
                    raise Unauthorized("initData تازه گرفته نشد")
                self.set_init(new)
                payload = json.dumps({"op": op, "run": self.run, **data}, ensure_ascii=False, separators=(",", ":"))
                continue
            if r.status_code >= 500 or r.status_code == 429:
                await asyncio.sleep(min(60, 5 * 2**attempt))
                continue
            try:
                j = r.json()
            except ValueError:
                raise AppError(f"جواب نامعتبر ({r.status_code})")
            if not j.get("ok", r.status_code == 200):
                raise AppError(str(j.get("description") or f"HTTP {r.status_code}"))
            res = j.get("result", j)
            return res if isinstance(res, dict) else {"value": res}
        raise AppError("سرور اپ جواب نداد")

    def set_init(self, init: str):
        mask(init, tma(init))
        self.init = init
