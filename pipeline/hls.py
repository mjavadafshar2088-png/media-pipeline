"""
تبدیل به HLS با رمز AES-128 خود HLS — نسخهٔ سبک برای ران‌رهای گیت‌هاب (۴ هسته).

دو کیفیت:
- v0 «اصل»: اگه ویدیو H.264 سازگار با مرورگر باشه (8 بیت، yuv420p، بدون چرخش، حداکثر 1080p) همون جریان
  بدون تبدیل (-c copy) تکه‌تکه می‌شه؛ صدای غیر AAC فقط صداش به AAC تبدیل می‌شه.
  وگرنه 720p با libx264 (یا اندازهٔ خود منبع اگه کوچیک‌تره).
- v1: 480p با libx264 preset veryfast (فقط اگه منبع بلندتر از 480 باشه).
تکه‌ها ~۶ ثانیه (MPEG-TS)؛ کیفیت copy فقط سر کلیدفریم‌های خود منبع می‌بُره.
کلید ۱۶ بایتی و IV رو سرور اپ می‌ده؛ توی پلی‌لیست فقط URI نمادین qkey://<شناسه> نوشته می‌شه.
خروجی: <out>/master.m3u8 و <out>/v<i>/index.m3u8 و <out>/v<i>/s00000.ts ...
(اسم‌ها با endpoints/hls.js سرور اپ هم‌خوانه: خط‌های vN/index.m3u8 توی master)
"""
import asyncio
import json
import os
import re

SEG = 6
GOP_SEC = 2
COPY_MAX_H = 1080
ENC0 = (720, 2500, 128)  # (ارتفاع، کیلوبیت ویدیو، کیلوبیت صدا) وقتی copy نمی‌شه
ENC1 = (480, 1200, 96)
H264_OK_PROFILES = {"Constrained Baseline", "Baseline", "Main", "High"}
PROFILE_IDC = {"Constrained Baseline": (66, 0xE0), "Baseline": (66, 0x00), "Main": (77, 0x40), "High": (100, 0x00)}


async def _run(cmd: list, on_time=None, dur: float = 0.0) -> tuple[int, str]:
    """اجرای ffmpeg/ffprobe؛ on_time(ثانیهٔ پردازش‌شده) از -progress"""
    p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.DEVNULL)
    tail: list[str] = []

    async def read_err():
        async for line in p.stderr:
            tail.append(line.decode(errors="ignore"))
            del tail[:-30]

    errt = asyncio.create_task(read_err())
    try:
        async for line in p.stdout:
            s = line.decode(errors="ignore").strip()
            if on_time and (s.startswith("out_time_us=") or s.startswith("out_time_ms=")):
                try:
                    t = int(s.split("=", 1)[1]) / 1e6
                except ValueError:
                    continue
                await on_time(t)
    except BaseException:  # لغو کار: ffmpeg رو نگه ندار
        if p.returncode is None:
            p.kill()
        await p.wait()
        errt.cancel()
        raise
    rc = await p.wait()
    await errt
    return rc, "".join(tail)[-600:]


async def probe(src: str) -> dict:
    p = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", src,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await p.communicate()
    if p.returncode != 0:
        raise ValueError("ffprobe: " + err.decode(errors="ignore")[-300:])
    j = json.loads(out or b"{}")
    v = next((s for s in j.get("streams", []) if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")), None)
    a = next((s for s in j.get("streams", []) if s.get("codec_type") == "audio"), None)
    if not v:
        raise ValueError("فایل ویدیو نیست")
    w, h = int(v.get("width") or 0), int(v.get("height") or 0)
    rot = abs(int((v.get("tags") or {}).get("rotate", 0) or 0))
    for sd in v.get("side_data_list") or []:
        if "rotation" in sd:
            rot = abs(int(sd["rotation"]))
    rot %= 360
    if rot in (90, 270):
        w, h = h, w
    dur = float(j.get("format", {}).get("duration") or v.get("duration") or 0)
    return {
        "w": w,
        "h": h,
        "dur": dur,
        "rot": rot,
        "vcodec": v.get("codec_name"),
        "profile": v.get("profile"),
        "level": int(v.get("level") or 0),
        "pix": v.get("pix_fmt"),
        "audio": a is not None,
        "acodec": a.get("codec_name") if a else None,
        "aprofile": a.get("profile") if a else None,
        "fps": v.get("avg_frame_rate") or v.get("r_frame_rate") or "",
    }


def can_copy(info: dict) -> bool:
    """جریان ویدیوی منبع رو مرورگرها (MSE) مستقیم پخش می‌کنن؟"""
    return (
        info["vcodec"] == "h264"
        and info.get("profile") in H264_OK_PROFILES
        and info.get("pix") in ("yuv420p", "yuvj420p")
        and info.get("rot", 0) == 0  # چرخش توی MPEG-TS گم می‌شه
        and 0 < info["h"] <= COPY_MAX_H
    )


def avc_codec(info: dict) -> str | None:
    p = PROFILE_IDC.get(info.get("profile") or "")
    if not p or not info.get("level"):
        return None
    return f"avc1.{p[0]:02x}{p[1]:02x}{info['level']:02x}"


def plan(info: dict) -> list[dict]:
    """کیفیت‌هایی که ساخته می‌شن"""
    h = info["h"]
    out = []
    if can_copy(info):
        out.append({"mode": "copy", "h": h, "w": info["w"], "acopy": info["acodec"] == "aac", "ab": 128})
    else:
        th = min(ENC0[0], h) if h else ENC0[0]
        th -= th % 2
        out.append({"mode": "enc", "h": th, "vb": ENC0[1] if th >= 600 else ENC1[1], "ab": ENC0[2]})
    if h > ENC1[0] + 40:  # منبع واقعاً بلندتر از 480
        out.append({"mode": "enc", "h": ENC1[0], "vb": ENC1[1], "ab": ENC1[2]})
    return out


def _key_args(key_info: str, out_dir: str) -> list:
    return [
        "-f", "hls", "-hls_time", str(SEG), "-hls_playlist_type", "vod", "-hls_list_size", "0",
        "-hls_segment_type", "mpegts", "-hls_key_info_file", key_info,
        "-hls_segment_filename", os.path.join(out_dir, "s%05d.ts"),
        "-progress", "pipe:1", "-stats_period", "2",
        os.path.join(out_dir, "index.m3u8"),
    ]


def build_cmd(src: str, out_dir: str, info: dict, r: dict, key_info: str, threads: int = 0) -> list:
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", src, "-map", "0:v:0"]
    if info["audio"]:
        cmd += ["-map", "0:a:0"]
    cmd += ["-sn", "-dn", "-map_metadata", "-1", "-map_chapters", "-1"]
    if r["mode"] == "copy":
        cmd += ["-c:v", "copy"]
    else:
        vb = r["vb"]
        cmd += [
            "-vf", f"scale=-2:{r['h']}:flags=bicubic,format=yuv420p",
            "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high",
            "-b:v", f"{vb}k", "-maxrate", f"{int(vb * 1.07)}k", "-bufsize", f"{int(vb * 1.5)}k",
            "-force_key_frames", f"expr:gte(t,n_forced*{GOP_SEC})", "-sc_threshold", "0",
        ]
        if threads:
            cmd += ["-threads", str(threads)]
    if info["audio"]:
        if r["mode"] == "copy" and r.get("acopy"):
            cmd += ["-c:a", "copy"]
        else:
            cmd += ["-c:a", "aac", "-b:a", f"{r['ab']}k", "-ac", "2"]
    return cmd + _key_args(key_info, out_dir)


def _seg_stats(out_dir: str) -> dict:
    """حجم، تعداد و بیشترین نرخ بیت تکه‌ها (برای BANDWIDTH و شمارندهٔ سرور)"""
    pl = open(os.path.join(out_dir, "index.m3u8")).read().splitlines()
    total = n = 0
    peak = 0.0
    durs = 0.0
    last = None
    for ln in pl:
        if ln.startswith("#EXTINF:"):
            try:
                last = float(ln[8:].split(",")[0])
            except ValueError:
                last = None
        elif ln and not ln.startswith("#"):
            size = os.path.getsize(os.path.join(out_dir, ln))
            total += size
            n += 1
            if last:
                durs += last
                peak = max(peak, size * 8 / max(last, 0.5))
    avg = total * 8 / durs if durs else 0
    return {"bytes": total, "segs": n, "peak": int(peak), "avg": int(avg)}


def master_text(info: dict, rungs: list) -> str:
    lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
    for i, r in enumerate(rungs):
        st = r["stats"]
        if r["mode"] == "copy":
            vc = avc_codec(info)
            w, h = r["w"], r["h"]
        else:
            vc = "avc1.640028" if r["h"] > 576 else "avc1.64001f"
            h = r["h"]
            w = int(round(info["w"] * h / info["h"] / 2) * 2) if info["h"] else 0
        codecs = ",".join(x for x in [vc, "mp4a.40.2" if info["audio"] else None] if x) if vc else None
        attrs = [f"BANDWIDTH={max(st['peak'], st['avg'], 1)}", f"AVERAGE-BANDWIDTH={max(st['avg'], 1)}"]
        if w and h:
            attrs.append(f"RESOLUTION={w}x{h}")
        if codecs:
            attrs.append(f'CODECS="{codecs}"')
        lines += ["#EXT-X-STREAM-INF:" + ",".join(attrs), f"v{i}/index.m3u8"]
    return "\n".join(lines) + "\n"


def _clean_playlist(path: str):
    """فقط اسم تکه؛ هیچ مسیر محلی‌ای توی پلی‌لیست نمونه"""
    txt = open(path).read()
    txt = re.sub(r"^.*/(s\d{5}\.ts)$", r"\1", txt, flags=re.M)
    open(path, "w").write(txt)


async def transcode(src: str, out: str, key: bytes, iv: bytes, key_uri: str, on_progress, threads: int = 0) -> dict:
    """تبدیل کامل؛ on_progress(کسر ۰..۱). خروجی: اطلاعات منبع + کیفیت‌ها"""
    info = await probe(src)
    rungs = plan(info)
    os.makedirs(out, exist_ok=True)
    kf = out.rstrip("/") + ".key"
    ki = out.rstrip("/") + ".keyinfo"
    with open(kf, "wb") as f:
        f.write(key)
    os.chmod(kf, 0o600)
    with open(ki, "w") as f:
        f.write(f"{key_uri}\n{kf}\n{iv.hex()}\n")
    # وزن هر کیفیت برای درصد کلی: copy خیلی سریع‌تره
    weights = [0.1 if r["mode"] == "copy" else 1.0 for r in rungs]
    tot = sum(weights)
    try:
        done = 0.0
        for i, r in enumerate(rungs):
            d = os.path.join(out, f"v{i}")
            os.makedirs(d, exist_ok=True)

            async def tick(t, _done=done, _w=weights[i]):
                if info["dur"]:
                    await on_progress(min(0.999, (_done + _w * min(1.0, t / info["dur"])) / tot))

            rc, tail = await _run(build_cmd(src, d, info, r, ki, threads), tick, info["dur"])
            if rc != 0:
                raise RuntimeError(f"ffmpeg v{i}: " + tail[-400:])
            _clean_playlist(os.path.join(d, "index.m3u8"))
            r["stats"] = _seg_stats(d)
            if not r["stats"]["segs"]:
                raise RuntimeError(f"ffmpeg v{i}: تکه‌ای ساخته نشد")
            done += weights[i]
    finally:
        for f in (kf, ki):  # کلید روی دیسک ران‌ر نمونه
            try:
                os.remove(f)
            except FileNotFoundError:
                pass
    with open(os.path.join(out, "master.m3u8"), "w") as f:
        f.write(master_text(info, rungs))
    await on_progress(1.0)
    return {
        "w": info["w"],
        "h": info["h"],
        "dur": info["dur"],
        "copy": rungs[0]["mode"] == "copy",
        "levels": [r["h"] for r in rungs],
        "sizes": [{"v": i, "h": r["h"], "bytes": r["stats"]["bytes"], "segs": r["stats"]["segs"]} for i, r in enumerate(rungs)],
    }


def est_disk(size: int) -> int:
    """جای لازم تقریبی: منبع + copy (~۱٫۰۵×) + 480p + حاشیه"""
    return int(size * 2.4) + 512 * 1024**2

