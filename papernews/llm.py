from __future__ import annotations

import json
import os
import threading
import time

_BACKEND = os.environ.get("LLM_BACKEND", "anthropic").lower()


def chat(system: str, user: str, max_tokens: int) -> str:
    """Single-shot chat. Always streams under the hood — large rewrite batches
    can exceed the API's non-streaming deadline, and a slow Ollama instance
    benefits from bytes-flowing keepalive through any reverse proxy."""
    if _BACKEND == "ollama":
        return _ollama(system, user, max_tokens)
    if _BACKEND == "groq":
        return _groq(system, user, max_tokens)
    return _anthropic(system, user, max_tokens)


def _anthropic(system: str, user: str, max_tokens: int) -> str:
    import anthropic

    client = anthropic.Anthropic()
    with client.messages.stream(
        model=os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5"),
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    ) as stream:
        final = stream.get_final_message()
    return final.content[0].text


# Groq free tier: 8000 tokens/minute as of writing, same across every
# current free chat model. That's a *rolling* budget, not just a per-request
# cap — several back-to-back calls that are each individually fine can still
# add up past it within the same 60s window. Track our own usage and pause
# before a request would push us over, rather than only reacting after the
# API rejects us. GROQ_TPM_BUDGET defaults below the real limit to leave
# margin for the token-count estimate being approximate.
_GROQ_TPM_BUDGET = int(os.environ.get("GROQ_TPM_BUDGET", "6500"))
_groq_lock = threading.Lock()
_groq_usage: list[tuple[float, int]] = []  # [(monotonic_time, tokens), ...]


def _groq_throttle(estimated_tokens: int) -> None:
    while True:
        with _groq_lock:
            now = time.monotonic()
            cutoff = now - 60
            _groq_usage[:] = [(t, n) for t, n in _groq_usage if t > cutoff]
            used = sum(n for _, n in _groq_usage)
            # If the window's already empty, go ahead regardless of estimate
            # size — otherwise a single request estimated above the whole
            # budget would wait forever. (It may still 413/429; the retry
            # loop in _groq handles that.)
            if not _groq_usage or used + estimated_tokens <= _GROQ_TPM_BUDGET:
                _groq_usage.append((now, estimated_tokens))
                return
            wait = _groq_usage[0][0] + 60 - now + 0.5
        time.sleep(max(wait, 0.5))


def _groq(system: str, user: str, max_tokens: int) -> str:
    # Free tier, OpenAI-compatible chat API. Model IDs churn as Groq
    # deprecates/replaces hosted models — override GROQ_MODEL if the
    # default below has since been retired (see
    # https://console.groq.com/docs/deprecations).
    import groq

    # Rough token estimate (~4 chars/token in English) to pace requests
    # against the TPM budget before sending. Uses max_tokens as the output
    # estimate since actual completion length isn't known upfront — an
    # overestimate here just means we pace a bit more conservatively.
    estimated_tokens = (len(system) + len(user)) // 4 + max_tokens
    _groq_throttle(estimated_tokens)

    client = groq.Groq()
    for attempt in range(5):
        try:
            stream = client.chat.completions.create(
                model=os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"),
                max_tokens=max_tokens,
                stream=True,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
            parts: list[str] = []
            for chunk in stream:
                delta = chunk.choices[0].delta.content
                if delta:
                    parts.append(delta)
            return "".join(parts)
        except groq.RateLimitError as e:
            if attempt == 4:
                raise
            retry_after = e.response.headers.get("retry-after")
            wait = float(retry_after) if retry_after else 2 ** attempt * 10
            time.sleep(wait)
    raise AssertionError("unreachable")


def _ollama(system: str, user: str, max_tokens: int) -> str:
    import httpx

    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    model = os.environ.get("OLLAMA_MODEL", "mistral")
    timeout = float(os.environ.get("OLLAMA_TIMEOUT", "1800"))
    parts: list[str] = []
    with httpx.stream(
        "POST",
        f"{host}/api/chat",
        json={
            "model": model,
            "stream": True,
            "options": {"num_predict": max_tokens},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        },
        timeout=timeout,
    ) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line:
                continue
            chunk = json.loads(line)
            if msg := chunk.get("message"):
                parts.append(msg.get("content", ""))
            if chunk.get("done"):
                break
    return "".join(parts)
