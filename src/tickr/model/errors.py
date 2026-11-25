"""Typed refusals of the model layer (hard rule 7: one recorded cause, never a guess)."""

from __future__ import annotations


class ModelRefused(Exception):
    """The model path refuses: a bundle that does not verify, feature code that is not the
    pinned training code, an input the training never saw. `cause` is a stable snake_case word
    for records and alerts; `detail` names what was checked (never a credential)."""

    def __init__(self, cause: str, detail: str = "") -> None:
        super().__init__(f"{cause}: {detail}" if detail else cause)
        self.cause = cause
        self.detail = detail
