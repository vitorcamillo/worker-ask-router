#!/usr/bin/env python3
"""Roteador CLI unico de workers (stdlib)."""

import argparse
import contextlib
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
import re
import signal
import shutil
import stat as statmod
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from quota_reset import MAX_RESET_SECONDS, MARKER, marker_reset

SCHEMA_VERSION = 1
TTL_SECONDS = 60.0
MAX_INPUT_BYTES = 64 * 1024
MAX_OUTPUT_BYTES = 1 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
MAX_ALLOWED_FILES = 128
CACHE_KEYS = ("zcode-worker", "codex-terra", "muse-ask", "opencode-ask", "codex-ask", "claude-ask")
ORCHESTRATORS = ("codex", "claude", "zcode", "muse", "opencode")
TIMEOUT_DIRECTIVE = "worker-ask: execucao interrompida por timeout; nao indica cota nem informa data de liberacao. Revise o estado da tarefa antes de uma nova tentativa."
DEFAULT_MIN_TIMEOUT = 300.0
TIMEOUT_COOLDOWN_SECONDS = 300.0
# Cota excedida sem reset conhecido: cooldown finito em vez de bloqueio permanente.
UNKNOWN_RESET_COOLDOWN_SECONDS = 3600.0
# A vaga e conferida antes de lancar; a fila do zcode vira so margem de corrida.
DEFAULT_ZCODE_QUEUE_TIMEOUT = 5.0

_CURRENT_PROC = None
_INTERRUPTED = False
# Log JSONL de execucoes (runs.jsonl): uma linha por execucao que chega a cadeia.
# Nunca recebe prompt, spec, stdout ou stderr dos workers, so metricas.
_RUN = None
_ISO = None  # isolamento da invocação: {"wi": módulo, "deps": Deps, "session": Session}; None = direto
_RUN_T0 = None
_RUN_WRITTEN = False
RUNS_LOG_MAX_BYTES = 5 * 1024 * 1024
# Orcamento por task_id: 1 execucao + 3 relancamentos (refeicoes).
MAX_RUNS_PER_TASK = 4
TASK_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_RUN_LOG_FILES = ("runs.jsonl", "runs.jsonl.1")
_RUN_RESULT_STATES = ("completed", "partial", "blocked", "blocked_question")
_LOG_FD = None
_LOG_FAILED = False
_LOG_REPORTED = False
_LOG_ENDS_WITH_NEWLINE = True
_EVENT_FD = None
_EVENT_FD_READY = False
_USAGE_KEYS = ("session", "model", "effort", "plan", "input", "output", "cacheRead", "cacheWrite")


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("escrita no log sem progresso")
        view = view[written:]


def _log_write(data):
    global _LOG_FAILED, _LOG_ENDS_WITH_NEWLINE
    if _LOG_FD is None:
        return True
    try:
        _write_all(_LOG_FD, data)
        if data:
            _LOG_ENDS_WITH_NEWLINE = data.endswith(b"\n")
        return True
    except OSError:
        _LOG_FAILED = True
        return False


def _log_exit(rc):
    if _LOG_FD is not None and not _LOG_ENDS_WITH_NEWLINE:
        _log_write(b"\n")
    _log_write(f"exit={rc}\n".encode())


def _note_log_failure():
    global _LOG_REPORTED
    if _LOG_REPORTED:
        return
    _LOG_REPORTED = True
    try:
        sys.stderr.write("erro: falha ao gravar --log; registro incompleto\n")
        sys.stderr.flush()
    except OSError:
        pass


def _err(msg):
    text = str(msg).rstrip("\n") + "\n"
    sys.stderr.write(text)
    sys.stderr.flush()
    _log_write(text.encode("utf-8", "replace"))


def _open_log(path):
    """Abre --log como `>`: arquivo regular, sem symlink final, modo 0600."""
    global _LOG_ENDS_WITH_NEWLINE
    if not path or any(ord(ch) < 32 for ch in path) or _is_sensitive(os.path.basename(path)):
        raise RuntimeError("caminho de --log recusado")
    try:
        fd = _open_nofollow(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        raise RuntimeError("nao foi possivel abrir --log")
    if not _is_regular_fd(fd):
        os.close(fd)
        raise RuntimeError("--log deve ser arquivo regular")
    os.set_blocking(fd, True)
    # Arquivo preexistente não herda o modo da criação; fecha em 0600 antes de gravar.
    os.fchmod(fd, 0o600)
    os.ftruncate(fd, 0)
    _LOG_ENDS_WITH_NEWLINE = True
    return fd


def _event_fd():
    """Canal de eventos da apresentação: `fd:inode` de um pipe herdado."""
    global _EVENT_FD, _EVENT_FD_READY
    if _EVENT_FD_READY:
        return _EVENT_FD
    _EVENT_FD_READY = True
    fd_s, _, ino_s = os.environ.get("ASK_PRESENTATION_FD", "").partition(":")
    if fd_s.isdigit() and ino_s.isdigit():
        try:
            st = os.fstat(int(fd_s))
            if statmod.S_ISFIFO(st.st_mode) and st.st_ino == int(ino_s):
                os.set_blocking(int(fd_s), False)
                _EVENT_FD = int(fd_s)
        except OSError:
            pass
    return _EVENT_FD


def _event(kind, legacy=None, **data):
    """Evento estruturado para a apresentação; sem canal, mantém a linha antiga."""
    if _RUN is not None and kind != "executor_start":
        # Métrica do runs.jsonl: registra antes de qualquer retorno interno.
        # "executor_start" duplica o "start" de _emit_executor_start; vai só ao canal.
        try:
            entry = {"event": kind, "t": round(time.monotonic() - _RUN_T0, 3)}
            for key in ("executor", "reason", "rc", "detail"):
                if data.get(key) is not None:
                    entry[key] = data[key]
            _RUN["attempts"].append(entry)
        except Exception:
            pass
    fd = _event_fd()
    if fd is not None:
        payload = {"event": kind, **{k: v for k, v in data.items() if v is not None}}
        try:
            os.write(fd, (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            return
        except OSError:
            pass
    if legacy:
        _err(legacy)


def _parse_usage(stdout_data):
    """Lê o rodapé `--- nome: k=v ... ---` do fim da resposta, sem alterá-la."""
    tail = stdout_data[-4096:].decode("utf-8", "replace")
    for line in reversed(tail.splitlines()):
        line = line.strip()
        if line.startswith("--- ") and line.endswith(" ---") and ": " in line:
            out = {}
            for token in line[4:-4].split(": ", 1)[1].split():
                key, sep, value = token.partition("=")
                if sep and key in _USAGE_KEYS and value and value != "n/d" and len(value) <= 64 and value.isprintable():
                    out[key] = value
            return out
    return {}


def _parse_run_usage(stdout_data):
    """Tokens para o runs.jsonl a partir do mesmo rodapé lido por `_parse_usage`."""
    out = _parse_usage(stdout_data)
    tail = stdout_data[-4096:].decode("utf-8", "replace")
    for line in reversed(tail.splitlines()):
        stripped = line.strip()
        if stripped.startswith("--- ") and stripped.endswith(" ---") and ": " in stripped:
            for token in stripped[4:-4].split(": ", 1)[1].split():
                key, sep, value = token.partition("=")
                if not sep or not value:
                    continue
                if key == "tokens":
                    for part in value.split("/"):
                        sub, colon, num = part.partition(":")
                        if not colon:
                            continue
                        try:
                            number = int(num) if num.isdigit() else None
                        except ValueError:
                            continue
                        if number is None:
                            continue
                        if sub == "in" and "input" not in out:
                            out["input"] = number
                        elif sub == "out" and "output" not in out:
                            out["output"] = number
                elif key == "custo" and "cost_usd" not in out:
                    raw = value[1:] if value.startswith("$") else value
                    try:
                        cost = float(raw)
                    except ValueError:
                        continue
                    if math.isfinite(cost) and cost >= 0:
                        out["cost_usd"] = cost
                elif key == "modelo" and "model" not in out:
                    if value != "n/d" and len(value) <= 64 and value.isprintable():
                        out["model"] = value
            break
    for field in ("input", "output", "cacheRead", "cacheWrite"):
        if isinstance(out.get(field), str) and out[field].isdigit():
            try:
                out[field] = int(out[field])
            except ValueError:
                pass
    return out


def _exit_timeout(legacy, executor=None, detail="timeout"):
    _event("stop", legacy, executor=executor, rc=124, detail=detail)
    _err(TIMEOUT_DIRECTIVE)
    sys.exit(124)


def _min_timeout():
    raw = os.environ.get("WORKER_ASK_MIN_TIMEOUT")
    if raw is None:
        return DEFAULT_MIN_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_MIN_TIMEOUT
    if not math.isfinite(value) or value < 0:
        return DEFAULT_MIN_TIMEOUT
    return value


def _unknown_reset_cooldown():
    raw = os.environ.get("WORKER_ASK_UNKNOWN_RESET_COOLDOWN")
    if raw is None:
        return UNKNOWN_RESET_COOLDOWN_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return UNKNOWN_RESET_COOLDOWN_SECONDS
    if not math.isfinite(value) or value <= 0:
        return UNKNOWN_RESET_COOLDOWN_SECONDS
    return value


def _zcode_queue_timeout():
    raw = os.environ.get("WORKER_ASK_ZCODE_QUEUE_TIMEOUT")
    if raw is None:
        return DEFAULT_ZCODE_QUEUE_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_ZCODE_QUEUE_TIMEOUT
    if not math.isfinite(value) or value <= 0:
        return DEFAULT_ZCODE_QUEUE_TIMEOUT
    return value


_EXECUTOR_LABELS = {
    "zcode-worker": "ZCode",
    "codex-terra": "Codex Terra",
    "muse-ask": "Muse",
    "opencode-ask": "OpenCode",
    "codex-ask": "Codex",
    "claude-ask": "Claude",
}

# codex-terra nao tem binario proprio: e o codex-ask fixado em gpt-6.1-terra medium.
WRAPPER_BINARIES = {"codex-terra": "codex-ask"}


def _emit_executor_start(name, mode, model=None):
    if _RUN is not None:
        # Métrica do runs.jsonl: inicio de cada executor tentado.
        try:
            start_entry = {"event": "start", "t": round(time.monotonic() - _RUN_T0, 3),
                           "executor": name}
            if model is not None:
                start_entry["model"] = model
            _RUN["attempts"].append(start_entry)
            # Interrupcao antes da coleta nao herda metricas da tentativa anterior.
            _RUN["executor"] = name
            for key in ("usage", "output_bytes", "changed_files", "result"):
                _RUN.pop(key, None)
        except Exception:
            pass
    if _event_fd() is not None:
        _event("executor_start", None, executor=name, mode=mode, model=model)
        return
    if os.environ.get("ASK_PRESENTATION", "").lower() == "off":
        return
    label = _EXECUTOR_LABELS.get(name, name)
    if os.environ.get("ASK_PRESENTATION", "").lower() == "json":
        _err(json.dumps({
            "event": "executor_start",
            "provider": "worker-ask",
            "executor": label,
            "mode": mode,
            "message": "Em execução",
        }, ensure_ascii=False))
    else:
        _err(f"  ↳ {label} · Em execução")


def _extract_run_result(stdout_data):
    """execution_state do ultimo bloco ```json do stdout, ou None; nunca levanta."""
    try:
        tail = stdout_data[-262144:].decode("utf-8", "replace")
        idx = tail.rfind("```json")
        if idx < 0:
            return None
        body = tail[idx + 7:]
        end = body.find("```")
        if end < 0:
            return None
        data = json.loads(body[:end])
    except Exception:
        return None
    if isinstance(data, dict):
        state = data.get("execution_state")
        if state in _RUN_RESULT_STATES:
            return state
    return None


def _record_run_metrics(name, stdout_data, allowed, cwd, snap_before):
    """Metricas do runs.jsonl da ultima tentativa, no sucesso e na falha.

    Chamada tambem nos caminhos de falha (rc!=0, rc75, timeout, interrupcao
    rc143/144) para nao perder executor/tokens/custo/arquivos alterados; falha de
    snapshot vira null e metricas de tentativa anterior sao substituidas. Nunca
    muda o rc nem o fluxo.
    """
    if _RUN is None:
        return
    try:
        run_usage = _parse_run_usage(stdout_data)
        if run_usage:
            _RUN["usage"] = run_usage
        else:
            _RUN.pop("usage", None)
        _RUN["executor"] = name
        _RUN["result"] = _extract_run_result(stdout_data)
        _RUN["output_bytes"] = len(stdout_data)
        if allowed:
            try:
                snap_after = snapshot_allowed(cwd, allowed)
                before = snap_before or {}
                _RUN["changed_files"] = sum(
                    1 for rel in allowed if snap_after.get(rel) != before.get(rel))
            except Exception:
                _RUN["changed_files"] = None
    except Exception:
        pass


def _state_dir():
    base = os.environ.get("XDG_STATE_HOME")
    if base:
        return Path(base) / "worker-ask"
    return Path.home() / ".local" / "state" / "worker-ask"


def ensure_state_dir(create=True):
    sd = _state_dir()
    if sd.is_symlink():
        raise RuntimeError("recusado: diretorio de estado eh symlink")
    if sd.exists():
        if not sd.is_dir():
            raise RuntimeError("recusado: nao eh diretorio")
    else:
        if not create:
            return sd
        sd.mkdir(parents=True, mode=0o700, exist_ok=True)
        if sd.is_symlink() or not sd.is_dir():
            raise RuntimeError("recusado: diretorio de estado invalido")
    try:
        os.chmod(sd, 0o700)
    except OSError:
        raise RuntimeError("falha ao ajustar permissoes do estado")
    return sd


def _open_nofollow(path, flags, mode=0o600):
    return os.open(str(path), flags | getattr(os, "O_NOFOLLOW", 0), mode)


def _is_regular_fd(fd):
    try:
        st = os.fstat(fd)
    except OSError:
        return False
    return statmod.S_ISREG(st.st_mode)


def _read_bounded_file(path, limit):
    # returns bytes
    fd = _open_nofollow(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    try:
        st = os.fstat(fd)
        if not statmod.S_ISREG(st.st_mode):
            raise RuntimeError("arquivo nao regular")
        chunks = []
        total = 0
        while True:
            data = os.read(fd, 65536)
            if not data:
                break
            chunks.append(data)
            total += len(data)
            if total > limit + 1:
                break
        return b"".join(chunks)
    finally:
        os.close(fd)


def _read_run_file(name):
    """Linhas JSON validas (dict) de um arquivo do runs log; falha de leitura vira vazio."""
    try:
        raw = _read_bounded_file(_state_dir() / name, RUNS_LOG_MAX_BYTES + 1)
    except Exception:
        return
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if isinstance(rec, dict):
            yield rec


def _count_task_runs(task_id):
    """Runs da task_id que consomem orcamento: sem event e fora blocked_question."""
    n = 0
    for name in _RUN_LOG_FILES:
        for rec in _read_run_file(name):
            if "event" in rec or rec.get("task_id") != task_id:
                continue
            if rec.get("result") == "blocked_question":
                continue
            n += 1
    return n


def _find_last_task_run(task_id):
    """Ultima linha (sem event) com a task_id; runs.jsonl precede runs.jsonl.1."""
    for name in _RUN_LOG_FILES:
        ultima = None
        for rec in _read_run_file(name):
            if "event" not in rec and rec.get("task_id") == task_id:
                ultima = rec
        if ultima is not None:
            return ultima
    return None


def _write_redo_event(task_id, orchestrator, found):
    """Grava evento redo pelo mesmo mecanismo de _write_run (lock/rotacao/NOFOLLOW)."""
    global _RUN
    _RUN = {
        "schema": 1,
        "event": "redo",
        "run_id": uuid.uuid4().hex,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "task_id": task_id,
        "orchestrator": orchestrator,
        "cwd": found.get("cwd"),
        "allowed_files": found.get("allowed_files"),
    }
    try:
        _write_run(0)
    finally:
        _RUN = None


def _stats_summary():
    """Resumo do runs log; log ausente ou ilegivel vira contagem zero."""
    runs = 0
    redo = 0
    total_fora = 0
    by_exec = {}
    task_counts = {}
    for name in _RUN_LOG_FILES:
        for rec in _read_run_file(name):
            if "event" in rec:
                if rec.get("event") == "redo":
                    redo += 1
                continue
            runs += 1
            run_fora = False
            key = rec.get("executor")
            key = key if isinstance(key, str) and key else "null"
            slot = by_exec.setdefault(key, {"runs": 0, "rc": {}, "result": {}, "_durations": [],
                                             "out_of_scope_runs": 0, "scope_checked_runs": 0})
            slot["runs"] += 1
            rc = rec.get("rc")
            rc_key = str(rc) if isinstance(rc, int) and not isinstance(rc, bool) else "null"
            slot["rc"][rc_key] = slot["rc"].get(rc_key, 0) + 1
            result = rec.get("result")
            result_key = result if isinstance(result, str) else "null"
            slot["result"][result_key] = slot["result"].get(result_key, 0) + 1
            duration = rec.get("duration_s")
            if isinstance(duration, (int, float)) and not isinstance(duration, bool):
                slot["_durations"].append(duration)
            if rec.get("isolation") == "worktree":
                slot["isolated_runs"] = slot.get("isolated_runs", 0) + 1
            for campo, chave in (("sync_conflicts", "sync_conflict_runs"), ("state_fallbacks", "state_fallback_runs")):
                valor = rec.get(campo)
                if isinstance(valor, int) and not isinstance(valor, bool) and valor > 0:
                    slot[chave] = slot.get(chave, 0) + 1
            tentativas = rec.get("out_of_scope_attempts")
            por_tentativa = (
                [t for t in tentativas if isinstance(t, dict) and "out_of_scope" in t]
                if isinstance(tentativas, list)
                else []
            )
            if por_tentativa:
                # Autoria por tentativa: fallback não apaga nem transfere a
                # violação de um executor para o executor final do run.
                for tentativa in por_tentativa:
                    nome = tentativa.get("executor")
                    nome = nome if isinstance(nome, str) and nome else "null"
                    alvo = by_exec.setdefault(nome, {"runs": 0, "rc": {}, "result": {}, "_durations": [],
                                                     "out_of_scope_runs": 0, "scope_checked_runs": 0})
                    fora_t = tentativa.get("out_of_scope")
                    if isinstance(fora_t, int) and not isinstance(fora_t, bool) and fora_t >= 0:
                        alvo["scope_checked_runs"] += 1
                        if fora_t > 0:
                            alvo["out_of_scope_runs"] += 1
                            run_fora = True
            else:
                fora = rec.get("out_of_scope")
                if isinstance(fora, int) and not isinstance(fora, bool) and fora >= 0:
                    slot["scope_checked_runs"] += 1
                    if fora > 0:
                        slot["out_of_scope_runs"] += 1
                        run_fora = True
            if run_fora:
                total_fora += 1
            task_id = rec.get("task_id")
            if isinstance(task_id, str) and task_id and result != "blocked_question":
                task_counts[task_id] = task_counts.get(task_id, 0) + 1
    by_executor = {}
    for key, slot in by_exec.items():
        durations = slot.pop("_durations")
        slot["avg_duration_s"] = round(sum(durations) / len(durations), 3) if durations else 0.0
        by_executor[key] = slot
    resumo = {
        "runs": runs,
        "redo": redo,
        "by_executor": by_executor,
        "tasks_relaunched": sum(1 for n in task_counts.values() if n > 1),
        "tasks_over_budget": sum(1 for n in task_counts.values() if n >= MAX_RUNS_PER_TASK),
    }
    if runs > 0:
        # Resumo vazio mantém o formato legado.
        resumo["out_of_scope_runs"] = total_fora
    return resumo


def _valid_until(value, now):
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    try:
        if not math.isfinite(value):
            return False
    except (TypeError, ValueError):
        return False
    if value < 0 or value > now + MAX_RESET_SECONDS + 1:
        return False
    return True


def _cache_entry(key, value, now):
    if not isinstance(value, dict) or value.get("wrapper") != key:
        return None
    until = value.get("until")
    # Entrada legada com bloqueio permanente (until None) nao eh mais valida:
    # destrava sozinha no proximo load.
    if until is None:
        return None
    if _valid_until(until, now) and until > now:
        entry = {"wrapper": key, "until": float(until), "reset_known": value.get("reset_known") is True}
        if value.get("blocked_reason") in ("timeout", "quota"):
            entry["blocked_reason"] = value["blocked_reason"]
    else:
        return None
    generation = value.get("generation")
    if isinstance(generation, str) and len(generation) == 32 and all(c in "0123456789abcdef" for c in generation):
        entry["generation"] = generation
    return entry


def load_cache(create_dir=False):
    sd = _state_dir()
    if sd.is_symlink():
        raise RuntimeError("recusado: diretorio de estado eh symlink")
    cp = sd / "cache.json"
    if cp.is_symlink():
        raise RuntimeError("recusado: cache eh symlink")
    if not sd.exists() or not cp.exists():
        return {}
    now = time.time()
    try:
        fd = _open_nofollow(cp, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        raise RuntimeError("falha de I/O ao ler cache")
    try:
        if not _is_regular_fd(fd):
            raise RuntimeError("cache invalido")
        raw = os.read(fd, 64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            _err("aviso: cache invalido, tratado como vazio")
            return {}
    except RuntimeError:
        raise
    except OSError:
        raise RuntimeError("falha de I/O ao ler cache")
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    # reopen not needed; raw already read. But fd closed. parse.
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        _err("aviso: cache invalido, tratado como vazio")
        return {}
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        _err("aviso: cache corrompido, tratado como vazio")
        return {}
    if not isinstance(data, dict):
        _err("aviso: cache invalido, tratado como vazio")
        return {}
    out = {}
    for k in CACHE_KEYS:
        v = data.get(k)
        entry = _cache_entry(k, v, now)
        if entry:
            out[k] = entry
    return out


def save_cache(updates, clear=()):
    sd = ensure_state_dir(create=True)
    cp = sd / "cache.json"
    lp = sd / "lock"
    if sd.is_symlink() or cp.is_symlink() or lp.is_symlink():
        raise RuntimeError("recusado: symlink em arquivo de estado")
    # open lock nonblock
    try:
        fd = _open_nofollow(lp, os.O_CREAT | os.O_RDWR | getattr(os, "O_NONBLOCK", 0), 0o600)
    except OSError:
        raise RuntimeError("falha de I/O no lock")
    try:
        if not _is_regular_fd(fd):
            raise RuntimeError("lock invalido")
        # bounded flock
        deadline_lock = time.monotonic() + 0.5
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline_lock:
                    raise RuntimeError("timeout ao adquirir lock")
                time.sleep(0.05)
        try:
            # read current
            current = {}
            if cp.exists() and not cp.is_symlink():
                try:
                    raw = _read_bounded_file(cp, 64 * 1024)
                    if len(raw) <= 64 * 1024:
                        d = json.loads(raw.decode("utf-8"))
                        if isinstance(d, dict):
                            current = d
                except Exception:
                    current = {}
            now = time.time()
            merged = {}
            for k in CACHE_KEYS:
                entry = _cache_entry(k, current.get(k), now)
                if entry:
                    merged[k] = entry
            for k, v in (updates or {}).items():
                if k not in CACHE_KEYS:
                    continue
                entry = _cache_entry(k, v, now)
                if not entry:
                    continue
                previous = merged.get(k)
                if previous and previous.get("blocked_reason") != "timeout" and entry.get("blocked_reason") == "timeout":
                    previous["generation"] = uuid.uuid4().hex
                    continue
                if previous and previous.get("blocked_reason") != "timeout" and entry.get("blocked_reason") != "timeout" and previous["until"] > entry["until"]:
                    previous["generation"] = uuid.uuid4().hex
                    continue
                entry["generation"] = uuid.uuid4().hex
                merged[k] = entry
            for key, expected in dict(clear).items():
                if merged.get(key) == expected:
                    merged.pop(key, None)
            # expunge expired/extras already by construction
            tmp_fd, tmp_path = tempfile.mkstemp(dir=str(sd), prefix=".cache-")
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                    json.dump(merged, f)
                    f.flush()
                    os.fsync(f.fileno())
                os.chmod(tmp_path, 0o600)
                os.replace(tmp_path, cp)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise RuntimeError("falha ao escrever cache")
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _write_run(rc):
    """Grava uma linha de metrica em `<state_dir>/runs.jsonl`; nunca altera rc/stdout/--log."""
    global _RUN_WRITTEN
    if _RUN_WRITTEN:
        return
    _RUN_WRITTEN = True
    run = _RUN
    if run is None:
        return
    if os.environ.get("WORKER_ASK_RUNS_LOG") == "0":
        return
    try:
        run["rc"] = rc
        try:
            run["duration_s"] = round(time.monotonic() - _RUN_T0, 3)
        except Exception:
            run["duration_s"] = 0.0
        serial = {k: v for k, v in run.items() if not k.startswith("_")}
        sd = ensure_state_dir(create=True)
        rp = sd / "runs.jsonl"
        bp = sd / "runs.jsonl.1"
        if rp.is_symlink():
            raise RuntimeError("recusado: runs.jsonl eh symlink")
        fd = None
        fd = _open_nofollow(rp, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NONBLOCK", 0), 0o600)
        try:
            if not _is_regular_fd(fd):
                raise RuntimeError("runs.jsonl invalido")

            def _lock_or_none(target):
                deadline = time.monotonic() + 0.5
                while True:
                    try:
                        fcntl.flock(target, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        return True
                    except (BlockingIOError, OSError):
                        if time.monotonic() >= deadline:
                            return False
                        time.sleep(0.05)

            if not _lock_or_none(fd):
                return
            try:
                for _attempt in range(3):
                    if rp.is_symlink():
                        raise RuntimeError("recusado: runs.jsonl eh symlink")
                    try:
                        st_path = os.stat(rp, follow_symlinks=False)
                    except FileNotFoundError:
                        st_path = None
                    except OSError:
                        raise RuntimeError("falha de I/O em runs.jsonl")
                    try:
                        st_fd = os.fstat(fd)
                    except OSError:
                        raise RuntimeError("falha de I/O em runs.jsonl")
                    if st_path is None or (st_path.st_ino, st_path.st_dev) != (st_fd.st_ino, st_fd.st_dev):
                        try:
                            fcntl.flock(fd, fcntl.LOCK_UN)
                        except OSError:
                            pass
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                        fd = None
                        if _attempt >= 2:
                            return
                        fd = _open_nofollow(rp, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NONBLOCK", 0), 0o600)
                        if not _is_regular_fd(fd):
                            raise RuntimeError("runs.jsonl invalido")
                        if not _lock_or_none(fd):
                            return
                        continue
                    break
                try:
                    size = os.fstat(fd).st_size
                except OSError:
                    raise RuntimeError("falha de I/O em runs.jsonl")
                if size > RUNS_LOG_MAX_BYTES:
                    # Rotacao com o fd ainda aberto e travado; recusa symlink nos dois.
                    if rp.is_symlink() or bp.is_symlink():
                        raise RuntimeError("recusado: symlink em arquivo de execucoes")
                    os.replace(str(rp), str(bp))
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    fd = None
                    fd = _open_nofollow(rp, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NONBLOCK", 0), 0o600)
                    if not _is_regular_fd(fd):
                        raise RuntimeError("runs.jsonl invalido")
                    if not _lock_or_none(fd):
                        return
                try:
                    os.fchmod(fd, 0o600)
                except OSError:
                    pass
                line = (json.dumps(serial, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
                _write_all(fd, line)
            finally:
                if fd is not None:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
    except Exception:
        try:
            sys.stderr.write("aviso: falha ao gravar runs.jsonl\n")
            sys.stderr.flush()
        except OSError:
            pass
        return


def _is_sensitive(name):
    low = str(name).lower()
    if low in (".ssh", ".aws", "credentials", "credential"):
        return True
    if low == ".env" or low.startswith(".env."):
        return True
    if low == "auth.json":
        return True
    if low.endswith(".pem") or low.endswith(".key"):
        return True
    if "cred" in low:
        return True
    if low in ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"):
        return True
    return False


def validate_allowed_file(cwd, rel):
    if not isinstance(rel, str) or not rel:
        raise ValueError("allowed-file invalido")
    # control chars
    for ch in rel:
        if ord(ch) < 32 or ord(ch) == 127:
            raise ValueError("allowed-file com caractere de controle")
    if rel.startswith("/") or "\\" in rel:
        raise ValueError("allowed-file deve ser relativo sem separador de OS")
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ValueError("allowed-file com componente invalido")
    for part in parts:
        if _is_sensitive(part):
            raise ValueError("allowed-file sensivel recusado")
    # ancestors symlink check
    cur = Path(cwd)
    for p in parts[:-1]:
        cur = cur / p
        if cur.is_symlink():
            raise ValueError("allowed-file com symlink em ancestral")
    return rel


def _snapshot_one(cwd, rel):
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in rel):
        raise RuntimeError("snapshot: caminho invalido")
    parts = rel.split("/")
    for part in parts:
        if _is_sensitive(part):
            raise RuntimeError("snapshot: caminho sensivel recusado")
    # ancestors revalidate
    cur = Path(cwd)
    for p in parts[:-1]:
        cur = cur / p
        if cur.is_symlink():
            raise RuntimeError("snapshot: symlink em ancestral")
    full = os.path.join(cwd, rel)
    try:
        fd = os.open(full, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return (False, "ABSENT", 0)
    except OSError:
        raise RuntimeError("snapshot: falha de I/O")
    try:
        st = os.fstat(fd)
        if statmod.S_ISLNK(st.st_mode):
            raise RuntimeError("snapshot: symlink recusado")
        if not statmod.S_ISREG(st.st_mode):
            raise RuntimeError("snapshot: arquivo especial recusado")
        if st.st_size > MAX_SNAPSHOT_BYTES:
            raise RuntimeError("snapshot: arquivo muito grande")
        h = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_SNAPSHOT_BYTES:
                raise RuntimeError("snapshot: arquivo muito grande")
            h.update(chunk)
        mode = statmod.S_IMODE(st.st_mode)
        return (True, h.hexdigest(), mode)
    except RuntimeError:
        raise
    except OSError:
        raise RuntimeError("snapshot: falha de I/O")
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def snapshot_allowed(cwd, allowed):
    out = {}
    for rel in allowed:
        out[rel] = _snapshot_one(cwd, rel)
    return out


# Limites da impressão da árvore suja (leitura barata, nunca trava o roteador).
_ARVORE_MAX_CAMINHOS = 5000
_ARVORE_MAX_BYTES = 8 * 1024 * 1024


def _parse_status_z(raw):
    """Caminhos (bytes, relativos ao toplevel) de `git status --porcelain=v1 -z`.

    Cada registro é `XY<espaço>caminho\\0`; rename/cópia traz a origem no próximo token.
    """
    rels = []
    tokens = raw.split(b"\x00")
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        i += 1
        if not tok or len(tok) < 4 or tok[2:3] != b" ":
            continue
        rels.append(tok[3:])
        if tok[0:1] in (b"R", b"C") or tok[1:2] in (b"R", b"C"):
            if i < len(tokens) and tokens[i]:
                rels.append(tokens[i])
            i += 1
    return rels


def _tree_fingerprint(cwd):
    """Impressão dos caminhos sujos do git; None quando não dá para avaliar.

    Leitura pura: nunca levanta, nunca segue symlink, nunca muda rc/stdout.
    Chave do dict: caminho ABSOLUTO normalizado; valor: sha256 do conteúdo,
    "big:<size>:<mtime_ns>", "symlink:<alvo>", "missing", "other" ou "err".
    """
    try:
        env = dict(os.environ)
        env["GIT_OPTIONAL_LOCKS"] = "0"
        proc_topo = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
                                   capture_output=True, timeout=15, env=env)
        if proc_topo.returncode != 0:
            return None
        raiz = proc_topo.stdout.decode("utf-8", "replace").strip()
        if not raiz:
            return None
        # Status roda no toplevel para os caminhos virem relativos a ele.
        proc_estado = subprocess.run(["git", "-C", raiz, "status", "--porcelain=v1",
                                      "-z", "--untracked-files=all"],
                                     capture_output=True, timeout=15, env=env)
        if proc_estado.returncode != 0:
            return None
        rels = _parse_status_z(proc_estado.stdout)
        if len(rels) > _ARVORE_MAX_CAMINHOS:
            return None
        saida = {}
        for rel in rels:
            cheio = os.path.normpath(os.path.join(raiz, os.fsdecode(rel)))
            try:
                info = os.lstat(cheio)
            except (FileNotFoundError, NotADirectoryError):
                saida[cheio] = "missing"
                continue
            except OSError:
                saida[cheio] = "err"
                continue
            try:
                if statmod.S_ISLNK(info.st_mode):
                    try:
                        alvo = os.readlink(cheio)
                    except OSError:
                        saida[cheio] = "err"
                    else:
                        saida[cheio] = "symlink:" + alvo
                elif statmod.S_ISREG(info.st_mode):
                    if info.st_size > _ARVORE_MAX_BYTES:
                        saida[cheio] = "big:%d:%d" % (info.st_size, info.st_mtime_ns)
                    else:
                        try:
                            fd = os.open(cheio, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                        except OSError:
                            saida[cheio] = "err"
                        else:
                            try:
                                if not statmod.S_ISREG(os.fstat(fd).st_mode):
                                    saida[cheio] = "err"
                                else:
                                    h = hashlib.sha256()
                                    total = 0
                                    cabe = True
                                    while True:
                                        bloco = os.read(fd, 65536)
                                        if not bloco:
                                            break
                                        total += len(bloco)
                                        if total > _ARVORE_MAX_BYTES:
                                            cabe = False
                                            break
                                        h.update(bloco)
                                    if cabe:
                                        saida[cheio] = h.hexdigest()
                                    else:
                                        saida[cheio] = "big:%d:%d" % (info.st_size, info.st_mtime_ns)
                            except OSError:
                                saida[cheio] = "err"
                            finally:
                                try:
                                    os.close(fd)
                                except OSError:
                                    pass
                else:
                    saida[cheio] = "other"
            except OSError:
                saida[cheio] = "err"
        return saida
    except Exception:
        return None


def _out_of_scope_detail(before, after, cwd, allowed):
    """(total, caminhos[:50]) alterados fora dos --allowed-file, relativos ao cwd.

    O total é contado antes do truncamento da lista. None se alguma
    impressão for None (sem condição de comparar). Caminhos autorizados
    saem da lista; ausência de um lado conta como diferença.
    """
    if before is None or after is None:
        return None
    try:
        base = os.path.realpath(cwd)
        autorizados = set()
        for rel in (allowed or []):
            try:
                autorizados.add(os.path.normpath(os.path.join(base, rel)))
            except Exception:
                continue
        fora = []
        for caminho in set(before) | set(after):
            if before.get(caminho) != after.get(caminho):
                try:
                    real = os.path.realpath(caminho)
                except Exception:
                    real = caminho
                if os.path.normpath(caminho) in autorizados or real in autorizados:
                    continue
                try:
                    fora.append(os.path.relpath(caminho, cwd))
                except Exception:
                    continue
        fora.sort()
        return len(fora), fora[:50]
    except Exception:
        return None


def _out_of_scope(before, after, cwd, allowed):
    """Caminhos alterados fora dos --allowed-file (máx. 50); ver _out_of_scope_detail."""
    detail = _out_of_scope_detail(before, after, cwd, allowed)
    return detail[1] if detail is not None else None


def _kill_group(proc, sig):
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        pass
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _terminate_group(proc):
    try:
        os.killpg(proc.pid, 0)
    except ProcessLookupError:
        proc.wait()
        return
    _kill_group(proc, signal.SIGTERM)
    grace_deadline = time.monotonic() + 0.2
    while time.monotonic() < grace_deadline:
        time.sleep(0.05)
    _kill_group(proc, signal.SIGKILL)
    try:
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.wait(timeout=1)
        except Exception:
            pass


def _safe_failure_marker(stderr_data):
    # Somente diagnóstico: quem decide o fallback é o laço principal, que ainda confere
    # o snapshot dos arquivos autorizados. Com aviso de efeitos, o marcador é outro.
    message = b"ERRO: launcher de autenticacao incompativel com o bundle do ZCode"
    warning = b" Arquivos podem ter sido alterados; revise o diff antes de tentar novamente."
    lines = stderr_data.splitlines()
    if message + warning in lines:
        return b"launcher_incompatible_effects"
    if message in lines:
        return b"launcher_incompatible"
    if any(line.startswith(b"ERRO: fila de admiss") and b"nenhum worker iniciado" in line for line in stderr_data.splitlines()):
        return b"admission_busy"
    if any(line.startswith(b"opencode-ask: vagas ocupadas") for line in stderr_data.splitlines()):
        return b"admission_busy"
    reset = marker_reset(stderr_data)
    return (MARKER + str(reset)).encode() if reset is not None else b""


def run_wrapper(argv, cwd, prompt_bytes, timeout, isolated=False):
    global _CURRENT_PROC
    deadline = time.monotonic() + timeout
    env = {k: v for k, v in os.environ.items() if k not in ("MUSE_ASK_MODEL", "MUSE_ASK_EFFORT", "ASK_PRESENTATION_FD")}
    env["WORKER_ASK_ACTIVE"] = "1"
    env["WORKER_ASK_QUOTA_HELPER"] = str(Path(__file__).resolve().with_name("quota_reset.py"))
    # O opencode-ask espera vaga ate 900 s por padrao; quem definiu a variavel mantem o valor.
    env.setdefault("OPENCODE_ASK_WAIT", "6")
    if isolated:
        env["WORKER_ASK_ISOLATED"] = "1"
    with contextlib.ExitStack() as stack:
        stdin_tmp, stdout_tmp, stderr_tmp = [
            stack.enter_context(tempfile.TemporaryFile()) for _ in range(3)
        ]
        stdin_tmp.write(prompt_bytes)
        stdin_tmp.seek(0)
        proc = subprocess.Popen(argv, stdin=stdin_tmp, stdout=stdout_tmp,
                                stderr=stderr_tmp, cwd=cwd, start_new_session=True, env=env)
        _CURRENT_PROC = proc
        timed_out = False
        try:
            while proc.poll() is None:
                overflow = any(os.fstat(f.fileno()).st_size > MAX_OUTPUT_BYTES
                               for f in (stdout_tmp, stderr_tmp))
                timed_out = time.monotonic() >= deadline
                if overflow or timed_out:
                    _terminate_group(proc)
                    break
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
            overflow = any(os.fstat(f.fileno()).st_size > MAX_OUTPUT_BYTES
                           for f in (stdout_tmp, stderr_tmp))
            if overflow:
                _terminate_group(proc)
                _err("erro: output excedeu limite")
                return 1, b"", b"", timed_out
            stdout_tmp.seek(0)
            stdout_data = stdout_tmp.read(MAX_OUTPUT_BYTES + 1)
            if len(stdout_data) > MAX_OUTPUT_BYTES:
                return 1, b"", b"", timed_out
            rc = proc.returncode
            stderr_tmp.seek(0)
            marker = _safe_failure_marker(stderr_tmp.read(MAX_OUTPUT_BYTES + 1))
            return (128 - rc if rc < 0 else rc), stdout_data, marker, timed_out
        finally:
            # Also terminate children left behind when a wrapper exits early.
            _terminate_group(proc)
            _CURRENT_PROC = None


def muse_opted_out(cwd):
    if os.environ.get("WORKER_ASK_NO_MUSE") == "1":
        return True
    try:
        cur = os.path.abspath(cwd)
    except OSError:
        return False
    while True:
        try:
            if os.path.isfile(os.path.join(cur, ".worker-ask-no-muse")):
                return True
        except OSError:
            pass
        try:
            has_git = os.path.exists(os.path.join(cur, ".git"))
        except OSError:
            has_git = False
        if has_git:
            return False
        parent = os.path.dirname(cur)
        if parent == cur:
            return False
        cur = parent


def wrapper_chain(orchestrator, allow_muse, mode):
    # A plataforma do orquestrador nao restringe o worker; recursao tem trava propria.
    chain = [("zcode-worker", "zcode")]
    if allow_muse:
        chain.append(("muse-ask", "muse"))
    chain.extend([("opencode-ask", "opencode"), ("codex-ask", "codex"),
                  ("claude-ask", "claude")])
    return chain


def build_argv(name, path, mode, cwd):
    cwd_abs = os.path.abspath(cwd)
    if name == "zcode-worker":
        m = "edit" if mode == "write" else "plan"
        return [path, "--mode", m, "--model", "glm-5.3-flash", "--effort", "high", "--cwd", cwd_abs,
                "--queue-timeout", f"{_zcode_queue_timeout():g}"]
    if name == "muse-ask":
        return [path, "--mode", mode, "--model", "muse-spark-1.3-contributor", "--effort", "high", "--cwd", cwd_abs]
    if name == "opencode-ask":
        return [path, "--mode", mode, "--model", "auto", "--effort", "high", "--cwd", cwd_abs]
    if name == "codex-terra":
        m = "write" if mode == "write" else "plan"
        return [path, "--mode", m, "--model", "gpt-6.1-terra", "--effort", "medium", "--cwd", cwd_abs]
    if name == "codex-ask":
        m = "write" if mode == "write" else "plan"
        return [path, "--mode", m, "--cwd", cwd_abs]
    if name == "claude-ask":
        return [path, "--mode", mode, "--model", "haiku", "--cwd", cwd_abs]
    return [path, "--cwd", cwd_abs]


def _iso_deps(wi):
    return wi.Deps(state_dir=_state_dir, is_sensitive=_is_sensitive, snapshot_allowed=snapshot_allowed,
                   parse_status=_parse_status_z, max_file_bytes=MAX_SNAPSHOT_BYTES,
                   max_paths=_ARVORE_MAX_CAMINHOS)


def _iso_requested(cwd, mode):
    """Módulo de isolamento quando esta invocação deve rodar isolada; None caso contrário."""
    explicit = os.environ.get("WORKER_ASK_ISOLATION", "").strip().lower()
    try:
        import worker_isolation as wi
    except ImportError:
        if explicit in ("write", "all"):
            _err("erro: isolamento pedido mas worker_isolation.py ausente")
            sys.exit(1)
        return None
    except Exception:
        # Módulo quebrado nunca derruba a execução direta; só para se o isolamento foi pedido.
        if explicit in ("write", "all"):
            _err("erro: isolamento pedido mas worker_isolation.py invalido")
            sys.exit(1)
        _err("aviso: worker_isolation.py invalido; seguindo sem isolamento")
        return None
    level = wi.resolve_mode(cwd)
    if level == "all" or (level == "write" and mode == "write"):
        return wi
    return None


def _iso_direct(reason):
    """Registra que a execução segue sem isolamento; com STRICT, para com rc 1."""
    if _RUN is not None:
        _RUN["isolation"] = "none"
        _RUN["isolation_reason"] = reason
    if os.environ.get("WORKER_ASK_ISOLATION_STRICT") == "1":
        _err(f"erro: isolamento indisponivel ({reason}) com WORKER_ASK_ISOLATION_STRICT=1")
        sys.exit(1)


def _iso_start(mode, cwd, allowed, deadline):
    """Abre a sessão de isolamento da invocação, se ligada (define _ISO)."""
    global _ISO
    wi = _iso_requested(cwd, mode)
    if wi is None:
        return
    try:
        ensure_state_dir(create=True)
    except RuntimeError as e:
        _err(str(e))
        sys.exit(1)
    deps = _iso_deps(wi)
    try:
        session = wi.open_session(cwd, allowed, _RUN["run_id"], deps, deadline=deadline)
    except wi.DirectMode as e:
        _iso_direct(e.reason)
        return
    except wi.IsolationError as e:
        _err(f"erro: {e}")
        sys.exit(1)
    _ISO = {"wi": wi, "deps": deps, "session": session}
    _RUN["isolation"] = "worktree"


def _iso_begin(cwd_arg, allowed, prompt_bytes):
    """Prepara o slot para uma tentativa; None em modo direto."""
    global _ISO
    if _ISO is None:
        return None
    wi, deps, session = _ISO["wi"], _ISO["deps"], _ISO["session"]
    try:
        prep = wi.prepare(session, allowed, deps)
    except wi.DirectMode as e:
        wi.close_session(session)
        _ISO = None
        _iso_direct(e.reason)
        return None
    except wi.IsolationError as e:
        _err(f"erro: {e}")
        sys.exit(1)
    except RuntimeError:
        _err("erro: snapshot falhou")
        sys.exit(1)
    texto = (wi.isolation_notice(session).encode("utf-8") + b"\n"
             + wi.rewrite_prompt(prompt_bytes, session, cwd_arg))
    if len(texto) > MAX_INPUT_BYTES:
        _err("erro: prompt excede 64KiB apos isolamento")
        sys.exit(1)
    _RUN["isolation_prep_s"] = round(_RUN.get("isolation_prep_s", 0.0) + prep.prep_s, 3)
    _RUN["overlay_files"] = prep.overlay_files
    try:
        fp_before = _tree_fingerprint(session.slot_cwd)
    except Exception:
        fp_before = None
    return {"prep": prep, "cwd": session.slot_cwd, "prompt": texto,
            "fp_before": fp_before, "fp_after": None, "sync": None}


def _iso_end(attempt, allowed):
    """Devolve os arquivos autorizados do slot para a árvore real (roda em finally)."""
    if attempt is None or _ISO is None or attempt["sync"] is not None:
        return
    attempt["sync"] = _ISO["wi"].sync_back(_ISO["session"], allowed, attempt["prep"], _ISO["deps"])


def _iso_report(attempt, name, mode, cwd, allowed, tree_before, since_ts, oos_total, oos):
    """Pós-tentativa isolada: guarda descartados, vigia a árvore real e trata conflito."""
    wi, deps, session = _ISO["wi"], _ISO["deps"], _ISO["session"]
    sync = attempt["sync"]
    try:
        if oos_total:
            info = wi.save_rejected(session, oos, deps)
            _RUN["oos_discarded"] = _RUN.get("oos_discarded", 0) + oos_total
            onde = f"copia em {info.dir}" if info.dir else "sem copia"
            nomes = ", ".join(oos[:10])
            if mode == "plan":
                _err(f"aviso: {name} alterou {oos_total} arquivo(s) em modo plan; NAO aplicados: {nomes} ({onde})")
            else:
                _err(f"aviso: {name} alterou {oos_total} arquivo(s) fora do escopo; NAO aplicados: {nomes} ({onde})")
        _RUN["sync_applied"] = _RUN.get("sync_applied", 0) + len(sync.applied)
        _RUN["sync_conflicts"] = _RUN.get("sync_conflicts", 0) + len(sync.conflicts)
        # Vigia da árvore real: desconta o que outras invocações devolveram na janela.
        real = _out_of_scope_detail(tree_before, _tree_fingerprint(cwd), cwd, allowed)
        if real is not None and real[0]:
            outros = wi.synced_by_others(session, since_ts, deps)
            sobra = [rel for rel in real[1]
                     if os.path.normpath(os.path.join(cwd, rel)) not in outros
                     and ".wa-sync-" not in os.path.basename(rel)]
            if sobra:
                _RUN["real_tree_changed"] = _RUN.get("real_tree_changed", 0) + len(sobra)
                _err("aviso: arvore real mudou durante a execucao (edicao concorrente ou worker fora da copia)")
    except Exception:
        pass
    if sync is not None and sync.conflicts:
        _err("aviso: conflito na devolucao; NAO aplicados: " + ", ".join(sync.conflicts[:10])
             + f" (copia do worker em {session.slot_cwd})")
        _event("stop", f"{name}: conflito na devolucao", executor=name, rc=1, detail="conflito")
        sys.exit(1)


# Executores sem shell: fora do slot não têm como deixar efeito no repositório.
# Lista fixa de propósito (não configurável): codex-ask e claude-ask têm shell.
_STATE_FALLBACK_EXECUTORS = ("zcode-worker", "muse-ask", "opencode-ask")


def _iso_no_effect(attempt, name, mode, cwd, allowed, snap_before, marker, deadline):
    """True quando a tentativa isolada falhou sem deixar efeito e a cadeia pode seguir.

    Decide só por estado conferido (cópia e árvore real), nunca por texto de erro.
    """
    if attempt is None or _ISO is None or mode != "write":
        return False
    if os.environ.get("WORKER_ASK_STATE_FALLBACK") != "1" or name not in _STATE_FALLBACK_EXECUTORS:
        return False
    if marker == b"launcher_incompatible_effects" or time.monotonic() >= deadline:
        return False
    sync = attempt["sync"]
    if sync is None or sync.applied or sync.conflicts:
        return False
    if attempt["fp_before"] is None or attempt["fp_after"] is None:
        return False
    if attempt["fp_before"] != attempt["fp_after"]:
        return False
    try:
        if snapshot_allowed(attempt["cwd"], allowed) != attempt["prep"].snap_slot_before:
            return False
        return snapshot_allowed(cwd, allowed) == snap_before
    except RuntimeError:
        return False


def _iso_shutdown():
    """Solta o slot da invocação; chamado no finally de main()."""
    global _ISO
    iso, _ISO = _ISO, None
    if iso is not None:
        try:
            iso["wi"].close_session(iso["session"])
        except Exception:
            pass


def _iso_status():
    """Slots de isolamento para --status; lista vazia sem módulo ou sem slots."""
    try:
        import worker_isolation as wi
        return wi.list_slots(_iso_deps(wi))
    except Exception:
        return []


def _main_impl(argv=None):
    global _LOG_FD
    parser = argparse.ArgumentParser(prog="worker-ask")
    parser.add_argument("--cwd")
    parser.add_argument("--mode", choices=["plan", "write"], default="write")
    parser.add_argument("--spec")
    parser.add_argument("prompt", nargs="?")
    parser.add_argument("--timeout", type=float, default=1800.0)
    parser.add_argument("--step-timeout", type=float, default=900.0)
    parser.add_argument("--allow-muse", action="store_true", help="obsoleto: Muse ja e padrao")
    parser.add_argument("--no-muse", action="store_true")
    parser.add_argument("--orchestrator", choices=list(ORCHESTRATORS))
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--refresh-availability", action="store_true",
                        help="atualiza a tabela de disponibilidade e sai")
    parser.add_argument("--task-id", help="identifica a fatia; grava task_id no runs.jsonl e habilita o orcamento de relancamentos")
    parser.add_argument("--mark-redo", metavar="ID", help="registra evento redo para a task_id informada e sai")
    parser.add_argument("--stats", action="store_true", help="imprime resumo JSON dos runs.jsonl e sai")
    parser.add_argument("--retry-exhausted", action="store_true")
    parser.add_argument("--allowed-file", action="append", default=[])
    parser.add_argument("--log", help="grava stdout+stderr e a linha final exit=N neste arquivo")
    parser.add_argument("--isolation-gc", action="store_true",
                        help="remove slots de isolamento sem uso e copias rejeitadas antigas, e sai")
    # --help sai dentro de parse_args, antes da abertura normal do --log:
    # abre o log antes e registra o texto de ajuda nele.
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    pre_dashdash = raw_argv[: raw_argv.index("--")] if "--" in raw_argv else raw_argv
    if any(arg in ("-h", "--help") or (len(arg) >= 4 and "--help".startswith(arg)) for arg in pre_dashdash):
        help_log = None
        for index, arg in enumerate(pre_dashdash):
            if arg == "--log":
                if index + 1 < len(pre_dashdash) and not pre_dashdash[index + 1].startswith("-"):
                    help_log = pre_dashdash[index + 1]
                else:
                    help_log = None
            elif arg.startswith("--log="):
                help_log = arg.partition("=")[2]
        if help_log is not None:
            if _LOG_FD is None:
                try:
                    _LOG_FD = _open_log(help_log)
                except (RuntimeError, OSError) as e:
                    _err(f"erro: {e}")
                    sys.exit(2)
            help_text = parser.format_help()
            sys.stdout.write(help_text)
            if not _log_write(help_text.encode("utf-8", "replace")):
                _note_log_failure()
                sys.exit(1)
            sys.exit(0)
    args = parser.parse_args(argv)

    if args.log is not None and _LOG_FD is None:
        try:
            _LOG_FD = _open_log(args.log)
        except (RuntimeError, OSError) as e:
            _err(f"erro: {e}")
            sys.exit(2)

    if args.task_id is not None and not TASK_ID_RE.match(args.task_id):
        _err("erro: --task-id invalido")
        sys.exit(1)

    # status: no prompt reading
    if args.status:
        try:
            cache = load_cache(create_dir=False)
        except RuntimeError as e:
            _err(str(e))
            sys.exit(1)
        now = time.time()
        snap = {}
        for k in CACHE_KEYS:
            v = cache.get(k)
            if v:
                remaining = max(0, v["until"] - now)
                snap[k] = {**v, "ttl_remaining": remaining}
        out = {"state_dir": str(_state_dir()), "cache": snap}
        slots = _iso_status()
        if slots:
            out["isolation_slots"] = slots
        import availability
        table = availability.load(_state_dir())
        out["availability"] = availability.describe(table, cache, now)
        out["availability_age"] = None if table is None else now - table["generated_at"]
        status_text = json.dumps(out) + "\n"
        sys.stdout.write(status_text)
        if not _log_write(status_text.encode("utf-8", "replace")):
            _note_log_failure()
            sys.exit(1)
        sys.exit(0)

    if args.refresh_availability:
        try:
            sd = ensure_state_dir(create=True)
            import availability
            availability.refresh(sd)
        except RuntimeError as e:
            _err(str(e))
            sys.exit(1)
        sys.exit(0)

    if args.mark_redo is not None:
        if not TASK_ID_RE.match(args.mark_redo):
            _err("erro: --task-id invalido")
            sys.exit(1)
        achada = _find_last_task_run(args.mark_redo)
        if achada is None:
            _err("erro: task_id sem execucao registrada")
            sys.exit(1)
        _write_redo_event(args.mark_redo, args.orchestrator, achada)
        sys.exit(0)

    if args.stats:
        stats_text = json.dumps(_stats_summary(), ensure_ascii=False) + "\n"
        sys.stdout.write(stats_text)
        if not _log_write(stats_text.encode("utf-8", "replace")):
            _note_log_failure()
            sys.exit(1)
        sys.exit(0)

    if os.environ.get("WORKER_ASK_ACTIVE") == "1":
        _err("erro: delegacao recursiva de worker bloqueada")
        sys.exit(2)

    if args.isolation_gc:
        try:
            import worker_isolation as wi
        except ImportError:
            _err("erro: worker_isolation.py ausente")
            sys.exit(1)
        gc_text = json.dumps(wi.gc(_iso_deps(wi))) + "\n"
        sys.stdout.write(gc_text)
        if not _log_write(gc_text.encode("utf-8", "replace")):
            _note_log_failure()
            sys.exit(1)
        sys.exit(0)

    # validate timeout
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        _err("erro: --timeout deve ser float positivo finito")
        sys.exit(2)
    if not math.isfinite(args.step_timeout) or args.step_timeout <= 0:
        _err("erro: --step-timeout deve ser float positivo finito")
        sys.exit(2)
    floor = _min_timeout()
    if args.timeout < floor:
        _err(f"worker-ask: --timeout {args.timeout:g}s abaixo do minimo; usando {floor:g}s")
        args.timeout = floor
    if args.step_timeout < floor:
        _err(f"worker-ask: --step-timeout {args.step_timeout:g}s abaixo do minimo; usando {floor:g}s")
        args.step_timeout = floor
    if not args.cwd:
        _err("erro: --cwd obrigatorio")
        sys.exit(2)
    cwd = os.path.realpath(args.cwd)
    if not os.path.isdir(cwd):
        _err("erro: --cwd deve ser diretorio valido")
        sys.exit(2)
    if not args.orchestrator:
        _err("erro: --orchestrator obrigatorio")
        sys.exit(2)

    allowed = list(args.allowed_file or [])
    if len(allowed) > MAX_ALLOWED_FILES:
        _err("erro: excesso de --allowed-file")
        sys.exit(2)
    if args.mode == "write" and not allowed:
        _err("erro: --allowed-file obrigatorio para --mode write")
        sys.exit(2)
    try:
        for rel in allowed:
            validate_allowed_file(cwd, rel)
    except ValueError as e:
        _err(f"erro: {e}")
        sys.exit(2)

    # input source
    if args.spec is not None and args.prompt is not None:
        _err("erro: use apenas uma fonte de prompt (--spec ou posicional)")
        sys.exit(2)
    prompt = None
    if args.spec is not None:
        try:
            spec_path = Path(os.path.abspath(args.spec))
            for part in spec_path.parts:
                if _is_sensitive(part):
                    raise RuntimeError("spec sensivel recusada")
            if any(parent.is_symlink() for parent in (spec_path, *spec_path.parents)):
                raise RuntimeError("symlink em spec recusado")
            raw = _read_bounded_file(args.spec, MAX_INPUT_BYTES)
        except RuntimeError as e:
            _err(f"erro: nao foi possivel ler --spec: {e}")
            sys.exit(2)
        except OSError:
            _err("erro: nao foi possivel ler --spec")
            sys.exit(2)
        if len(raw) > MAX_INPUT_BYTES:
            _err("erro: prompt excede 64KiB")
            sys.exit(2)
        try:
            prompt = raw.decode("utf-8")
        except UnicodeDecodeError:
            _err("erro: prompt nao eh UTF-8 valido")
            sys.exit(2)
    elif args.prompt is not None:
        prompt = args.prompt
    else:
        try:
            raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        except OSError:
            _err("erro: nao foi possivel ler stdin")
            sys.exit(2)
        if len(raw) > MAX_INPUT_BYTES:
            _err("erro: prompt excede 64KiB")
            sys.exit(2)
        try:
            prompt = raw.decode("utf-8")
        except UnicodeDecodeError:
            _err("erro: prompt nao eh UTF-8 valido")
            sys.exit(2)

    if not prompt:
        _err("erro: prompt vazio")
        sys.exit(2)
    try:
        prompt_bytes = prompt.encode("utf-8")
    except UnicodeEncodeError:
        _err("erro: prompt nao eh UTF-8 valido")
        sys.exit(2)
    if len(prompt_bytes) > MAX_INPUT_BYTES:
        _err("erro: prompt excede 64KiB")
        sys.exit(2)

    # Bytes originais do usuario, antes do prefixo de --allowed-file (metrica).
    spec_raw = bytes(prompt_bytes)
    if allowed:
        prefix = "[worker-router] Arquivos autorizados (relativos ao cwd): " + ", ".join(allowed) + ". Edite apenas estes; nao edite nenhum outro arquivo."
        prompt = prefix + "\n\n" + prompt
        prompt_bytes = prompt.encode("utf-8")
        if len(prompt_bytes) > MAX_INPUT_BYTES:
            _err("erro: prompt excede 64KiB apos prefixo")
            sys.exit(2)

    try:
        cache = load_cache(create_dir=False)
    except RuntimeError as e:
        _err(str(e))
        sys.exit(1)

    muse_excluded = args.no_muse or muse_opted_out(cwd)
    chain = wrapper_chain(args.orchestrator, not muse_excluded, args.mode)

    if args.task_id is not None:
        n_runs = _count_task_runs(args.task_id)
        if n_runs >= MAX_RUNS_PER_TASK:
            _err(f"worker-ask: orcamento esgotado para task_id {args.task_id} ({n_runs} execucoes); reavalie a abordagem: o orquestrador investiga, divide com nova task_id ou assume apos 2 falhas")
            sys.exit(1)

    global _RUN, _RUN_T0
    _RUN = {
        "schema": 1,
        "run_id": uuid.uuid4().hex,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "orchestrator": args.orchestrator,
        "task_id": args.task_id,
        "mode": args.mode,
        "cwd": cwd,
        "spec_sha256": hashlib.sha256(spec_raw).hexdigest(),
        "prompt_bytes": len(spec_raw),
        "allowed_files": sorted(allowed),
        "attempts": [],
    }
    _RUN_T0 = time.monotonic()
    start = _RUN_T0
    deadline = start + args.timeout
    had_timeout = False
    _iso_start(args.mode, cwd, allowed, deadline)

    import availability
    usar_tabela = not args.retry_exhausted
    table = availability.load(_state_dir()) if usar_tabela else None
    if usar_tabela and table is None and (_state_dir() / availability.TABLE_NAME).exists():
        _err("aviso: tabela de disponibilidade vencida ou invalida; consultando cota na chamada "
             "(timer worker-ask-disponibilidade parado?)")
    indisponiveis = []   # (nome, motivo, until ou None), para a mensagem final

    for name, orch in chain:
        if time.monotonic() >= deadline:
            _exit_timeout("erro: timeout total esgotado", detail="prazo total esgotado")
        if not args.retry_exhausted:
            entry = cache.get(name)
            if entry and name == "zcode-worker" and entry.get("blocked_reason") != "timeout":
                # Cooldown de cota do zcode não vale com o Start Plan livre: o
                # worker usa o Start primeiro (o sucesso limpa a entrada).
                from quota_provider import start_plan_free
                if start_plan_free():
                    entry = None
            if entry and time.time() < entry["until"]:
                _event("skip", f"{name}: skip (cooldown)", executor=name, reason="cooldown")
                indisponiveis.append((name, "cooldown", entry["until"]))
                continue
        path = shutil.which(WRAPPER_BINARIES.get(name, name))
        if path is None:
            _event("skip", f"{name}: skip (wrapper ausente)", executor=name, reason="ausente")
            indisponiveis.append((name, "ausente", None))
            continue

        if table is None and name not in cache and not args.retry_exhausted:
            # Contingencia: tabela de disponibilidade ausente ou vencida; consulta sincrona.
            from quota_provider import query_reset
            reset = query_reset(name, budget=max(0, deadline - time.monotonic()))
            if time.monotonic() >= deadline:
                _exit_timeout("erro: timeout total esgotado", detail="prazo total esgotado")
        elif table is not None:
            reset = availability.blocked_until(table, name)
        else:
            reset = None
        if reset is not None:
            quota_entry = {"wrapper": name, "until": reset, "reset_known": True}
            try:
                save_cache({name: quota_entry})
            except RuntimeError as e:
                _err(str(e))
                sys.exit(1)
            cache[name] = quota_entry
            _event("skip", f"{name}: skip (cota ate {reset:g})", executor=name, reason="cota", until=reset)
            indisponiveis.append((name, "cota", reset))
            continue

        # Reserva atômica: chamadas simultâneas não disputam a mesma vaga.
        reserva = availability.reserve(name, _state_dir())
        if reserva is False:
            _event("skip", f"{name}: skip (ocupado)", executor=name, reason="ocupado")
            indisponiveis.append((name, "ocupado", None))
            continue

        iso_t0 = time.time()
        snap_before = None
        if allowed:
            try:
                snap_before = snapshot_allowed(cwd, allowed)
            except RuntimeError:
                _err("erro: snapshot falhou")
                sys.exit(1)

        # Impressão da árvore suja para detectar edição fora do escopo (plan e write).
        try:
            tree_before = _tree_fingerprint(cwd)
        except Exception:
            tree_before = None
        attempt = _iso_begin(args.cwd, allowed, prompt_bytes)
        exec_cwd = attempt["cwd"] if attempt is not None else cwd
        exec_prompt = attempt["prompt"] if attempt is not None else prompt_bytes

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _exit_timeout("erro: timeout total esgotado", detail="prazo total esgotado")
        # zcode-worker espera na fila antes de executar; a espera nao consome o
        # tempo de execucao da fatia, so o prazo total.
        queue_extra = _zcode_queue_timeout() if name == "zcode-worker" else 0.0
        budget = min(args.step_timeout + queue_extra, remaining)

        argv_list = build_argv(name, path, args.mode, exec_cwd)
        model = argv_list[argv_list.index("--model") + 1] if "--model" in argv_list else None
        _emit_executor_start(name, args.mode, model)
        try:
            try:
                # Sem isolamento a chamada fica idêntica à anterior (4 argumentos posicionais).
                iso_kwargs = {"isolated": True} if attempt is not None else {}
                rc, stdout_data, stderr_data, timed_out = run_wrapper(
                    argv_list, exec_cwd, exec_prompt, budget, **iso_kwargs)
            finally:
                availability.release(reserva)
                _iso_end(attempt, allowed)
        except FileNotFoundError:
            _event("skip", f"{name}: skip (wrapper ausente)", executor=name, reason="ausente")
            indisponiveis.append((name, "ausente", None))
            continue
        except RuntimeError as e:
            _err(str(e))
            sys.exit(1)

        # Coleta unica por tentativa, antes de ramificar: interrupcao (rc143/144)
        # apos fallback nao pode herdar executor/tokens da tentativa anterior.
        _record_run_metrics(name, stdout_data, allowed, cwd, snap_before)

        oos_total = None
        oos = None
        # Detecção fora do escopo: métrica e aviso; nunca muda rc, stdout ou fluxo.
        try:
            if _RUN is not None:
                if attempt is not None:
                    attempt["fp_after"] = _tree_fingerprint(exec_cwd)
                    detail = _out_of_scope_detail(attempt["fp_before"], attempt["fp_after"], exec_cwd, allowed)
                else:
                    detail = _out_of_scope_detail(tree_before, _tree_fingerprint(cwd), cwd, allowed)
                oos_total = detail[0] if detail is not None else None
                oos = detail[1] if detail is not None else None
                # Persiste por tentativa e agrega: fallback limpo não apaga
                # violação (nem autoria) de tentativa anterior.
                tentativas_oos = _RUN.setdefault("out_of_scope_attempts", [])
                if not isinstance(tentativas_oos, list):
                    tentativas_oos = _RUN["out_of_scope_attempts"] = []
                tentativas_oos.append({
                    "executor": name,
                    "out_of_scope": oos_total,
                    "out_of_scope_files": oos[:20] if oos else [],
                })
                conhecidos = [t["out_of_scope"] for t in tentativas_oos
                              if isinstance(t, dict)
                              and isinstance(t.get("out_of_scope"), int)
                              and not isinstance(t.get("out_of_scope"), bool)]
                _RUN["out_of_scope"] = sum(conhecidos) if conhecidos else None
                arquivos = []
                for tentativa in tentativas_oos:
                    if isinstance(tentativa, dict):
                        for arquivo in tentativa.get("out_of_scope_files") or []:
                            if arquivo not in arquivos:
                                arquivos.append(arquivo)
                _RUN["out_of_scope_files"] = arquivos[:20]
                if oos_total and attempt is None:
                    nomes = ", ".join(oos[:10])
                    if args.mode == "plan":
                        _err(f"aviso: {name} alterou {oos_total} arquivo(s) em modo plan (só leitura): {nomes} (pode incluir edição concorrente)")
                    else:
                        _err(f"aviso: {name} alterou {oos_total} arquivo(s) fora do escopo (--allowed-file): {nomes} (pode incluir edição concorrente)")
        except Exception:
            pass

        if attempt is not None:
            _iso_report(attempt, name, args.mode, cwd, allowed, tree_before, iso_t0, oos_total, oos)

        if timed_out or rc == 124:
            # Circuito aberto: nao repetir automaticamente um executor sem resposta.
            # Fatia de escrita com progresso (mesma contagem de changed_files do
            # registro de run) nao abre circuito: executor travado nao altera
            # arquivos; estouro com trabalho feito indica fatia grande, nao morte.
            progress = 0
            if args.mode != "plan" and _RUN is not None:
                changed = _RUN.get("changed_files")
                if isinstance(changed, int):
                    progress = changed
            if progress > 0:
                _err(f"worker-ask: {name} estourou com progresso; sem cooldown (fatia grande?)")
                _exit_timeout(f"{name}: rc=124 (timeout); parando sem fallback", name, "escrita sem fallback")
            try:
                save_cache({name: {"wrapper": name, "until": time.time() + TIMEOUT_COOLDOWN_SECONDS,
                                   "reset_known": False, "blocked_reason": "timeout"}})
            except RuntimeError as e:
                _err(str(e))
                sys.exit(1)
            if _iso_no_effect(attempt, name, args.mode, cwd, allowed, snap_before, stderr_data, deadline):
                # Cópia isolada intacta: o worker sem shell não deixou efeito; a cadeia segue.
                had_timeout = True
                _RUN["state_fallbacks"] = _RUN.get("state_fallbacks", 0) + 1
                _event("fallback", f"{name}: rc=timeout, fallback (sem efeito)", executor=name, rc=124,
                       reason="sem_efeito")
                continue
            if args.mode != "plan":
                # write: snapshot igual nao exclui efeitos externos do worker morto.
                _exit_timeout(f"{name}: rc=124 (timeout); parando sem fallback", name, "escrita sem fallback")
            # plan e so leitura: outro worker pode tentar a mesma fatia.
            had_timeout = True
            _event("fallback", f"{name}: rc=timeout, fallback (plan)", executor=name, rc=124, reason="timeout")
            continue

        if rc == 75:
            admission_busy = stderr_data == b"admission_busy"
            reset = marker_reset(stderr_data)
            if reset is None and not admission_busy:
                from quota_provider import query_reset
                reset = query_reset(name, budget=max(0, deadline - time.monotonic()))
            if reset is not None:
                quota_entry = {"wrapper": name, "until": reset, "reset_known": True}
            else:
                # Sem reset conhecido: cooldown finito; destrava sozinho apos expirar.
                quota_entry = {"wrapper": name, "until": time.time() + _unknown_reset_cooldown(),
                               "reset_known": False, "blocked_reason": "quota"}
            if not admission_busy:
                try:
                    save_cache({name: quota_entry})
                except RuntimeError as e:
                    _err(str(e))
                    sys.exit(1)
                cache[name] = quota_entry
            if time.monotonic() >= deadline:
                _exit_timeout("erro: timeout total esgotado", detail="prazo total esgotado")

            if allowed:
                try:
                    snap_after = snapshot_allowed(cwd, allowed)
                except RuntimeError:
                    _err("aviso: snapshot falhou; parando")
                    sys.exit(1)
                if snap_after != snap_before:
                    _err("aviso: arquivos autorizados alterados apos falha rc75; parando sem fallback")
                    sys.exit(1)
            motivo_rc75 = "fila" if admission_busy else "cota"
            _event("fallback", f"{name}: rc=75, fallback", executor=name, rc=75, reason=motivo_rc75)
            indisponiveis.append((name, motivo_rc75,
                                  None if admission_busy else quota_entry["until"]))
            if _LOG_FAILED:
                _note_log_failure()
                sys.exit(1)
            continue

        if rc == 143 or rc == 144:
            _event("stop", f"{name}: rc={rc}", executor=name, rc=rc, detail="interrompido")
            sys.exit(rc)

        if rc != 0:
            launcher = name == "zcode-worker" and rc == 1 and stderr_data.startswith(b"launcher_incompatible")
            if launcher and stderr_data == b"launcher_incompatible":
                # O launcher aborta antes de qualquer chamada a modelo; ainda assim só segue
                # a cadeia se os arquivos autorizados estiverem intactos (mesma regra do rc 75).
                unchanged = True
                if allowed:
                    try:
                        unchanged = snapshot_allowed(cwd, allowed) == snap_before
                    except RuntimeError:
                        unchanged = False
                if unchanged and time.monotonic() < deadline:
                    _err("zcode-worker: launcher de autenticacao incompativel com o bundle; "
                         "CORRIGIR o launch.cjs. Seguindo para o proximo worker (rc=1)")
                    _event("fallback", f"{name}: rc=1, fallback", executor=name, rc=1,
                           reason="launcher_incompativel")
                    if _LOG_FAILED:
                        _note_log_failure()
                        sys.exit(1)
                    continue
            if launcher:
                _err("zcode-worker: launcher de autenticacao incompativel com o bundle; fallback bloqueado (rc=1)")
            if rc == 1 and _iso_no_effect(attempt, name, args.mode, cwd, allowed, snap_before, stderr_data,
                                          deadline):
                # Cópia isolada intacta: o worker sem shell não deixou efeito; a cadeia segue.
                _RUN["state_fallbacks"] = _RUN.get("state_fallbacks", 0) + 1
                _event("fallback", f"{name}: rc=1, fallback (sem efeito)", executor=name, rc=1,
                       reason="sem_efeito")
                if _LOG_FAILED:
                    _note_log_failure()
                    sys.exit(1)
                continue
            _event("stop", f"{name}: rc={rc}", executor=name, rc=rc, detail="falha")
            sys.exit(rc if rc > 0 else 1)

        if name in cache:
            try:
                save_cache({}, clear={name: cache[name]})
            except RuntimeError as e:
                _err(str(e))
                sys.exit(1)
        sys.stdout.buffer.write(stdout_data)
        sys.stdout.buffer.flush()
        if not _log_write(stdout_data):
            _note_log_failure()
            sys.exit(1)
        _event("result", f"{name}: rc=0", executor=name, rc=0, usage=_parse_usage(stdout_data) or None)
        sys.exit(0)

    if time.monotonic() >= deadline:
        _exit_timeout("erro: timeout total esgotado", detail="prazo total esgotado")
    if had_timeout:
        _exit_timeout("erro: todos os workers indisponiveis ou com timeout", detail="todos indisponiveis ou com timeout")
    partes = []
    for nome, motivo, until in indisponiveis:
        quando = f" ate {datetime.fromtimestamp(until).strftime('%H:%M')}" if until is not None else ""
        partes.append(f"{nome} {motivo}{quando}")
    if partes:
        _err("worker-ask: nenhum worker disponivel agora: " + "; ".join(partes))
    _event("stop", "erro: todos os workers indisponiveis", rc=75, detail="nenhum worker disponivel")
    sys.exit(75)


def main(argv=None):
    global _INTERRUPTED
    prev_int = signal.getsignal(signal.SIGINT)
    prev_term = signal.getsignal(signal.SIGTERM)

    def handler(signum, frame):
        global _INTERRUPTED
        _INTERRUPTED = True
        p = _CURRENT_PROC
        if p is not None:
            _terminate_group(p)
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)
    try:
        try:
            _main_impl(argv)
        except SystemExit as e:
            code = e.code
            if code is None:
                rc = 0
            elif isinstance(code, int):
                rc = code
            else:
                rc = 1
            if _RUN is not None:
                _write_run(rc)
            raise
        except BaseException:
            if _RUN is not None:
                _write_run(1)
            raise
        else:
            if _RUN is not None:
                _write_run(0)
    finally:
        _iso_shutdown()
        signal.signal(signal.SIGINT, prev_int)
        signal.signal(signal.SIGTERM, prev_term)


def _presentation_helper():
    """Prefere a apresentação irmã do checkout; a instalada é alternativa."""
    for helper in (Path(__file__).resolve().parent / "bin" / "ask_presentation.py",
                   Path.home() / ".local" / "bin" / "ask_presentation.py"):
        if helper.is_file():
            return helper
    return None


def _cli():
    try:
        main()
    except SystemExit as e:
        code = e.code
        rc = code if isinstance(code, int) else (0 if code is None else 1)
        if _LOG_FAILED and rc in (0, 75):
            rc = 1
        _log_exit(rc)
        if _LOG_FAILED:
            _note_log_failure()
            if rc in (0, 75):
                rc = 1
        if isinstance(code, int) or code is None:
            raise SystemExit(rc)
        raise
    except RuntimeError as e:
        _err(str(e))
        _log_exit(1)
        if _LOG_FAILED:
            _note_log_failure()
        sys.exit(1)
    except Exception:
        _err("erro: falha local de infraestrutura; execucao interrompida")
        _log_exit(1)
        if _LOG_FAILED:
            _note_log_failure()
        sys.exit(1)
    rc = 1 if _LOG_FAILED else 0
    _log_exit(rc)
    if _LOG_FAILED:
        _note_log_failure()
        sys.exit(1)


if __name__ == "__main__":
    if os.environ.get("ASK_PRESENTATION_ACTIVE") != "1":
        _helper = _presentation_helper()
        if _helper is not None:
            os.execv(sys.executable, [sys.executable, str(_helper), "--provider", "worker-ask", "--", os.path.abspath(__file__), *sys.argv[1:]])
    _cli()
