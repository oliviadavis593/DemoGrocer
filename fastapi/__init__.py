"""Minimal FastAPI-compatible stub for local testing."""
from __future__ import annotations

from .app import Body, Depends, FastAPI, HTTPException, Query
from .staticfiles import StaticFiles

__all__ = ["FastAPI", "Depends", "HTTPException", "Query", "Body", "StaticFiles"]
