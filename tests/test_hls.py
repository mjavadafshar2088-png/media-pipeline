"""
آزمون محلی بدون شبکه:  python3 -m tests.test_hls   (از پوشهٔ gh-worker؛ ffmpeg لازمه)

۱) HLS: منبع H.264+AAC → v0 بدون تبدیل (copy) + v1 480p؛ منبع MPEG-4 → v0 720p + v1 480p؛
   H.264 + MP3 → ویدیو copy و فقط صدا AAC. هر تکه با کلید/IV رمزگشایی و امضای MPEG-TS چک می‌شه.
۲) جعبهٔ boot (h1) رفت و برگشت + رد امضای دست‌خورده.
۳) کل خط لوله (Worker.one) با تلگرام/سرور/هاگینگ‌فیس قلابی: claim ← قفل ← «دانلود» ← HLS ← «آپلود» ← done.
"""
import asyncio
import os
import re
import shutil
import subprocess
import tempfile

os.environ.setdefault("WORKER_SECRET", "t" * 48)
os.environ.setdefault("APP_API", "https://example.invalid/api")

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes  # noqa: E402

from pipeline import crypto, hls, jobs, run  # noqa: E402


def make(path, vcodec="libx264", acodec="aac", size="1280x720", t=13):
    extra = ["-preset", "ultrafast", "-g", "50"] if vcodec == "libx264" else []
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=25", "-f", "lavfi", "-i", "sine=frequency=440", "-t", str(t), "-pix_fmt", "yuv420p", "-c:v", vcodec, *extra, "-c:a", acodec, path], check=True)


def check_out(out, key, iv, n, uri="qkey://test"):
    master = open(os.path.join(out, "master.m3u8")).read()
    assert master.count("#EXT-X-STREAM-INF") == n, master
    assert re.findall(r"^v\d/index\.m3u8$", master, re.M) == [f"v{i}/index.m3u8" for i in range(n)], master
    for v in range(n):
        pl = open(os.path.join(out, f"v{v}", "index.m3u8")).read()
        m = re.search(r'#EXT-X-KEY:METHOD=AES-128,URI="' + re.escape(uri) + r'",IV=0x([0-9a-fA-F]{32})', pl)
        assert m and bytes.fromhex(m.group(1)) == iv, pl
        segs = [ln for ln in pl.splitlines() if ln and not ln.startswith("#")]
        assert segs and all(re.fullmatch(r"s\d{5}\.ts", s) for s in segs), segs
        ct = open(os.path.join(out, f"v{v}", segs[0]), "rb").read()
        assert ct[0] != 0x47 or ct[188] != 0x47, "تکه رمز نشده!"
        dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        pt = dec.update(ct) + dec.finalize()
        pt = pt[: -pt[-1]]
        assert all(pt[i] == 0x47 for i in range(0, len(pt), 188)), "رمزگشایی درست نیست"
        if v == 0:
            with open(os.path.join(out, "..", os.path.basename(out) + ".dec0.ts"), "wb") as f:
                f.write(pt)
    return master


def codecs(ts):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name", "-of", "csv=p=0", ts], capture_output=True, text=True, check=True)
    return sorted({x.strip().strip(",") for x in r.stdout.split()})


def test_hls():
    d = tempfile.mkdtemp()
    key, iv = os.urandom(16), os.urandom(16)

    async def prog(_p):
        pass

    try:
        # H.264 + AAC → copy
        src = os.path.join(d, "a.mp4")
        make(src)
        info = asyncio.run(hls.transcode(src, os.path.join(d, "a"), key, iv, "qkey://test", prog))
        assert info["copy"] and info["levels"] == [720, 480], info
        m = check_out(os.path.join(d, "a"), key, iv, 2)
        assert 'CODECS="avc1.' in m and "RESOLUTION=1280x720" in m, m
        assert not [f for f in os.listdir(d) if f.endswith((".key", ".keyinfo"))], "فایل کلید پاک نشده"
        # MPEG-4 Part 2 → تبدیل 720p
        src = os.path.join(d, "b.mp4")
        make(src, vcodec="mpeg4")
        info = asyncio.run(hls.transcode(src, os.path.join(d, "b"), key, iv, "qkey://test", prog))
        assert not info["copy"] and info["levels"] == [720, 480], info
        check_out(os.path.join(d, "b"), key, iv, 2)
        # H.264 + MP3، کوچیک (360p) → فقط یه کیفیت، copy ویدیو
        src = os.path.join(d, "c.mkv")
        make(src, acodec="libmp3lame", size="640x360")
        info = asyncio.run(hls.transcode(src, os.path.join(d, "c"), key, iv, "qkey://test", prog))
        assert info["copy"] and info["levels"] == [360], info
        check_out(os.path.join(d, "c"), key, iv, 1)
        assert codecs(os.path.join(d, "c.dec0.ts")) == ["aac", "h264"], codecs(os.path.join(d, "c.dec0.ts"))
        print("  HLS: copy / encode / audio-only-encode OK")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_boot():
    b = crypto.seal_boot('{"x":"سلام"}', "s" * 40)
    assert crypto.open_boot(b, "s" * 40) == '{"x":"سلام"}'
    assert crypto.open_boot(b, "z" * 40) is None
    bad = b[:-2] + ("A" if b[-2] != "A" else "B") + b[-1]
    assert crypto.open_boot(bad, "s" * 40) is None
    print("  boot box OK")


# ---------- خط لولهٔ کامل با قلابی‌ها ----------
class FakeFile:
    def __init__(self, size):
        self.size = size


class FakeMsg:
    def __init__(self, path):
        self.path = path
        self.file = FakeFile(os.path.getsize(path))
        self.document = object()
        self.deleted = False

    async def delete(self):
        self.deleted = True


class FakeTG:
    msg: FakeMsg = None

    def __init__(self, cfg):
        assert cfg["session"] == "SESSION" and cfg["apiHash"] == "h" * 32, cfg

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def find_message(self, src):
        return FakeTG.msg if src.get("token") == "qj1xabc" else None

    async def download(self, m, dest, size, on_bytes):
        shutil.copy(m.path, dest)
        await on_bytes(size)

    async def init_data(self):
        return "auth_date=9999999999&hash=x"


class FakeApp:
    def __init__(self, job):
        self.calls = []
        self.job = job
        self.init = "auth_date=9999999999&hash=x"

    async def call(self, op, **d):
        self.calls.append((op, d))
        if op == "claim":
            j, self.job = self.job, None
            return {"job": j}
        if op == "lease":
            return {"ok": True}
        return {"ok": True}

    def set_init(self, i):
        self.init = i


def test_pipeline():
    d = tempfile.mkdtemp()
    try:
        src = os.path.join(d, "v.mp4")
        make(src, t=8)
        FakeTG.msg = FakeMsg(src)
        up = {}

        def fake_upload(acc, local, prefix, tries=4):
            dst = os.path.join(d, "hub", acc["repo"], prefix)
            shutil.copytree(local, dst)
            up["dst"] = dst

        jobs.TG = FakeTG
        jobs.upload = fake_upload
        key, iv = os.urandom(16).hex(), os.urandom(16).hex()
        job = {"id": 1, "kind": "ingest", "src": {"token": "qj1xabc"}, "acc": {"id": 3, "repo": "o/media-1", "token": "hf_x", "public": True}, "keyId": "kid", "key": key, "iv": iv, "prefix": "videos/kid", "tg": {"apiId": 2040, "apiHash": "h" * 32, "session": "SESSION", "bot": "b", "appUrl": "https://a/"}}
        os.environ["WORK_DIRS"] = os.path.join(d, "work")
        w = run.Worker({"apiId": 2040, "apiHash": "h" * 32, "session": "SESSION", "bot": "b", "appUrl": "https://a/"}, "nonce1")
        fa = FakeApp(job)
        w.app = fa
        assert asyncio.run(w.one()) is True
        assert asyncio.run(w.one()) is False
        ops = [c[0] for c in fa.calls]
        assert ops[:2] == ["claim", "progress"] and "lease" in ops and ops.count("done") == 1, ops
        lease = [c[1] for c in fa.calls if c[0] == "lease"]
        assert lease[0]["on"] is True and lease[-1]["on"] is False, lease
        done = next(c[1] for c in fa.calls if c[0] == "done")
        assert done["prefix"] == "videos/kid" and done["accId"] == 3 and done["levels"][0]["h"] == 720 and "v1" in done["lists"], {k: v for k, v in done.items() if k != "lists"}
        assert FakeTG.msg.deleted, "پیام کمکی پاک نشد"
        assert os.path.exists(os.path.join(up["dst"], "master.m3u8")) and os.path.exists(os.path.join(up["dst"], "v1", "s00000.ts"))
        assert not os.path.exists(os.path.join(d, "work", "mp", "job-1")), "پوشهٔ کار پاک نشد"
        check_out(up["dst"], bytes.fromhex(key), bytes.fromhex(iv), 2, "qkey://kid")
        print("  pipeline OK:", [o for o in ops], {k: done[k] for k in ("dur", "height", "copy", "size")})
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_cancel():
    """لغو وسط تبدیل: ffmpeg کشته و Canceled بالا میاد، چیزی گزارش نمی‌شه"""
    d = tempfile.mkdtemp()
    try:
        src = os.path.join(d, "v.mp4")
        make(src, vcodec="mpeg4", t=20)
        FakeTG.msg = FakeMsg(src)
        jobs.TG = FakeTG
        jobs.upload = lambda *a, **k: (_ for _ in ()).throw(AssertionError("نباید آپلود بشه"))
        job = {"id": 2, "kind": "ingest", "src": {"token": "qj1xabc"}, "acc": {"id": 3, "repo": "o/m", "token": "hf_x"}, "keyId": "k", "key": "00" * 16, "iv": "11" * 16, "prefix": "videos/k", "tg": {"apiId": 2040, "apiHash": "h" * 32, "session": "SESSION", "bot": "b", "appUrl": "https://a/"}}
        os.environ["WORK_DIRS"] = os.path.join(d, "work")
        w = run.Worker({"apiId": 2040, "apiHash": "h" * 32, "session": "SESSION", "bot": "b", "appUrl": "https://a/"}, "n2")

        class CancelApp(FakeApp):
            async def call(self, op, **dd):
                r = await super().call(op, **dd)
                if op == "progress" and dd.get("stage") == "transcode" and dd.get("pct", 0) > 0:
                    return {"ok": True, "cancel": True}
                return r

        fa = CancelApp(job)
        w.app = fa
        import pipeline.jobs as J
        J_sent = J.Job.progress

        async def fast(self, stage=None, pct=None, force=False):  # بدون فاصلهٔ ۱۵ ثانیه
            return await J_sent(self, stage, pct, True)

        J.Job.progress = fast
        try:
            assert asyncio.run(w.one()) is True
        finally:
            J.Job.progress = J_sent
        ops = [c[0] for c in fa.calls]
        assert "done" not in ops and "error" not in ops, ops
        out = subprocess.run(["pgrep", "-af", "^ffmpeg .*job-2"], capture_output=True, text=True).stdout.strip()
        assert not out, "ffmpeg هنوز زنده‌ست: " + out
        print("  cancel OK")
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    test_boot()
    test_hls()
    test_pipeline()
    test_cancel()
    print("OK")
