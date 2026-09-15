"""Where things live on disk.

One module owns this. Before the reorganisation every file worked out its own
paths from `__file__`, which was fine while they all sat in the same directory
and silently wrong the moment any of them moved.
"""

from __future__ import annotations

from pathlib import Path

# draftmons/paths.py -> draftmons/ -> the project root
ROOT = Path(__file__).resolve().parent.parent

# Showdown's published data, and the schema. Checked in, so a clone runs.
DATA = ROOT / "data"

# The static frontend, served at /app by the API.
WEB = ROOT / "web"
