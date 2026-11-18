"""
اجرای یه کار روی ران‌ر:
1) ingest: قفل تلگرام ← دانلود (تا ۲ گیگ) ← پاک کردن پیام کمکی ← آزاد کردن قفل ← HLS (اصل + 480p، AES-128)
   ← آپلود پوشهٔ videos/<uuid>/ روی مخزن dataset ← گزارش done با پلی‌لیست‌ها.
2) move: کپی videos/<uuid-قدیم>/ به مسیر/مخزن تازه و پاک کردن قبلی (کلید عوض نمی‌شه).

کلیدها و توکن‌ها فقط توی حافظه‌ان (و ::add-mask:: شدن)؛ لاگ فقط شمارهٔ کار و مرحله.
"""
import asyncio
import os
import shutil
import time

from huggingface_hub import HfApi

from .crypto import ctr_encryptor
from .hls import est_disk, transcode
from .readme import DATASET_README
from .safe import err_name, mask
from .tg import TG, init_age

MAX_SIZE = 2 * 1024**3 + 64 * 1024**2
INIT_REFRESH = 30 * 60  # initData کهنه‌تر از این رو موقع داشتن قفل تازه کن


class Fatal(Exception):
    """خطایی که تلاش دوباره درستش نمی‌کنه"""


class Canceled(Exception):
    """ادمین کار رو لغو کرد (یا کار به اجرای دیگه‌ای رسید)"""


def mask_job(j: dict):
    """هر چیز رازی که با کار رسیده"""
    acc = j.get("acc") or {}
    fr = j.get("from") or {}
    mask(j.get("key"), j.get("iv"), j.get("origKey"), acc.get("token"), fr.get("token"), (j.get("src") or {}).get("token"))
    mask((j.get("tg") or {}).get("apiHash"), (j.get("tg") or {}).get("session"))


class Job:
    def __init__(self, w, j: dict, root: str):
        self.w = w  # Worker (run.py)
        self.j = j
        self.id = str(j["id"])
        self.dir = os.path.join(root, f"job-{self.id}")
        self.stage = "find"
        self.pct = 0.0
        self.size = int(j.get("size") or 0)
        self._sent = 0.0
        self.canceled = False

    # ---------- گزارش ----------
    async def progress(self, stage: str | None = None, pct: float | None = None, force=False):
        if self.canceled:
            raise Canceled()
        if stage and stage != self.stage:
            self.stage, force = stage, True
        if pct is not None:
            self.pct = pct
        now = time.time()
        if force or now - self._sent > 15:
            self._sent = now
            try:
                r = await self.w.app.call("progress", job=self.id, stage=self.stage, pct=round(self.pct, 3), size=self.size, lease=self.w.holding)
            except Exception as e:  # گزارش پیشرفت مهم نیست
                self.w.log.info("progress job %s skipped: %s", self.id, err_name(e))
                return
            if r.get("cancel"):
                self.canceled = True
                raise Canceled()

    async def heartbeat(self):
        """هر ۴۵ ثانیه: زنده بودن کار (و تمدید قفل تلگرام) حتی وسط آپلود طولانی"""
        while True:
            await asyncio.sleep(45)
            try:
                await self.progress(force=True)
            except Canceled:
                return  # کار اصلی سر progress بعدی متوجه می‌شه

    # ---------- ingest ----------
    async def ingest(self) -> dict:
        j = self.j
        os.makedirs(self.dir, exist_ok=True)
        src = os.path.join(self.dir, "src")
        await self.progress("find", 0, True)
        await self.w.lease_on(self)
        try:
            async with TG(self.w.tgcfg(j)) as t:
                self.w.tg = t
                m = await t.find_message(j["src"])
                if not m or not m.file:
                    raise RuntimeError("پیام فایل توی تلگرام پیدا نشد")
                size = int(m.file.size or 0)
                if size <= 0 or size > MAX_SIZE:
                    raise Fatal("حجم فایل نامعتبره")
                self.size = size
                if shutil.disk_usage(os.path.dirname(self.dir)).free < est_disk(size):
                    raise RuntimeError("جای خالی دیسک ران‌ر کمه")
                await self.progress("download", 0, True)
                await t.download(m, src, size, lambda have: self.progress("download", have / size))
                # پیام کمکی دیگه لازم نیست (هر claim پیام تازه می‌گیره)
                try:
                    await m.delete()
                except Exception:
                    pass
                if init_age(self.w.app.init) > INIT_REFRESH:
                    new = await self.w.refresh_init()
                    if new:
                        self.w.app.set_init(new)
        finally:
            self.w.tg = None
            await self.w.lease_off()

        out = os.path.join(self.dir, "hls")
        shutil.rmtree(out, ignore_errors=True)
        await self.progress("transcode", 0, True)
        try:
            info = await transcode(src, out, bytes.fromhex(j["key"]), bytes.fromhex(j["iv"]), f"qkey://{j['keyId']}", lambda p: self.progress("transcode", p), threads=os.cpu_count() or 0)
        except ValueError as e:  # ffprobe: ویدیو نیست
            raise Fatal(str(e))

        if j.get("origKey"):
            await asyncio.to_thread(_encrypt_file, src, os.path.join(out, "orig.bin"), bytes.fromhex(j["origKey"]))
        try:
            os.remove(src)  # جای دیسک
        except FileNotFoundError:
            pass

        await self.progress("upload", 0, True)
        acc = j["acc"]
        await asyncio.to_thread(upload, acc, out, j["prefix"])
        return {
            "keyId": j["keyId"],
            "accId": acc["id"],
            "prefix": j["prefix"],
            "dur": info.get("dur"),
            "height": info.get("h"),
            "copy": info.get("copy"),
            "levels": info["sizes"],
            "orig": bool(j.get("origKey")),
            "size": self.size,
            "lists": read_lists(out),
        }

    # ---------- move ----------
    async def move(self) -> dict:
        j = self.j
        os.makedirs(self.dir, exist_ok=True)
        fr = j["from"]
        old_api = HfApi(token=fr["token"])
        pre = fr["prefix"].strip("/")
        await self.progress("download", 0, True)
        local = os.path.join(self.dir, "dl")
        await asyncio.to_thread(old_api.snapshot_download, fr["repo"], repo_type="dataset", allow_patterns=[pre + "/*", pre + "/**"], local_dir=local)
        tree = os.path.join(local, pre)
        if not os.path.isdir(tree):
            raise Fatal("مسیر قدیمی روی هاگینگ‌فیس پیدا نشد")
        shutil.rmtree(os.path.join(tree, ".cache"), ignore_errors=True)
        await self.progress("upload", 0, True)
        await asyncio.to_thread(upload, j["acc"], tree, j["prefix"])
        try:
            await asyncio.to_thread(old_api.delete_folder, pre, repo_id=fr["repo"], repo_type="dataset", commit_message="cleanup")
        except Exception as e:  # پاک نشد؛ مهم نیست (بدون کلید بی‌مصرفه)
            self.w.log.info("job %s: old folder not deleted (%s)", self.id, err_name(e))
        return {"keyId": j.get("keyId") or "", "accId": j["acc"]["id"], "prefix": j["prefix"], "moved": True}

    async def run(self) -> dict:
        hb = asyncio.create_task(self.heartbeat())
        try:
            return await (self.ingest() if self.j.get("kind", "ingest") == "ingest" else self.move())
        finally:
            hb.cancel()
            shutil.rmtree(self.dir, ignore_errors=True)


# ---------- هاگینگ‌فیس ----------
def ensure_repo(api: HfApi, acc: dict):
    """مخزن رو (اگه نبود) بساز و README خنثی بذار"""
    api.create_repo(acc["repo"], repo_type="dataset", private=not acc.get("public"), exist_ok=True)
    if not api.file_exists(acc["repo"], "README.md", repo_type="dataset"):
        api.upload_file(path_or_fileobj=DATASET_README.encode(), path_in_repo="README.md", repo_id=acc["repo"], repo_type="dataset", commit_message="init")


def upload(acc: dict, local: str, prefix: str, tries: int = 4):
    """کل پوشه با یه commit (upload_folder)؛ فایل‌های تکراری رو هاب خودش دوباره نمی‌فرسته"""
    api = HfApi(token=acc["token"])
    ensure_repo(api, acc)
    for i in range(tries):
        try:
            api.upload_folder(repo_id=acc["repo"], repo_type="dataset", folder_path=local, path_in_repo=prefix.strip("/"), commit_message="add blobs")
            return
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(30 * 2**i)


def read_lists(local: str) -> dict:
    """پلی‌لیست‌ها (متن کوچیک) تا سرور اپ خودش سرو کنه"""
    with open(os.path.join(local, "master.m3u8")) as f:
        lists = {"master": f.read()}
    for d in sorted(os.listdir(local)):
        p = os.path.join(local, d, "index.m3u8")
        if os.path.exists(p):
            with open(p) as f:
                lists[d] = f.read()
    return lists


def _encrypt_file(src: str, dst: str, key: bytes):
    """فایل اصلی (اختیاری): AES-256-CTR با IV صفر (کلید یک‌بارمصرف همین فایله)"""
    enc = ctr_encryptor(key, b"\0" * 16, 0)
    with open(src, "rb") as a, open(dst, "wb") as b:
        while True:
            buf = a.read(4 * 1024 * 1024)
            if not buf:
                break
            b.write(enc.update(buf))
        b.write(enc.finalize())

