#!/usr/bin/env python3
"""Call the OpenCode Zen chat-completions API from CI with a minimal, stdlib-only client.

OpenCode Zen (https://opencode.ai/zen) exposes an OpenAI-compatible chat-completions
endpoint. The endpoint and the model are both configurable via the environment, so the
same script can target any OpenAI-compatible gateway (including OpenRouter) by setting
OPENROUTER_ENDPOINT / OPENROUTER_MODEL.

Reads configuration from the environment:

  OPENROUTER_API_KEY   (required) — API key (e.g. OpenCode Zen key, or OpenRouter for OpenRouter). Never echoed anywhere.
  OPENROUTER_MODEL     (optional) — model slug, default: deepseek-v4-flash-free (OpenCode Zen "DeepSeek V4 Flash Free")
  OPENROUTER_ENDPOINT  (optional) — API endpoint, default: https://opencode.ai/zen/v1/chat/completions
  OPENROUTER_SITE_URL  (optional) — sent as the HTTP-Referer header (provider ranking credit)
  OPENROUTER_APP_NAME  (optional) — sent as the X-Title header (provider ranking credit)

Accepts the user prompt from a file (--prompt-file), the system prompt from a file
(--system-file) and prints ONLY the model's reply to stdout. Everything else goes to
stderr. Exits non-zero on any failure with a human-readable message.

Security:
  - The API key is never written to stdout/stderr or echoed.
  - The value of OPENROUTER_API_KEY and other common secret patterns are redacted from
    both stdout and stderr as a defense-in-depth measure.
  - Prompt size is capped (--max-chars); oversized inputs are truncated with a marker.

Example:
  OPENROUTER_API_KEY=sk-or-v1-... python scripts/openrouter_ai.py \
      --system-file prompt-system.md \
      --prompt-file prompt-user.md
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

DEFAULT_ENDPOINT = "https://opencode.ai/zen/v1/chat/completions"
DEFAULT_MODEL = "deepseek-v4-flash-free"
DEFAULT_MAX_CHARS = 120_000
DEFAULT_TIMEOUT = 120
DEFAULT_MAX_TOKENS = 8192
# Output-token ceiling (deepseek-v4-flash-free advertises 128k max output). Retries
# double the budget but must never exceed the model's real cap, or the provider
# rejects the request.
MODEL_MAX_OUTPUT_TOKENS = 128_000

# Per-1M-token prices (USD) for the models this repo uses. The "Free" tier is charged
# like its commercial counterpart so every report always shows a real economic value,
# even when the provider bills $0. Set OPENROUTER_PRICE_INPUT/OUTPUT to override.
MODEL_PRICES_USD_PER_1M = {
    "deepseek-v4-flash": (0.14, 0.28),
    "deepseek-v4-flash-free": (0.14, 0.28),
    "deepseek-v4-pro": (1.74, 3.48),
    "claude-sonnet-4.5": (3.00, 15.00),
    "gpt-4o": (2.50, 10.00),
}

# Common secret patterns redacted defensively from any output (defense-in-depth).
_SECRET_RE = [
    re.compile(r"sk-or-v1-[A-Za-z0-9\-_]{10,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9\-_.]{20,}", re.IGNORECASE),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"(?i)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
]


class OpenRouterError(Exception):
    """Raised for any API failure."""


def _read_env_secret(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    marker = f"\n\n[ai-script: input truncated from {len(text)} to {max_chars} chars]"
    return text[: max_chars - len(marker)] + marker


def _redact(text: str, api_key: str | None) -> str:
    if api_key:
        text = text.replace(api_key, "***REDACTED***")
    for pattern in _SECRET_RE:
        text = pattern.sub("***REDACTED***", text)
    return text


def _http_error_message(exc: urllib.error.HTTPError, api_key: str | None) -> str:
    detail = ""
    try:
        body = json.loads(exc.read().decode("utf-8", errors="replace"))
        if isinstance(body, dict):
            err = body.get("error", {})
            if isinstance(err, dict):
                detail = err.get("message", "") or err.get("type", "")
            else:
                detail = str(err)
    except Exception:
        pass
    detail = _redact(detail, api_key)
    msg = f"OpenRouter HTTP {exc.code}: {detail or exc.reason}".strip()
    return _redact(msg, api_key)


def _model_price(model: str) -> tuple[float, float]:
    """Input/output price in USD per 1M tokens for the given model."""
    price_in = _read_env_secret("OPENROUTER_PRICE_INPUT")
    price_out = _read_env_secret("OPENROUTER_PRICE_OUTPUT")
    if price_in and price_out:
        return (float(price_in), float(price_out))
    return MODEL_PRICES_USD_PER_1M.get(model, (0.0, 0.0))


def _estimate_cost(model: str, usage: dict) -> tuple[float, dict]:
    """Return (cost_usd, detail) for a response 'usage' dict."""
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    total = prompt_tokens + completion_tokens
    if total == 0:
        return 0.0, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    price_in, price_out = _model_price(model)
    cost = (prompt_tokens / 1_000_000) * price_in + (completion_tokens / 1_000_000) * price_out
    detail = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total,
        "price_input_usd_per_1m": price_in,
        "price_output_usd_per_1m": price_out,
        "cost_usd": round(cost, 8),
    }
    return cost, detail


def _extract_content(message: dict) -> str:
    """Extract visible text from an OpenAI-compatible message.

    `content` may be a plain string, a list of typed parts, or missing. Returns
    the concatenated text, or '' when there is nothing to render.
    """
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


def _extract_reasoning(message: dict) -> str:
    """Extract chain-of-thought from reasoning models, if the provider exposed it.

    DeepSeek-style reasoning models that exhaust the output budget often return an
    empty `content` while the "thinking" lives in `reasoning_content`. Rendering it
    guarantees the analysis is never silently empty.
    """
    reasoning = message.get("reasoning_content")
    if reasoning is None:
        reasoning = message.get("reasoning")
    if isinstance(reasoning, str):
        return reasoning
    if isinstance(reasoning, list):
        parts: list[str] = []
        for part in reasoning:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                text = part.get("text") or part.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


def _request(
    *,
    api_key: str,
    model: str,
    endpoint: str,
    system: str,
    user: str,
    max_tokens: int,
    temperature: float | None,
    timeout: int,
) -> tuple[str, str, dict]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "openrouter-ai-script/1.0 (GitHub Actions)",
    }
    site_url = _read_env_secret("OPENROUTER_SITE_URL")
    if site_url:
        headers["HTTP-Referer"] = site_url
    app_name = _read_env_secret("OPENROUTER_APP_NAME")
    if app_name:
        headers["X-Title"] = app_name

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})

    payload: dict = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if temperature is not None:
        payload["temperature"] = temperature

    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise OpenRouterError(_http_error_message(exc, api_key)) from exc
    except urllib.error.URLError as exc:
        reason = str(exc.reason)
        if isinstance(exc.reason, (TimeoutError, OSError)) and "timed out" in reason:
            raise OpenRouterError(f"OpenRouter request timed out after {timeout}s") from exc
        raise OpenRouterError(f"OpenRouter connection error: {_redact(reason, api_key)}") from exc
    except TimeoutError as exc:
        raise OpenRouterError(f"OpenRouter request timed out after {timeout}s") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise OpenRouterError("OpenRouter returned non-JSON response") from exc

    if isinstance(data, dict) and data.get("error"):
        raise OpenRouterError(_redact(str(data["error"]), api_key))

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise OpenRouterError("OpenRouter response missing choices[0].message.content") from exc

    message = data["choices"][0]["message"]
    content = _extract_content(message)
    reasoning = _extract_reasoning(message)

    if not content.strip() and not reasoning.strip():
        raise OpenRouterError(
            "OpenRouter returned empty content (model reasoning consumed the output budget)"
        )

    usage = data.get("usage") or {}
    return content, reasoning, usage


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="openrouter_ai.py",
        description="Call OpenRouter chat completions and print only the model reply.",
    )
    parser.add_argument("--system-file", help="File containing the system prompt (optional)")
    parser.add_argument("--prompt-file", help="File containing the user prompt (default: stdin)")
    parser.add_argument("--model", help="Model slug (env OPENROUTER_MODEL overrides)")
    parser.add_argument("--endpoint", help="API endpoint (env OPENROUTER_ENDPOINT overrides)")
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS, help="Max prompt chars")
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS, help="Max output tokens")
    parser.add_argument("--temperature", type=float, default=None, help="Sampling temperature")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="Request timeout (s)")
    parser.add_argument("--usage-file", help="Write token usage + estimated cost (JSON) to this file")
    parser.add_argument("--retries", type=int, default=3, help="Retry count when the model returns an empty reply")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])

    api_key = _read_env_secret("OPENROUTER_API_KEY")
    if not api_key:
        print("error: OPENROUTER_API_KEY is not set", file=sys.stderr)
        return 2

    model = args.model or _read_env_secret("OPENROUTER_MODEL") or DEFAULT_MODEL
    endpoint = args.endpoint or _read_env_secret("OPENROUTER_ENDPOINT") or DEFAULT_ENDPOINT

    system = ""
    if args.system_file:
        try:
            with open(args.system_file, "r", encoding="utf-8") as fh:
                system = fh.read()
        except OSError as exc:
            print(f"error: cannot read system file: {exc}", file=sys.stderr)
            return 3

    try:
        if args.prompt_file:
            with open(args.prompt_file, "r", encoding="utf-8") as fh:
                user = fh.read()
        else:
            user = sys.stdin.read()
    except OSError as exc:
        print(f"error: cannot read prompt: {exc}", file=sys.stderr)
        return 3

    user = _truncate(user, args.max_chars)
    system = _truncate(system, args.max_chars)

    timeout = args.timeout
    try:
        content, reasoning, usage = _request(
            api_key=api_key,
            model=model,
            endpoint=endpoint,
            system=system,
            user=user,
            max_tokens=args.max_tokens,
            temperature=args.temperature,
            timeout=timeout,
        )
    except OpenRouterError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # Hard guarantee: if the first attempt came back with a blank `content`
    # (reasoning models can blow the whole token budget on chain-of-thought),
    # retry with a larger budget and an explicit "answer directly" nudge before
    # falling back to the raw reasoning text. Only if BOTH stay empty we fail.
    if not content.strip():
        for attempt in range(1, args.retries + 1):
            budget = min(args.max_tokens * (2 ** attempt), MODEL_MAX_OUTPUT_TOKENS)
            print(
                f"warning: empty reply, retrying (attempt {attempt}/{args.retries}) with "
                f"max_tokens={budget}",
                file=sys.stderr,
            )
            nudge = (
                system
                + f"\n\nIMPORTANT: You responded with an empty answer. Reply now with the "
                f"complete report text directly in `content` — do not spend tokens on reasoning."
            )
            try:
                content, reasoning, usage = _request(
                    api_key=api_key,
                    model=model,
                    endpoint=endpoint,
                    system=nudge,
                    user=user,
                    max_tokens=budget,
                    temperature=args.temperature,
                    timeout=timeout,
                )
            except OpenRouterError as exc2:
                print(f"error: {exc2}", file=sys.stderr)
                return 1
            if content.strip():
                break

    if not content.strip() and reasoning.strip():
        print(
            "warning: model produced only chain-of-thought, using reasoning as the report",
            file=sys.stderr,
        )
        content = reasoning

    if not content.strip():
        print("error: OpenRouter returned empty content (model reasoning consumed the output budget)", file=sys.stderr)
        return 1

    if args.usage_file:
        try:
            _, usage_detail = _estimate_cost(model, usage)
            usage_detail["model"] = model
            with open(args.usage_file, "w", encoding="utf-8") as fh:
                json.dump(usage_detail, fh, indent=2)
        except OSError as exc:
            print(f"error: cannot write usage file: {exc}", file=sys.stderr)
            return 3

    print(_redact(content, api_key))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
