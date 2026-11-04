"""出站媒体预处理：GIF/动图 -> 静态 PNG 首帧；音频时长。"""

from __future__ import annotations

import asyncio
import io
import mimetypes
import os
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


async def to_wav(path: str) -> str | None:
    """尝试把任意音频(含 silk 之外的格式)转成 wav，供 ASR 使用。失败返回 None。"""
    out = tempfile.mktemp(suffix=".wav", dir=os.path.dirname(path))
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
    return None
