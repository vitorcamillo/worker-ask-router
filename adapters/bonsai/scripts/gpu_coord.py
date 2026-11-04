#!/usr/bin/env python3
"""Coordenação GPU: jogo > Whisper > Bonsai.

Lease exclusivo: pausa o Bonsai, usa a GPU e restaura somente se ele estava
ativo, sem GameMode e sem cancelamento manual (`bonsai-server stop` grava
cancel-token). Estado em ~/.local/state/bonsai: whisper.lock (flock) e
whisper-phase.json (atômico 0600); fase "whisper" bloqueia o Bonsai,
"restoring" com lock held não bloqueia mas impede outro Whisper; marcador
sem lock só é aceito com PID vivo. Erros de arquivo/DBus: fail closed. Sem
segredos; injeção de dependências só para testes; sem flag de ambiente.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

STATE_DIR = Path.home() / ".local" / "state" / "bonsai"
LOCK_PATH = STATE_DIR / "whisper.lock"
PHASE_PATH = STATE_DIR / "whisper-phase.json"
CANCEL_PATH = STATE_DIR / "cancel-token"
PHASE_WHISPER = "whisper"
PHASE_RESTORING = "restoring"
RUNNING_STATES = ("active", "activating")
STOPPED_STATES = ("inactive", "failed")

STOP_BONSAI_ARGV = ["systemctl", "--user", "stop", "bonsai-llama.service"]
START_BONSAI_ARGV = ["systemctl", "--user", "start", "bonsai-llama.service"]
BONSAI_STATE_ARGV = [
    "systemctl", "--user", "show", "bonsai-llama.service",
    "--property=ActiveState,MainPID",
]
CMD_TIMEOUT_S = 10.0
LOCK_RETRY_WINDOW_S = 0.05
LOCK_RETRY_INTERVAL_S = 0.005

MSG_BUSY = "Whisper já está usando a GPU; aguarde a liberação."
MSG_STATE = "Estado do Whisper ilegível; GPU não liberada."
MSG_GAMEMODE_ACTIVE = "GameMode ativo; Whisper não pode usar a GPU agora."
MSG_GAMEMODE_UNKNOWN = "Estado do GameMode desconhecido; GPU não liberada."
MSG_STOP = "Não foi possível pausar o Bonsai com segurança; GPU não liberada."
MSG_BONSAI_UNKNOWN = "Estado do Bonsai desconhecido; GPU não liberada."


def _log(message: str) -> None:
    sys.stderr.write(f"gpu-coord: {message}\n")
    sys.stderr.flush()


class Unavailable(Exception):
    """GPU indisponível: `code` estável e `message` PT-BR seguro (sem segredo)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _atomic_write(path: Path, data: bytes) -> None:
    """Substitui `path` atomicamente; conteúdo novo nasce 0600."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


_MISSING_PHASE = object()
_INVALID_PHASE = object()


def _read_phase(path: Path):
    try:
        texto = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _MISSING_PHASE
    except OSError:
        return _INVALID_PHASE
    try:
        return json.loads(texto)
    except (TypeError, ValueError):
        return _INVALID_PHASE


def _pid_alive(pid) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _phase_is_busy(phase, lock_busy: bool) -> bool:
    if phase is _MISSING_PHASE:
        return lock_busy
    if phase is _INVALID_PHASE or not isinstance(phase, dict):
        return True
    kind = phase.get("phase")
    if kind == PHASE_WHISPER:
        return lock_busy
    if kind == PHASE_RESTORING:
        return not _pid_alive(phase.get("pid"))
    return True


def _ler_token(path: Path):
    """None se ausente; em erro de leitura, valor que nunca iguala snapshot."""
    try:
        texto = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError:
        return uuid.uuid4().hex
    return texto or None


def whisper_busy(lock_path=None, phase_path=None) -> bool:
    """True só se o Whisper está de fato ocupando (ou prestes a ocupar) a GPU.

    Sem lock held => False para marcador "whisper" stale; "restoring" só libera
    com PID vivo. Lock held: "whisper" => True, "restoring" => False; fase
    ausente/corrompida => True. Erro => True.
    """
    lock_path = Path(lock_path) if lock_path else LOCK_PATH
    phase_path = Path(phase_path) if phase_path else PHASE_PATH
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return True
    lock_busy = False
    lock_acquired = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_busy = True
        except OSError:
            return True
        else:
            lock_acquired = True
            return False  # sem dono do lock, qualquer fase remanescente é obsoleta
        return _phase_is_busy(_read_phase(phase_path), lock_busy)
    finally:
        if lock_acquired:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


def cancel_resume(cancel_path=None) -> str:
    """Grava cancel-token (UUID, atômico 0600): proíbe restore do Bonsai."""
    cancel_path = Path(cancel_path) if cancel_path else CANCEL_PATH
    token = uuid.uuid4().hex
    _atomic_write(cancel_path, token.encode("utf-8"))
    return token


def _run_systemctl(argv):
    try:
        return subprocess.run(
            argv, timeout=CMD_TIMEOUT_S, check=False,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _stop_bonsai() -> bool:
    done = _run_systemctl(STOP_BONSAI_ARGV)
    return done is not None and done.returncode == 0


def _start_bonsai() -> bool:
    done = _run_systemctl(START_BONSAI_ARGV)
    return done is not None and done.returncode == 0


def _parse_bonsai_state(stdout):
    linhas = [linha.strip() for linha in (stdout or "").splitlines() if linha.strip()]
    if any("=" in linha for linha in linhas):
        propriedades = {}
        for linha in linhas:
            chave, separador, valor = linha.partition("=")
            if not separador or not chave or chave in propriedades:
                return None
            propriedades[chave] = valor
        estado = propriedades.get("ActiveState")
        pid_texto = propriedades.get("MainPID")
    elif len(linhas) == 2:
        estado, pid_texto = linhas
    else:
        return None

    if estado not in RUNNING_STATES + STOPPED_STATES:
        return None
    try:
        pid = int(pid_texto)
    except (TypeError, ValueError):
        return None
    if pid < 0:
        return None
    if estado in RUNNING_STATES and pid == 0:
        return None
    if estado in STOPPED_STATES and pid != 0:
        return None
    return estado


def _bonsai_state():
    """Lê ActiveState/MainPID e só aceita estados com PID coerente."""
    done = _run_systemctl(BONSAI_STATE_ARGV)
    if done is None or done.returncode != 0:
        return None
    return _parse_bonsai_state(done.stdout)


def _gamemode_status():
    """Import local: evita ciclo, pois game_guard consulta gpu_coord."""
    import game_guard

    return game_guard.check_gamemode()


class WhisperLease:
    """`with gpu_coord.WhisperLease() as lease:` — GPU do Whisper sob arbitragem.

    __enter__: GameMode ok; lock exclusivo (ocupado => whisper_busy); fase
    "whisper"; snapshot do cancel-token e do estado do Bonsai; stop do Bonsai
    (argv fixo, timeout 10) com confirmação de unidade parada; rechecagem do
    GameMode. Falha limpa lock/fase e levanta Unavailable, sem liberar GPU.
    `cancel_restore()` proíbe a restauração. __exit__ (chamado APÓS o filho
    estar morto) restaura em fase "restoring" (lock held) só se estava ativo,
    sem cancelamento e com GameMode ok; revalida ambos após o starter e para
    novamente se a condição mudou; nunca lança; sempre limpa.
    """

    def __init__(self, *, stopper=None, starter=None, state_checker=None,
                 gamemode_checker=None, state_dir=None):
        self._state_dir = Path(state_dir) if state_dir else STATE_DIR
        self._stopper = stopper or _stop_bonsai
        self._starter = starter or _start_bonsai
        self._state_checker = state_checker or _bonsai_state
        self._gamemode_checker = gamemode_checker or _gamemode_status
        self._lock_path = self._state_dir / "whisper.lock"
        self._phase_path = self._state_dir / "whisper-phase.json"
        self._cancel_path = self._state_dir / "cancel-token"
        self.cancelled = False
        self.was_active = False
        self._fd = None
        self._token = None

    def cancel_restore(self) -> None:
        self.cancelled = True

    def __enter__(self):
        self._exigir_gamemode_ok()
        self._acquire_lock()
        try:
            self._write_phase(PHASE_WHISPER)
            self._token = _ler_token(self._cancel_path)
            estado = self._state_checker()
            if estado not in RUNNING_STATES + STOPPED_STATES:
                raise Unavailable("bonsai_state_unknown", MSG_BONSAI_UNKNOWN)
            self.was_active = estado in RUNNING_STATES
            if self.was_active:
                if not self._stopper():
                    raise Unavailable("bonsai_stop_failed", MSG_STOP)
                if self._state_checker() not in STOPPED_STATES:
                    raise Unavailable("bonsai_stop_failed", MSG_STOP)
            self._exigir_gamemode_ok()
        except BaseException:
            self._cleanup()
            raise
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if not self.was_active:
                _log("Bonsai já estava parado; nada a restaurar")
            elif self.cancelled:
                _log("restore cancelado pelo job; Bonsai permanece parado")
            elif not self._restoration_allowed():
                _log("stop manual ou GameMode ativo/desconhecido; Bonsai não é restaurado")
            else:
                self._write_phase(PHASE_RESTORING)  # lock segue held
                started = self._starter()
                if not started:
                    _log("falha ao reativar o Bonsai")
                if not self._restoration_allowed():
                    _log("cancelamento ou GameMode detectado após restore; Bonsai é parado")
                    if not self._stopper():
                        _log("não consegui parar o Bonsai após cancelamento")
        except Exception as exc:
            _log(f"restauração interrompida ({type(exc).__name__})")
        finally:
            self._cleanup()
        return False

    def _exigir_gamemode_ok(self):
        status, _count = self._gamemode_checker()
        if status == "active":
            raise Unavailable("gamemode_active", MSG_GAMEMODE_ACTIVE)
        if status != "ok":
            raise Unavailable("gamemode_unknown", MSG_GAMEMODE_UNKNOWN)

    def _acquire_lock(self):
        try:
            self._lock_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            raise Unavailable("whisper_state_error", MSG_STATE)
        deadline = time.monotonic() + LOCK_RETRY_WINDOW_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                restante = deadline - time.monotonic()
                if restante <= 0:
                    os.close(fd)
                    raise Unavailable("whisper_busy", MSG_BUSY)
                time.sleep(min(LOCK_RETRY_INTERVAL_S, restante))
            except OSError:
                os.close(fd)
                raise Unavailable("whisper_state_error", MSG_STATE)
        self._fd = fd

    def _write_phase(self, fase: str):
        try:
            _atomic_write(
                self._phase_path,
                json.dumps({"phase": fase, "pid": os.getpid()}).encode("utf-8"),
            )
        except OSError:
            raise Unavailable("whisper_state_error", MSG_STATE)

    def _restoration_allowed(self) -> bool:
        if _ler_token(self._cancel_path) != self._token:
            return False
        try:
            status, _count = self._gamemode_checker()
        except Exception:
            return False
        return status == "ok"

    def _cleanup(self):
        """Remove fase, libera lock e fecha fd; nunca lança."""
        try:
            self._phase_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            _log("não removi o marcador de fase")
        fd, self._fd = self._fd, None
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass


USAGE = "uso: gpu_coord.py cancel-resume   impede lease do Whisper de restaurar o Bonsai\n"


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] != "cancel-resume":
        sys.stderr.write(USAGE)
        return 2
    try:
        cancel_resume()
    except OSError:
        _log("não consegui gravar o cancel-token")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
