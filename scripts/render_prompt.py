#!/usr/bin/env python3
"""Render the shared review prompt template into a ready-to-send system prompt.

The template (prompts/pr-review-system.md) holds everything that must not drift
between the event-driven reviewer and the scheduled sweep -- most importantly
the machine-parsed "VERDICT: CLEAN | NEEDS_REVIEW" contract, which an
auto-merge decision reads. It carries one `{{PROJECT_CONTEXT}}` placeholder.

Project context is assembled from two optional, additive sources:
  --extra      caller-supplied text (from a workflow input), for
               "this class of PR" guidance
  --rules-file a file in the CONSUMER repo (.github/ai-review-rules.md), for
               durable project-specific guidance that lives next to the code
               it describes. Missing file is not an error.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys

PLACEHOLDER = "{{PROJECT_CONTEXT}}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Render the review system prompt.")
    parser.add_argument("--template", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument(
        "--extra",
        default=None,
        help="Extra context text. Defaults to the EXTRA environment variable, "
             "which keeps untrusted/multiline values out of the command line.",
    )
    parser.add_argument("--rules-file", default=None, help="Optional; ignored if absent")
    args = parser.parse_args()

    template = pathlib.Path(args.template).read_text()
    if PLACEHOLDER not in template:
        print(f"error: {args.template} has no {PLACEHOLDER} placeholder", file=sys.stderr)
        return 1

    parts: list[str] = []
    extra = args.extra if args.extra is not None else os.environ.get("EXTRA", "")
    if extra.strip():
        parts.append(extra.strip())
    if args.rules_file:
        rules = pathlib.Path(args.rules_file)
        if rules.is_file():
            content = rules.read_text().strip()
            if content:
                parts.append(content)

    rendered = template.replace(PLACEHOLDER, "\n\n".join(parts))
    pathlib.Path(args.out).write_text(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
