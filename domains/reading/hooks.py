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
    # Remove calibre HTML class markers
    text = re.sub(r'\{\.calibre\d+\}', '', text)
    # Remove ISBN metadata lines
    text = re.sub(r'(?im)^ISBN[:\s].*$', '', text)
    # Remove empty links
    text = re.sub(r'\[\]\(.*?\)', '', text)
    # Remove Telegram ad patterns
    text = re.sub(r'(?i)(?:关注|加入).{0,20}(?:频道|群组|telegram).{0,50}', '', text)
    return text


def _normalize_whitespace(text: str, meta: dict) -> str:
    """Normalize whitespace: collapse multiple blank lines."""
    return re.sub(r'\n{3,}', '\n\n', text).strip()
