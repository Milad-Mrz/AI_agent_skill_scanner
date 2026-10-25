"""Reveal hidden content in a skill (deterministic, no model).

For every text file this produces a *revealed* copy in which nothing can stay hidden:

- invisible and format characters (zero-width, bidirectional controls, variation
  selectors, Unicode tag characters) are replaced by visible markers, and tag
  characters are decoded because they can carry hidden ASCII text;
- look-alike letters from other scripts are marked with the ASCII letter they imitate;
- encoded blobs (base64, hex, binary, Morse) are decoded and shown inline;
- text that is hidden when markdown is rendered (HTML comments, ``<details>``,
  image alt text, link titles) is listed;
- unreadable tokens (long high-entropy strings, or many words outside the
  dictionary) are flagged.

The revealed text is meant for human review and for the explain step, which reads
the revealed text instead of the raw file.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path

from skillspector.nodes.analyzers.static_fork_gaps import _decode_candidates
from skillspector.unicode_confusables import ASCII_CONFUSABLE_SKELETON

TEXT_SUFFIXES = {
    ".md",
    ".markdown",
    ".txt",
    ".py",
    ".sh",
    ".bash",
    ".zsh",
    ".js",
    ".mjs",
    ".ts",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".html",
    ".xml",
    ".ps1",
    ".rb",
    ".go",
    ".rs",
}
MAX_FILE_BYTES = 1_000_000
_DICTIONARY_PATHS = ("/usr/share/dict/words", "/usr/share/dict/american-english")

# Characters that render as nothing (or reorder text) and have no place in a skill.
_INVISIBLE = {
    0x00AD,
    0x034F,
    0x061C,
    0x115F,
    0x1160,
    0x17B4,
    0x17B5,
    0x180E,
    0x200B,
    0x200C,
    0x200D,
    0x200E,
    0x200F,
    0x202A,
    0x202B,
    0x202C,
    0x202D,
    0x202E,
    0x2060,
    0x2061,
    0x2062,
    0x2063,
    0x2064,
    0x2066,
    0x2067,
    0x2068,
    0x2069,
    0x206A,
    0x206B,
    0x206C,
    0x206D,
    0x206E,
    0x206F,
    0x3164,
    0xFEFF,
    0xFFA0,
}


@dataclass
class HiddenItem:
    kind: str  # invisible | tag-text | homoglyph | encoded | markup | unreadable
    line: int
    detail: str
    original: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RevealedFile:
    path: str
    revealed: str
    items: list[HiddenItem] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.items)

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "revealed": self.revealed,
            "items": [item.to_dict() for item in self.items],
            "counts": dict(Counter(item.kind for item in self.items)),
        }


def _char_name(code: int) -> str:
    try:
        return unicodedata.name(chr(code))
    except ValueError:
        return "UNNAMED"


def _is_invisible(code: int) -> bool:
    if code in _INVISIBLE or 0xFE00 <= code <= 0xFE0F or 0xE0100 <= code <= 0xE01EF:
        return True
    return unicodedata.category(chr(code)) == "Cf" and not (0xE0000 <= code <= 0xE007F)


@lru_cache(maxsize=1)
def _dictionary() -> frozenset[str]:
    for candidate in _DICTIONARY_PATHS:
        path = Path(candidate)
        if path.is_file():
            words = path.read_text(encoding="utf-8", errors="ignore").split()
            return frozenset(w.lower() for w in words if w.isalpha())
    return frozenset()


def _entropy(token: str) -> float:
    counts = Counter(token)
    return -sum(n / len(token) * math.log2(n / len(token)) for n in counts.values())


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _reveal_characters(text: str, items: list[HiddenItem]) -> str:
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        code = ord(ch)
        if 0xE0000 <= code <= 0xE007F:
            # Unicode tag characters mirror ASCII and are a known smuggling channel.
            j = i
            while j < len(text) and 0xE0000 <= ord(text[j]) <= 0xE007F:
                j += 1
            hidden = "".join(
                chr(ord(c) - 0xE0000) for c in text[i:j] if 0x20 <= ord(c) - 0xE0000 < 0x7F
            )
            out.append(f"⟦TAG TEXT: {hidden}⟧")
            items.append(HiddenItem("tag-text", _line_of(text, i), f"hidden tag text {hidden!r}"))
            i = j
            continue
        if 0xFE00 <= code <= 0xFE0F and i > 0 and unicodedata.category(text[i - 1]) == "So":
            # Emoji presentation selector after a symbol is normal.
            out.append(ch)
        elif _is_invisible(code):
            out.append(f"⟦U+{code:04X} {_char_name(code)}⟧")
            items.append(
                HiddenItem("invisible", _line_of(text, i), f"U+{code:04X} {_char_name(code)}")
            )
        elif (
            code > 0x7F
            and code in ASCII_CONFUSABLE_SKELETON
            and unicodedata.category(ch).startswith("L")
            and _in_ascii_word(text, i)
        ):
            ascii_form = ASCII_CONFUSABLE_SKELETON[code]
            script = _char_name(code).split(" ")[0].title()
            out.append(f"⟦{ch}→{ascii_form} {script}⟧")
            items.append(
                HiddenItem(
                    "homoglyph",
                    _line_of(text, i),
                    f"{script} {_char_name(code)} looks like {ascii_form!r}",
                    original=_word_at(text, i),
                )
            )
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _in_ascii_word(text: str, index: int) -> bool:
    """True when the character sits in a word that is otherwise mostly ASCII letters."""
    word = _word_at(text, index)
    letters = [c for c in word if c.isalpha()]
    ascii_letters = sum(c.isascii() for c in letters)
    return len(letters) >= 2 and ascii_letters >= len(letters) / 2


def _word_at(text: str, index: int) -> str:
    start = index
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    end = index
    while end < len(text) and not text[end].isspace():
        end += 1
    return text[start:end]


_MARKUP = [
    ("HTML comment", re.compile(r"<!--(.*?)-->", re.S)),
    ("collapsed <details> block", re.compile(r"<details\b[^>]*>(.*?)</details>", re.S | re.I)),
    ("image alt text", re.compile(r"!\[([^\]]{3,})\]\(")),
    ("link title", re.compile(r"\[[^\]]*\]\([^)\s]+\s+[\"']([^\"']{3,})[\"']\)")),
    (
        "HTML hidden element",
        re.compile(r"<[^>]+(?:hidden|display\s*:\s*none)[^>]*>(.*?)</", re.S | re.I),
    ),
]


def _markup_items(text: str, items: list[HiddenItem]) -> None:
    for label, pattern in _MARKUP:
        for match in pattern.finditer(text):
            inner = " ".join(match.group(1).split())
            if inner:
                items.append(
                    HiddenItem("markup", _line_of(text, match.start()), f"{label}: {inner[:300]}")
                )


_TOKEN = re.compile(r"[A-Za-z0-9+/=_\-]{20,}")
_WORD = re.compile(r"[A-Za-z]{3,}")


def _readable_identifier(token: str) -> bool:
    """True for identifiers built from words or numbers (kebab, snake, camel case, dates)."""
    dictionary = _dictionary()
    parts = [
        p
        for p in re.split(
            r"[-_/+=.]|(?<=[a-z])(?=[A-Z])|(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", token
        )
        if p
    ]
    if len(parts) < 2:
        return False
    known = sum(
        p.isdigit()
        or (len(p) <= 3)
        or (p.lower() in dictionary)
        or (p.lower().rstrip("s") in dictionary)
        for p in parts
    )
    return known / len(parts) >= 0.7


def _unreadable_items(
    text: str, items: list[HiddenItem], decoded_spans: list[tuple[int, int]], prose: bool
) -> None:
    for match in _TOKEN.finditer(text):
        token = match.group(0)
        if any(start <= match.start() < end for start, end in decoded_spans):
            continue
        word = _word_at(text, match.start())
        if "://" in word or word.startswith("www.") or ("/" in word and "." in word):
            continue  # URLs and file paths
        if "/" in token and token.count("/") > 2:  # paths
            continue
        if token.replace("_", "").replace("-", "").isalpha():  # long identifiers
            continue
        if re.fullmatch(r"[0-9a-f]{32,128}", token):  # hashes are common and benign
            continue
        if _entropy(token) >= 4.0 and not _readable_identifier(token):
            items.append(
                HiddenItem(
                    "unreadable",
                    _line_of(text, match.start()),
                    f"high-entropy token ({_entropy(token):.1f} bits/char)",
                    original=token[:120],
                )
            )
    dictionary = _dictionary()
    if not dictionary or not prose:
        return
    in_code = False
    for number, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("```"):
            in_code = not in_code
            continue
        if in_code or line.startswith(("    ", "\t")) or "`" in line:
            continue
        words = [w.lower() for w in _WORD.findall(line)]
        if len(words) < 6:
            continue
        unknown = [w for w in words if w not in dictionary and w.rstrip("s") not in dictionary]
        if len(unknown) / len(words) > 0.6:
            items.append(
                HiddenItem(
                    "unreadable",
                    number,
                    f"{len(unknown)}/{len(words)} words not in the dictionary",
                    original=line.strip()[:160],
                )
            )


def reveal_text(text: str, path: str = "") -> RevealedFile:
    """Return the revealed form of ``text`` and the hidden items found in it."""
    items: list[HiddenItem] = []
    decoded_spans: list[tuple[int, int]] = []
    insertions: list[tuple[int, str]] = []
    for match, encoding, decoded in _decode_candidates(text):
        decoded_spans.append((match.start(), match.end()))
        items.append(
            HiddenItem(
                "encoded",
                _line_of(text, match.start()),
                f"{encoding} decodes to {decoded[:300]!r}",
                original=match.group(0)[:120],
            )
        )
        insertions.append((match.end(), f" ⟦DECODED {encoding}: {decoded[:300]}⟧"))
    with_decoded = text
    for position, note in sorted(insertions, reverse=True):
        with_decoded = with_decoded[:position] + note + with_decoded[position:]
    if path.lower().endswith((".md", ".markdown")) or not path:
        _markup_items(text, items)
    prose = not path or path.lower().endswith((".md", ".markdown", ".txt"))
    _unreadable_items(text, items, decoded_spans, prose)
    revealed = _reveal_characters(with_decoded, items)
    items.sort(key=lambda item: (item.line, item.kind))
    return RevealedFile(path=path, revealed=revealed, items=items)


def reveal_skill(root: Path) -> list[RevealedFile]:
    """Reveal every text file under ``root``; binary and oversized files are skipped."""
    results: list[RevealedFile] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink() or ".git" in path.parts:
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name != "SKILL.md":
            continue
        if path.stat().st_size > MAX_FILE_BYTES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        results.append(reveal_text(text, str(path.relative_to(root))))
    return results
