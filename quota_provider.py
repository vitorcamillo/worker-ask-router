"""Consulta de reset sem inferencia e sem gerar chamadas de modelo."""

import datetime
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time

from quota_reset import MAX_BYTES, timestamp


def _blocked_until(windows, now):
    if not isinstance(windows, list):
        return None
    resets = []
    for window in windows[:128]:
        if not isinstance(window, dict):
            return None
        pct = window.get("pct")
        if isinstance(pct, bool) or not isinstance(pct, (int, float)):
            continue
        if pct >= 100:
            reset = timestamp(window.get("reset"), now)
            if reset is None:
                return None
            resets.append(reset)
    return max(resets) if resets else None


def _read_cache(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        raw = os.read(fd, MAX_BYTES + 1)
        return json.loads(raw) if len(raw) <= MAX_BYTES else None
    finally:
        os.close(fd)


def _start_plan_until(now):
    """Retorna até quando o Start Plan está indisponível, ou None se livre."""
    if os.environ.get("ZCODE_WORKER_START_PLAN", "").strip().lower() in {
        "0",
        "off",
        "false",
        "no",
    }:
        return float("inf")
    state = os.environ.get(
        "ZCODE_WORKER_CONTEXT_STATE", str(Path.home() / ".local/state/zcode-worker")
    )
    try:
        text = (Path(state) / "start-plan-unavailable").read_text()
        tokens = text.split()
        until = datetime.datetime.fromisoformat(tokens[0])
        # Ocupado (rate_limit/refused) é transitório e não bloqueia o Start,
        # como em start_plan_blocked do adapter.
        if len(tokens) > 1 and tokens[1] in {"rate_limit", "refused"}:
            return None
        if until.tzinfo is not None:
            return None
        ts = until.timestamp()
    except (OSError, ValueError, IndexError):
        return None
    return ts if ts > now else None


def start_plan_free(now=None):
    """True quando o Start Plan do zcode-worker pode ser tentado agora."""
    return _start_plan_until(time.time() if now is None else now) is None


def _query_plan_reset(wrapper, now, budget):
    """Retorna reset comprovado ou None (disponibilidade desconhecida)."""
    if budget <= 0:
        return None
    budget = min(10.0, budget)
    try:
        if wrapper == "claude-ask":
            if os.environ.get("CLAUDE_CONFIG_DIR"):
                return None
            data = _read_cache(
                Path.home() / ".local/state/ask-bridges/claude-limits.json"
            )
            if not isinstance(data, dict):
                return None
            at = data.get("at")
            if (
                isinstance(at, bool)
                or not isinstance(at, (int, float))
                or not 0 <= now - at / 1000 <= 1800
            ):
                return None
            limits = data.get("rate_limits")
            if not isinstance(limits, dict):
                return None
            return _blocked_until(
                [
                    {"pct": w.get("used_percentage"), "reset": w.get("resets_at")}
                    for w in limits.values()
                    if isinstance(w, dict)
                ],
                now,
            )
        if wrapper not in ("zcode-worker", "codex-ask", "codex-terra"):
            return None
        source = Path.home() / ".local/bin/ask-limites-json"
        # Aceita symlink de codigo instalado, resolve apenas o caminho do helper.
        if not source.is_file():
            return None
        method = "limites_glm" if wrapper == "zcode-worker" else "limites_gpt"
        code = (
            "import runpy,json,sys,os; m=runpy.run_path(sys.argv[1]); "
            'f=m[sys.argv[2]]; f.__globals__["TIMEOUT_S"]=8; '
            'f.__globals__["CODEX_HOME"]=os.environ.get("CODEX_HOME", os.path.expanduser("~/.codex")); '
            'print(json.dumps(f().get("janelas", [])))'
        )
        with tempfile.TemporaryFile() as output:
            proc = subprocess.Popen(
                [sys.executable, "-c", code, str(source.resolve()), method],
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                deadline = time.monotonic() + budget
                while proc.poll() is None:
                    if (
                        time.monotonic() >= deadline
                        or os.fstat(output.fileno()).st_size > MAX_BYTES
                    ):
                        return None
                    time.sleep(0.02)
                if proc.returncode or os.fstat(output.fileno()).st_size > MAX_BYTES:
                    return None
                output.seek(0)
                return _blocked_until(json.loads(output.read(MAX_BYTES)), now)
            finally:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
    except (OSError, ValueError, TypeError, RecursionError):
        return None


def query_reset(wrapper, now=None, budget=10.0):
    """Reset efetivo do zcode-worker: Start Plan livre libera sem consultar o pago."""
    now = time.time() if now is None else now
    if wrapper != "zcode-worker":
        return _query_plan_reset(wrapper, now, budget)
    start = _start_plan_until(now)
    if start is None:
        return None
    paid = _query_plan_reset(wrapper, now, budget)
    if paid is None:
        return None
    return min(paid, start)
