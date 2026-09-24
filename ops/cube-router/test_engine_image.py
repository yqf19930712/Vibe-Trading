"""Static checks on the engine image (ops/cube-engine/Dockerfile + launcher.py).

Run: python -m pytest test_engine_image.py

The image cannot be built here, so the Dockerfile is parsed instead:

  · Engine code under /app is never handed to the runtime user: no chown of
    /app or anything below it (RUN chown, COPY/ADD --chown), and no chmod
    that grants group/other write there. The engine's shell tools run as
    that user; writable engine code would run with the tenant-shared LLM
    credentials on the next /boot.
  · Bytecode is compiled at build time, before the image switches user; the
    runtime env disables bytecode writes and the user site-packages dir, and
    pins VIBE_DATA_DIR so no fallback lands tenant state in /app.
  · The container runs as the unprivileged user.
  · The launcher writes nothing under /app (its only file write is the
    egress key under ~/.ssh).
"""
from __future__ import annotations

import ast
import posixpath
import re
import shlex
from pathlib import Path

import pytest

_ENGINE_DIR = Path(__file__).resolve().parent.parent / "cube-engine"
DOCKERFILE = _ENGINE_DIR / "Dockerfile"
LAUNCHER = _ENGINE_DIR / "launcher.py"
CODE_ROOT = "/app"
RUNTIME_USER = "vibe"


# ── Dockerfile parsing ───────────────────────────────────────────────────────


def instructions(text: str) -> list[tuple[str, str]]:
    """(INSTRUCTION, arguments) pairs, continuation lines joined, comments dropped."""
    out: list[tuple[str, str]] = []
    buf = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        buf += line
        head, _, rest = buf.strip().partition(" ")
        out.append((head.upper(), rest.strip()))
        buf = ""
    return out


def _commands(run_args: str) -> list[list[str]]:
    """Split a RUN shell line into simple commands (on &&, ||, ;)."""
    cmds: list[list[str]] = []
    for part in re.split(r"&&|\|\||;", run_args):
        try:
            words = shlex.split(part)
        except ValueError:
            words = part.split()
        if words:
            cmds.append(words)
    return cmds


def _under_code_root(path: str, workdir: str) -> bool:
    p = posixpath.normpath(posixpath.join(workdir, path))
    return p == CODE_ROOT or p.startswith(CODE_ROOT + "/")


def _grants_group_or_other_write(mode: str) -> bool:
    if re.fullmatch(r"[0-7]{3,4}", mode):
        digits = mode[-3:]
        return bool(int(digits[1]) & 2 or int(digits[2]) & 2)
    for clause in mode.split(","):
        m = re.fullmatch(r"([ugoa]*)([+=])([rwxXst]*)", clause)
        if m and "w" in m.group(3) and (not m.group(1) or set(m.group(1)) & {"g", "o", "a"}):
            return True
    return False


def violations(text: str) -> list[str]:
    """Every way the Dockerfile hands /app to the runtime user."""
    found: list[str] = []
    workdir = "/"
    for inst, args in instructions(text):
        if inst == "WORKDIR":
            workdir = posixpath.normpath(posixpath.join(workdir, args))
        elif inst in ("COPY", "ADD"):
            words = shlex.split(args)
            if any(w.startswith("--chown") for w in words):
                dest = [w for w in words if not w.startswith("--")][-1]
                if _under_code_root(dest, workdir):
                    found.append(f"{inst} --chown into {dest}")
        elif inst == "RUN":
            for words in _commands(args):
                name = posixpath.basename(words[0])
                operands = [w for w in words[1:] if not w.startswith("-")]
                if name == "chown" and len(operands) >= 2:
                    for target in operands[1:]:
                        if _under_code_root(target, workdir):
                            found.append(f"chown {operands[0]} {target}")
                elif name == "chmod" and len(operands) >= 2:
                    mode = operands[0]
                    for target in operands[1:]:
                        if _under_code_root(target, workdir) and _grants_group_or_other_write(mode):
                            found.append(f"chmod {mode} {target}")
    return found


def _env(text: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for inst, args in instructions(text):
        if inst == "ENV":
            for word in shlex.split(args):
                if "=" in word:
                    k, v = word.split("=", 1)
                    env[k] = v
    return env


# ── the real Dockerfile ──────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def test_code_root_is_never_handed_to_the_runtime_user(dockerfile):
    assert violations(dockerfile) == []


def test_runs_as_the_unprivileged_user(dockerfile):
    users = [args for inst, args in instructions(dockerfile) if inst == "USER"]
    assert users and users[-1] == RUNTIME_USER


def test_bytecode_is_compiled_before_the_user_switch(dockerfile):
    seq = instructions(dockerfile)
    user_at = max(i for i, (inst, _) in enumerate(seq) if inst == "USER")
    compile_at = [
        i for i, (inst, args) in enumerate(seq)
        if inst == "RUN" and "compileall" in args and CODE_ROOT in args
    ]
    assert compile_at and compile_at[0] < user_at


def test_runtime_env_closes_the_import_side_doors(dockerfile):
    env = _env(dockerfile)
    assert env.get("PYTHONDONTWRITEBYTECODE") == "1"
    assert env.get("PYTHONNOUSERSITE") == "1"
    assert env.get("VIBE_DATA_DIR") == "/home/vibe/.vibe-trading"
    assert env.get("HOME") == "/home/vibe"


# ── the checker itself catches the patterns it exists for ────────────────────


@pytest.mark.parametrize("snippet", [
    "WORKDIR /app\nRUN useradd vibe && chown -R vibe:vibe /app /home/vibe/.vibe-trading",
    "WORKDIR /app\nRUN mkdir -p agent/runs \\\n    && chown -R vibe agent/",
    "WORKDIR /app\nRUN chown vibe:vibe /app/agent/src",
    "WORKDIR /app\nCOPY --chown=vibe:vibe agent/ agent/",
    "WORKDIR /app\nRUN chmod -R 777 /app",
    "WORKDIR /app\nRUN chmod -R o+w agent",
    "WORKDIR /srv\nRUN chmod a+w /app/agent",
])
def test_checker_flags_writable_code(snippet):
    assert violations(snippet)


@pytest.mark.parametrize("snippet", [
    "WORKDIR /app\nRUN chown vibe:vibe /home/vibe/.vibe-trading",
    "WORKDIR /app\nRUN chmod -R go-w /app",
    "WORKDIR /app\nRUN chmod 755 /app/agent",
    "WORKDIR /app\nCOPY --chown=vibe:vibe cfg /home/vibe/cfg",
    "WORKDIR /app\n# RUN chown -R vibe /app\nRUN true",
])
def test_checker_accepts_safe_lines(snippet):
    assert violations(snippet) == []


# ── launcher writes only under HOME ──────────────────────────────────────────


def test_launcher_writes_only_the_egress_key_under_home():
    tree = ast.parse(LAUNCHER.read_text(encoding="utf-8"))
    writes: list[str] = []
    makedirs: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = ast.unparse(node.func)
        if fn == "open" and len(node.args) >= 2:
            mode = node.args[1]
            if isinstance(mode, ast.Constant) and any(c in str(mode.value) for c in "wax+"):
                writes.append(ast.unparse(node.args[0]))
        elif fn == "os.makedirs":
            makedirs.append(ast.unparse(node.args[0]))
    assert writes == ["key_path"]
    assert makedirs == ["ssh_dir"]
    src = LAUNCHER.read_text(encoding="utf-8")
    assert "ssh_dir = os.path.expanduser('~/.ssh')" in src or \
        'ssh_dir = os.path.expanduser("~/.ssh")' in src
    assert 'key_path = os.path.join(ssh_dir, "egress_key")' in src
