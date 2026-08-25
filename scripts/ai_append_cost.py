#!/usr/bin/env python3
"""Append an "AI usage & cost" footer to a report file, from a --usage-file JSON.

Usage: scripts/ai_append_cost.py --usage-file .ai/usage.json --report-file .ai/ai-report.md
"""

from __future__ import annotations

import argparse
import json
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Append AI usage & cost section to a report.")
    parser.add_argument("--usage-file", required=True, help="JSON produced by openrouter_ai.py --usage-file")
    parser.add_argument("--report-file", required=True, help="Markdown report to append the footer to")
    args = parser.parse_args(argv or sys.argv[1:])

    try:
        with open(args.usage_file, "r", encoding="utf-8") as fh:
            usage = json.load(fh)
    except OSError as exc:
        print(f"error: cannot read usage file: {exc}", file=sys.stderr)
        return 3
    except json.JSONDecodeError as exc:
        print(f"error: usage file is not valid JSON: {exc}", file=sys.stderr)
        return 3

    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    total = prompt + completion
    cost = float(usage.get("cost_usd") or 0.0)
    model = usage.get("model") or "unknown"
    price_in = usage.get("price_input_usd_per_1m")
    price_out = usage.get("price_output_usd_per_1m")

    if total > 0 and cost > 0:
        cost_line = (
            f"**Estimated cost**: ${cost:.6f} (model **{model}**, "
            f"{price_in}/{price_out} USD per 1M in/out tokens)"
        )
        cost_badge = f"${cost:.2f} (model {model})"
    else:
        cost_line = f"**Estimated cost**: ${cost:.6f} (free tier / price not reported; model **{model}**)"
        cost_badge = "$0.00 (free tier)"

    section = (
        "\n\n---\n\n"
        "## 🤖 AI Usage & Cost\n\n"
        f"- **Tokens**: {total:,} total (input: {prompt:,}, output: {completion:,})\n"
        f"- **Cost**: {cost_badge}\n\n"
        f"_{cost_line}_\n"
    )

    try:
        with open(args.report_file, "a", encoding="utf-8") as fh:
            fh.write(section)
    except OSError as exc:
        print(f"error: cannot write report file: {exc}", file=sys.stderr)
        return 3

    return 0


if __name__ == "__main__":
    raise SystemExit(main())