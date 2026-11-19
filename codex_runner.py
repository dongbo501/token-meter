"""Run the official CLI through a one-request, budget-enforcing local relay.

The CLI supplies its real client headers. The relay never invents a client identity.
No provider key, application data or host workspace is exposed to the CLI.
"""

import hmac
import json
import os
import secrets
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import HTTPRedirectHandler, Request, build_opener

from metering import endpoint, usage

MAX_RESPONSE = 4 * 1024 * 1024


class LocalReject(ValueError):
    """A local check failed before sending a billable request."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def stream_usage(raw, content_type):
    if "text/event-stream" not in content_type:
        data = json.loads(raw)
        return usage(data.get("usage") if isinstance(data, dict) else None)
    # SSE allows CRLF and multiple data lines in a single event.
    for block in raw.decode().replace("\r\n", "\n").split("\n\n"):
        lines = [
            line[5:].lstrip() for line in block.splitlines() if line.startswith("data:")
        ]
        if not lines or lines == ["[DONE]"]:
            continue
        event = json.loads("\n".join(lines))
        if event.get("type") in {
            "response.completed",
            "response.incomplete",
            "response.failed",
        }:
            return usage(event.get("response", {}).get("usage"))
    raise ValueError("流式响应未报告最终用量")


def read_response(response, content_type):
    """Stop at the terminal SSE event; some gateways keep the HTTP stream open."""
    if "text/event-stream" not in content_type:
        raw = response.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise ValueError("响应过大")
        return raw
    deadline = time.monotonic() + 120
    chunks, pending, size = [], b"", 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("流式请求超过时限")
        # HTTPResponse does not expose a public socket-timeout setter.
        response.fp.raw._sock.settimeout(remaining)
        chunk = response.read1(65536)
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > MAX_RESPONSE:
            raise ValueError("响应过大")
        chunks.append(chunk)
        pending += chunk
        normalized = pending.replace(b"\r\n", b"\n")
        blocks = normalized.split(b"\n\n")
        for block in blocks[:-1]:
            lines = [
                line[5:].lstrip()
                for line in block.splitlines()
                if line.startswith(b"data:")
            ]
            if lines and lines != [b"[DONE]"]:
                event = json.loads(b"\n".join(lines))
                if event.get("type") in {
                    "response.completed",
                    "response.incomplete",
                    "response.failed",
                }:
                    return b"".join(chunks)
        # Keep raw trailing bytes so a CRLF split across reads stays intact.
        if len(blocks) > 1:
            pending = blocks[-1]


def run_codex(book, job):
    ident, cfg, prompt, _ = job
    binary = shutil.which("codex")
    if not binary:
        raise LocalReject("未安装官方 Codex CLI，请使用发布的容器镜像")
    token = secrets.token_urlsafe(32)
    result = {}
    gate = threading.Lock()

    class Relay(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, status, body=b"{}", content_type="application/json"):
            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self):
            if self.path != "/v1/responses" or not hmac.compare_digest(
                self.headers.get("Authorization", ""), "Bearer " + token
            ):
                return self.reply(403)
            with gate:
                if result.get("called"):
                    return self.reply(409)  # No retries or second model turns.
                result["called"] = True
            try:
                self.connection.settimeout(10)
                size = int(self.headers.get("Content-Length", 0))
                if not 0 < size <= 1024 * 1024 or self.headers.get("Content-Encoding"):
                    raise LocalReject("Codex 请求体格式不支持，未发送到供应商")
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict):
                    raise LocalReject("Codex 请求格式无效，未发送到供应商")
                # Self-contained programming exercises require no tool execution.
                body.update(
                    model=cfg["model"], tools=[], tool_choice="none", store=False
                )
                body.pop("previous_response_id", None)
                try:
                    body["max_output_tokens"] = book.resize(
                        ident, cfg, json.dumps(body)
                    )
                except ValueError as exc:
                    raise LocalReject(str(exc)) from None
                headers = {
                    k: v
                    for k, v in self.headers.items()
                    if k.lower()
                    not in {
                        "host",
                        "authorization",
                        "content-length",
                        "connection",
                        "transfer-encoding",
                        "accept-encoding",
                        "content-encoding",
                    }
                }
                headers.update(
                    {
                        "Authorization": "Bearer " + cfg["api_key"],
                        "Content-Type": "application/json",
                    }
                )
                req = Request(
                    endpoint(cfg["base_url"], "responses"),
                    data=json.dumps(body).encode(),
                    headers=headers,
                    method="POST",
                )
                result["sent"] = True
                with build_opener(NoRedirect).open(req, timeout=120) as response:
                    content_type = response.headers.get(
                        "Content-Type", "application/json"
                    )
                    raw = read_response(response, content_type)
                    result["counts"] = stream_usage(raw, content_type)
                self.reply(200, raw, content_type)
            except (
                OSError,
                HTTPException,
                ValueError,
                TypeError,
                RecursionError,
            ) as exc:
                result["error"] = exc
                self.reply(502)

    with tempfile.TemporaryDirectory(prefix="token-meter-codex-") as root:
        cli_home, work = Path(root) / "home", Path(root) / "work"
        cli_home.mkdir(mode=0o700)
        work.mkdir(mode=0o700)
        instructions = Path(root) / "instructions.md"
        instructions.write_text(
            secrets.token_hex(24)
            + "\nYou are a programming assistant. Solve the standalone "
            "coding exercise in the user message. Provide code, tests and reasoning in your answer. "
            "For a connection test, reply only OK. Do not use tools, read local files, browse "
            f"the web or execute programs. Limit your answer to {cfg['max_output']} output tokens."
        )
        http = ThreadingHTTPServer(("127.0.0.1", 0), Relay)
        thread = threading.Thread(
            target=http.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()
        config = {
            "model_provider": "meter",
            "model_providers.meter.name": "Token Meter",
            "model_providers.meter.base_url": f"http://127.0.0.1:{http.server_port}/v1",
            "model_providers.meter.env_key": "METER_RELAY_KEY",
            "model_providers.meter.wire_api": "responses",
            "model_providers.meter.request_max_retries": 0,
            "model_providers.meter.stream_max_retries": 0,
            "model_providers.meter.stream_idle_timeout_ms": 130000,
            "model_instructions_file": str(instructions),
            "model_reasoning_effort": "low",
            "features.shell_tool": False,
            "features.multi_agent": False,
            "web_search": "disabled",
            "history.persistence": "none",
        }
        args = [
            binary,
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--json",
            "-C",
            str(work),
            "-m",
            cfg["model"],
        ]
        for key, value in config.items():
            args.extend(["-c", f"{key}={json.dumps(value)}"])
        args.append("-")
        env = {
            key: os.environ[key] for key in ("PATH", "SYSTEMROOT") if key in os.environ
        }
        env.update(
            HOME=str(cli_home),
            CODEX_HOME=str(cli_home),
            METER_RELAY_KEY=token,
            TMPDIR=root,
            XDG_CACHE_HOME=str(cli_home / "cache"),
        )
        proc = None
        try:
            proc = subprocess.Popen(
                args,
                env=env,
                cwd=work,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                proc.communicate(prompt.encode(), timeout=140)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate()
        finally:
            if proc and proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            http.shutdown()
            thread.join()
            http.server_close()
    if "counts" in result:
        return result["counts"]
    if "error" in result:
        raise result["error"]
    if not result.get("sent"):
        raise LocalReject("Codex CLI 未发出请求，请检查镜像版本和模型设置")
    raise ValueError("Codex 请求未返回可结算的用量")
