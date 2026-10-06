"""Small text helpers: HTML-to-text conversion, safe filenames, time zones."""

from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

_BLOCK_TAGS = {
    "p", "div", "br", "li", "ul", "ol", "tr", "table", "h1", "h2", "h3",
    "h4", "h5", "h6", "blockquote", "pre", "hr",
}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "li":
            self.parts.append("\n- ")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(value: str | None) -> str:
    """Convert the (sometimes HTML-formatted) comment body to plain text."""
    if not value:
        return ""
    if "<" not in value:
        text = html.unescape(value)
    else:
        parser = _TextExtractor()
        parser.feed(value)
        parser.close()
        text = "".join(parser.parts)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    lines = [line.rstrip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_filename(name: str | None, max_length: int = 80) -> str:
    """Return a filename component that is safe on Windows, macOS and Linux."""
    name = _UNSAFE_CHARS.sub("_", name or "")
    name = re.sub(r"_+", "_", re.sub(r"\s+", " ", name))
    name = name.strip(" .").rstrip("_ .")
    if len(name) > max_length:
        name = name[:max_length].rstrip(" ._")
    if name.upper() in _WINDOWS_RESERVED:
        name = f"_{name}"
    return name


def parse_api_datetime(value: str) -> datetime:
    """Parse a Regulations.gov timestamp such as ``2020-08-10T15:58:52Z``."""
    value = value.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _nth_sunday(year: int, month: int, n: int) -> datetime:
    first = datetime(year, month, 1)
    offset = (6 - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _to_us_eastern_fallback(dt_utc: datetime) -> datetime:
    """US Eastern conversion using post-2007 DST rules (no tz database needed)."""
    year = dt_utc.year
    # DST starts 2:00 EST (07:00 UTC) on the 2nd Sunday of March and ends
    # 2:00 EDT (06:00 UTC) on the 1st Sunday of November.
    dst_start = _nth_sunday(year, 3, 2).replace(hour=7, tzinfo=timezone.utc)
    dst_end = _nth_sunday(year, 11, 1).replace(hour=6, tzinfo=timezone.utc)
    offset = -4 if dst_start <= dt_utc < dst_end else -5
    return (dt_utc + timedelta(hours=offset)).replace(tzinfo=None)


def to_eastern_filter_value(value: str) -> str:
    """Convert an API timestamp to the Eastern-time format used by date filters.

    The API returns ``lastModifiedDate`` in UTC, but ``filter[lastModifiedDate]``
    expects ``YYYY-MM-DD HH:MM:SS`` in US Eastern time.
    """
    dt_utc = parse_api_datetime(value).astimezone(timezone.utc)
    try:
        from zoneinfo import ZoneInfo

        local = dt_utc.astimezone(ZoneInfo("America/New_York")).replace(tzinfo=None)
    except Exception:  # tz database unavailable (e.g. Windows without tzdata)
        local = _to_us_eastern_fallback(dt_utc)
    return local.strftime("%Y-%m-%d %H:%M:%S")
