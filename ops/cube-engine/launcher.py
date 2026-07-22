"""Cube sandbox in-guest launcher for the Vibe-Trading engine.

Listens on :8898 (template probe target) and manages the engine process on
:8899. The router boots/reboots the engine with per-tenant env via POST /boot,
so a single template serves every tenant and every LLM configuration.

Endpoints:
  GET  /health -> 200 {"launcher": "ok", "engine": "running"|"stopped"}
  POST /boot   -> {"env": {...}} kill current engine (if any), spawn
                  `vibe-trading serve --host 0.0.0.0 --port 8899` with
                  os.environ + env, wait until /health on 8899 answers.
  POST /stop   -> kill the engine process.
"""

import json
import os
import signal
import subprocess
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ENGINE_PORT = 8899
LAUNCHER_PORT = 8898
BOOT_TIMEOUT_SEC = int(os.environ.get("VIBE_LAUNCHER_BOOT_TIMEOUT", "120"))

_engine = {"proc": None}


def _engine_alive():
    proc = _engine["proc"]
    return proc is not None and proc.poll() is None


def _engine_healthy():
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{ENGINE_PORT}/health", timeout=3
        ) as resp:
            return resp.status == 200
    except Exception:
        return False


def _stop_engine():
    proc = _engine["proc"]
    if proc is None:
        return
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
    _engine["proc"] = None


def _boot_engine(extra_env):
    _stop_engine()
    env = dict(os.environ)
    env.update({str(k): str(v) for k, v in extra_env.items()})
    _engine["proc"] = subprocess.Popen(
        ["vibe-trading", "serve", "--host", "0.0.0.0", "--port", str(ENGINE_PORT)],
        env=env,
        cwd=os.environ.get("VIBE_APP_DIR", "/app"),
        start_new_session=True,
    )
    deadline = time.time() + BOOT_TIMEOUT_SEC
    while time.time() < deadline:
        if not _engine_alive():
            return False, "engine process exited during boot"
        if _engine_healthy():
            return True, "ok"
        time.sleep(1)
    return False, f"engine not healthy after {BOOT_TIMEOUT_SEC}s"


class Handler(BaseHTTPRequestHandler):
    def _reply(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            state = "running" if (_engine_alive() and _engine_healthy()) else (
                "starting" if _engine_alive() else "stopped"
            )
            self._reply(200, {"launcher": "ok", "engine": state})
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/boot":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                extra_env = payload.get("env", {})
                if not isinstance(extra_env, dict):
                    raise ValueError("env must be an object")
            except (ValueError, json.JSONDecodeError) as exc:
                self._reply(400, {"error": str(exc)})
                return
            ok, msg = _boot_engine(extra_env)
            self._reply(200 if ok else 500, {"ok": ok, "detail": msg})
        elif self.path == "/stop":
            _stop_engine()
            self._reply(200, {"ok": True})
        else:
            self._reply(404, {"error": "not found"})

    def log_message(self, fmt, *args):  # keep guest console quiet
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", LAUNCHER_PORT), Handler).serve_forever()
