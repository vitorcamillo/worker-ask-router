import base64
import hashlib
import os
import shutil
import subprocess
import threading
import functools
import http.server
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "zcode-update"
pytestmark = pytest.mark.skipif(shutil.which("dpkg-deb") is None, reason="sem dpkg-deb")


def build_deb(root: Path) -> Path:
    pkg = root / "pkg"
    (pkg / "opt/ZCode/resources/glm").mkdir(parents=True)
    (pkg / "opt/ZCode/resources/config/provider").mkdir(parents=True)
    (pkg / "DEBIAN").mkdir()
    (pkg / "DEBIAN/control").write_text(
        "Package: zcode\nVersion: 4.0.0\nArchitecture: amd64\nMaintainer: t <t@t>\nDescription: fake\n")
    (pkg / "opt/ZCode/resources/glm/zcode.cjs").write_text("// fake new\n")
    (pkg / "opt/ZCode/resources/config/provider/zcode-builtin.json").write_text('{"new": true}')
    deb = root / "ZCode-4.0.0-linux-x64.deb"
    subprocess.run(["dpkg-deb", "--build", str(pkg), str(deb)], check=True, capture_output=True)
    return deb


@pytest.fixture
def env(tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    deb = build_deb(tmp_path)
    shutil.copy(deb, web / deb.name)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(web))
    handler.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    data = deb.read_bytes()

    cli = tmp_path / "glm"
    (cli / "provider").mkdir(parents=True)
    (cli / "zcode.cjs").write_text("// old\n")
    (cli / "provider/zcode-builtin.json").write_text('{"old": true}')
    (cli / "VERSION").write_text("3.18.2\n")

    bindir = tmp_path / "bin"
    bindir.mkdir()

    def fake_node(ok=True):
        p = bindir / "node"
        p.write_text("#!/bin/sh\n" + ("echo 9.9.9\n" if ok else "exit 1\n"))
        p.chmod(0o755)

    fake_node(True)

    class E:
        pass

    e = E()
    e.cli, e.web, e.base, e.fake_node = cli, web, base, fake_node
    e.deb_name = deb.name
    e.sha = base64.b64encode(hashlib.sha512(data).digest()).decode()
    e.size = len(data)

    def manifest(version="4.0.0", sha=None, size=None, url=None):
        url = url or f"{base}/{deb.name}"
        (web / "manifest.yml").write_text(
            f"version: {version}\nfiles:\n    - url: {url}\n      sha512: {sha or e.sha}\n"
            f"      size: {size or e.size}\npath: x\nreleaseNotes: |-\n    n\n")

    e.manifest = manifest
    manifest()

    def run(*args, **extra):
        envv = {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(tmp_path),
                "ZCODE_CLI_DIR": str(cli),
                "ZCODE_UPDATE_MANIFEST_URL": f"{base}/manifest.yml",
                "ZCODE_UPDATE_ALLOWED_PREFIX": base + "/"}
        envv.update(extra)
        return subprocess.run(["python3", str(SCRIPT), *args], env=envv, capture_output=True, text=True)

    e.run = run
    yield e
    srv.shutdown()


def intact(e):
    return ((e.cli / "VERSION").read_text() == "3.18.2\n"
            and (e.cli / "zcode.cjs").read_text() == "// old\n"
            and not list(e.cli.glob(".staging-*")))


def test_update(env):
    r = env.run()
    assert r.returncode == 0, r.stderr
    assert "3.18.2 -> 4.0.0" in r.stdout
    assert (env.cli / "VERSION").read_text() == "4.0.0\n"
    assert "new" in (env.cli / "zcode.cjs").read_text()
    assert oct((env.cli / "zcode.cjs").stat().st_mode & 0o777) == "0o755"
    assert (env.cli / "zcode.cjs.prev").read_text() == "// old\n"
    assert (env.cli / "VERSION.prev").read_text() == "3.18.2\n"
    assert (env.cli / "provider/zcode-builtin.json.prev").exists()
    assert not list(env.cli.glob(".staging-*"))


def test_same_version_noop(env):
    env.manifest(version="3.18.2")
    r = env.run()
    assert r.returncode == 0 and "atualizado" in r.stdout and intact(env)


def test_remote_lower(env):
    env.manifest(version="3.0.0")
    r = env.run("--force")
    assert r.returncode == 0 and intact(env)


def test_bad_sha(env):
    env.manifest(sha=base64.b64encode(b"x" * 64).decode())
    assert env.run().returncode == 1 and intact(env)


def test_bad_size(env):
    env.manifest(size=env.size - 5)
    assert env.run().returncode == 1 and intact(env)
    env.manifest(size=env.size + 5)
    assert env.run().returncode == 1 and intact(env)


def test_url_outside_prefix(env):
    r = env.run(ZCODE_UPDATE_ALLOWED_PREFIX="https://cdn-zcode.z.ai/zcode/electron/releases/")
    assert r.returncode == 1 and intact(env)


def test_smoke_fail(env):
    env.fake_node(False)
    assert env.run().returncode == 1 and intact(env)


def test_rollback(env):
    assert env.run().returncode == 0
    r = env.run("--rollback")
    assert r.returncode == 0, r.stderr
    assert (env.cli / "VERSION").read_text() == "3.18.2\n"
    assert (env.cli / "zcode.cjs").read_text() == "// old\n"
    assert env.run("--rollback").returncode == 1


def test_check_no_change(env):
    r = env.run("--check")
    assert r.returncode == 0 and "4.0.0" in r.stdout and "3.18.2" in r.stdout
    assert intact(env)


def test_disabled_no_network(env):
    r = env.run(WORKER_AUTO_UPDATE="0", ZCODE_UPDATE_MANIFEST_URL="http://127.0.0.1:1/x")
    assert r.returncode == 0 and "desativado" in r.stdout and intact(env)
