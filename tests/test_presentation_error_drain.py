import os
import select
import time

import test_presentation_signal_drain as drain

PAYLOAD = b"x" * 6000


def test_error_exit_preserves_stdout_after_drain_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(
        drain,
        "WRITER",
        "import sys; sys.stdout.buffer.write(b'x' * 6000); "
        "sys.stdout.buffer.flush(); sys.exit(1)",
    )
    log = tmp_path / "x.log"
    proc, read_fd = drain._start_with_pipe(log)
    try:
        first_line = proc.stderr.readline()
        assert first_line
        time.sleep(8)
        assert proc.poll() is None

        chunks = []
        deadline = time.monotonic() + 5
        while True:
            remaining = deadline - time.monotonic()
            assert remaining > 0, "stdout não chegou a EOF"
            ready, _, _ = select.select([read_fd], [], [], remaining)
            assert ready, "stdout não chegou a EOF"
            data = os.read(read_fd, 65536)
            if not data:
                break
            chunks.append(data)

        assert proc.wait(timeout=5) == 1
        proc.stderr.read()
        assert b"".join(chunks) == PAYLOAD
        contents = log.read_bytes()
        assert PAYLOAD in contents
        assert contents.endswith(b"exit=1\n")
    finally:
        os.close(read_fd)
        proc.stderr.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
