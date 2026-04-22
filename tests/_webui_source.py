"""Shared helper for structural tests that inspect the WebUI source.

The WebUI is being refactored out of a single monolithic tts_ui.py
into multiple files: static/style.css, static/ui.js, and eventually
pages/*.py. Tests that grep the source for HTML, CSS, or JS shapes
need to see the UNION of all those files, not just tts_ui.py.

This helper returns the concatenated source. Each structural test
fixture delegates here so adding a new page module or static asset
doesn't require editing every test file.
"""

from pathlib import Path

_WEBUI_ROOT = Path(__file__).resolve().parent.parent / "glados" / "webui"


def webui_combined_source() -> str:
    """Return the concatenated WebUI source across all split files.

    Order: tts_ui.py first, then every .css/.js under static/, then
    every .py under pages/ (recursively, if present). Files joined
    with newlines so grep patterns match across boundaries but don't
    accidentally bridge tokens.
    """
    parts: list[str] = [(_WEBUI_ROOT / "tts_ui.py").read_text(encoding="utf-8")]

    static_dir = _WEBUI_ROOT / "static"
    if static_dir.is_dir():
        for f in sorted(static_dir.iterdir()):
            if f.is_file() and f.suffix in {".css", ".js"}:
                parts.append(f.read_text(encoding="utf-8"))

    pages_dir = _WEBUI_ROOT / "pages"
    if pages_dir.is_dir():
        for f in sorted(pages_dir.rglob("*.py")):
            parts.append(f.read_text(encoding="utf-8"))

    return "\n".join(parts)
