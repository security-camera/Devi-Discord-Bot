from __future__ import annotations

import asyncio
import io
import json
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps, ImageSequence

from cogs.quotes.image_generator import FONT_BOLD, _ellipsize, _font, _wrap

log = logging.getLogger(__name__)

MAX_SIDE = 2048
MAX_PIXELS = 60_000_000
MAX_FRAMES = 150
MAX_VIDEO_SECONDS = 30
FFMPEG_TIMEOUT = 180

ANIMATED_SIDES = [480, 360, 240] # max side in pixels, tried from best to worst
VIDEO_PRESETS = [(1280, 23), (854, 26), (640, 29), (426, 32)] # (max width, x264 crf)


class ResultTooLarge(Exception):
    pass


@dataclass(frozen=True)
class CaptionOptions:
    style: str = "outside" # outside | on
    position: str = "top" # top | bottom
    font: Path | None = None # outside style only; None = default


@dataclass
class Overlay:
    image: Image.Image # RGB (outside), RGBA (on)
    outside: bool
    top: bool

    @property
    def bar_height(self) -> int:
        return self.image.height if self.outside else 0


def _font_path(font: Path | None) -> Path:
    if font:
        try:
            _font(font, 10)
            return font
        except OSError:
            log.warning("Caption font %s cannot be loaded, using the default one", font)
    return FONT_BOLD


def _fit(draw: ImageDraw.ImageDraw, text: str, path: Path, max_width: int, start: int, minimum: int, max_height: int, spacing: float):
    size = start
    while True:
        font = _font(path, size)
        lines = _wrap(draw, text, font, max_width) or [""]
        line_height = int(size * spacing)
        if len(lines) * line_height <= max_height or size <= minimum:
            break
        size -= 2

    max_lines = max(1, max_height // line_height)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = _ellipsize(draw, lines[-1] + "…", font, max_width)
    return font, lines, line_height, size


def build_overlay(width: int, height: int, text: str, options: CaptionOptions) -> Overlay:
    top = options.position != "bottom"
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))

    if options.style == "on":
        font, lines, line_height, size = _fit(
            probe, text, FONT_BOLD, int(width * 0.92),
            start=max(16, int(width * 0.075)), minimum=max(12, int(width * 0.035)),
            max_height=max(int(height * 0.35), 40), spacing=1.2,
        )
        stroke = max(2, size // 10)
        margin = max(8, int(size * 0.6))
        y = margin if top else height - margin - len(lines) * line_height

        layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        for line in lines:
            draw.text((width // 2, y), line, font=font, fill="white", stroke_width=stroke, stroke_fill="black", anchor="ma")
            y += line_height
        return Overlay(layer, outside=False, top=top)

    path = _font_path(options.font)
    font, lines, line_height, size = _fit(
        probe, text, path, width - 2 * max(8, int(width * 0.04)),
        start=max(14, int(width * 0.11)), minimum=max(12, int(width * 0.04)),
        max_height=max(int(height * 0.45), 40), spacing=1.15,
    )
    pad_y = int(size * 0.4)
    bar_height = len(lines) * line_height + 2 * pad_y
    bar_height += bar_height % 2

    bar = Image.new("RGB", (width, bar_height), "white")
    draw = ImageDraw.Draw(bar)
    y = pad_y
    for line in lines:
        draw.text((width // 2, y), line, font=font, fill="black", anchor="ma")
        y += line_height
    return Overlay(bar, outside=True, top=top)


def _flatten(image: Image.Image) -> Image.Image:
    if image.mode != "RGBA":
        return image.convert("RGB")
    background = Image.new("RGB", image.size, "white")
    background.paste(image, mask=image)
    return background


def compose(frame: Image.Image, overlay: Overlay) -> Image.Image:
    frame = frame.convert("RGBA")
    if overlay.outside:
        canvas = Image.new("RGB", (frame.width, frame.height + overlay.bar_height), "white")
        canvas.paste(frame, (0, overlay.bar_height if overlay.top else 0), frame)
        canvas.paste(overlay.image, (0, 0 if overlay.top else frame.height))
        return canvas
    return Image.alpha_composite(frame, overlay.image)


def _check_size(img: Image.Image):
    if img.width * img.height > MAX_PIXELS:
        raise ValueError("image is too large")


def is_animated(data: bytes) -> bool:
    try:
        with Image.open(io.BytesIO(data)) as img:
            return getattr(img, "n_frames", 1) > 1
    except Exception:
        return False


def _encode_static(image: Image.Image, limit: int) -> tuple[bytes, str]:
    for scale in (1.0, 0.75, 0.5, 0.35):
        current = image
        if scale != 1.0:
            current = image.resize((max(2, int(image.width * scale)), max(2, int(image.height * scale))), Image.LANCZOS)

        out = io.BytesIO()
        current.save(out, format="PNG")
        if out.tell() <= limit:
            return out.getvalue(), "png"

        out = io.BytesIO()
        _flatten(current).save(out, format="JPEG", quality=88)
        if out.tell() <= limit:
            return out.getvalue(), "jpg"
    raise ResultTooLarge


def render_static(data: bytes, text: str, options: CaptionOptions, limit: int) -> tuple[bytes, str]:
    with Image.open(io.BytesIO(data)) as img:
        _check_size(img)
        frame = ImageOps.exif_transpose(img).convert("RGBA") # respects the phone orientation
    frame.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)

    overlay = build_overlay(frame.width, frame.height, text, options)
    return _encode_static(compose(frame, overlay), limit)


def _animated_gif(data: bytes, text: str, options: CaptionOptions, side: int) -> bytes:
    frames, durations = [], []
    overlay = None
    with Image.open(io.BytesIO(data)) as img:
        _check_size(img)
        for frame in ImageSequence.Iterator(img):
            durations.append(frame.info.get("duration", 100))
            rgba = frame.convert("RGBA")
            rgba.thumbnail((side, side), Image.LANCZOS)
            if not overlay:
                overlay = build_overlay(rgba.width, rgba.height, text, options)
            frames.append(_flatten(compose(rgba, overlay)))
            if len(frames) >= MAX_FRAMES:
                break

    out = io.BytesIO()
    frames[0].save(out, format="GIF", save_all=True, append_images=frames[1:], duration=durations, loop=0, disposal=2)
    return out.getvalue()


def render_animated(data: bytes, text: str, options: CaptionOptions, limit: int) -> bytes:
    for side in ANIMATED_SIDES:
        result = _animated_gif(data, text, options, side)
        if len(result) <= limit:
            return result
    raise ResultTooLarge


async def _run(cmd: list[str], timeout: int) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode, out, err


async def _probe(path: Path) -> tuple[int, int]:
    code, out, err = await _run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height:stream_tags=rotate:stream_side_data=rotation",
        "-of", "json", str(path),
    ], timeout=30)
    if code != 0:
        raise ValueError(err.decode(errors="replace")[-300:])

    stream = json.loads(out)["streams"][0]
    width, height = int(stream["width"]), int(stream["height"])
    rotation = int(float(stream.get("tags", {}).get("rotate", 0)))
    for side_data in stream.get("side_data_list", []):
        if "rotation" in side_data:
            rotation = int(side_data["rotation"])
    if abs(rotation) % 180 == 90:
        width, height = height, width
    return width, height


def _video_filter(width: int, height: int, overlay: Overlay) -> str:
    base = f"[0:v]scale={width}:{height}:flags=lanczos,setsar=1[v0];"
    if overlay.outside:
        bar = overlay.bar_height
        pad_y = bar if overlay.top else 0
        overlay_y = 0 if overlay.top else height
        return base + f"[v0]pad={width}:{height + bar}:0:{pad_y}:color=white[p];[p][1:v]overlay=0:{overlay_y}:shortest=1,format=yuv420p[v]"
    return base + "[v0][1:v]overlay=0:0:shortest=1,format=yuv420p[v]"


async def render_video(data: bytes, suffix: str, text: str, options: CaptionOptions, limit: int) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        src, overlay_path, dst = folder / f"in{suffix}", folder / "overlay.png", folder / "out.mp4"
        await asyncio.to_thread(src.write_bytes, data)
        source_width, source_height = await _probe(src)

        for max_width, crf in VIDEO_PRESETS:
            width = min(source_width, max_width)
            height = round(source_height * width / source_width)
            width -= width % 2 # yuv420p needs even dimensions
            height -= height % 2
            if width < 2 or height < 2:
                raise ValueError("video is too small")

            overlay = await asyncio.to_thread(build_overlay, width, height, text, options)
            await asyncio.to_thread(overlay.image.save, overlay_path)

            code, _, err = await _run([
                "ffmpeg", "-y", "-v", "error", "-t", str(MAX_VIDEO_SECONDS), "-i", str(src),
                "-loop", "1", "-i", str(overlay_path),
                "-filter_complex", _video_filter(width, height, overlay),
                "-map", "[v]", "-map", "0:a?",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
                "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(dst),
            ], timeout=FFMPEG_TIMEOUT)
            if code != 0 or not dst.exists():
                raise ValueError(err.decode(errors="replace")[-300:])

            if dst.stat().st_size <= limit:
                return await asyncio.to_thread(dst.read_bytes)
        raise ResultTooLarge