"""出站媒体预处理：GIF/动图 -> 静态 PNG 首帧；音频时长；长语音切段。"""

from __future__ import annotations

import asyncio
import io
import mimetypes
import os
import re
import tempfile


def prepare_image_bytes(data: bytes, name: str = "image") -> tuple[bytes, str, str]:
    """返回 (bytes, filename, content_type)。

    微信守护进程不支持发送表情包(sticker)，所以 GIF / 动态 WebP / APNG 一律转成
    静态 PNG（取第一帧）；普通 PNG/JPEG 原样发送。
    """
    from PIL import Image, ImageSequence

    stem = os.path.splitext(os.path.basename(name))[0] or "image"
    try:
        im = Image.open(io.BytesIO(data))
        fmt = (im.format or "").upper()
        animated = bool(getattr(im, "is_animated", False)) or fmt == "GIF"
        if fmt in ("PNG", "JPEG") and not animated:
            ext = "png" if fmt == "PNG" else "jpg"
            return data, f"{stem}.{ext}", f"image/{'png' if fmt == 'PNG' else 'jpeg'}"
        frame = next(ImageSequence.Iterator(im)).copy()
        if frame.mode not in ("RGB", "RGBA"):
            frame = frame.convert("RGBA")
        out = io.BytesIO()
        frame.save(out, format="PNG")
        return out.getvalue(), f"{stem}.png", "image/png"
    except Exception:
        # 无法识别：原样交给守护进程
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        return data, os.path.basename(name) or "image", ctype


def is_big_photo(data: bytes, min_edge: int = 1600) -> bool:
    """长边够大的静态图（AI 画的图这类）：守护进程压到 64KB 后会明显变糊。表情包、动图不算。"""
    from PIL import Image

    try:
        im = Image.open(io.BytesIO(data))
        return not getattr(im, "is_animated", False) and max(im.size) >= min_edge
    except Exception:
        return False


def qr_modules(png: bytes) -> list[list[bool]] | None:
    """守护进程给的登录二维码 PNG → 模块矩阵（True=黑）。按左上角定位块（7 个模块宽）量出模块大小再逐格取样。"""
    try:
        from PIL import Image

        im = Image.open(io.BytesIO(png)).convert("L")
    except Exception:
        return None
    dark = im.point(lambda p: 255 if p < 128 else 0)
    bbox = dark.getbbox()
    if not bbox:
        return None
    im = im.crop(bbox)
    w, h = im.size
    px = im.load()
    run = 0
    while run < w and px[run, 0] < 128:
        run += 1
    m = run / 7
    n = round(w / m) if m else 0
    if m < 1 or not 21 <= n <= 177 or abs(round(h / m) - n) > 1:
        return None
    return [[px[min(w - 1, int((x + 0.5) * m)), min(h - 1, int((y + 0.5) * m))] < 128 for x in range(n)] for y in range(n)]


def qr_text(modules: list[list[bool]], quiet: int = 2) -> str:
    """模块矩阵 → 终端/日志里能扫的字符画：亮的模块画成方块（适合深色背景的日志窗口），两行合成一个字符。"""
    n = len(modules) + 2 * quiet
    light = [[not (quiet <= y < n - quiet and quiet <= x < n - quiet and modules[y - quiet][x - quiet]) for x in range(n)] for y in range(n)]
    light.append([False] * n)
    chars = {(True, True): "█", (True, False): "▀", (False, True): "▄", (False, False): " "}
    return "\n".join("".join(chars[(light[y][x], light[y + 1][x])] for x in range(n)) for y in range(0, n, 2))


async def audio_duration_ms(path: str) -> int | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1", path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), 20)
        val = float(out.decode().strip())
        return max(1, int(val * 1000))
    except Exception:
        return None


VOICE_MAX_SECONDS = 55.0  # 微信语音最长 60 秒，留点余量
_SILENCE = re.compile(r"silence_(start|end): (-?[\d.]+)")


def pick_cuts(total: float, silences: list[tuple[float, float]], limit: float = VOICE_MAX_SECONDS) -> list[float]:
    """长语音的切点：每段不超过 limit 秒，尽量切在换气的静音里（取静音中点）；后半段找不到静音就硬切。"""
    cuts: list[float] = []
    start = 0.0
    while total - start > limit:
        lo, hi = start + limit / 2, start + limit
        hits = [(s, e) for s, e in silences if s < hi and e > lo]
        cut = min(max((hits[-1][0] + hits[-1][1]) / 2, lo), hi) if hits else hi
        cuts.append(cut)
        start = cut
    return cuts


async def split_voice(path: str, total_ms: int) -> list[str]:
    """把超过 VOICE_MAX_SECONDS 的音频按换气停顿切成几段 wav；失败返回 []。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-nostats", "-i", path, "-af", "silencedetect=noise=-35dB:d=0.3", "-f", "null", "-",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await asyncio.wait_for(proc.communicate(), 60)
        marks = [(k, float(v)) for k, v in _SILENCE.findall(err.decode(errors="ignore"))]
        silences, open_at = [], None
        for k, v in marks:
            if k == "start":
                open_at = v
            elif open_at is not None:
                silences.append((open_at, v))
                open_at = None
        total = total_ms / 1000
        if open_at is not None:
            silences.append((open_at, total))
        bounds = [0.0, *pick_cuts(total, silences), total]
        parts = []
        for i, (a, b) in enumerate(zip(bounds, bounds[1:])):
            out = f"{os.path.splitext(path)[0]}_part{i + 1}.wav"
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-loglevel", "error", "-i", path, "-ss", f"{a:.3f}", "-to", f"{b:.3f}",
                "-ar", "16000", "-ac", "1", out,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), 60)
            if proc.returncode != 0:
                raise RuntimeError(f"ffmpeg cut failed at part {i + 1}")
            parts.append(out)
        return parts
    except Exception:
        return []


async def to_wav(path: str) -> str | None:
    """尝试把任意音频(含 silk 之外的格式)转成 wav，供 ASR 使用。失败返回 None。"""
    fd, out = tempfile.mkstemp(suffix=".wav", dir=os.path.dirname(path))
    os.close(fd)
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-loglevel", "error", "-i", path,
            "-ar", "16000", "-ac", "1", out,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), 60)
        if proc.returncode == 0 and os.path.getsize(out) > 44:
            return out
    except Exception:
        pass
    try:
        os.remove(out)
    except OSError:
        pass
    return None
