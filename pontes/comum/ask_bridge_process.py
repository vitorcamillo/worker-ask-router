"""Bounded native I/O; trusted synchronous callbacks must impose their own I/O timeout."""
import codecs
import math
import os
import selectors
import signal
import subprocess
import sys
import time


class FalhaStreaming(ValueError):
    """A bounded native stream violated its contract."""


def executar_limitado(cmd, *, cwd, env, timeout, entrada=None, por_linha=None,
                     pulso=None, tick=None, max_stdout=16 * 1024 * 1024,
                     max_stderr=1024 * 1024, max_linha=4 * 1024 * 1024,
                     max_input=4 * 1024 * 1024, capturar_stdout=True):
    if type(capturar_stdout) is not bool:
        raise FalhaStreaming("captura inválida")
    if not isinstance(cmd, list) or not cmd or any(
            not isinstance(x, str) or "\0" in x for x in cmd):
        raise FalhaStreaming("argv inválido")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or \
            not math.isfinite(timeout) or not 0 < timeout <= 3600:
        raise FalhaStreaming("prazo inválido")
    for limite in (max_stdout, max_stderr, max_linha, max_input):
        if type(limite) is not int or limite <= 0:
            raise FalhaStreaming("limite inválido")
    try:
        if entrada is not None and not isinstance(entrada, str):
            raise FalhaStreaming("entrada inválida")
        input_bytes = entrada.encode("utf-8", "strict") if entrada is not None else b""
    except UnicodeError as erro:
        raise FalhaStreaming("UTF-8 inválido") from erro
    if len(input_bytes) > max_input:
        raise FalhaStreaming("limite de entrada excedido")
    for callback in (por_linha, pulso, tick):
        if callback is not None and not callable(callback):
            raise FalhaStreaming("callback inválido")
    p = subprocess.Popen(cmd, cwd=cwd, env=env, shell=False, start_new_session=True,
                         stdin=subprocess.PIPE if entrada is not None else subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    selector = selectors.DefaultSelector()
    buffers = {"stdout": [], "stderr": []}
    counts = {"stdout": 0, "stderr": 0}
    limits = {"stdout": max_stdout, "stderr": max_stderr}
    decoders = {name: codecs.getincrementaldecoder("utf-8")("strict") for name in buffers}
    pending = bytearray()
    input_pos = 0
    started = time.monotonic()
    deadline = started + timeout
    next_tick, next_pulse = started, started + 1
    exited_at = None

    def close_pipe(pipe):
        try:
            selector.unregister(pipe)
        except KeyError:
            pass
        pipe.close()

    def deliver_record(record):
        if len(record) > max_linha:
            raise FalhaStreaming("limite de linha excedido")
        try:
            text = record.decode("utf-8", "strict")
        except UnicodeError as erro:
            raise FalhaStreaming("UTF-8 inválido") from erro
        if por_linha is not None:
            por_linha(text)

    try:
        for name in buffers:
            pipe = getattr(p, name)
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, name)
        if p.stdin is not None:
            if input_bytes:
                os.set_blocking(p.stdin.fileno(), False)
                selector.register(p.stdin, selectors.EVENT_WRITE, "stdin")
            else:
                p.stdin.close()
        while True:
            now = time.monotonic()
            # WNOWAIT retains the leader until group cleanup, preventing PID reuse.
            exited = os.waitid(os.P_PID, p.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            rc = None if exited is None else (
                exited.si_status if exited.si_code == os.CLD_EXITED else -exited.si_status)
            if now >= deadline:
                if rc is None:
                    raise subprocess.TimeoutExpired(cmd, timeout)
                break
            if now >= next_tick:
                if tick is not None:
                    tick()
                next_tick = now + .25
            if now >= next_pulse:
                if pulso is not None:
                    pulso()
                next_pulse = now + 1
            if rc is not None:
                if exited_at is None:
                    exited_at = now
                if not selector.get_map() or now - exited_at >= .25:
                    break
            for key, _ in selector.select(min(.05, max(0, deadline - now))):
                pipe, name = key.fileobj, key.data
                if name == "stdin":
                    try:
                        input_pos += os.write(pipe.fileno(), input_bytes[input_pos:input_pos + 65536])
                    except BrokenPipeError:
                        close_pipe(pipe)
                        continue
                    except BlockingIOError:
                        continue
                    if input_pos == len(input_bytes):
                        close_pipe(pipe)
                    continue
                try:
                    data = os.read(pipe.fileno(), 65536)
                except BlockingIOError:
                    continue
                if not data:
                    try:
                        decoders[name].decode(b"", final=True)
                    except UnicodeError as erro:
                        raise FalhaStreaming("UTF-8 inválido") from erro
                    if name == "stdout" and pending:
                        deliver_record(bytes(pending))
                        pending.clear()
                    close_pipe(pipe)
                    continue
                counts[name] += len(data)
                if counts[name] > limits[name]:
                    raise FalhaStreaming("limite de saída excedido")
                try:
                    decoders[name].decode(data)
                except UnicodeError as erro:
                    raise FalhaStreaming("UTF-8 inválido") from erro
                if name != "stdout" or capturar_stdout:
                    buffers[name].append(data)
                if name == "stdout":
                    pending.extend(data)
                    while True:
                        end = pending.find(b"\n")
                        if end < 0:
                            break
                        deliver_record(bytes(pending[:end + 1]))
                        del pending[:end + 1]
                    if len(pending) > max_linha:
                        raise FalhaStreaming("limite de linha excedido")
        # A child holding inherited pipes must not turn its leader's exit into a hang.
        if pending:
            deliver_record(bytes(pending))
        try:
            stdout = b"".join(buffers["stdout"]).decode("utf-8", "strict")
            stderr = b"".join(buffers["stderr"]).decode("utf-8", "strict")
        except UnicodeError as erro:
            raise FalhaStreaming("UTF-8 inválido") from erro
        return subprocess.CompletedProcess(cmd, rc, stdout, stderr)
    finally:
        original_error = sys.exc_info()[0] is not None
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            p.wait(timeout=1)
        except subprocess.TimeoutExpired:
            if not original_error:
                raise FalhaStreaming("encerramento do processo não confirmado") from None
            print("[bridge-process] cleanup_pending", file=sys.stderr)
        finally:
            selector.close()
            for pipe in (p.stdin, p.stdout, p.stderr):
                if pipe is not None and not pipe.closed:
                    pipe.close()
