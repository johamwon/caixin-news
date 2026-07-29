"""Vercel serverless entry point.

Vercel's Python runtime looks for a WSGI-compatible ``app`` object here.
All routes are rewritten to this function (see ../vercel.json); the Flask
app itself lives one directory up so it can also be run locally as-is.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import app  # noqa: E402,F401  (re-exported for Vercel)
