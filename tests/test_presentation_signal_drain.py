import fcntl
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ASK_PATH = Path(__file__).resolve().parents[1] / "bin/ask_presentation.py"
PAYLOAD = b"x" * 131072
WRITER = "import sys; sys.stdout.buffer.write(b'x' * 131072); sys.stdout.buffer.flush()"


def _start_with_pipe(log):
    read_fd, write_fd = os.pipe()
    fcntl.fcntl(write_fd, fcntl.F_SETPIPE_SZ, 4096)
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("ASK_PRESENTATION_ACTIVE", "ASK_TITLE", "ASK_PRESENTATION")
    }
    env["ASK_PRESENTATION"] = "json"
    proc = subprocess.Popen(
        [
            sys.executable,
            str(ASK_PATH),
            "--provider",
            "worker-ask",
            "--",
            sys.executable,
            "-c",
            WRITER,
            "--log",
            str(log),
        ],
        stdin=subprocess.DEVNULL,
        stdout=write_fd,
        stderr=subprocess.PIPE,
        env=env,
    )
    os.close(write_fd)
    return proc, read_fd


def test_sigterm_exits_when_stdout_reader_is_stalled(tmp_path):
    log = tmp_path / "x.log"
    proc, read_fd = _start_with_pipe(log)
    try:
        # Confirma que a apresentação iniciou; aguarda o relay encher o pipe.
        first_line = proc.stderr.readline()
        assert first_line
        time.sleep(0.5)
        started = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=5)
        elapsed = time.monotonic() - started
        stderr = (first_line + proc.stderr.read()).decode(errors="replace")
        assert elapsed <= 5
        assert proc.returncode == 143
        assert "Traceback" not in stderr
    finally:
        os.close(read_fd)
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_normal_reader_preserves_complete_output_and_log(tmp_path):
    log = tmp_path / "x.log"
    proc, read_fd = _start_with_pipe(log)
    chunks = []
    try:
        while True:
            data = os.read(read_fd, 65536)
            if not data:
                break
            chunks.append(data)
        assert proc.wait(timeout=5) == 0
        assert b"".join(chunks) == PAYLOAD
        assert PAYLOAD in log.read_bytes()
    finally:
        os.close(read_fd)
        if proc.poll() is None:
            proc.kill()
            proc.wait()
