"""UI Stylesheets and theme helpers."""

from pathlib import Path


def load_stylesheet() -> str:
    """Reads and returns the master QSS stylesheet content."""
    qss_path = Path(__file__).resolve().parent / "stylesheet.qss"
    if qss_path.exists():
        with open(qss_path, "r", encoding="utf-8") as f:
            return f.read()
    return ""
