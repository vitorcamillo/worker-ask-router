import os
import signal
import subprocess
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "servico" / "entrypoint.sh"


def test_invalid_interval_falls_back_and_execs(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    sentinel = tmp_path / "sentinel"
    log = tmp_path / "log"
    env = dict(os.environ, HOME=str(home), WORKER_AUTO_UPDATE="1",
               WORKER_UPDATE_DELAY="0", WORKER_UPDATE_INTERVAL="invalido",
               SENTINEL=str(sentinel))
    with open(log, "wb") as out:
        p = subprocess.Popen(
            ["bash", str(SCRIPT), "sh", "-c", 'echo ok > "$SENTINEL"; sleep 30'],
            env=env, stdout=out, stderr=out, start_new_session=True)
        try:
            for _ in range(50):
                if sentinel.exists():
                    break
                time.sleep(0.1)
            assert sentinel.read_text().strip() == "ok"
            assert p.poll() is None
        finally:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            p.wait(timeout=5)
    text = log.read_text()
    assert "WORKER_UPDATE_INTERVAL invalido" in text
