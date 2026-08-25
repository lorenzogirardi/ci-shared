"""Unit tests for scripts/openrouter_ai.py using a local mock OpenRouter server.

No real network calls are made: the script is pointed at a ThreadingHTTPServer that
emits canned OpenAI-compatible responses and records requests for assertions.
"""

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "openrouter_ai.py"
API_KEY = "sk-or-v1-test-key-0123456789abcdef"


class MockHandler(BaseHTTPRequestHandler):
    """Minimal OpenAI-compatible chat completions endpoint."""

    status = 200
    response_body = b'{"choices":[{"message":{"content":"mock-reply"}}],"usage":{"prompt_tokens":7,"completion_tokens":2,"total_tokens":9}}'
    captured: dict | None = None

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        type(self).requests.append(
            {
                "path": self.path,
                "headers": dict(self.headers),
                "body": json.loads(raw) if raw else None,
            }
        )
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self.response_body)

    def log_message(self, *args):  # silence server logs
        pass


@pytest.fixture
def mock_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    MockHandler.requests = []
    MockHandler.status = 200
    MockHandler.response_body = b'{"choices":[{"message":{"content":"mock-reply"}}],"usage":{"prompt_tokens":7,"completion_tokens":2,"total_tokens":9}}'
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    thread.join(timeout=5)


def run_script(*args, stdin: str = "", env: dict | None = None):
    base_env = {
        "OPENROUTER_API_KEY": API_KEY,
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }
    if env:
        # Allow tests to override the API key entirely (e.g. missing).
        base_env.update({k: v for k, v in env.items() if v is not None})
        for k, v in env.items():
            if v is None and k in base_env:
                base_env.pop(k, None)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        input=env.get("STDIN") if env and "STDIN" in env else stdin,
        capture_output=True,
        text=True,
        env=base_env,
        cwd=str(SCRIPT.parents[1]),
    )


def test_success_with_prompt_file(mock_server, tmp_path):
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Hello model")
    result = run_script("--prompt-file", str(prompt), "--endpoint", mock_server)
    assert result.returncode == 0
    assert result.stdout.strip() == "mock-reply"
    req = MockHandler.requests[0]
    assert req["body"]["model"] == "deepseek-v4-flash-free"
    assert req["headers"]["Authorization"] == f"Bearer {API_KEY}"
    assert req["headers"]["Content-Type"] == "application/json"
    assert "Python-urllib" not in req["headers"].get("User-Agent", "")
    assert req["headers"].get("User-Agent", "").startswith("openrouter-")


def test_success_with_stdin_and_system(mock_server, tmp_path):
    result = run_script("--endpoint", mock_server, env={"STDIN": "hello from stdin"})
    assert result.returncode == 0
    assert MockHandler.requests[0]["body"]["messages"][0]["role"] == "user"
    system_file = tmp_path / "system.md"
    system_file.write_text("You are a helpful reviewer.")
    result = run_script("--system-file", str(system_file), "--endpoint", mock_server)
    assert result.returncode == 0
    sys_msg = MockHandler.requests[-1]["body"]["messages"][0]
    assert sys_msg == {"role": "system", "content": "You are a helpful reviewer."}


def test_missing_api_key_returns_nonzero():
    result = run_script(env={"OPENROUTER_API_KEY": None, "Stdin": ""})
    assert result.returncode == 2
    assert "OPENROUTER_API_KEY" in result.stderr


def test_http_error_exposes_status_not_key(mock_server):
    MockHandler.status = 401
    MockHandler.response_body = json.dumps({"error": {"message": "Unauthorized token"}}).encode()
    result = run_script("--endpoint", mock_server, env={"STDIN": "hi"})
    assert result.returncode == 1
    assert "HTTP 401" in result.stderr or "401" in result.stderr
    assert API_KEY not in result.stderr
    assert API_KEY not in result.stdout


def test_non_json_response(mock_server):
    MockHandler.response_body = b"<html>oops</html>"
    result = run_script("--endpoint", mock_server, env={"STDIN": "hi"})
    assert result.returncode == 1
    assert "non-JSON" in result.stderr


def test_missing_content_field(mock_server):
    MockHandler.response_body = b'{"choices":[{"message":{}}]}'
    result = run_script("--endpoint", mock_server, env={"STDIN": "hi"})
    assert result.returncode == 1
    assert "content" in result.stderr


def test_blank_content_is_treated_as_failure(mock_server):
    # Reasoning models can return empty "content" after spending the token
    # budget on chain-of-thought; blank output must be a hard error, not a
    # silently empty report.
    MockHandler.response_body = b'{"choices":[{"message":{"content":"   "}}],"usage":{}}'
    result = run_script("--endpoint", mock_server, env={"STDIN": "hi"})
    assert result.returncode == 1
    assert "empty content" in result.stderr
    assert result.stdout.strip() == ""


def test_reasoning_content_fallback(mock_server):
    # When the model returns blank `content` but exposes `reasoning_content`,
    # the script must surface the reasoning instead of failing or being silent.
    MockHandler.response_body = (
        b'{"choices":[{"message":{"content":"","reasoning_content":"deep thinking..."}}],'
        b'"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}'
    )
    result = run_script("--endpoint", mock_server, env={"STDIN": "hi"})
    assert result.returncode == 0
    assert "deep thinking..." in result.stdout


def test_openrouter_error_object(mock_server):
    MockHandler.response_body = json.dumps({"error": "rate limited"}).encode()
    result = run_script("--endpoint", mock_server, env={"STDIN": "hi"})
    assert result.returncode == 1
    assert "rate limited" in result.stderr


def test_prompt_size_limit(mock_server):
    MockHandler.response_body = b'{"choices":[{"message":{"content":"ok"}}],"usage":{}}'
    big = "x" * 500
    result = run_script("--max-chars", "100", "--endpoint", mock_server, env={"STDIN": big})
    assert result.returncode == 0
    sent = MockHandler.requests[-1]["body"]["messages"][-1]["content"]
    assert len(sent) <= 100 + 500  # truncation marker keeps it bounded
    assert "truncated" in sent


def test_usage_file_written(mock_server, tmp_path):
    usage_file = tmp_path / "usage.json"
    result = run_script("--endpoint", mock_server, "--usage-file", str(usage_file), env={"STDIN": "hi"})
    assert result.returncode == 0
    data = json.loads(usage_file.read_text())
    assert data["model"] == "deepseek-v4-flash-free"
    assert data["prompt_tokens"] == 7
    assert data["completion_tokens"] == 2
    assert data["total_tokens"] == 9
    assert data["cost_usd"] > 0
    assert data["price_input_usd_per_1m"] == 0.14


def test_usage_file_free_model_cost(mock_server, tmp_path):
    usage_file = tmp_path / "usage.json"
    result = run_script(
        "--endpoint", mock_server,
        "--usage-file", str(usage_file),
        "--model", "deepseek-v4-flash-free",
        env={"STDIN": "hi"},
    )
    assert result.returncode == 0
    data = json.loads(usage_file.read_text())
    # free tier billed at the commercial rate, so cost > 0
    assert data["total_tokens"] == 9
    assert data["cost_usd"] > 0


def test_append_cost_footer(tmp_path):
    usage = tmp_path / "usage.json"
    report = tmp_path / "report.md"
    usage.write_text(
        json.dumps(
            {"model": "deepseek-v4-flash-free", "prompt_tokens": 100, "completion_tokens": 50,
             "total_tokens": 150, "cost_usd": 0.000021, "price_input_usd_per_1m": 0.14,
             "price_output_usd_per_1m": 0.28}
        )
    )
    report.write_text("body text")
    proc = subprocess.run(
        [sys.executable, "scripts/ai_append_cost.py", "--usage-file", str(usage), "--report-file", str(report)],
        capture_output=True, text=True, cwd=str(SCRIPT.parents[1]),
    )
    assert proc.returncode == 0
    out = report.read_text()
    assert "AI Usage & Cost" in out
    assert "154" not in out  # totals are computed
    assert "150" in out  # token total
    assert "0.14/0.28" in out