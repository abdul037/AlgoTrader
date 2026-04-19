"""Identifier helpers."""

from __future__ import annotations

from uuid import uuid4


def generate_id(prefix: str) -> str:
    """Generate a compact prefixed identifier."""

    return f"{prefix}_{uuid4().hex[:12]}"
