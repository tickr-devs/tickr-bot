"""Typed refusals of the observer layer."""

from __future__ import annotations


class ObserverError(Exception):
    """Observers cannot be set up (bad configuration, missing strategy folder or module)."""
