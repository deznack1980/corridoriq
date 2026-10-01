"""Model provider boundary. Business decisions do not live here.

v0.1 runs the deterministic decision engine with narration off. A provider
can be configured later for a wording pass. The pass is not allowed to
change the locked recommendation.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from agents.framework.limits import CallLimiter, clip, max_model_calls


class ModelDisabled(RuntimeError):
    """No model call was made because narration is off or no key is configured."""


@dataclass
class ModelResponse:
    text: str
    provider: str
    model: str
    prompt_chars: int
    completion_tokens: int | None = None
    prompt_tokens: int | None = None


def narration_enabled() -> bool:
    return os.environ.get("CEO_ENABLE_MODEL_NARRATION", "0").strip() not in {
        "",
        "0",
        "false",
        "False",
        "no",
    }


def provider_name() -> str:
    return os.environ.get("CEO_MODEL_PROVIDER", "none").strip().lower() or "none"


def model_name() -> str:
    return os.environ.get("CEO_MODEL_NAME", "").strip()


def _api_key() -> str:
    return os.environ.get("CEO_MODEL_API_KEY", "").strip()


class ModelProvider:
    """Small provider wrapper. Network IO happens only inside ``complete``."""

    def __init__(self, limiter: CallLimiter | None = None):
        self.limiter = limiter or CallLimiter(max_model_calls())
        self.usage: list[dict] = []

    def complete(self, messages: list[dict], *, purpose: str) -> ModelResponse:
        if not narration_enabled() or provider_name() in {"", "none", "off"}:
            raise ModelDisabled("Model narration is disabled.")
        key = _api_key()
        if not key:
            raise ModelDisabled("CEO_MODEL_API_KEY is not set.")
        name = provider_name()
        if name not in {"openai", "anthropic"}:
            raise ModelDisabled(f"Unsupported CEO_MODEL_PROVIDER: {name}")
        self.limiter.acquire()
        clipped = []
        prompt_chars = 0
        for message in messages:
            content = clip(str(message.get("content", "")))
            prompt_chars += len(content)
            clipped.append({"role": message.get("role", "user"), "content": content})
        # The HTTP call is intentionally isolated. v0.1 tests never reach it
        # unless narration and a key are both set.
        response = _post(name, model_name() or _default_model(name), key, clipped)
        self.usage.append(
            {
                "purpose": purpose,
                "provider": name,
                "model": response.model,
                "prompt_chars": prompt_chars,
                "prompt_tokens": response.prompt_tokens,
                "completion_tokens": response.completion_tokens,
            }
        )
        response.prompt_chars = prompt_chars
        return response


def _default_model(name: str) -> str:
    if name == "anthropic":
        return "claude-3-5-sonnet-latest"
    return "gpt-4o-mini"


def _post(name: str, model: str, api_key: str, messages: list[dict]) -> ModelResponse:
    import urllib.request

    if name == "openai":
        url = "https://api.openai.com/v1/chat/completions"
        body = {"model": model, "messages": messages, "temperature": 0}
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
    else:
        url = "https://api.anthropic.com/v1/messages"
        system = "\n".join(
            m["content"] for m in messages if m["role"] == "system"
        )
        turns = [m for m in messages if m["role"] != "system"]
        body = {
            "model": model,
            "max_tokens": 800,
            "system": system,
            "messages": turns or [{"role": "user", "content": ""}],
        }
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if name == "openai":
        text = payload["choices"][0]["message"]["content"]
        usage = payload.get("usage") or {}
        return ModelResponse(
            text=text,
            provider=name,
            model=model,
            prompt_chars=0,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
        )
    parts = payload.get("content") or []
    text = "\n".join(part.get("text", "") for part in parts if isinstance(part, dict))
    usage = payload.get("usage") or {}
    return ModelResponse(
        text=text,
        provider=name,
        model=model,
        prompt_chars=0,
        prompt_tokens=usage.get("input_tokens"),
        completion_tokens=usage.get("output_tokens"),
    )
