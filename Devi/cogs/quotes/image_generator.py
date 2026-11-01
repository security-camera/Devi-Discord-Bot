from __future__ import annotations

import io
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from paths import env_var, FONTS_DIR

_regular = env_var("QUOTE_FONT_REGULAR", "NotoSans-Regular")
if not _regular.endswith(".ttf"):
    _regular += ".ttf"

_bold = env_var("QUOTE_FONT_BOLD", "NotoSans-Bold")
if not _bold.endswith(".ttf"):
    _bold += ".ttf"

FONT_REGULAR = FONTS_DIR / _regular
FONT_BOLD = FONTS_DIR / _bold

WIDTH, HEIGHT = 1200, 630
BACKGROUND = (0, 0, 0)

AVATAR_SIZE = HEIGHT
FADE_START = 0.45
FADE_END = 0.92
GRAYSCALE = True

TEXT_CENTER_X = 900
TEXT_MAX_WIDTH = 480
QUOTE_MAX_HEIGHT = 340
QUOTE_MAX_SIZE = 46
QUOTE_MIN_SIZE = 22
NAME_SIZE = 32
USERNAME_SIZE = 26
BOT_USERNAME_SIZE = 24
BLOCK_GAP = 30

WHITE = (255, 255, 255)
GRAY = (128, 128, 128)


@lru_cache(maxsize=64)
def _font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(path), size)


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


def _fit_quote(draw: ImageDraw.ImageDraw, text: str):
    for size in range(QUOTE_MAX_SIZE, QUOTE_MIN_SIZE - 1, -2):
        font = _font(FONT_REGULAR, size)
        lines = _wrap(draw, text, font, TEXT_MAX_WIDTH)
        line_height = int(size * 1.3)
        if len(lines) * line_height <= QUOTE_MAX_HEIGHT:
            return font, lines, line_height

    font = _font(FONT_REGULAR, QUOTE_MIN_SIZE)
    line_height = int(QUOTE_MIN_SIZE * 1.3)
    lines = _wrap(draw, text, font, TEXT_MAX_WIDTH)
    lines = lines[: max(1, QUOTE_MAX_HEIGHT // line_height)]
    lines[-1] = _ellipsize(draw, lines[-1] + "…", font, TEXT_MAX_WIDTH)
    return font, lines, line_height


def _build_avatar_layer(avatar_bytes: bytes | None, grayscale: bool = GRAYSCALE) -> tuple[Image.Image, Image.Image]:
    if avatar_bytes:
        avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGB")
        avatar = avatar.resize((AVATAR_SIZE, AVATAR_SIZE), Image.LANCZOS)
    else: # Avatar could not be downloaded
        avatar = Image.new("RGB", (AVATAR_SIZE, AVATAR_SIZE), (40, 40, 40))

    if grayscale:
        avatar = avatar.convert("L").convert("RGB")

    start = int(AVATAR_SIZE * FADE_START)
    end = int(AVATAR_SIZE * FADE_END)
    row = []
    for x in range(AVATAR_SIZE):
        if x <= start:
            row.append(255)
        elif x >= end:
            row.append(0)
        else:
            row.append(int(255 * (end - x) / (end - start)))
    mask = Image.new("L", (AVATAR_SIZE, 1))
    mask.putdata(row)
    mask = mask.resize((AVATAR_SIZE, AVATAR_SIZE))
    return avatar, mask


def render_quote(avatar_bytes: bytes | None, quote: str, display_name: str, username: str, bot_username: str, grayscale: bool = GRAYSCALE) -> io.BytesIO:
    canvas = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
    avatar, mask = _build_avatar_layer(avatar_bytes, grayscale)
    canvas.paste(avatar, (0, 0), mask)

    draw = ImageDraw.Draw(canvas)

    quote_font, quote_lines, line_height = _fit_quote(draw, f"“{quote}”")
    name_font = _font(FONT_BOLD, NAME_SIZE)
    user_font = _font(FONT_BOLD, USERNAME_SIZE)
    bot_font = _font(FONT_BOLD, BOT_USERNAME_SIZE)

    name_text = _ellipsize(draw, display_name, name_font, TEXT_MAX_WIDTH)
    user_text = _ellipsize(draw, f"@{username}", user_font, TEXT_MAX_WIDTH)

    name_height = int(NAME_SIZE * 1.3)
    user_height = int(USERNAME_SIZE * 1.3)
    block_height = len(quote_lines) * line_height + BLOCK_GAP + name_height + user_height
    y = (HEIGHT - block_height) // 2

    for line in quote_lines:
        draw.text((TEXT_CENTER_X, y), line, font=quote_font, fill=WHITE, anchor="ma")
        y += line_height

    y += BLOCK_GAP
    draw.text((TEXT_CENTER_X, y), name_text, font=name_font, fill=WHITE, anchor="ma")
    y += name_height
    draw.text((TEXT_CENTER_X, y), user_text, font=user_font, fill=GRAY, anchor="ma")

    draw.text((WIDTH - 20, HEIGHT - 16),f"@{bot_username}", font=bot_font, fill=GRAY, anchor="rd")

    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer