"""Entry point: `uvicorn main:app`.

A shim rather than the application itself. The app lives in draftmons/app.py
with the rest of the package; this keeps the documented run command working
from the project root, which is also where the data/ and web/ directories the
app reads are.
"""

from draftmons.app import app

__all__ = ["app"]
