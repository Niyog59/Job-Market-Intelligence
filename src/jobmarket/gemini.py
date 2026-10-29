"""A thin client for one Gemini structured-output request (official google-genai SDK).

It sends one document, returns the reply text with its token usage, and turns every failure
into a ModelError with a kind the extraction queue can act on:

  rate_limited     HTTP 429 for a per-minute limit: wait (Google's retry delay if given)
  quota_exhausted  HTTP 429 for a per-day limit: stop until the daily reset
  server_error     HTTP 5xx: retry later
  network_error    timeout or connection failure: retry later
  client_error     another 4xx (bad request, key, permission): stop and report
  blocked          no usable text came back (safety block, empty reply)

The SDK's own retries are switched off so the queue's retry policy is the only one. The API
key is passed to the SDK and never logged, printed, or stored; error details are taken
from Google's error body, which does not contain it.
"""

import re
from dataclasses import dataclass
from typing import Any

API_KEY_ENV = "GEMINI_API_KEY"

KINDS = (
    "rate_limited", "quota_exhausted", "server_error", "network_error", "client_error",
    "blocked",
)  # fmt: skip


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int | None = None
    output_tokens: int | None = None
    thought_tokens: int | None = None
    total_tokens: int | None = None
    traffic_type: str | None = None


@dataclass(frozen=True)
class Reply:
    text: str
    usage: Usage
    model_version: str | None
    finish_reason: str | None


class ModelError(Exception):
    def __init__(
        self,
        kind: str,
        detail: str,
        http_status: int | None = None,
        retry_after: float | None = None,
        usage: Usage | None = None,
        quota_id: str | None = None,
    ):
        super().__init__(f"{kind}: {detail}")
        self.kind, self.detail, self.http_status = kind, detail, http_status
        self.retry_after, self.usage, self.quota_id = retry_after, usage, quota_id


_DELAY = re.compile(r"^(\d+(?:\.\d+)?)s$")


def _quota_details(body: Any) -> tuple[str | None, float | None]:
    """(first quotaId, retry delay seconds) from a google.rpc error body."""
    error = body.get("error", body) if isinstance(body, dict) else {}
    quota_id = delay = None
    for item in error.get("details") or [] if isinstance(error, dict) else []:
        if not isinstance(item, dict):
            continue
        kind = item.get("@type", "")
        if kind.endswith("QuotaFailure"):
            for violation in item.get("violations") or []:
                if isinstance(violation, dict) and violation.get("quotaId"):
                    quota_id = quota_id or str(violation["quotaId"])
        elif kind.endswith("RetryInfo"):
            match = _DELAY.match(str(item.get("retryDelay", "")))
            if match:
                delay = float(match.group(1))
    return quota_id, delay


def classify_api_error(code: int | None, body: Any, message: str) -> ModelError:
    detail = (message or "")[:500]
    if code == 429:
        quota_id, delay = _quota_details(body)
        daily = quota_id is not None and "perday" in quota_id.lower()
        return ModelError(
            "quota_exhausted" if daily else "rate_limited",
            detail,
            429,
            delay,
            quota_id=quota_id,
        )
    if code is not None and code >= 500:
        return ModelError("server_error", detail, code)
    return ModelError("client_error", detail, code)


def _usage(meta: Any) -> Usage:
    if meta is None:
        return Usage()
    traffic = getattr(meta, "traffic_type", None)
    return Usage(
        getattr(meta, "prompt_token_count", None),
        getattr(meta, "candidates_token_count", None),
        getattr(meta, "thoughts_token_count", None),
        getattr(meta, "total_token_count", None),
        str(getattr(traffic, "value", traffic)) if traffic is not None else None,
    )


class GeminiClient:
    """Sends one structured-output request per call to generate()."""

    def __init__(self, api_key: str, model: str, thinking_level: str, timeout_seconds: float):
        # Imported here so the rest of the package works without the SDK installed.
        from google import genai
        from google.genai import errors, types

        self._types, self._errors = types, errors
        self.model, self.thinking_level = model, thinking_level
        self._client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(
                timeout=int(timeout_seconds * 1000),
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )

    def generate(self, instructions: str, text: str, schema: dict) -> Reply:
        types, errors = self._types, self._errors
        config = types.GenerateContentConfig(
            system_instruction=instructions,
            response_mime_type="application/json",
            response_json_schema=schema,
            temperature=0,
            thinking_config=types.ThinkingConfig(thinking_level=self.thinking_level.upper()),
        )
        try:
            response = self._client.models.generate_content(
                model=self.model, contents=text, config=config
            )
        except errors.APIError as exc:
            raise classify_api_error(exc.code, exc.details, exc.message or str(exc)) from None
        except Exception as exc:  # httpx timeouts and connection errors surface here
            name = type(exc).__name__
            if "Timeout" in name or "Connect" in name or "Network" in name or "Read" in name:
                raise ModelError("network_error", f"{name}: {str(exc)[:300]}") from None
            raise
        usage = _usage(response.usage_metadata)
        candidate = (response.candidates or [None])[0]
        finish = getattr(getattr(candidate, "finish_reason", None), "value", None)
        text_out = response.text if candidate is not None else None
        if not text_out:
            feedback = getattr(response, "prompt_feedback", None)
            raise ModelError(
                "blocked",
                f"no text in the reply (finish reason {finish}, feedback {feedback})",
                usage=usage,
            )
        return Reply(text_out, usage, response.model_version, finish and str(finish))
