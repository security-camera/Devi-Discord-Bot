import re

CODE_BLOCK_RE = re.compile(r"```(?:[^\n`]*\n)?(.*?)```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"(`+)(.+?)\1", re.DOTALL)
ESCAPED_RE = re.compile(r"\\([\\*_~`|>#\-\[\]()<:])")

MASKED_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((?:<[^>\n]*>|[^)\n]*)\)")
AUTOLINK_RE = re.compile(r"<(https?://[^>\s]+)>")

BOLD_STAR_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
UNDERLINE_RE = re.compile(r"__(.+?)__", re.DOTALL)
STRIKE_RE = re.compile(r"~~(.+?)~~", re.DOTALL)
SPOILER_RE = re.compile(r"\|\|(.+?)\|\|", re.DOTALL)
ITALIC_STAR_RE = re.compile(r"(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?![*\w])")
ITALIC_UNDER_RE = re.compile(r"(?<![_\w])_(?!\s)(.+?)(?<!\s)_(?![_\w])")

HEADER_RE = re.compile(r"^[ \t]*#{1,3}[ \t]+", re.MULTILINE)
SUBTEXT_RE = re.compile(r"^[ \t]*-#[ \t]+", re.MULTILINE)
BLOCKQUOTE_RE = re.compile(r"^[ \t]*>(?:>>)?[ \t]?", re.MULTILINE)
BULLET_RE = re.compile(r"^([ \t]*)[-*][ \t]+", re.MULTILINE)

STASH_RE = re.compile(r"\x00(\d+)\x00")


def strip_markdown(text: str) -> str:
    stash: list[str] = []

    def keep(value: str) -> str:
        stash.append(value)
        return f"\x00{len(stash) - 1}\x00"

    text = CODE_BLOCK_RE.sub(lambda m: keep(m[1].strip("\n")), text)
    text = INLINE_CODE_RE.sub(lambda m: keep(m[2].strip()), text)
    text = ESCAPED_RE.sub(lambda m: keep(m[1]), text)

    text = MASKED_LINK_RE.sub(r"\1", text)
    text = AUTOLINK_RE.sub(r"\1", text)

    text = HEADER_RE.sub("", text)
    text = SUBTEXT_RE.sub("", text)
    text = BLOCKQUOTE_RE.sub("", text)
    text = BULLET_RE.sub(r"\1• ", text)

    for pattern in (BOLD_STAR_RE, UNDERLINE_RE, STRIKE_RE, SPOILER_RE, ITALIC_STAR_RE, ITALIC_UNDER_RE):
        text = pattern.sub(r"\1", text)

    return STASH_RE.sub(lambda m: stash[int(m[1])], text)