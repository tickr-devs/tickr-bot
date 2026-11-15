"""The model call. `AdvisorClient` is the seam: tests use a fake, the CLI uses Claude.

`AnthropicClient` is the only importer of the Anthropic SDK in this repository. Credentials:
the SDK resolves `ANTHROPIC_API_KEY` from the environment itself; this code checks only that
the configured variable NAME is set and never reads, prints or stores its value (rule 4).
One request per run, streamed (long input and output), adaptive thinking, structured output
against the proposal schema, server-side refusal fallback. A failed request is not retried
by this code beyond the SDK's own transport retries: the run fails with one cause.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from tickr.advisor.errors import AdvisorError
from tickr.config import AdvisorConfig

SUPPORTED_KEY_VARIABLE = "ANTHROPIC_API_KEY"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass(frozen=True)
class AdvisorReply:
    text: str
    """The JSON text of the answer (validated later by advisor.proposal)."""
    model: str
    """The model that actually answered (a fallback model if the first one declined)."""


class AdvisorClient(Protocol):
    def propose(self, system: str, user: str, schema: Mapping[str, Any]) -> AdvisorReply: ...


class AnthropicClient:
    def __init__(self, settings: AdvisorConfig, environ: Mapping[str, str] | None = None,
                 sdk: Any = None):
        """`sdk` replaces the `anthropic` module in tests; the real run leaves it None."""
        environ = os.environ if environ is None else environ
        if settings.api_key_variable != SUPPORTED_KEY_VARIABLE:
            raise AdvisorError(
                f"[advisor] api_key_variable must be {SUPPORTED_KEY_VARIABLE} "
                "(the SDK reads it itself)"
            )
        if not environ.get(settings.api_key_variable):  # presence only
            raise AdvisorError(f"{settings.api_key_variable} is not set")
        anthropic = sdk
        if anthropic is None:
            try:
                import anthropic
            except ImportError as exc:
                raise AdvisorError("Anthropic SDK missing: pip install -e '.[advisor]'") from exc
        self._anthropic = anthropic
        self._client = anthropic.Anthropic(timeout=float(settings.request_timeout_seconds))
        self.settings = settings

    def propose(self, system: str, user: str, schema: Mapping[str, Any]) -> AdvisorReply:
        settings = self.settings
        anthropic = self._anthropic
        try:
            with self._client.beta.messages.stream(
                model=settings.model,
                max_tokens=settings.max_output_tokens,
                betas=[FALLBACK_BETA],
                fallbacks="default",
                thinking={"type": "adaptive"},
                output_config={
                    "effort": settings.effort,
                    "format": {"type": "json_schema", "schema": dict(schema)},
                },
                system=system,
                messages=[{"role": "user", "content": user}],
            ) as stream:
                message = stream.get_final_message()
        except anthropic.AuthenticationError as exc:
            raise AdvisorError("Anthropic API refused the credentials") from exc
        except anthropic.RateLimitError as exc:
            raise AdvisorError("Anthropic API rate limit; run again later") from exc
        except anthropic.APIStatusError as exc:
            raise AdvisorError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise AdvisorError(f"Anthropic API not reachable: {exc}") from exc
        if message.stop_reason == "refusal":
            raise AdvisorError("the model declined the request (refusal)")
        if message.stop_reason == "max_tokens":
            raise AdvisorError("answer cut at max_output_tokens; raise [advisor] max_output_tokens")
        text = next((b.text for b in message.content if b.type == "text"), None)
        if text is None:
            raise AdvisorError(f"no text in the answer (stop_reason {message.stop_reason})")
        return AdvisorReply(text=text, model=message.model)
