"""Document ingestion — parse, chunk, store."""
import os
import re
import subprocess
import tempfile
from pathlib import Path
from dataclasses import dataclass


@dataclass
class Chunk:
    section_path: str
    heading_level: int
    body: str
    line_start: int


def ingest_file(file_path: str, chunk_size: int = 1500, chunk_overlap: int = 150) -> tuple[str, str, list[Chunk]]:
    """Ingest a file, return (title, author, list of Chunks). Supports .md, .txt, .epub."""
    path = Path(file_path)
    suffix = path.suffix.lower()

    if suffix == ".epub":
        text = _epub_to_markdown(str(path))
        title = _extract_title(text) or path.stem
        author = ""
        chunks = _chunk_markdown(text)
    elif suffix == ".md":
        text = path.read_text(encoding="utf-8")
        title = _extract_title(text) or path.stem
        author = ""
        chunks = _chunk_markdown(text)
    elif suffix == ".txt":
        text = path.read_text(encoding="utf-8")
        title = path.stem
        author = ""
        chunks = _chunk_sliding_window(text, chunk_size, chunk_overlap)
    else:
        raise ValueError(f"Unsupported file type: {suffix}")

    return title, author, chunks


def _extract_title(text: str) -> str | None:
    """Extract title from first # heading or first line."""
    for line in text.split("\n")[:20]:
        m = re.match(r"^#\s+(.+)", line)
        if m:
            return m.group(1).strip()
    first_line = text.split("\n")[0].strip()
    return first_line[:200] if first_line else None


def _chunk_markdown(text: str) -> list[Chunk]:
    """Chunk markdown by ## and ### headings."""
    chunks = []
    sections = re.split(r"\n(?=## )", text)
    line_offset = 0

    for section in sections:
        lines = section.split("\n")
        heading_match = re.match(r"^(#{2,3})\s+(.+)", lines[0]) if lines else None
        if heading_match:
            level = len(heading_match.group(1))  # 2 for ##, 3 for ###
            section_path = heading_match.group(2).strip()
            body = "\n".join(lines[1:]).strip()
        else:
            level = 0
            section_path = ""
            body = section.strip()

        if not body:
            line_offset += len(lines)
            continue

        chunks.append(Chunk(
            section_path=section_path,
            heading_level=level,
            body=body,
            line_start=line_offset,
        ))
        line_offset += len(lines)

    return chunks


def _chunk_sliding_window(text: str, chunk_size: int, overlap: int) -> list[Chunk]:
    """Sliding window chunking for plain text."""
    chunks = []
    text = text.strip()
    pos = 0
    while pos < len(text):
        end = min(pos + chunk_size, len(text))
        # Try to break at paragraph boundary
        if end < len(text):
            para_break = text.rfind("\n\n", pos, end)
            if para_break > pos + chunk_size // 2:
                end = para_break
        body = text[pos:end].strip()
        if body:
            chunks.append(Chunk(
                section_path="",
                heading_level=0,
                body=body,
                line_start=pos,
            ))
        pos = end - overlap if end < len(text) else len(text)
    return chunks


def _epub_to_markdown(file_path: str) -> str:
    """Convert epub to markdown via pandoc."""
    try:
        result = subprocess.run(
            ["pandoc", file_path, "-t", "markdown", "--wrap=none"],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            return result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Fallback: extract from zip
    import zipfile
    with zipfile.ZipFile(file_path) as zf:
        for name in zf.namelist():
            if name.endswith((".html", ".xhtml", ".htm")) and "nav" not in name.lower():
                html = zf.read(name).decode("utf-8", errors="replace")
                # Crude HTML-to-text: strip tags
                text = re.sub(r"<[^>]+>", " ", html)
                text = re.sub(r"\s+", " ", text).strip()
                return text
    return ""
