from __future__ import annotations

import colorsys
import io
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from paths import env_var, FONTS_DIR

log = logging.getLogger(__name__)

_regular = env_var("QUOTE_FONT_REGULAR", "NotoSans-Regular")
if not _regular.endswith(".ttf"):
    _regular += ".ttf"

_bold = env_var("QUOTE_FONT_BOLD", "NotoSans-Bold")
if not _bold.endswith(".ttf"):
    _bold += ".ttf"

FONT_REGULAR = FONTS_DIR / _regular
FONT_BOLD = FONTS_DIR / _bold

MAX_CUSTOM_FONTS = 24

BACKGROUND = (0, 0, 0)
WHITE = (255, 255, 255)
PLACEHOLDER = (40, 40, 40) # avatar could not be downloaded

FADE_START = 0.45
FADE_END = 0.92

MODES = ("gray", "color", "tint")

TEXT_COLORS = {
    "white": WHITE,
    "red": (255, 82, 82),
    "orange": (255, 152, 67),
    "yellow": (255, 214, 64),
    "green": (92, 214, 110),
    "cyan": (64, 208, 230),
    "blue": (92, 140, 255),
    "purple": (178, 110, 255),
    "pink": (255, 105, 180),
}
AUTO_COLOR = "auto"

@dataclass(frozen=True)
class Layout:
    width: int
    height: int
    avatar_size: int
    vertical_fade: bool # True: the avatar sits on top and fades downwards
    text_center_x: int
    text_max_width: int
    text_top: int # the text block is centered between text_top and text_bottom
    text_bottom: int
    quote_max_height: int
    quote_max_size: int
    quote_min_size: int
    name_size: int
    username_size: int
    bot_size: int
    block_gap: int
    bot_xy: tuple[int, int]
    bot_anchor: str


LANDSCAPE = Layout(
    width=1200, height=630, avatar_size=630, vertical_fade=False,
    text_center_x=900, text_max_width=480, text_top=0, text_bottom=630,
    quote_max_height=340, quote_max_size=46, quote_min_size=22,
    name_size=32, username_size=26, bot_size=24, block_gap=30,
    bot_xy=(1180, 614), bot_anchor="rd",
)

# 9:16
PORTRAIT = Layout(
    width=1080, height=1920, avatar_size=1080, vertical_fade=True,
    text_center_x=540, text_max_width=880, text_top=1060, text_bottom=1800,
    quote_max_height=560, quote_max_size=64, quote_min_size=28,
    name_size=48, username_size=36, bot_size=32, block_gap=48,
    bot_xy=(540, 1870), bot_anchor="md",
)


@dataclass(frozen=True)
class QuoteOptions:
    portrait: bool = False
    mode: str = "gray" # gray | color | tint
    color: tuple[int, int, int] | str | None = None
    font: Path | None = None #None = default


@lru_cache(maxsize=128)
def _font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(path), size)


def list_fonts() -> dict[str, Path]:
    if not FONTS_DIR.is_dir():
        return {}

    skip = {FONT_REGULAR.resolve(), FONT_BOLD.resolve()}
    found = sorted(
        (
            path for path in FONTS_DIR.iterdir()
            if path.is_file()
            and path.suffix.lower() == ".ttf"
            and path.resolve() not in skip
            and len(path.name) <= 100
        ),
        key=lambda path: path.name.lower(),
    )
    return {path.name: path for path in found[:MAX_CUSTOM_FONTS]}


def font_label(path: Path) -> str:
    return path.stem.replace("_", " ").replace("-", " ")

def _resolve_fonts(font: Path | None) -> tuple[Path, Path]:
    if font:
        try:
            _font(font, 10)
            return font, font
        except OSError:
            log.warning("Quote font %s cannot be loaded, using the default one", font)
    return FONT_REGULAR, FONT_BOLD


def _ellipsize(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> str:
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return text.rstrip() + "…"


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.split("\n"):
        words = paragraph.split()
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if draw.textlength(candidate, font=font) <= max_width:
                current = candidate
                continue
            if current:
                lines.append(current)
            while draw.textlength(word, font=font) > max_width:
                cut = len(word)
                while cut > 1 and draw.textlength(word[:cut], font=font) > max_width:
                    cut -= 1
                lines.append(word[:cut])
                word = word[cut:]
            current = word
        if current:
            lines.append(current)
    return lines


def _fit_quote(draw: ImageDraw.ImageDraw, text: str, layout: Layout, font_path: Path):
    for size in range(layout.quote_max_size, layout.quote_min_size - 1, -2):
        font = _font(font_path, size)
        lines = _wrap(draw, text, font, layout.text_max_width)
        line_height = int(size * 1.3)
        if len(lines) * line_height <= layout.quote_max_height:
            return font, lines, line_height

    font = _font(font_path, layout.quote_min_size)
    line_height = int(layout.quote_min_size * 1.3)
    lines = _wrap(draw, text, font, layout.text_max_width)
    lines = lines[: max(1, layout.quote_max_height // line_height)]
    lines[-1] = _ellipsize(draw, lines[-1] + "…", font, layout.text_max_width)
    return font, lines, line_height


def _accent_color(avatar: Image.Image) -> tuple[int, int, int]:
    data = avatar.convert("RGB").resize((48, 48), Image.BILINEAR).tobytes()

    buckets = [[0.0, 0.0, 0.0, 0.0] for _ in range(12)]
    for i in range(0, len(data), 3):
        r, g, b = data[i], data[i + 1], data[i + 2]
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        if s < 0.25 or v < 0.25:
            continue
        weight = s * v
        bucket = buckets[int(h * 12) % 12]
        bucket[0] += weight
        bucket[1] += r * weight
        bucket[2] += g * weight
        bucket[3] += b * weight

    best = max(buckets, key=lambda bucket: bucket[0])
    if best[0] == 0:
        return WHITE # no color in the avatar (gray, black and white)

    r, g, b = (best[i] / best[0] / 255 for i in (1, 2, 3))
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    r, g, b = colorsys.hsv_to_rgb(h, min(s, 0.8), max(v, 0.88))
    return int(r * 255), int(g * 255), int(b * 255)


def _build_avatar_layer(avatar_bytes: bytes | None, mode: str, color, layout: Layout):
    size = layout.avatar_size
    if avatar_bytes:
        avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGB")
        avatar = avatar.resize((size, size), Image.LANCZOS)
    else:
        avatar = Image.new("RGB", (size, size), PLACEHOLDER)

    if color == AUTO_COLOR:
        text_color = _accent_color(avatar)
    elif isinstance(color, tuple):
        text_color = color
    else:
        text_color = WHITE

    if mode == "tint":
        avatar = ImageOps.colorize(avatar.convert("L"), black=BACKGROUND, white=text_color)
    elif mode == "gray":
        avatar = avatar.convert("L").convert("RGB")

    start = int(size * FADE_START)
    end = int(size * FADE_END)
    row = []
    for x in range(size):
        if x <= start:
            row.append(255)
        elif x >= end:
            row.append(0)
        else:
            row.append(int(255 * (end - x) / (end - start)))
    mask = Image.new("L", (size, 1))
    mask.putdata(row)
    mask = mask.resize((size, size))
    if layout.vertical_fade:
        mask = mask.transpose(Image.Transpose.TRANSPOSE)
    return avatar, mask, text_color


def render_quote(avatar_bytes: bytes | None, quote: str, display_name: str, username: str, bot_username: str, options: QuoteOptions = QuoteOptions()) -> io.BytesIO:
    layout = PORTRAIT if options.portrait else LANDSCAPE
    mode = options.mode if options.mode in MODES else "gray"
    regular_path, bold_path = _resolve_fonts(options.font)

    canvas = Image.new("RGB", (layout.width, layout.height), BACKGROUND)
    avatar, mask, text_color = _build_avatar_layer(avatar_bytes, mode, options.color, layout)
    canvas.paste(avatar, (0, 0), mask)

    muted = tuple(c // 2 for c in text_color)

    draw = ImageDraw.Draw(canvas)

    quote_font, quote_lines, line_height = _fit_quote(draw, f"“{quote}”", layout, regular_path)
    name_font = _font(bold_path, layout.name_size)
    user_font = _font(bold_path, layout.username_size)
    bot_font = _font(bold_path, layout.bot_size)

    name_text = _ellipsize(draw, display_name, name_font, layout.text_max_width)
    user_text = _ellipsize(draw, f"@{username}", user_font, layout.text_max_width)

    name_height = int(layout.name_size * 1.3)
    user_height = int(layout.username_size * 1.3)
    block_height = len(quote_lines) * line_height + layout.block_gap + name_height + user_height
    y = layout.text_top + (layout.text_bottom - layout.text_top - block_height) // 2
    x = layout.text_center_x

    for line in quote_lines:
        draw.text((x, y), line, font=quote_font, fill=text_color, anchor="ma")
        y += line_height

    y += layout.block_gap
    draw.text((x, y), name_text, font=name_font, fill=text_color, anchor="ma")
    y += name_height
    draw.text((x, y), user_text, font=user_font, fill=muted, anchor="ma")

    draw.text(layout.bot_xy, f"@{bot_username}", font=bot_font, fill=muted, anchor=layout.bot_anchor)

    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer