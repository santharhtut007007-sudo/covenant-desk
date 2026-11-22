"""Turn downloaded filing HTML into clean plain text that people and agents can read."""

from __future__ import annotations

import re
import warnings
from pathlib import Path

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

# Inline-XBRL 10-Qs are XHTML; the HTML parser handles them fine.
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)


def html_to_text(html: str | bytes) -> str:
    """Strip HTML tags, keep paragraph breaks, and normalise whitespace."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()
    # Conformed copies mark deleted words with strikethrough. Drop them, so the text reads as
    # the agreement *as amended* (otherwise "December 31, 2024" + "March 31, 2026" become
    # "DecemberMarch 31, 20242026").
    for tag in soup.find_all(["strike", "s", "del"]):
        tag.decompose()
    for tag in soup.find_all(style=re.compile(r"line-through", re.IGNORECASE)):
        tag.decompose()
    # Put a line break after every block-level element so sections stay separate.
    for tag in soup.find_all(["p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "table"]):
        tag.append("\n")
    text = soup.get_text()
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


TEXT_VERSION = 2  # bump when html_to_text changes, so cached text is rebuilt


def load_text(path: Path) -> str:
    """Read a downloaded filing document as plain text (cached next to the original)."""
    txt_path = path.with_suffix(path.suffix + f".v{TEXT_VERSION}.txt")
    if txt_path.exists():
        return txt_path.read_text()
    text = html_to_text(path.read_bytes())
    txt_path.write_text(text)
    return text


def title_of(text: str, max_lines: int = 12) -> str:
    """First few non-empty lines of a document, which usually name the agreement."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return " | ".join(lines[:max_lines])
