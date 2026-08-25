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

import base64
import json
import os
import signal
import socket
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ENGINE_PORT = 8899
LAUNCHER_PORT = 8898
BOOT_TIMEOUT_SEC = int(os.environ.get("VIBE_LAUNCHER_BOOT_TIMEOUT", "120"))
TUNNEL_LOCAL_PORT = int(os.environ.get("VIBE_EGRESS_LOCAL_PORT", "8118"))

_engine = {"proc": None}
# In-guest encrypted egress tunnel: sandbox -> ssh -> server B's loopback
# tinyproxy (domain-whitelisted). A plaintext HTTP proxy across the border
# gets keyword-reset on the CONNECT line for blocked domains; SSH does not.
# The key is delivered via /boot env and is restricted server-side to
# port-forwarding tinyproxy only (authorized_keys restrict,permitopen).
_tunnel = {"proc": None, "cmd": None, "last_spawn": 0.0}


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


def _stop_tunnel():
    proc = _tunnel["proc"]
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    _tunnel["proc"] = None


def _configure_tunnel(extra_env):
    """Consume VIBE_EGRESS_* from the boot env and (re)start the ssh tunnel.

    Pops the key material so it is not passed into the engine process env.
    Missing/empty config simply disables the tunnel.
    """
    key_b64 = str(extra_env.pop("VIBE_EGRESS_SSH_KEY_B64", "") or "")
    dest = str(extra_env.pop("VIBE_EGRESS_SSH_DEST", "") or "")
    remote = str(extra_env.pop("VIBE_EGRESS_REMOTE", "") or "127.0.0.1:8888")
    _stop_tunnel()
    _tunnel["cmd"] = None
    if not key_b64 or not dest:
        return
    ssh_dir = os.path.expanduser("~/.ssh")
    os.makedirs(ssh_dir, mode=0o700, exist_ok=True)
    key_path = os.path.join(ssh_dir, "egress_key")
    with open(key_path, "wb") as f:
        f.write(base64.b64decode(key_b64))
    os.chmod(key_path, 0o600)
    _tunnel["cmd"] = [
        "ssh", "-i", key_path, "-N",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=3",
        "-L", "127.0.0.1:%d:%s" % (TUNNEL_LOCAL_PORT, remote),
        dest,
    ]
    _ensure_tunnel()


def _ensure_tunnel():
    """Respawn the tunnel if configured but dead (>=10s between attempts)."""
    cmd = _tunnel["cmd"]
    if not cmd:
        return
    proc = _tunnel["proc"]
    if proc is not None and proc.poll() is None:
        return
    now = time.time()
    if now - _tunnel["last_spawn"] < 10:
        return
    _tunnel["last_spawn"] = now
    _tunnel["proc"] = subprocess.Popen(cmd, start_new_session=True)


def _tunnel_port_open(timeout=1.0):
    try:
        with socket.create_connection(("127.0.0.1", TUNNEL_LOCAL_PORT), timeout):
            return True
    except OSError:
        return False


def _wait_tunnel_ready(deadline_s):
    """Bounded wait until the local forward accepts connections.

    Cold boot spawns ssh and immediately serves the first ask; if that ssh dies
    (guest network not up yet) every proxied tool call in the run fails with
    ECONNREFUSED (2026-08-25, attempt 88e080ef0a46). Best-effort: on timeout we
    proceed anyway — the keeper thread keeps retrying in the background.
    """
    if not _tunnel["cmd"]:
        return True
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        if _tunnel_port_open():
            return True
        _ensure_tunnel()
        time.sleep(1)
    return _tunnel_port_open()


def _tunnel_keeper():
    """Respawn a dead tunnel DURING runs, not only on /health probes.

    The router only probes /health before an ask, so a tunnel that died
    mid-run used to stay dead for the whole run.
    """
    while True:
        time.sleep(10)
        try:
            _ensure_tunnel()
        except Exception:  # noqa: BLE001 - keeper must never die
            pass


def _boot_engine(extra_env):
    _stop_engine()
    _configure_tunnel(extra_env)
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
            # Engine is up; give the egress tunnel a bounded head start so the
            # first ask doesn't run its whole data-collection phase proxyless.
            _wait_tunnel_ready(15)
            return True, "ok"
        _ensure_tunnel()
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
            _ensure_tunnel()  # router probes before every ask -> self-heals egress
            state = "running" if (_engine_alive() and _engine_healthy()) else (
                "starting" if _engine_alive() else "stopped"
            )
            tunnel_proc = _tunnel["proc"]
            ssh_alive = tunnel_proc is not None and tunnel_proc.poll() is None
            # "up" requires the forward to actually accept connections — an
            # alive ssh with a dead forward is what starved run 88e080ef0a46.
            tunnel = (
                "off" if not _tunnel["cmd"]
                else "up" if ssh_alive and _tunnel_port_open()
                else "down"
            )
            self._reply(200, {"launcher": "ok", "engine": state, "egress_tunnel": tunnel})
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
    threading.Thread(target=_tunnel_keeper, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", LAUNCHER_PORT), Handler).serve_forever()
