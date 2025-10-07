"""Minimal FastAPI-compatible stub for local testing."""
from __future__ import annotations

from .app import Depends, FastAPI, HTTPException, Query

__all__ = ["FastAPI", "Depends", "HTTPException", "Query"]
