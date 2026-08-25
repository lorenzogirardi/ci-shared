#!/usr/bin/env python3
"""Sanitize and cap the size of CI test/lint output before sending it to an LLM.

Used in .github/workflows/pipeline.yml (ai-analysis) to build a safe
.ai/security-context.txt and to guard uploaded artifacts: the AI must never
receive or re-publish secrets.

Two modes:

  bundle  (default)
      python3 scripts/ai_sanitize.py --out FILE --max-bytes N FILE...
      Rewrites each input file's content redacting secret patterns, prepends a
      small header block per file, and truncates the result to at most N bytes.
      Exits 0 if at least one byte was written, 1 otherwise.

  check:
      python3 scripts/ai_sanitize.py --check FILE
      Exits 0 if FILE contains no detectable secret patterns, 1 if it does.
      Used before uploading an artifact to avoid publishing secrets.

  redact-file:
      python3 scripts/ai_sanitize.py --redact-file FILE
      Rewrites FILE in place replacing every secret pattern with
      ***REDACTED***. Exits 0 if the file was already clean, 1 if at least one
      pattern was found and masked. Used on the AI report so it can still be
      shown and uploaded without rigging the pipeline to drop it.

Note: a security report intentionally quotes source code that contains things
like `password = "x"` or `DATABASE_URL = "..."`; those are citations, not
leaked credentials. Use --check / --redact-file on the *final* report only to
block/obfuscate real credentials (API keys, bearer tokens, JWTs), not to
delete the whole artifact on an alarm.

Stdlib only; safe to run in any Ubuntu runner.
"""

from __future__ import annotations

import argparse
import re
import sys

# Credentials and tokens that are almost always real secrets no matter the context.
_HARD_SECRET_RE = [
    re.compile(r"sk-or-v1-[A-Za-z0-9\-_]{10,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9\-_.]{20,}", re.IGNORECASE),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),  # JWT
]

# Config-style assignments. Legit for documentation/reporting (hence matched on
# lowercase keywords like `password =`, `database_url =`) but still masked.
_SOFT_SECRET_RE = [
    re.compile(r"(?i)(DATABASE_URL|REDIS_URL|WEBHOOK_URL|MONGO_URL|MYSQL_URL|AMQP_URL|PASSWORD)\s*=\s*['\"][^'\"]+['\"]"),
]

_SECRET_RE = _HARD_SECRET_RE + _SOFT_SECRET_RE


def redact(text: str) -> str:
    for pattern in _SECRET_RE:
        text = pattern.sub("***REDACTED***", text)
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description="Build or check a sanitized AI context file.")
    parser.add_argument("--check", action="store_true", help="Check a file for secrets instead of building")
    parser.add_argument("--redact-file", help="In-place redact this file (report) instead of building/checking")
    parser.add_argument("--out", help="Output file for --build mode")
    parser.add_argument("--max-bytes", type=int, default=150_000, help="Max output size in bytes")
    parser.add_argument("files", nargs="*", help="Input files (or one file in --check mode)")
    args = parser.parse_args()

    if args.redact_file and args.check:
        parser.error("--redact-file and --check are mutually exclusive")

    if args.redact_file:
        path = args.redact_file
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            original = fh.read()
        redacted = redact(original)
        if redacted != original:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(redacted)
            print(f"warning: {path} contained secret patterns; now masked", file=sys.stderr)
            return 1
        return 0

    if args.check:
        if not args.files:
            parser.error("--check requires one input file")
        path = args.files[0]
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read()
        return 1 if redact(content) != content else 0

    parts: list[str] = []
    total = 0
    for path in args.files:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except OSError:
            continue
        safe = redact(content)
        block = f"### {path} ({len(content)} chars)\n{safe}\n"
        parts.append(block)
        total += len(block)
        if total >= args.max_bytes:
            break

    if not parts:
        print("error: no input files produced content", file=sys.stderr)
        return 1

    out = "\n".join(parts)[: args.max_bytes]
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(out)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())