#!/usr/bin/env python3
"""Apresentação em stderr; resposta e stdin seguem diretamente ao worker.

Contrato: o stdout do wrapper chega byte a byte ao chamador, o código de saída é
preservado e pedido/spec/stdin nunca são lidos (da spec só o nome aparece).
O worker-ask envia eventos estruturados por um pipe dedicado
(``ASK_PRESENTATION_FD=fd:inode``); eles mudam só a forma de exibir.

Personalização:
  ASK_PRESENTATION=auto|plain|json|off  auto: cor e linha viva em TTY.
  ASK_STYLE=compact|full|plain          (alias WORKER_ASK_STYLE) nível de detalhe.
  ASK_PRESENTATION_INTERVAL=N           batimento fora de TTY, 1..3600 s (60).
  NO_COLOR                              desliga cor; fora de TTY nunca há cor.
"""

import argparse
import json
import math
import os
import queue
import re
import select
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import unicodedata

ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]")
PROVIDERS = {
    "codex": "Codex",
    "claude": "Claude",
    "muse": "Muse",
    "opencode": "OpenCode",
    "zcode": "ZCode",
    "bonsai": "Bonsai",
    "worker": "Workers",
}
EXECUTORS = {
    "zcode-worker": "ZCode",
    "codex-terra": "Codex Terra",
    "muse-ask": "Muse",
    "opencode-ask": "OpenCode",
    "codex-ask": "Codex",
    "claude-ask": "Claude",
}
MODE_LABELS = {
    "write": "escrita",
    "edit": "escrita",
    "plan": "leitura",
    "review": "revisão",
    "consulta": "consulta",
}
REASONS = {"cooldown": "em espera", "ausente": "ausente", "cota": "cota", "timeout": "timeout"}
DIRECTIVE_PREFIX = b"worker-ask: timeout sem entrega"
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
COLORS = {"head": "1", "ok": "32", "err": "31", "warn": "33", "dim": "2", "directive": "1;33"}
PASSTHROUGH = ("-h", "--help", "--list-models", "--_background-child", "--status")
DEFAULT_INTERVAL = 60.0
DRAIN_TIMEOUT = 5.0
INTERRUPTED_DRAIN_TIMEOUT = 2.0


def sanitize(value, limit=80):
    text = ANSI.sub("", str(value or ""))
    return "".join(c for c in text if not unicodedata.category(c).startswith("C"))[
        :limit
    ]


def _parse_interval():
    try:
        value = float(os.environ.get("ASK_PRESENTATION_INTERVAL", DEFAULT_INTERVAL))
        return value if math.isfinite(value) and 1 <= value <= 3600 else DEFAULT_INTERVAL
    except ValueError:
        return DEFAULT_INTERVAL


def _mode_default(provider):
    if provider.startswith("worker"):
        return "write"
    if provider.startswith("codex"):
        return "review"
    if provider.startswith("zcode"):
        return "edit"
    return "consulta" if provider.startswith("bonsai") else "plan"


def _styles():
    """Devolve (saída, detalhe): saída em auto|plain|json|off, detalhe em compact|full."""
    output = os.environ.get("ASK_PRESENTATION", "auto").lower()
    if output not in ("auto", "plain", "json", "off"):
        output = "auto"
    detail = (
        os.environ.get("ASK_STYLE") or os.environ.get("WORKER_ASK_STYLE") or "compact"
    ).lower()
    if detail == "plain":
        output = "plain" if output == "auto" else output
    return output, ("full" if detail == "full" else "compact")


def fmt_duration(seconds):
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def fmt_limit(seconds):
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(value) or value <= 0:
        return ""
    if value >= 60 and value % 60 == 0:
        return f"{int(value // 60)}min"
    return fmt_duration(value)


def fmt_tokens(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return sanitize(value, 12)
    if number < 1000:
        return str(number)
    if number < 1_000_000:
        return f"{number / 1000:.1f}k"
    return f"{number / 1_000_000:.1f}M"


def short_path(path, limit=40):
    path = os.path.abspath(path)
    home = os.path.expanduser("~")
    if home not in ("", "/") and (path == home or path.startswith(home + os.sep)):
        path = "~" + path[len(home):]
    if len(path) <= limit:
        return sanitize(path, limit)
    parts = [p for p in path.split(os.sep) if p]
    tail = os.sep.join(parts[-2:]) if len(parts) >= 2 else path
    if len(tail) > limit - 2:
        tail = parts[-1]
    return sanitize(("~/…/" if path.startswith("~") else "…/") + tail, limit + 4)


def _options(args, consume_log=False):
    """Extrai campos visuais; nunca lê pedido, spec ou stdin."""
    view = ("--cwd", "--mode", "--model", "--spec", "--timeout", "--orchestrator", "--allowed-file")
    consumed = ("--title", "--log") if consume_log else ("--title",)
    fields, forwarded = {"allowed-file": []}, []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            forwarded.extend(args[i:])
            break
        name, sep, value = arg.partition("=")
        if name in view or name in consumed:
            if not sep and i + 1 < len(args) and not args[i + 1].startswith("-"):
                value = args[i + 1]
                i += 1
                if name not in consumed:
                    forwarded.extend((arg, value))
            elif sep and name not in consumed:
                forwarded.append(arg)
            elif not sep and name not in consumed:
                forwarded.append(arg)
            if name == "--allowed-file":
                fields["allowed-file"].append(value)
            else:
                fields[name[2:]] = value
        else:
            forwarded.append(arg)
        i += 1
    return fields, forwarded


def _sensitive_name(name):
    low = name.lower()
    return (
        low == ".env"
        or low.startswith(".env.")
        or low == "auth.json"
        or "cred" in low
        or low.endswith((".pem", ".key"))
        or low.startswith("id_")
    )


def open_log(path):
    """Abre o log como `>`: arquivo regular, sem symlink final, modo 0600."""
    if not path or any(ord(c) < 32 for c in path) or _sensitive_name(os.path.basename(path)):
        raise OSError("caminho de --log recusado")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NONBLOCK
    fd = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0), 0o600)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError("--log deve ser arquivo regular")
    os.set_blocking(fd, True)
    # Arquivo preexistente não herda o modo da criação; fecha em 0600 antes de gravar.
    os.fchmod(fd, 0o600)
    os.ftruncate(fd, 0)
    return fd


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("escrita no log sem progresso")
        view = view[written:]


class View:
    """Escreve no stderr sem misturar linhas do filho com a linha viva."""

    def __init__(self, output, color, live, log_fd):
        self.output, self.color, self.live, self.log_fd = output, color, live, log_fd
        self.lock = threading.Lock()
        self.live_text = ""
        self.live_shown = False
        self.partial = False
        self.log_error = None
        self._log_ends_with_newline = True
        self.stdout_cancel = threading.Event()

    def _paint(self, text, kind):
        return f"\033[{COLORS[kind]}m{text}\033[0m" if self.color and kind in COLORS else text

    def _log(self, data):
        if self.log_fd is not None:
            try:
                _write_all(self.log_fd, data)
            except OSError as exc:
                if self.log_error is None:
                    self.log_error = exc
            else:
                if data:
                    self._log_ends_with_newline = data.endswith(b"\n")

    def _clear_live(self):
        if self.live_shown:
            sys.stderr.write("\r\033[2K")
            self.live_shown = False

    def line(self, text, kind=None):
        with self.lock:
            self._clear_live()
            sys.stderr.write(self._paint(text, kind) + "\n")
            sys.stderr.flush()
            self._log((text + "\n").encode("utf-8", "replace"))
            self._draw()

    def set_live(self, text):
        with self.lock:
            self.live_text = text
            self._draw()

    def stop_live(self):
        with self.lock:
            self._clear_live()
            self.live_text = ""
            sys.stderr.flush()

    def _draw(self):
        if not self.live or not self.live_text or self.partial:
            return
        width = 80
        try:
            width = os.get_terminal_size(sys.stderr.fileno()).columns
        except OSError:
            pass
        text = self.live_text[: max(10, width - 1)]
        sys.stderr.write("\r\033[2K" + self._paint(text, "dim"))
        sys.stderr.flush()
        self.live_shown = True

    def child_err(self, data):
        with self.lock:
            self._clear_live()
            shown = data
            if self.color and data.startswith(DIRECTIVE_PREFIX):
                body = data.rstrip(b"\n")
                shown = b"\033[" + COLORS["directive"].encode() + b"m" + body + b"\033[0m" + data[len(body):]
            try:
                sys.stderr.flush()
                sys.stderr.buffer.write(shown)
                sys.stderr.buffer.flush()
            finally:
                self._log(data)
            self.partial = not data.endswith(b"\n")
            self._draw()

    def child_out(self, data):
        try:
            try:
                stdout_fd = sys.stdout.fileno()
                stdout_mode = os.fstat(stdout_fd).st_mode
            except (AttributeError, OSError, ValueError):
                # Streams artificiais usados por callers e testes não têm
                # necessariamente um descritor que possamos aguardar.
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
                return not self.stdout_cancel.is_set()

            if stat.S_ISREG(stdout_mode):
                # Reabrir um regular cria um offset independente; manter o
                # buffer original preserva o offset compartilhado com o shell.
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
                return not self.stdout_cancel.is_set()

            writer = None
            owned_fd = None
            try:
                if stat.S_ISSOCK(stdout_mode):
                    owned_fd = os.dup(stdout_fd)
                    writer = socket.socket(fileno=owned_fd)
                elif stat.S_ISFIFO(stdout_mode) or (stat.S_ISCHR(stdout_mode) and os.isatty(stdout_fd)):
                    flags = os.O_WRONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
                    if stat.S_ISCHR(stdout_mode):
                        flags |= getattr(os, "O_NOCTTY", 0)
                    owned_fd = os.open(f"/proc/self/fd/{stdout_fd}", flags)
                else:
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
                    return not self.stdout_cancel.is_set()

                remaining = memoryview(data)
                while remaining and not self.stdout_cancel.is_set():
                    try:
                        if writer is not None:
                            written = writer.send(remaining, socket.MSG_DONTWAIT)
                        else:
                            written = os.write(owned_fd, remaining)
                    except BlockingIOError:
                        if writer is not None:
                            select.select([], [writer], [], 0.2)
                        else:
                            select.select([], [owned_fd], [], 0.2)
                        continue
                    if written <= 0:
                        raise OSError("escrita no stdout sem progresso")
                    remaining = remaining[written:]
            finally:
                if writer is not None:
                    writer.close()
                elif owned_fd is not None:
                    os.close(owned_fd)
        finally:
            with self.lock:
                self._log(data)
        return not self.stdout_cancel.is_set()

    def cancel_stdout(self):
        self.stdout_cancel.set()


def _relay(fd, sink, line_mode, done=None):
    """Repasse de bytes; em modo linha, quebra só em fronteiras de linha."""
    pending = b""
    try:
        while True:
            ready, _, _ = select.select([fd], [], [], 0.3)
            if not ready:
                if done is not None and done.is_set():
                    break
                if pending:
                    sink(pending)
                    pending = b""
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            if not line_mode:
                if sink(chunk) is False:
                    break
                continue
            pending += chunk
            head, sep, pending = pending.rpartition(b"\n")
            if sep:
                for item in (head + sep).splitlines(keepends=True):
                    sink(item)
    except BaseException:
        os.close(fd)
        raise
    try:
        if pending:
            sink(pending)
    finally:
        os.close(fd)


def _read_events(fd, events, done=None):
    buffer = b""
    try:
        while True:
            ready, _, _ = select.select([fd], [], [], 0.3)
            if not ready:
                if done is not None and done.is_set():
                    break
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            buffer += chunk
            *lines, buffer = buffer.split(b"\n")
            for raw in lines:
                if len(raw) > 8192:
                    continue
                try:
                    item = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(item, dict) and isinstance(item.get("event"), str):
                    events.put(item)
            if len(buffer) > 65536:
                buffer = b""
    except OSError:
        pass
    finally:
        os.close(fd)


class Run:
    """Estado da execução, derivado só de argumentos e eventos."""

    def __init__(self, provider, fields):
        self.provider = provider
        self.is_worker = provider.startswith("worker")
        self.label = PROVIDERS.get(provider.split("-")[0], sanitize(provider))
        self.mode = sanitize(fields.get("mode") or _mode_default(provider), 20)
        self.mode_label = MODE_LABELS.get(self.mode, self.mode)
        explicit = fields.get("title", os.environ.get("ASK_TITLE", ""))
        self.title = sanitize(explicit)
        self.cwd = fields.get("cwd") or os.getcwd()
        self.project = sanitize(os.path.basename(os.path.normpath(self.cwd)))
        self.model = sanitize(fields.get("model", ""))
        self.spec = sanitize(os.path.basename(fields.get("spec") or ""), 60)
        self.limit = fmt_limit(fields.get("timeout"))
        self.orchestrator = sanitize(fields.get("orchestrator", ""), 20)
        self.allowed = [sanitize(a, 120) for a in fields.get("allowed-file", [])]
        self.executor = ""
        self.executor_model = ""
        self.attempts = []
        self.usage = {}
        self.stop_detail = ""

    def exec_label(self):
        if not self.executor:
            return self.label
        return f"{self.executor} ({self.executor_model})" if self.executor_model else self.executor

    def header(self, detail):
        parts = [self.label]
        if self.title:
            parts.append(self.title)
        parts.append(self.mode_label)
        parts.append(short_path(self.cwd))
        if not self.is_worker:
            parts.append(self.model or "modelo nativo")
        if self.allowed:
            parts.append(f"{len(self.allowed)} arquivo{'s' if len(self.allowed) != 1 else ''}")
        if self.spec:
            parts.append(self.spec)
        if self.limit:
            parts.append(f"limite {self.limit}")
        lines = ["◆ " + " · ".join(parts)]
        if detail == "full":
            extra = []
            if self.orchestrator:
                extra.append(f"orquestrador {self.orchestrator}")
            if self.is_worker and self.model:
                extra.append(f"modelo {self.model}")
            if self.allowed:
                extra.append("arquivos: " + ", ".join(self.allowed))
            if extra:
                lines.append("  " + " · ".join(extra))
        return lines

    def tokens(self, detail):
        if "input" not in self.usage and "output" not in self.usage:
            return ""
        text = f"{fmt_tokens(self.usage.get('input', '?'))}→{fmt_tokens(self.usage.get('output', '?'))} tokens"
        if detail == "full":
            cache = [f"{k} {fmt_tokens(self.usage[key])}" for k, key in (("cache leitura", "cacheRead"), ("cache escrita", "cacheWrite")) if key in self.usage]
            text += "".join(" · " + c for c in cache)
        return text

    def trail(self):
        return " → ".join(f"{name} {why}" for name, why in self.attempts)

    def summary(self, rc, duration, interrupted, detail):
        dur = fmt_duration(duration)
        if rc == 0:
            parts = [f"✓ {self.exec_label()}", dur]
            tokens = self.tokens(detail)
            if tokens:
                parts.append(tokens)
            parts.append("validação pendente")
            kind = "ok"
        else:
            if interrupted or rc in (143, 144, 130):
                what = "interrompido"
            elif rc == 124:
                what = "tempo limite"
            elif rc == 75:
                what = "sem worker disponível" if self.is_worker else "limite de cota"
            else:
                what = "falha"
            parts = [f"× {what} (rc {rc})"]
            if self.executor or not self.is_worker:
                parts.append(self.exec_label())
            if self.stop_detail and self.stop_detail not in ("falha", "interrompido"):
                parts.append(self.stop_detail)
            parts.append(dur)
            kind = "err"
        lines = [" · ".join(parts)]
        if self.attempts and (detail == "full" or any(why != "✓" for _, why in self.attempts)):
            lines.append("  tentativas: " + self.trail())
        if detail == "full" and self.usage.get("session"):
            lines.append(f"  sessão {sanitize(self.usage['session'], 64)}")
        return lines, kind


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description="Apresentação comum dos wrappers ask.")
    parser.add_argument("--provider", default="ask")
    if "--" not in argv:
        parser.parse_args(argv)
        parser.error("informe -- WRAPPER [ARGS]")
    split = argv.index("--")
    provider = parser.parse_args(argv[:split]).provider
    if len(argv) <= split + 1:
        parser.error("wrapper ausente")
    wrapper, raw_args = argv[split + 1], argv[split + 2 :]
    passthrough = any(a in PASSTHROUGH for a in raw_args)
    fields, args = _options(raw_args, consume_log=provider.startswith("worker") and not passthrough)
    command = (
        [sys.executable, wrapper, *args]
        if wrapper.endswith(".py")
        else [wrapper, *args]
    )
    active = os.environ.get("ASK_PRESENTATION_ACTIVE") == "1"
    os.environ["ASK_PRESENTATION_ACTIVE"] = "1"
    os.environ.pop("ASK_PRESENTATION_FD", None)
    output, detail = _styles()
    log_path = fields.get("log")
    if active or passthrough or (output == "off" and log_path is None):
        os.execv(command[0], command)

    run = Run(provider, fields)
    tty = sys.stderr.isatty() and os.environ.get("TERM") != "dumb"
    color = output == "auto" and tty and "NO_COLOR" not in os.environ
    live = output == "auto" and tty
    quiet = output == "off"
    log_fd = None
    if log_path is not None:
        try:
            log_fd = open_log(log_path)
        except OSError:
            print("worker-ask: não foi possível abrir --log (use um arquivo regular, sem symlink)", file=sys.stderr)
            return 2
    view = View(output, color, live, log_fd)
    started = time.monotonic()

    def emit(event, message, kind=None, **extra):
        if quiet:
            return
        if output == "json":
            view.line(json.dumps({
                "event": event,
                "provider": sanitize(provider),
                "project": run.project,
                "mode": run.mode,
                "model": run.model or ("seleção automática" if run.is_worker else "modelo nativo"),
                "title": run.title,
                "message": " | ".join(m.strip() for m in message) if isinstance(message, list) else message.strip(),
                **{k: v for k, v in extra.items() if v is not None},
            }, ensure_ascii=False))
        else:
            for text in message if isinstance(message, list) else [message]:
                view.line(text, kind)

    def finish(rc):
        if log_fd is not None:
            if view.log_error is not None and rc in (0, 75):
                rc = 1
            if not view._log_ends_with_newline:
                view._log(b"\n")
            view._log(f"exit={rc}\n".encode())
            try:
                os.close(log_fd)
            except OSError as exc:
                if view.log_error is None:
                    view.log_error = exc
            if view.log_error is not None:
                print("worker-ask: falha ao gravar --log; registro incompleto", file=sys.stderr)
                if rc in (0, 75):
                    rc = 1
        return rc

    if any(a == "--saida" or a.startswith("--saida=") for a in args):
        emit("submitted", f"◌ {run.label} · Enviado para segundo plano", "dim")
        os.execv(command[0], command)

    emit("start", run.header(detail), "head", allowed=len(run.allowed), spec=run.spec,
         limit=run.limit, cwd=short_path(run.cwd))

    events = queue.Queue()
    ev_read = ev_write = None
    pass_fds = ()
    env = dict(os.environ)
    if run.is_worker:
        ev_read, ev_write = os.pipe()
        env["ASK_PRESENTATION_FD"] = f"{ev_write}:{os.fstat(ev_write).st_ino}"
        pass_fds = (ev_write,)
    capture = live or log_fd is not None
    try:
        proc = subprocess.Popen(
            command,
            start_new_session=True,
            env=env,
            pass_fds=pass_fds,
            stdout=subprocess.PIPE if log_fd is not None else None,
            stderr=subprocess.PIPE if capture else None,
        )
    except OSError:
        for fd in (ev_read, ev_write):
            if fd is not None:
                os.close(fd)
        emit("end", "× não foi possível iniciar a execução (rc 1)", "err", exit=1, duration=0)
        return finish(1)
    threads = []
    relay_done = threading.Event()

    def run_relay(fd, sink, line_mode):
        try:
            _relay(fd, sink, line_mode, relay_done)
        except OSError as exc:
            with view.lock:
                if view.log_error is None:
                    view.log_error = exc

    if ev_write is not None:
        os.close(ev_write)
        threads.append(threading.Thread(target=_read_events, args=(ev_read, events, relay_done), daemon=True))
    if proc.stdout is not None:
        threads.append(threading.Thread(target=run_relay, args=(os.dup(proc.stdout.fileno()), view.child_out, False), daemon=True))
        proc.stdout.close()
    if proc.stderr is not None:
        threads.append(threading.Thread(target=run_relay, args=(os.dup(proc.stderr.fileno()), view.child_err, True), daemon=True))
        proc.stderr.close()
    for thread in threads:
        thread.start()

    interrupted = [None, None]

    def forward(signum, _frame):
        interrupted[:] = [signum, time.monotonic()]
        view.cancel_stdout()
        try:
            os.killpg(proc.pid, signal.SIGKILL if proc.poll() is not None else signum)
        except ProcessLookupError:
            pass

    previous = {
        sig: signal.signal(sig, forward)
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    }

    def handle(item):
        kind = item.get("event")
        name = sanitize(item.get("executor", ""), 40)
        label = EXECUTORS.get(name, name)
        if kind == "executor_start":
            run.executor = label
            run.executor_model = sanitize(item.get("model", ""), 40)
            if not live:
                emit("executor_start", f"  ↳ {run.exec_label()}", "dim", executor=label)
        elif kind == "skip":
            why = REASONS.get(item.get("reason"), sanitize(item.get("reason", ""), 20))
            run.attempts.append((label, why))
            if detail == "full":
                emit("skip", f"  · {label} {why}", "dim", executor=label, reason=why)
        elif kind == "fallback":
            why = REASONS.get(item.get("reason"), sanitize(item.get("reason", ""), 20))
            run.attempts.append((label, f"{why} (rc {sanitize(item.get('rc', '?'), 4)})"))
            emit("fallback", f"  ↷ {label}: {why}, tentando o próximo", "warn", executor=label, reason=why)
        elif kind == "result":
            run.attempts.append((label, "✓"))
            usage = item.get("usage")
            if isinstance(usage, dict):
                run.usage = {sanitize(k, 20): sanitize(v, 64) for k, v in usage.items()}
        elif kind == "stop":
            run.stop_detail = sanitize(item.get("detail", ""), 60)
            if name:
                run.executor = label

    interval = _parse_interval()
    next_notice = time.monotonic() + interval
    frame = 0
    try:
        while True:
            rc = proc.poll()
            if rc is not None:
                break
            try:
                # Espera eventos em vez de dormir: "↳ executor" aparece na hora.
                handle(events.get(timeout=0.2))
                while not events.empty():
                    handle(events.get_nowait())
            except queue.Empty:
                pass
            now = time.monotonic()
            if interrupted[1] is not None and now - interrupted[1] >= 1:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if live and interrupted[0] is None:
                frame += 1
                view.set_live(f"{SPINNER[frame % len(SPINNER)]} {run.exec_label()} · {fmt_duration(now - started)}")
            elif now >= next_notice and interrupted[0] is None:
                emit("heartbeat", f"  … {run.exec_label()} · {fmt_duration(now - started)}", "dim",
                     duration=round(now - started, 2))
                next_notice = now + interval
    finally:
        if interrupted[0] is not None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    # O processo principal terminou: em sucesso, preservar trabalhos
    # em segundo plano do mesmo grupo; só limpar o grupo quando a
    # execução falhou ou foi interrompida. Nesse caso relay_done
    # encerra as threads; em sucesso, não sinalizar: a drenagem espera
    # o EOF dos pipes em sucesso, preservando toda a saída tardia
    # de quem os mantém abertos (fechar o fd aqui perderia a saída e
    # daria SIGPIPE). Os handlers seguem instalados durante a drenagem
    # para encaminhar SIGINT/SIGTERM/SIGHUP ao grupo; restaurar antes
    # deixaria um sinal matar só a apresentação e órfão o descendente
    # que ainda segura o pipe.
    try:
        def join_relay(thread, timeout):
            try:
                thread.join(timeout=timeout)
            except InterruptedError:
                # Um sinal pode interromper a espera no thread principal; o
                # handler já registrou a interrupção e o laço reavalia o prazo.
                pass

        if interrupted[0] is not None or rc != 0:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            relay_done.set()
        deadline = time.monotonic() + DRAIN_TIMEOUT
        for thread in threads:
            while rc == 0 and interrupted[0] is None and thread.is_alive():
                join_relay(thread, 0.2)
            join_relay(thread, 0.0 if interrupted[0] is not None else max(0.0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in threads):
            if interrupted[0] is not None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
            relay_done.set()
            grace = time.monotonic() + 1.0
            for thread in threads:
                join_relay(thread, max(0.0, grace - time.monotonic()))
        # Após relay_done + grace, quem segue vivo está bloqueado escrevendo
        # no sink (a leitura encerra via relay_done em até 0,3 s). Finalizar
        # o interpretador ou fechar o log com essa escrita em voo aborta o
        # Python (_enter_buffered_busy), perde bytes e destrói o rc:
        # aguardar o repasse terminar (o leitor drenar ou fechar o pipe).
        final_drain_deadline = None
        while any(thread.is_alive() for thread in threads):
            if interrupted[0] is not None:
                if final_drain_deadline is None:
                    final_drain_deadline = time.monotonic() + INTERRUPTED_DRAIN_TIMEOUT
                remaining = final_drain_deadline - time.monotonic()
                if remaining <= 0:
                    # Evita finalizar o interpretador com uma thread daemon
                    # presa em escrita bufferizada (fatal _enter_buffered_busy).
                    os._exit(128 + interrupted[0])
                timeout = min(0.5, remaining)
            else:
                timeout = 0.5
            for thread in threads:
                join_relay(thread, timeout)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    while not events.empty():
        handle(events.get_nowait())
    view.stop_live()
    rc = (
        128 + interrupted[0]
        if interrupted[0] is not None
        else 128 - rc
        if rc < 0
        else rc
    )
    if view.log_error is not None and rc in (0, 75):
        rc = 1
    duration = time.monotonic() - started
    lines, kind = run.summary(rc, duration, interrupted[0] is not None, detail)
    emit("end", lines, kind, exit=rc, duration=round(duration, 3),
         executor=run.executor or None, usage=run.usage or None)
    return finish(rc)


if __name__ == "__main__":
    raise SystemExit(main())
