import re




def _normalize_closing(s: str) -> str:
    """Нормализует closing для СРАВНЕНИЯ: убирает \r\n, схлопывает любые whitespace
    (включая переносы строк) в одиночный пробел, тримит.

    Это позволяет считать эквивалентными:
      "С уважением,\nАлександр Сверинов"
      "С уважением, Александр Сверинов"
      "С уважением,   Александр Сверинов"
    """
    if not s:
        return ""
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    # схлопываем любые whitespace в одиночный пробел
    s = " ".join(s.split())
    return s.strip()


_HORIZONTAL_RULE = re.compile(r"^[ \t]*(?:-{3,}|_{3,}|\*{3,})[ \t]*$")
_PUNCTUATION_ONLY = re.compile(r"^[ \t]*[.…。·•,:;!?-]+[ \t]*$")
_MARKDOWN_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_COMPLETE_ENDING = re.compile(r'(?:[.!?…][»”"\')\]]*|[🙂👋])$')
_TRAILING_CLOSING = re.compile(
    r"(?is)\s*(?:с\s+уважением|с\s+почтением|best\s+regards|с\s+наилучшими\s+пожеланиями)"
    r"[,.]?\s*[\n\s]*[А-ЯЁA-Z][а-яёa-z]*(?:\s+[А-ЯЁA-Z][а-яёa-z]*){0,2}\s*[.!]?\s*$"
)


def letter_urls(text: str) -> set[str]:
    return {
        match.rstrip(".,;:!?")
        for match in re.findall(r"(?<![\w/@])(?:https?://|www\.)[^\s<>\[\]()\"']+", text)
    }


def has_required_portfolio(text: str, required_url: str) -> bool:
    return not required_url or required_url in letter_urls(text)


def _plain_markdown_link(match: re.Match[str]) -> str:
    label = match.group(1).strip()
    url = match.group(2)
    return url if label.rstrip("/") == url.rstrip("/") else label


def normalize_cover_letter_body(
    letter: str, required_url: str = "", closing: str = ""
) -> str:
    """Convert an LLM draft into clean plain text without changing its prose."""
    body = letter.replace("\r\n", "\n").replace("\r", "\n").strip()
    body = _MARKDOWN_LINK.sub(_plain_markdown_link, body)
    body = _TRAILING_CLOSING.sub("", body).rstrip()
    if closing and body.rstrip().endswith(closing):
        body = body.rstrip()[: -len(closing)].rstrip()
    if required_url:
        body = re.sub(
            r"(?im)^[ \t]*Портфолио:[ \t]*" + re.escape(required_url) + r"[ \t]*$",
            "",
            body,
        )
        body = body.replace(required_url, "")

    clean_lines: list[str] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if _HORIZONTAL_RULE.fullmatch(line) or _PUNCTUATION_ONLY.fullmatch(line):
            continue
        if not line:
            if clean_lines and clean_lines[-1]:
                clean_lines.append("")
            continue
        clean_lines.append(line)
    while clean_lines and not clean_lines[-1]:
        clean_lines.pop()
    return "\n".join(clean_lines).strip()


def _body_without_footer(text: str, required_url: str, closing: str) -> str:
    body = _TRAILING_CLOSING.sub("", text).strip()
    if closing and body.endswith(closing):
        body = body[: -len(closing)].rstrip()
    if required_url:
        body = re.sub(
            r"(?im)^[ \t]*Портфолио:[ \t]*" + re.escape(required_url) + r"[ \t]*$",
            "",
            body,
        ).strip()
    return body


def cover_letter_validation_error(
    text: str, required_url: str, closing: str, max_length: int
) -> str:
    if not text.strip():
        return "cover_letter_empty"
    # Мягкий допуск: LLM плохо соблюдает точные лимиты, поэтому
    # разрешаем письмо до max_length + 400 знаков (~1500 при max=1100).
    # Жёсткий лимит (max_length + 400) — отсекаем.
    if len(text) > max_length + 400:
        return "cover_letter_too_long"
    if "```" in text or _MARKDOWN_LINK.search(text):
        return "cover_letter_markdown"
    for line in text.splitlines():
        if _HORIZONTAL_RULE.fullmatch(line) or _PUNCTUATION_ONLY.fullmatch(line):
            return "cover_letter_layout_artifact"
    if not has_required_portfolio(text, required_url):
        return "required_portfolio_missing"
    _nc = _normalize_closing(closing)
    if _nc and not _normalize_closing(text).endswith(_nc):
        return "required_closing_missing"
    body = _body_without_footer(text, required_url, closing)
    if not body:
        return "cover_letter_body_empty"
    if (required_url or closing) and not _COMPLETE_ENDING.search(body.rstrip()):
        return "cover_letter_incomplete"
    return ""


def is_sendable_cover_letter(
    text: str, required_url: str, closing: str, max_length: int
) -> bool:
    return not cover_letter_validation_error(
        text, required_url, closing, max_length
    )


def format_portfolio_footer(
    letter: str, required_url: str, closing: str, max_length: int
) -> str:
    """Normalize a draft and append the exact footer without truncating prose."""
    body = normalize_cover_letter_body(letter, required_url, closing)
    if not body:
        return ""
    footer_parts = []
    if required_url:
        footer_parts.append(f"Портфолио: {required_url}")
    if closing:
        footer_parts.append(closing)
    result = "\n\n".join([body, *footer_parts])
    if cover_letter_validation_error(result, required_url, closing, max_length):
        return ""
    return result
