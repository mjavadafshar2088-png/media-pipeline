"""
نقطهٔ شروع ران‌ر:  python -m pipeline.run

ورودی‌ها:
- سکرت‌های مخزن (env): WORKER_SECRET، APP_API
- ورودی workflow_dispatch (از فایل رویداد GITHUB_EVENT_PATH، نه env تا توی سرآیند لاگ نیاد):
  nonce (شناسهٔ این اجرا که سرور ساخته) و boot (جعبهٔ h1 رمزشده: apiId، apiHash، نشست v1، bot، appUrl، initData)

حلقه: hello ← claim ← اجرا ← done/error ← claim … تا وقتی کاری نمونه یا CLAIM_HOURS (پیش‌فرض ۵ ساعت) بگذره ← bye.
"""
import asyncio
import json
import os
import shutil
import sys
import time

from .app import App, AppError, Unauthorized
from .crypto import open_boot
from .jobs import Canceled, Fatal, Job, mask_job
from .safe import err_name, mask, setup_logging
from .tg import TG, NoSession, init_age

log = setup_logging()
STARTED = time.time()
CLAIM_SEC = float(os.environ.get("CLAIM_HOURS", "5")) * 3600
MAX_IDLE_WAIT = 600  # اگه کاری «به‌زودی» آماده می‌شه (تلاش دوباره) حداکثر این‌قدر صبر کن


def event_inputs() -> dict:
    p = os.environ.get("GITHUB_EVENT_PATH")
    if p and os.path.exists(p):
        try:
            with open(p) as f:
                return json.load(f).get("inputs") or {}
        except (OSError, ValueError):
            return {}
    # اجرای محلی
    return {"nonce": os.environ.get("RUN_NONCE", ""), "boot": os.environ.get("RUN_BOOT", "")}


def work_root() -> str:
    """پوشهٔ کار روی دیسکی که بیشترین جای خالی رو داره (/mnt روی ران‌رهای گیت‌هاب معمولاً بزرگ‌تره)"""
    cands = [x for x in os.environ.get("WORK_DIRS", "/mnt/work:" + os.environ.get("RUNNER_TEMP", "/tmp")).split(":") if x]
    best, free = None, -1
    for c in cands:
        try:
            os.makedirs(c, exist_ok=True)
            t = os.path.join(c, ".w")
            open(t, "w").close()
            os.remove(t)
            f = shutil.disk_usage(c).free
        except OSError:
            continue
        if f > free:
            best, free = c, f
    root = os.path.join(best or "/tmp", "mp")
    os.makedirs(root, exist_ok=True)
    return root


class Worker:
    def __init__(self, boot: dict, run_id: str):
        self.boot = boot
        self.run_id = run_id
        self.log = log
        self.app = App(init=boot.get("initData") or None, run=run_id)
        self.app.refresh = self.refresh_init
        self.tg: TG | None = None  # کلاینت وصل (فقط وقتی قفل دست ماست)
        self.holding = False
        self.new_init = False  # initData تازه گرفتیم و سرور هنوز نگرفته
        self.root = work_root()
        self.done = 0

    def tgcfg(self, j: dict | None = None) -> dict:
        """تنظیمات تلگرام: تازه‌ترین از claim، وگرنه از boot"""
        t = (j or {}).get("tg") or {}
        return {k: t.get(k) or self.boot.get(k) for k in ("apiId", "apiHash", "session", "bot", "appUrl")}

    async def refresh_init(self) -> str | None:
        """initData تازه. اگه همین الان وصلیم از همون کلاینت؛ وگرنه یه اتصال خیلی کوتاه"""
        if self.tg:
            new = await self.tg.init_data()
        else:
            async with TG(self.tgcfg()) as t:
                new = await t.init_data()
        self.new_init = self.new_init or bool(new)
        return new

    # ---------- قفل تلگرام (یه ران‌ر در لحظه وصل به حساب ورکر) ----------
    async def lease_on(self, job: Job):
        waited = 0
        while True:
            r = await self.app.call("lease", on=True, job=job.id)
            if r.get("ok"):
                self.holding = True
                return
            wait = max(10, min(60, int(r.get("wait") or 20)))
            await job.progress("wait", 0, force=waited == 0)
            await asyncio.sleep(wait)
            waited += wait
            if waited > 3 * 3600:
                raise RuntimeError("قفل تلگرام آزاد نشد")

    async def lease_off(self):
        if not self.holding:
            return
        self.holding = False
        try:
            await self.app.call("lease", on=False)
        except Exception as e:
            log.info("lease release failed: %s", err_name(e))

    # ---------- حلقه ----------
    async def one(self) -> bool:
        """یه کار؛ False یعنی دیگه کاری نیست"""
        if self.new_init:  # سرور برای اجراهای بعدی نگهش داره
            self.new_init = False
            await self.app.call("init", data=self.app.init)
        r = await self.app.call("claim")
        j = r.get("job")
        if not j:
            wait = int(r.get("wait") or 0)
            if 0 < wait <= MAX_IDLE_WAIT and time.time() - STARTED + wait < CLAIM_SEC:
                log.info("no job ready; waiting %ss", wait)
                await asyncio.sleep(wait)
                return True
            return False
        mask_job(j)
        job = Job(self, j, self.root)
        log.info("job %s (%s) started", job.id, j.get("kind", "ingest"))
        t0 = time.time()
        try:
            res = await job.run()
        except NoSession as e:
            await self.report_error(job, str(e), code="session")
            raise
        except Canceled:
            log.info("job %s canceled", job.id)
            return True
        except Fatal as e:
            log.warning("job %s failed (final): %s", job.id, err_name(e))
            await self.report_error(job, str(e), final=True)
            return True
        except Unauthorized:
            raise
        except Exception as e:
            log.warning("job %s failed: %s", job.id, err_name(e))
            await self.report_error(job, str(e) or err_name(e))
            return True
        await self.app.call("done", job=job.id, **res)
        self.done += 1
        log.info("job %s done in %ss", job.id, int(time.time() - t0))
        return True

    async def report_error(self, job: Job, msg: str, final=False, code: str | None = None):
        try:
            await self.app.call("error", job=job.id, err=msg[:400], final=final, stage=job.stage, **({"code": code} if code else {}))
        except Exception as e:
            log.info("error report failed: %s", err_name(e))

    async def main(self) -> int:
        if init_age(self.app.init) > 6 * 3600:  # initData نداریم یا خیلی کهنه‌ست
            new = await self.refresh_init()
            if not new:
                log.error("could not get initData")
                return 1
            self.app.set_init(new)
        try:
            await self.app.call("hello", gh=os.environ.get("GITHUB_RUN_ID", ""), disk=round(shutil.disk_usage(self.root).free / 1024**3, 1))
            while time.time() - STARTED < CLAIM_SEC:
                if not await self.one():
                    break
            return 0
        except NoSession:
            log.error("telegram session invalid; run login.py again")
            return 2
        except Unauthorized:
            log.error("app rejected initData")
            return 3
        except AppError as e:
            log.error("app error: %s", err_name(e))
            return 4
        finally:
            try:
                await self.app.call("bye", done=self.done)
            except Exception:
                pass
            await self.app.close()
            log.info("finished: %s job(s)", self.done)


def main() -> int:
    if not os.environ.get("WORKER_SECRET") or not os.environ.get("APP_API"):
        log.error("WORKER_SECRET / APP_API missing")
        return 1
    inp = event_inputs()
    nonce = str(inp.get("nonce") or "")
    blob = str(inp.get("boot") or "")
    mask(blob)
    raw = open_boot(blob)
    if not raw:
        log.error("boot input missing or not signed with this WORKER_SECRET")
        return 1
    boot = json.loads(raw)
    mask(boot.get("apiHash"), boot.get("session"), boot.get("initData"))
    if not nonce or nonce != str(boot.get("nonce") or ""):
        log.error("nonce mismatch")
        return 1
    if time.time() > float(boot.get("exp") or 0):
        log.error("boot expired")
        return 1
    return asyncio.run(Worker(boot, nonce).main())


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # بدون traceback: لاگ عمومیه
        log.error("crashed: %s", err_name(e))
        sys.exit(1)
