"""Testes da ponte worker-ask vision (unittest, sem rede externa)."""

import base64
import http.client
import importlib.machinery
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest


def _load_bridge():
    here = os.path.dirname(os.path.abspath(__file__))
    cand = os.path.join(here, "vision_bridge.py")
    spec = importlib.util.spec_from_file_location("vision_bridge", cand)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


wav = _load_bridge()

FAKE_OK = """#!/bin/sh
cwd=""
task=""
prev=""
for a in "$@"; do
  if [ "$prev" = "--cwd" ]; then cwd="$a"; prev=""; continue; fi
  if [ "$prev" = "--task-id" ]; then task="$a"; prev=""; continue; fi
  case "$a" in --cwd|--task-id) prev="$a";; esac
done
cat > /dev/null
has="no"
if [ -n "$cwd" ] && [ -f "$cwd/image.png" ]; then has="yes"; fi
echo '```json'
echo "{\\"task_id\\": \\"$task\\", \\"answer\\": \\"POINT=1,2\\", \\"has_image\\": \\"$has\\"}"
echo '```'
exit 0
"""

FAKE_75 = """#!/bin/sh
cat > /dev/null
exit 75
"""

FAKE_SLOW = """#!/bin/sh
sleep 3
cat > /dev/null
echo done
exit 0
"""

FAKE_SLEEP30 = """#!/bin/sh
sleep 30
exit 0
"""

FAKE_ENV = """#!/bin/sh
cat > /dev/null
env
exit 0
"""

FAKE_ARGV = """#!/bin/sh
cat > /dev/null
echo "$@"
exit 0
"""

FAKE_FAIL = """#!/bin/sh
echo "should not be called" >&2
exit 99
"""

FAKE_ZCODE_FAST = """#!/usr/bin/env python3
import sys, os, json
args = sys.argv[1:]
def get(flag):
    if flag in args:
        i = args.index(flag)
        if i + 1 < len(args):
            return args[i + 1]
    return None
assert "--chat" in args, "missing --chat"
assert get("--mode") == "plan", get("--mode")
assert get("--model") == "glm-5.3-flash", get("--model")
assert get("--effort") == "low", get("--effort")
assert "--json" in args, "missing --json"
spec = get("--spec")
assert spec is not None and os.path.isfile(spec), spec
assert open(spec, encoding="utf-8").read().strip() != "", "empty spec"
has_attach = "--attach" in args
attach = get("--attach") if has_attach else None
expect = os.environ.get("EXPECT_ATTACH")
if expect == "1":
    assert has_attach, "expected --attach"
    assert attach is not None and os.path.isfile(attach), attach
elif expect == "0":
    assert not has_attach, "unexpected --attach"
print(json.dumps({"status": "ok", "response": " a red circle \\n", "usage": {"inputTokens": 900, "outputTokens": 5}}))
sys.exit(0)
"""

FAKE_ZCODE_75 = """#!/bin/sh
exit 75
"""

FAKE_ZCODE_BADJSON = """#!/bin/sh
echo "not json at all"
exit 0
"""

FAKE_ZCODE_BADSTATUS = """#!/bin/sh
echo '{"status":"error","response":"x"}'
exit 0
"""

FAKE_ZCODE_ENV = """#!/bin/sh
echo "{\\"status\\":\\"ok\\",\\"response\\":\\"paid=$ZCODE_WORKER_ALLOW_PAID tok=${WORKER_ASK_VISION_TOKEN:-none}\\"}"
exit 0
"""


def _write_exe(path, content):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    os.chmod(path, 0o700)


class BridgeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="wav-test-")
        d = cls.tmp.name
        cls.token = "A" * 40
        cls.token_file = os.path.join(d, "token")
        with open(cls.token_file, "w", encoding="utf-8") as f:
            f.write(cls.token + "\n")
        os.chmod(cls.token_file, 0o600)
        cls.fake_ok = os.path.join(d, "fake-ok.sh")
        cls.fake_75 = os.path.join(d, "fake-75.sh")
        cls.fake_slow = os.path.join(d, "fake-slow.sh")
        cls.fake_sleep30 = os.path.join(d, "fake-sleep30.sh")
        cls.fake_argv = os.path.join(d, "fake-argv.sh")
        cls.fake_fail = os.path.join(d, "fake-fail.sh")
        cls.fake_zcode_fast = os.path.join(d, "fake-zcode-fast.py")
        cls.fake_zcode_75 = os.path.join(d, "fake-zcode-75.sh")
        cls.fake_zcode_badjson = os.path.join(d, "fake-zcode-badjson.sh")
        cls.fake_zcode_badstatus = os.path.join(d, "fake-zcode-badstatus.sh")
        cls.fake_zcode_env = os.path.join(d, "fake-zcode-env.sh")
        _write_exe(cls.fake_ok, FAKE_OK)
        _write_exe(cls.fake_75, FAKE_75)
        _write_exe(cls.fake_slow, FAKE_SLOW)
        _write_exe(cls.fake_sleep30, FAKE_SLEEP30)
        _write_exe(cls.fake_argv, FAKE_ARGV)
        _write_exe(cls.fake_fail, FAKE_FAIL)
        _write_exe(cls.fake_zcode_fast, FAKE_ZCODE_FAST)
        _write_exe(cls.fake_zcode_75, FAKE_ZCODE_75)
        _write_exe(cls.fake_zcode_badjson, FAKE_ZCODE_BADJSON)
        _write_exe(cls.fake_zcode_badstatus, FAKE_ZCODE_BADSTATUS)
        _write_exe(cls.fake_zcode_env, FAKE_ZCODE_ENV)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _start(self, worker, **over):
        kw = dict(
            bind="127.0.0.1",
            port=0,
            token_file=self.token_file,
            worker_ask=worker,
        )
        kw.update(over)
        cfg = wav.Config(**kw)
        srv = wav.build_server(cfg, self.token)
        th = threading.Thread(
            target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        th.start()
        port = srv.server_address[1]
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                c.request("GET", "/healthz")
                r = c.getresponse()
                r.read()
                c.close()
                if r.status == 200:
                    break
            except OSError:
                time.sleep(0.05)
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close(), th.join(timeout=5)))
        return srv, port

    def _post(self, port, obj, token=None, extra_headers=None, timeout=20):
        body = json.dumps(obj).encode()
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        hdrs = {"Content-Type": "application/json"}
        if token is not None:
            hdrs["Authorization"] = "Bearer " + token
        if extra_headers:
            hdrs.update(extra_headers)
        c.request("POST", "/v1/vision", body=body, headers=hdrs)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data

    def _get(self, port, path, timeout=20):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        c.request("GET", path)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data

    def test_healthz(self):
        _, port = self._start(self.fake_ok)
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/healthz")
        r = c.getresponse()
        body = r.read()
        c.close()
        self.assertEqual(r.status, 200)
        self.assertEqual(json.loads(body), {"ok": True})

    def test_no_auth_401(self):
        _, port = self._start(self.fake_ok)
        st, _ = self._post(port, {"task_id": "a", "spec": "oi"}, token=None)
        self.assertEqual(st, 401)

    def test_wrong_token_401(self):
        _, port = self._start(self.fake_ok)
        st, _ = self._post(port, {"task_id": "a", "spec": "oi"}, token="B" * 40)
        self.assertEqual(st, 401)

    def test_forged_host_421(self):
        _, port = self._start(self.fake_ok)
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            body = json.dumps({"task_id": "a", "spec": "oi"}).encode()
            req = (
                "POST /v1/vision HTTP/1.1\r\n"
                "Host: evil.example\r\n"
                "Authorization: Bearer " + self.token + "\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode() + body
            s.sendall(req)
            resp = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                resp += chunk
        finally:
            s.close()
        self.assertTrue(resp.startswith(b"HTTP/1.1 421") or b" 421 " in resp.split(b"\r\n")[0])

    def test_body_too_large_413(self):
        _, port = self._start(self.fake_ok, max_body=100)
        st, _ = self._post(
            port,
            {"task_id": "abc", "spec": "x" * 200},
            token=self.token,
        )
        self.assertEqual(st, 413)

    def test_bad_task_id_400(self):
        _, port = self._start(self.fake_ok)
        st, _ = self._post(
            port, {"task_id": "a/b c!", "spec": "oi"}, token=self.token
        )
        self.assertEqual(st, 400)

    def test_bad_image_400(self):
        _, port = self._start(self.fake_ok)
        st, _ = self._post(
            port,
            {"task_id": "abc", "spec": "oi", "image_b64": "!!!"},
            token=self.token,
        )
        self.assertEqual(st, 400)

    def test_happy_with_image(self):
        _, port = self._start(self.fake_ok)
        img = base64.b64encode(b"fakepng").decode()
        st, data = self._post(
            port,
            {
                "task_id": "task-1",
                "spec": "descreva o ponto",
                "image_b64": img,
                "image_ext": ".png",
                "timeout_seconds": 20,
            },
            token=self.token,
        )
        self.assertEqual(st, 200)
        obj = json.loads(data)
        self.assertEqual(obj["rc"], 0)
        self.assertIn("POINT=1,2", obj["stdout"])
        self.assertIn("task-1", obj["stdout"])
        self.assertIn("yes", obj["stdout"])

    def test_providers_forwarded_to_child(self):
        _, port = self._start(self.fake_argv)
        st, data = self._post(
            port,
            {"task_id": "prov1", "spec": "oi", "providers": ["zcode"]},
            token=self.token,
        )
        self.assertEqual(st, 200)
        self.assertIn("--providers zcode", json.loads(data)["stdout"])

    def test_without_providers_field_no_flag(self):
        _, port = self._start(self.fake_argv)
        st, data = self._post(
            port, {"task_id": "prov2", "spec": "oi"}, token=self.token
        )
        self.assertEqual(st, 200)
        self.assertNotIn("--providers", json.loads(data)["stdout"])

    def test_bad_providers_400(self):
        _, port = self._start(self.fake_ok)
        for bad in ("zcode", [], ["x"], ["zcode", "zcode"], [1]):
            with self.subTest(providers=bad):
                st, data = self._post(
                    port,
                    {"task_id": "prov3", "spec": "oi", "providers": bad},
                    token=self.token,
                )
                self.assertEqual(st, 400)
                self.assertEqual(json.loads(data), {"error": "bad providers"})

    def test_child_env_sanitized(self):
        fake = os.path.join(self.tmp.name, "fake-env.sh")
        _write_exe(fake, FAKE_ENV)
        old_token = os.environ.get("WORKER_ASK_VISION_TOKEN")
        old_probe = os.environ.get("WAV_PROBE_TEST")
        try:
            os.environ["WORKER_ASK_VISION_TOKEN"] = "D" * 40
            os.environ["WAV_PROBE_TEST"] = "probe-wav"
            _, port = self._start(fake)
            st, data = self._post(
                port, {"task_id": "env1", "spec": "oi"}, token=self.token
            )
        finally:
            if old_token is None:
                os.environ.pop("WORKER_ASK_VISION_TOKEN", None)
            else:
                os.environ["WORKER_ASK_VISION_TOKEN"] = old_token
            if old_probe is None:
                os.environ.pop("WAV_PROBE_TEST", None)
            else:
                os.environ["WAV_PROBE_TEST"] = old_probe
        self.assertEqual(st, 200)
        out = json.loads(data)["stdout"]
        # O filho nao pode ver o token nem nenhuma WAV_* do servico.
        self.assertNotIn("D" * 40, out)
        self.assertNotIn("WORKER_ASK_VISION_TOKEN", out)
        self.assertNotIn("WAV_PROBE_TEST", out)
        # O env nao foi esvaziado: heranca normal + config do filho seguem.
        self.assertIn("PATH=", out)
        self.assertIn("WORKER_ASK_MIN_TIMEOUT=", out)

    def test_worker_exit_75(self):
        _, port = self._start(self.fake_75)
        st, data = self._post(
            port, {"task_id": "t75", "spec": "oi"}, token=self.token, timeout=30
        )
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(data)["rc"], 75)

    def test_fast_happy_with_image(self):
        old = os.environ.get("EXPECT_ATTACH")
        os.environ["EXPECT_ATTACH"] = "1"
        try:
            _, port = self._start(self.fake_fail, zcode_worker=self.fake_zcode_fast)
            img = base64.b64encode(b"fakepng").decode()
            st, data = self._post(
                port,
                {
                    "task_id": "fast1",
                    "spec": "oi",
                    "question": "describe this image",
                    "fast": True,
                    "image_b64": img,
                    "image_ext": ".png",
                    "timeout_seconds": 20,
                },
                token=self.token,
            )
        finally:
            if old is None:
                os.environ.pop("EXPECT_ATTACH", None)
            else:
                os.environ["EXPECT_ATTACH"] = old
        self.assertEqual(st, 200)
        obj = json.loads(data)
        self.assertEqual(obj["rc"], 0)
        self.assertTrue(obj["fast"])
        self.assertEqual(obj["usage"], {"input": 900, "output": 5})
        lines = obj["stdout"].split("\n")
        start = lines.index("```json")
        end = lines.index("```", start + 1)
        inner = "\n".join(lines[start + 1 : end])
        inner_obj = json.loads(inner)
        self.assertEqual(inner_obj["task_id"], "fast1")
        self.assertEqual(inner_obj["schema_version"], 1)
        self.assertEqual(inner_obj["answer"], "a red circle")
        self.assertTrue(inner_obj["image_seen"])
        self.assertTrue(inner_obj["fast"])

    def test_fast_without_image(self):
        old = os.environ.get("EXPECT_ATTACH")
        os.environ["EXPECT_ATTACH"] = "0"
        try:
            _, port = self._start(self.fake_fail, zcode_worker=self.fake_zcode_fast)
            st, data = self._post(
                port,
                {
                    "task_id": "fast2",
                    "spec": "oi",
                    "question": "describe this image",
                    "fast": True,
                },
                token=self.token,
            )
        finally:
            if old is None:
                os.environ.pop("EXPECT_ATTACH", None)
            else:
                os.environ["EXPECT_ATTACH"] = old
        self.assertEqual(st, 200)
        obj = json.loads(data)
        self.assertEqual(obj["rc"], 0)
        self.assertTrue(obj["fast"])
        lines = obj["stdout"].split("\n")
        start = lines.index("```json")
        end = lines.index("```", start + 1)
        inner = "\n".join(lines[start + 1 : end])
        inner_obj = json.loads(inner)
        self.assertFalse(inner_obj["image_seen"])

    def test_fast_does_not_call_router(self):
        old = os.environ.get("EXPECT_ATTACH")
        os.environ["EXPECT_ATTACH"] = "0"
        try:
            _, port = self._start(self.fake_fail, zcode_worker=self.fake_zcode_fast)
            st, data = self._post(
                port,
                {"task_id": "fast3", "spec": "oi", "question": "q", "fast": True},
                token=self.token,
            )
        finally:
            if old is None:
                os.environ.pop("EXPECT_ATTACH", None)
            else:
                os.environ["EXPECT_ATTACH"] = old
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(data)["rc"], 0)

    def test_no_fast_does_not_call_zcode(self):
        _, port = self._start(self.fake_ok, zcode_worker=self.fake_fail)
        st, data = self._post(
            port, {"task_id": "nofast1", "spec": "oi"}, token=self.token
        )
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(data)["rc"], 0)

    def test_fast_child_env(self):
        old_token = os.environ.get("WORKER_ASK_VISION_TOKEN")
        old_paid = os.environ.get("ZCODE_WORKER_ALLOW_PAID")
        try:
            os.environ["WORKER_ASK_VISION_TOKEN"] = "D" * 40
            _, port = self._start(self.fake_fail, zcode_worker=self.fake_zcode_env)
            st, data = self._post(
                port,
                {
                    "task_id": "fastenv",
                    "spec": "oi",
                    "question": "q",
                    "fast": True,
                },
                token=self.token,
            )
        finally:
            if old_token is None:
                os.environ.pop("WORKER_ASK_VISION_TOKEN", None)
            else:
                os.environ["WORKER_ASK_VISION_TOKEN"] = old_token
            if old_paid is None:
                os.environ.pop("ZCODE_WORKER_ALLOW_PAID", None)
            else:
                os.environ["ZCODE_WORKER_ALLOW_PAID"] = old_paid
        self.assertEqual(st, 200)
        out = json.loads(data)["stdout"]
        self.assertEqual(json.loads(data)["rc"], 0)
        # Pago desligado na ponte: sem ZCODE_WORKER_ALLOW_PAID o worker devolve 75
        # se o Start Plan falhar (só o degrau zcode-pago define a variável).
        self.assertIn("paid=", out)
        self.assertNotIn("paid=1", out)
        self.assertIn("tok=none", out)
        self.assertNotIn("D" * 40, out)

    def test_fast_argv_aceito_pelo_parser_real(self):
        # Integração com o parser real: o argv de _run_fast precisa ser aceito
        # pelo bin/zcode-worker (core) e pela cápsula (features), sem rc 2.
        here = os.path.dirname(os.path.abspath(__file__))
        repo = os.path.dirname(here)
        worker = os.path.join(repo, "bin", "zcode-worker")
        self.assertTrue(os.path.isfile(worker), worker)
        with tempfile.TemporaryDirectory() as td:
            spec = os.path.join(td, "question.md")
            with open(spec, "w", encoding="utf-8") as f:
                f.write("describe")
            img = os.path.join(td, "image.png")
            with open(img, "wb") as f:
                f.write(b"fakepng")
            argv = [
                "--chat", "--mode", "plan", "--model", "glm-5.3-flash",
                "--effort", "low", "--cwd", td, "--queue-timeout", "10",
                "--timeout", "20", "--json", "--spec", spec,
                "--attach", img,
            ]
            # Core (bin/zcode-worker): _parse_args com allow_abbrev=False.
            loader = importlib.machinery.SourceFileLoader(
                "zcode_worker_core_test", worker
            )
            spec_core = importlib.util.spec_from_loader(loader.name, loader)
            core = importlib.util.module_from_spec(spec_core)
            loader.exec_module(core)
            parsed_core = core._parse_args(argv)
            self.assertTrue(parsed_core.chat)
            self.assertTrue(parsed_core.json)
            self.assertEqual(parsed_core.attach, [img])
            self.assertEqual(parsed_core.timeout, 20.0)
            # Cápsula (features): mesmo argv precisa parsear e validar anexos.
            src = os.path.join(repo, "adapters", "zcode", "src")
            if src not in sys.path:
                sys.path.insert(0, src)
            from zcode_worker import features as _feat

            parsed_feat = _feat.parse_args(argv)
            self.assertTrue(parsed_feat.chat)
            self.assertTrue(parsed_feat.json)
            # Help do executável real (via integração) declara as flags.
            env = dict(os.environ)
            env["ASK_PRESENTATION_ACTIVE"] = "1"
            r = subprocess.run(
                [sys.executable, worker, "--help"],
                capture_output=True, text=True, env=env, timeout=60,
            )
            self.assertEqual(r.returncode, 0, r.stderr[-400:])
            for flag in ("--chat", "--json", "--attach", "--timeout"):
                self.assertIn(flag, r.stdout)

    def test_fast_zcode_exit_75(self):
        _, port = self._start(self.fake_ok, zcode_worker=self.fake_zcode_75)
        st, data = self._post(
            port,
            {"task_id": "fast75", "spec": "oi", "question": "q", "fast": True},
            token=self.token,
            timeout=30,
        )
        self.assertEqual(st, 200)
        obj = json.loads(data)
        self.assertEqual(obj["rc"], 75)
        self.assertEqual(obj["stdout"], "")

    def test_fast_rc0_nonjson(self):
        _, port = self._start(self.fake_ok, zcode_worker=self.fake_zcode_badjson)
        st, data = self._post(
            port,
            {"task_id": "fastnj", "spec": "oi", "question": "q", "fast": True},
            token=self.token,
        )
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(data)["rc"], 1)

    def test_fast_rc0_badstatus(self):
        _, port = self._start(self.fake_ok, zcode_worker=self.fake_zcode_badstatus)
        st, data = self._post(
            port,
            {"task_id": "fastbs", "spec": "oi", "question": "q", "fast": True},
            token=self.token,
        )
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(data)["rc"], 1)

    def test_bad_fast_400(self):
        _, port = self._start(self.fake_ok)
        for body in (
            {"task_id": "bf1", "spec": "oi", "fast": "yes"},
            {"task_id": "bf2", "spec": "oi", "fast": True, "providers": ["opencode"]},
        ):
            with self.subTest(body=body):
                st, data = self._post(port, body, token=self.token)
                self.assertEqual(st, 400)
                self.assertEqual(json.loads(data), {"error": "bad fast"})

    def test_bad_question_400(self):
        _, port = self._start(self.fake_ok)
        cases = [
            {"task_id": "bq1", "spec": "oi", "fast": True},
            {"task_id": "bq2", "spec": "oi", "fast": True, "question": "  "},
            {"task_id": "bq3", "spec": "oi", "fast": True, "question": "x" * 4001},
        ]
        for body in cases:
            with self.subTest(body=body):
                st, data = self._post(port, body, token=self.token)
                self.assertEqual(st, 400)
                self.assertEqual(json.loads(data), {"error": "bad question"})

    def test_fast_false_uses_router(self):
        _, port = self._start(self.fake_ok)
        img = base64.b64encode(b"fakepng").decode()
        st, data = self._post(
            port,
            {
                "task_id": "fastfalse",
                "spec": "descreva o ponto",
                "image_b64": img,
                "image_ext": ".png",
                "fast": False,
                "timeout_seconds": 20,
            },
            token=self.token,
        )
        self.assertEqual(st, 200)
        self.assertNotIn("fast", json.loads(data))

    def test_concurrency_503(self):
        _, port = self._start(self.fake_slow, max_concurrency=1)
        results = {}

        def one(idx, delay=0):
            if delay:
                time.sleep(delay)
            try:
                st, data = self._post(
                    port,
                    {"task_id": f"c{idx}", "spec": "oi"},
                    token=self.token,
                    timeout=15,
                )
                results[idx] = (st, data)
            except Exception as e:  # noqa: BLE001
                results[idx] = ("exc", str(e).encode())

        t1 = threading.Thread(target=one, args=(1,))
        t2 = threading.Thread(target=one, args=(2,), kwargs={"delay": 0.3})
        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)
        codes = sorted(v[0] for v in results.values())
        self.assertEqual(codes, [200, 503])

    def test_timeout_rc124(self):
        _, port = self._start(
            self.fake_sleep30, min_timeout=1, max_timeout=5
        )
        t0 = time.time()
        st, data = self._post(
            port,
            {"task_id": "tmo", "spec": "oi", "timeout_seconds": 1},
            token=self.token,
            timeout=30,
        )
        dt = time.time() - t0
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(data)["rc"], 124)
        self.assertLess(dt, 25)

    def test_load_token_rejects(self):
        bad = os.path.join(self.tmp.name, "badtoken")
        with open(bad, "w", encoding="utf-8") as f:
            f.write(self.token)
        os.chmod(bad, 0o644)
        with self.assertRaises(SystemExit):
            wav.load_token(bad)
        short = os.path.join(self.tmp.name, "shorttoken")
        with open(short, "w", encoding="utf-8") as f:
            f.write("curto")
        os.chmod(short, 0o600)
        with self.assertRaises(SystemExit):
            wav.load_token(short)

    def test_env_token_used_and_short(self):
        old = os.environ.get("WORKER_ASK_VISION_TOKEN")
        cfg = wav.Config(
            token_file=os.path.join(self.tmp.name, "nao-existe")
        )
        try:
            os.environ["WORKER_ASK_VISION_TOKEN"] = "C" * 40
            self.assertEqual(wav.resolve_token(cfg), "C" * 40)
            os.environ["WORKER_ASK_VISION_TOKEN"] = "C" * 10
            with self.assertRaises(SystemExit):
                wav.resolve_token(cfg)
        finally:
            if old is None:
                os.environ.pop("WORKER_ASK_VISION_TOKEN", None)
            else:
                os.environ["WORKER_ASK_VISION_TOKEN"] = old

    def test_allow_any_bind_gate(self):
        old = os.environ.pop("WAV_ALLOW_ANY_BIND", None)
        try:
            with self.assertRaises(SystemExit) as cm:
                wav.validate_bind("0.0.0.0")
            self.assertEqual(cm.exception.code, 2)
            with self.assertRaises(SystemExit) as cm:
                wav.validate_bind("::")
            self.assertEqual(cm.exception.code, 2)
            os.environ["WAV_ALLOW_ANY_BIND"] = "1"
            wav.validate_bind("0.0.0.0")
            wav.validate_bind("::")
            wav.validate_bind("127.0.0.1")
        finally:
            if old is None:
                os.environ.pop("WAV_ALLOW_ANY_BIND", None)
            else:
                os.environ["WAV_ALLOW_ANY_BIND"] = old

    def test_healthz_clis(self):
        ok = os.path.join(self.tmp.name, "login-ok.sh")
        bad = os.path.join(self.tmp.name, "login-bad.sh")
        missing = os.path.join(self.tmp.name, "login-missing-bin")
        _write_exe(ok, "#!/bin/sh\necho SEGREDO123\nexit 0\n")
        _write_exe(bad, "#!/bin/sh\necho SEGREDO123\nexit 1\n")
        _, port = self._start(
            self.fake_ok,
            login_checks={
                "ok": [ok],
                "ruim": [bad],
                "sumiu": [missing],
            },
        )
        st, data = self._get(port, "/healthz?clis=1")
        self.assertEqual(st, 200)
        obj = json.loads(data)
        self.assertEqual(obj["ok"], True)
        self.assertEqual(
            obj["clis"],
            {"ok": "logado", "ruim": "sem-login", "sumiu": "ausente"},
        )
        self.assertNotIn(b"SEGREDO123", data)
        st, data = self._get(port, "/healthz?outra=1")
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(data), {"ok": True})

    def test_healthz_clis_cache(self):
        counter = os.path.join(self.tmp.name, "login-counter")
        script = os.path.join(self.tmp.name, "login-count.sh")
        _write_exe(
            script,
            "#!/bin/sh\n"
            f"echo x >> {counter}\n"
            "echo SEGREDO123\n"
            "exit 0\n",
        )
        _, port = self._start(
            self.fake_ok,
            login_checks={"ok": [script]},
            login_check_ttl=300,
        )
        for _ in range(2):
            st, data = self._get(port, "/healthz?clis=1")
            self.assertEqual(st, 200)
            self.assertEqual(json.loads(data)["clis"], {"ok": "logado"})
        with open(counter, "r", encoding="utf-8") as f:
            self.assertEqual(len(f.read().splitlines()), 1)

    def test_bind_all_refused(self):
        old_argv, old_env = sys.argv[:], os.environ.get("WAV_BIND")
        old_tf = os.environ.get("WAV_TOKEN_FILE")
        old_any = os.environ.pop("WAV_ALLOW_ANY_BIND", None)
        sys.argv = ["prog"]
        os.environ["WAV_BIND"] = "0.0.0.0"
        os.environ["WAV_TOKEN_FILE"] = self.token_file
        try:
            with self.assertRaises(SystemExit) as cm:
                wav.main([])
            self.assertEqual(cm.exception.code, 2)
        finally:
            sys.argv = old_argv
            if old_env is None:
                os.environ.pop("WAV_BIND", None)
            else:
                os.environ["WAV_BIND"] = old_env
            if old_tf is None:
                os.environ.pop("WAV_TOKEN_FILE", None)
            else:
                os.environ["WAV_TOKEN_FILE"] = old_tf
            if old_any is None:
                os.environ.pop("WAV_ALLOW_ANY_BIND", None)
            else:
                os.environ["WAV_ALLOW_ANY_BIND"] = old_any


if __name__ == "__main__":
    unittest.main()


class OpencodeLoginCheckTests(unittest.TestCase):
    """opencode auth list sai 0 sem credencial; o check precisa olhar a saída."""

    def _check(self, script):
        with tempfile.TemporaryDirectory() as d:
            fake = os.path.join(d, "opencode")
            with open(fake, "w") as f:
                f.write("#!/bin/sh\n" + script)
            os.chmod(fake, 0o755)
            old = os.environ["PATH"]
            os.environ["PATH"] = d + os.pathsep + old
            try:
                return wav._check_cli(wav.DEFAULT_LOGIN_CHECKS["opencode"])
            finally:
                os.environ["PATH"] = old

    def test_sem_credencial_rc0_e_sem_login(self):
        self.assertEqual(self._check("echo 'No authenticated integrations'\nexit 0\n"), "sem-login")

    def test_com_credencial_e_logado(self):
        self.assertEqual(self._check("echo 'OpenCode Go  api'\nexit 0\n"), "logado")

    def test_rc_diferente_de_zero_e_sem_login(self):
        self.assertEqual(self._check("exit 1\n"), "sem-login")
