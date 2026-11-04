"""Contratos de encaminhamento do stdout do worker."""

import fcntl
import importlib.util
import io
import os
from pathlib import Path
import pty
import select
import socket
import stat
import threading

import pytest

ASK_PATH = Path(__file__).resolve().parents[1] / "bin/ask_presentation.py"
spec = importlib.util.spec_from_file_location("ask_presentation_stdout", ASK_PATH)
ask = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ask)


class StdoutAdapter:
    def __init__(self, buffer):
        self.buffer = buffer

    def fileno(self):
        return self.buffer.fileno()


def send(view, monkeypatch, buffer):
    with monkeypatch.context() as scoped:
        scoped.setattr(ask.sys, "stdout", StdoutAdapter(buffer))
        return view.child_out(b"\x00worker\xff")


def test_pipe_preserves_bytes_blocking_and_closes_reopened_fd(monkeypatch):
    read_fd, write_fd = os.pipe()
    stream = os.fdopen(os.dup(write_fd), "wb")
    opened = []
    real_open = os.open

    def record_open(path, *args, **kwargs):
        fd = real_open(path, *args, **kwargs)
        if os.fspath(path).startswith("/proc/self/fd/"):
            opened.append(fd)
        return fd

    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(ask.os, "open", record_open)
            assert send(ask.View("plain", False, False, None), monkeypatch, stream)
        assert os.read(read_fd, 64) == b"\x00worker\xff"
        assert fcntl.fcntl(write_fd, fcntl.F_GETFL) & os.O_NONBLOCK == 0
        assert len(opened) == 1
        with pytest.raises(OSError):
            os.fstat(opened[0])
    finally:
        stream.close()
        os.close(read_fd)
        os.close(write_fd)


def test_regular_file_keeps_shared_offset(monkeypatch, tmp_path):
    path = tmp_path / "stdout"
    with path.open("w+b") as stream:
        stream.write(b"prefix:")
        with monkeypatch.context() as scoped:
            scoped.setattr(ask.sys, "stdout", StdoutAdapter(stream))
            ask.View("plain", False, False, None).child_out(b"payload")
        stream.write(b":tail")
        stream.flush()
        stream.seek(0)
        assert stream.read() == b"prefix:payload:tail"


def test_memory_buffer_without_fileno_preserves_bytes(monkeypatch):
    buffer = io.BytesIO()
    with monkeypatch.context() as scoped:
        scoped.setattr(ask.sys, "stdout", StdoutAdapter(buffer))
        ask.View("plain", False, False, None).child_out(b"\x00memory\xff")
    assert buffer.getvalue() == b"\x00memory\xff"


def test_socket_pair_preserves_data_and_original_blocking(monkeypatch):
    sender, receiver = socket.socketpair()
    try:
        was_blocking = sender.getblocking()
        assert send(ask.View("plain", False, False, None), monkeypatch, sender)
        assert receiver.recv(64) == b"\x00worker\xff"
        assert sender.getblocking() is was_blocking
        assert os.get_blocking(sender.fileno())
    finally:
        sender.close()
        receiver.close()


def test_cancel_unblocks_full_socket_without_peer_read(monkeypatch):
    sender, receiver = socket.socketpair()
    thread = None
    try:
        for _ in range(1024):
            try:
                sender.send(b"x" * 4096, socket.MSG_DONTWAIT)
            except BlockingIOError:
                break
        else:
            pytest.fail("socketpair nao ficou cheio dentro do limite")
        assert sender.getblocking()
        view = ask.View("plain", False, False, None)
        selecting = threading.Event()
        real_select = select.select

        def signaled_select(*args, **kwargs):
            selecting.set()
            return real_select(*args, **kwargs)

        with monkeypatch.context() as scoped:
            scoped.setattr(ask.sys, "stdout", StdoutAdapter(sender))
            scoped.setattr(ask.select, "select", signaled_select)
            thread = threading.Thread(target=view.child_out, args=(b"y" * 65536,), daemon=True)
            thread.start()
            assert selecting.wait(0.5)
            view.cancel_stdout()
            thread.join(1)
        assert not thread.is_alive()
        assert os.get_blocking(sender.fileno())
        assert sender.getblocking()
    finally:
        if thread is not None and thread.is_alive():
            view.cancel_stdout()
            thread.join(1)
        sender.close()
        receiver.close()


@pytest.mark.parametrize("kind", ["pipe", "socket"])
def test_cancel_before_send_logs_captured_data_without_log_error(monkeypatch, tmp_path, kind):
    log_path = tmp_path / "captured.log"
    log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT, 0o600)
    read_fd = write_fd = None
    sender = receiver = None
    buffer = None
    try:
        if kind == "pipe":
            read_fd, write_fd = os.pipe()
            buffer = os.fdopen(os.dup(write_fd), "wb")
        else:
            sender, receiver = socket.socketpair()
            buffer = sender
        view = ask.View("plain", False, False, log_fd)
        view.cancel_stdout()
        assert send(view, monkeypatch, buffer) is False
        os.close(log_fd)
        log_fd = None
        assert log_path.read_bytes() == b"\x00worker\xff"
        assert view.log_error is None
    finally:
        if log_fd is not None:
            os.close(log_fd)
        for fd in (read_fd, write_fd):
            if fd is not None:
                os.close(fd)
        if kind == "pipe" and buffer is not None:
            buffer.close()
        for sock in (sender, receiver):
            if sock is not None:
                sock.close()


def test_pty_forwards_bytes_and_preserves_flags(monkeypatch):
    stream = None
    try:
        master, slave = pty.openpty()
    except OSError as exc:
        pytest.skip(f"PTY indisponível neste sistema: {exc}")
    try:
        original_flags = fcntl.fcntl(slave, fcntl.F_GETFL)
        payload = b"pty-bytes"
        stream = os.fdopen(os.dup(slave), "wb")
        with monkeypatch.context() as scoped:
            scoped.setattr(ask.sys, "stdout", StdoutAdapter(stream))
            ask.View("plain", False, False, None).child_out(payload)
        assert select.select([master], [], [], 0.5)[0]
        assert os.read(master, len(payload)) == payload
        assert fcntl.fcntl(slave, fcntl.F_GETFL) == original_flags
    finally:
        if stream is not None:
            stream.close()
        os.close(master)
        os.close(slave)


def test_dev_null_non_tty_falls_back_to_buffer(monkeypatch):
    fd = os.open("/dev/null", os.O_WRONLY)
    buffer = os.fdopen(fd, "wb")
    try:
        assert not stat.S_ISFIFO(os.fstat(fd).st_mode)
        assert not os.isatty(fd)
        send(ask.View("plain", False, False, None), monkeypatch, buffer)
    finally:
        buffer.close()


@pytest.mark.parametrize("failure", ["open", "write", "zero"])
def test_stdout_errors_propagate_and_reopened_fd_closes(monkeypatch, failure):
    read_fd, write_fd = os.pipe()
    stream = os.fdopen(os.dup(write_fd), "wb")
    opened = []
    real_open = os.open

    def record_open(path, *args, **kwargs):
        fd = real_open(path, *args, **kwargs)
        if os.fspath(path).startswith("/proc/self/fd/"):
            opened.append(fd)
        return fd

    def fail_open(path, *args, **kwargs):
        if os.fspath(path).startswith("/proc/self/fd/"):
            raise OSError("open failed")
        return real_open(path, *args, **kwargs)

    def fail_write(fd, data):
        if failure == "zero":
            return 0
        raise OSError("write failed")

    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(ask.os, "open", fail_open if failure == "open" else record_open)
            if failure != "open":
                scoped.setattr(ask.os, "write", fail_write)
            scoped.setattr(ask.sys, "stdout", StdoutAdapter(stream))
            with pytest.raises(OSError):
                ask.View("plain", False, False, None).child_out(b"boom")
        if opened:
            assert len(opened) == 1
            with pytest.raises(OSError):
                os.fstat(opened[0])
        else:
            assert failure == "open"
    finally:
        stream.close()
        os.close(read_fd)
        os.close(write_fd)
