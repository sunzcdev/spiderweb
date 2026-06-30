"""Reading domain hooks — pre_ingest cleaners, post_extract rule engine, custom validators."""
import re
from spiderweb.engine.hooks import DomainHooks


class ReadingHooks(DomainHooks):
    """Hooks for the reading domain."""

    def pre_ingest(self):
        """Chain: strip EPUB artifacts, normalize whitespace."""
        return [
            _clean_epub_artifacts,
            _normalize_whitespace,
        ]

    def post_extract(self):
        """Chain: no rule engine by default, but domain authors can add here."""
        return []

    def validators(self):
        """Chain: custom validation rules."""
        return []


# === Pre-ingest hooks ===

def _clean_epub_artifacts(text: str, meta: dict) -> str:
    """Strip common EPUB/calibre artifacts."""
    # calibre HTML class markers: {.calibreN}, {.calibreNN}
    text = re.sub(r'(?i)\{\.calibre\d+\}', '', text)
    # ISBN / word-count metadata lines
    text = re.sub(r'(?im)^(?:ISBN|字数|Word Count)[:\s].*$', '', text)
    # Image references: ![alt](...) and empty links [](#...)
    text = re.sub(r'(?i)!\[.*?\]\(.*?\)', '', text)
    text = re.sub(r'(?i)\[\]\(.*?\)', '', text)
    # Telegram / channel subscription ads
    text = re.sub(r'(?im)(?:关注|加入|订阅)\s*.{0,30}(?:频道|群组|telegram|公众号).{0,80}', '', text)
    return text


def _normalize_whitespace(text: str, meta: dict) -> str:
    """Normalize whitespace: collapse multiple blank lines."""
    return re.sub(r'\n{3,}', '\n\n', text).strip()
