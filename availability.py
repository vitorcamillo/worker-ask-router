"""Tabela de disponibilidade dos workers, gravada por timer, sem consultar cota na chamada.

O roteador decide em milissegundos lendo a tabela em disco; a sonda de slots
verifica vagas ao vivo do ZCode e do OpenCode. Nenhuma função aqui chama modelo.
"""

import contextlib
import fcntl
import json
import math
import os
import stat
import sys
import tempfile
import time
from pathlib import Path

import quota_provider

WORKERS = ("zcode-worker", "muse-ask", "opencode-ask", "codex-ask", "claude-ask")
TABLE_NAME = "availability.json"
DEFAULT_MAX_AGE = 330.0  # segundos; env WORKER_ASK_AVAIL_MAX_AGE sobrepõe (finito, > 0)
SCHEMA = 1
MAX_TABLE_BYTES = 64 * 1024


def max_age() -> float:
    """Idade máxima da tabela; env inválida (não finita ou <= 0) cai no padrão."""
    raw = os.environ.get("WORKER_ASK_AVAIL_MAX_AGE", "")
    if raw.strip():
        try:
            value = float(raw)
        except ValueError:
            return DEFAULT_MAX_AGE
        if math.isfinite(value) and value > 0:
            return value
    return DEFAULT_MAX_AGE


def _numero(valor):
    """Número finito (bool não conta como número); seguro para inteiros enormes."""
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        return False
    try:
        return math.isfinite(float(valor))
    except (OverflowError, ValueError):
        return False


def refresh(state_dir, now=None, budget=12.0) -> dict:
    """Consulta reset de cada worker sob orçamento total e grava a tabela atomicamente.

    Consulta sem orçamento restante ou com exceção deixam o nome com
    ``checked: false``. Levanta RuntimeError para state_dir inválido, destino
    symlink ou falha de I/O ao gravar.
    """
    state_dir = Path(state_dir)
    now = time.time() if now is None else now
    if state_dir.is_symlink() or not state_dir.is_dir():
        raise RuntimeError("state_dir inválido para availability.refresh")
    start = time.monotonic()
    workers = {}
    for name in WORKERS:
        remaining = budget - (time.monotonic() - start)
        checked = remaining > 0
        blocked = None
        if checked:
            try:
                reset = quota_provider.query_reset(name, now, budget=remaining)
            except Exception:
                checked = False
            else:
                if _numero(reset) and reset > now:
                    blocked = float(reset)
        workers[name] = {"checked": checked, "blocked_until": blocked}
    table = {"schema": SCHEMA, "generated_at": float(now), "workers": workers}
    _grava_atomica(state_dir, table)
    return table


def _grava_atomica(state_dir, table):
    """Grava a tabela em temporário 0600 no mesmo diretório e faz os.replace."""
    dest = state_dir / TABLE_NAME
    if os.path.islink(dest):
        raise RuntimeError("destino da tabela é symlink")
    try:
        fd, tmp = tempfile.mkstemp(dir=state_dir)
    except OSError:
        raise RuntimeError("falha de I/O ao criar temporário da tabela") from None
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(table, handle)
        os.replace(tmp, dest)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise RuntimeError("falha de I/O ao gravar tabela") from None
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def load(state_dir, now=None):
    """Devolve a tabela validada ou None quando ausente/malformada/velha; nunca levanta."""
    state_dir = Path(state_dir)
    now = time.time() if now is None else now
    try:
        fd = os.open(state_dir / TABLE_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        raw = os.read(fd, MAX_TABLE_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(raw) > MAX_TABLE_BYTES:
        return None
    return _valida(raw, now)


def _valida(raw, now):
    """Valida conteúdo da tabela; conteúdo ruim vira None, entrada ruim é descartada."""
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict) or isinstance(data.get("schema"), bool):
        return None
    if data.get("schema") != SCHEMA:
        return None
    generated = data.get("generated_at")
    if not _numero(generated):
        return None
    generated = float(generated)
    if generated > now + 5.0 or now - generated > max_age():
        return None
    bruto = data.get("workers")
    workers = {}
    if isinstance(bruto, dict):
        for name, entry in bruto.items():
            if not isinstance(entry, dict) or not isinstance(entry.get("checked"), bool):
                continue
            blocked = entry.get("blocked_until")
            if blocked is not None and not _numero(blocked):
                continue
            workers[name] = {
                "checked": entry["checked"],
                "blocked_until": None if blocked is None else float(blocked),
            }
    return {"schema": SCHEMA, "generated_at": generated, "workers": workers}


def blocked_until(table, name, now=None):
    """Epoch de reset comprovado do nome, ou None quando inexistente/expirado."""
    now = time.time() if now is None else now
    if not isinstance(table, dict) or not isinstance(table.get("workers"), dict):
        return None
    entry = table["workers"].get(name)
    if not isinstance(entry, dict) or entry.get("checked") is not True:
        return None
    blocked = entry.get("blocked_until")
    if not _numero(blocked) or blocked <= now:
        return None
    return float(blocked)


def slots(name):
    """Sonda ao vivo de vagas; nunca levanta exceção, None = desconhecido."""
    if os.environ.get("WORKER_ASK_SLOT_PROBE", "").strip().lower() in {
        "0",
        "off",
        "false",
        "no",
    }:
        return None
    if name == "zcode-worker":
        return _slots_zcode()
    if name == "opencode-ask":
        return _slots_opencode()
    return None


def reserve(name, state_dir):
    """Reserva atômica de vaga entre chamadas simultâneas do roteador.

    Devolve um fd com flock (segurar até o worker terminar; ``release`` solta),
    ``False`` quando não há vaga, ou ``None`` quando o worker não tem controle
    de vagas ou a sonda está desligada. Nunca levanta exceção.
    """
    probe = slots(name)
    if probe is None:
        return None
    if not probe.get("free"):
        return False
    flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW
    base = os.path.join(os.fspath(state_dir), f"vaga-{name}")
    mestre = livre = None
    try:
        mestre = os.open(base + ".lock", flags, 0o600)
        fcntl.flock(mestre, fcntl.LOCK_EX)  # serializa a decisão entre chamadas
        pendentes, agora = 0, time.time()
        for i in range(1, int(probe["max"]) + 1):
            fd = os.open(f"{base}-{i}.lock", flags, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                # Reserva recente ainda fora da sonda: soma à ocupação real.
                # A execução da própria reserva já sondada não soma duas vezes.
                feita, dono = _carimbo(os.pread(fd, 64, 0))
                pendentes += (
                    feita is None
                    or (agora - feita < RESERVA_PENDENTE and not _execucao_sondada(probe, dono))
                )
                os.close(fd)
                continue
            livre = fd if livre is None else (os.close(fd) or livre)
        if livre is None or int(probe["active"]) + pendentes >= int(probe["max"]):
            release(livre)
            return False
        os.ftruncate(livre, 0)
        os.pwrite(livre, f"{agora!r} {os.getpid()}".encode(), 0)
        return livre
    except Exception:
        return None
    finally:
        release(mestre)


RESERVA_PENDENTE = 15.0  # s até o worker reservado aparecer na sonda de vagas


def _carimbo(raw):
    """(epoch, pid) gravados na reserva; ilegível devolve (None, None)."""
    try:
        partes = raw.decode().split()
    except UnicodeError:
        return None, None
    try:
        epoch = float(partes[0])
    except (IndexError, ValueError):
        return None, None
    try:
        pid = int(partes[1])
    except (IndexError, ValueError):
        pid = None
    return (epoch if _numero(epoch) else None), pid


def _execucao_sondada(probe, dono):
    """True quando a execução reservada por dono já está entre os pids da sonda."""
    pids = probe.get("pids") if isinstance(probe, dict) else None
    if dono is None or not isinstance(pids, (list, tuple, set, frozenset)):
        return False  # sem correlação possível: a reserva continua pendente
    return any(_descende_de(pid, dono) for pid in pids)


def _descende_de(pid, raiz):
    """True se pid tem raiz como ancestral pelo /proc; erro de leitura é False."""
    atual = pid
    for _ in range(64):  # teto de hops protege de árvore ciclica
        if not isinstance(atual, int) or isinstance(atual, bool) or atual <= 1:
            return False
        try:
            with open(f"/proc/{atual}/stat", "rb") as fh:
                campos = fh.read().rsplit(b")", 1)[1].split()
            ppid = int(campos[1])
        except (OSError, ValueError, IndexError):
            return False
        if ppid == raiz:
            return True
        atual = ppid
    return False


def release(reserva):
    """Solta a reserva devolvida por ``reserve`` (aceita None/False)."""
    if isinstance(reserva, int) and not isinstance(reserva, bool):
        with contextlib.suppress(OSError):
            os.close(reserva)


def _slots_zcode():
    """Vagas do zcode-worker via zcode_worker.base, importado preguiçosamente."""
    try:
        try:
            import zcode_worker.base as base
        except ImportError:
            adapter = str(Path(__file__).resolve().parent / "adapters/zcode/src")
            if adapter not in sys.path:
                sys.path.insert(0, adapter)
            import zcode_worker.base as base
        max_parallel = int(base._read_settings())
        pids = base.active_cli_pids()
        active = len(pids)
        mem_ok = base._read_mem_available_bytes() >= base.MIN_MEM_AVAILABLE
        return {
            "active": active,
            "max": max_parallel,
            "mem_ok": mem_ok,
            "free": mem_ok and active < max_parallel,
            "pids": pids,
        }
    except Exception:
        return None


def _slots_opencode():
    """Vagas do opencode-ask: arquivos flock em /tmp (ou dir alternativo)."""
    try:
        raw = os.environ.get("OPENCODE_ASK_SLOTS")
        max_slots = 2
        if raw is not None:
            try:
                value = int(raw)
            except ValueError:
                value = 0
            if 1 <= value <= 16:
                max_slots = value
        base_dir = os.environ.get("WORKER_ASK_OPENCODE_VAGA_DIR") or "/tmp"
        uid = os.getuid()
        busy = 0
        pids = []
        for i in range(1, max_slots + 1):
            path = os.path.join(base_dir, f"opencode-ask-{uid}.vaga{i}")
            ocupada, dono = _vaga_estado(path)
            if not ocupada:
                continue
            busy += 1
            if dono is not None:
                pids.append(dono)
        return {"active": busy, "max": max_slots, "free": busy < max_slots, "pids": pids}
    except Exception:
        return None


def _vaga_estado(path):
    """(ocupada, pid) da vaga: flock exclusivo tomado e pid do último carimbo."""
    # Inexistente = livre (não cria); demais erros de abertura propagam
    # para a sonda devolver None (desconhecido).
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return False, None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True, _pid_do_carimbo(fd)
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        return False, None
    finally:
        os.close(fd)


def _pid_do_carimbo(fd):
    """Pid da última linha carimbada na vaga; ausente ou ilegível é None."""
    try:
        fim = os.fstat(fd).st_size
        campos = os.pread(fd, 64, max(0, fim - 64)).decode(errors="replace").split()
    except OSError:
        return None
    for campo in reversed(campos):
        if campo.isdigit():
            return int(campo)
    return None


def _cache_until(cache, name, now):
    """Epoch futuro do cache do roteador para o nome, ou None."""
    if not isinstance(cache, dict):
        return None
    entry = cache.get(name)
    if not isinstance(entry, dict) or not _numero(entry.get("until")):
        return None
    until = float(entry["until"])
    return until if until > now else None


def describe(table, cache, now=None):
    """Resumo por worker para o --status: precedência cooldown > cota > ocupado > ok."""
    now = time.time() if now is None else now
    status = {}
    for name in WORKERS:
        cooldown = _cache_until(cache, name, now)
        quota = None if cooldown is not None else blocked_until(table, name, now)
        probe = slots(name)
        if cooldown is not None:
            reason, available = "cooldown", False
        elif quota is not None:
            reason, available = "cota", False
        elif isinstance(probe, dict) and not probe.get("free"):
            reason, available = "ocupado", False
        else:
            reason, available = "ok", True
        status[name] = {
            "available": available,
            "reason": reason,
            "until": cooldown if cooldown is not None else quota,
            "slots": probe,
        }
    return status
