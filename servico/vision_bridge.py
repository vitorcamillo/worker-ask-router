#!/usr/bin/env python3
"""Servico HTTP de visao em container que expoe o roteador `worker-ask`.

O phantoraCaptcha chama POST /v1/vision pela rede interna do Docker com
task_id, spec, imagem base64, timeout e, opcionalmente, providers (lista de
provedores para esta chamada, na ordem dada); o servico monta o comando
`worker-ask` (stdin = spec) e devolve rc + stdout. GET /healthz e aberto
sem auth; GET /healthz?clis=1 relata o estado de login dos CLIs.

Feito para rodar em container (Coolify), com /home/worker em volume. Env:
WAV_BIND, WAV_ALLOW_ANY_BIND, WAV_PORT, WAV_TOKEN_FILE,
WORKER_ASK_VISION_TOKEN (token via env; minimo 32 caracteres),
WAV_ALLOWED_HOSTS (virgulas), WAV_MAX_CONCURRENCY, WAV_MAX_BODY,
WAV_WORKER_ASK, WAV_ZCODE_WORKER, WAV_ORCHESTRATOR, WAV_NO_MUSE ("0" desliga),
WAV_MAX_TIMEOUT, WAV_LOGIN_CHECKS (JSON nome->argv), WAV_LOGIN_CHECK_TTL.
"""

import argparse
import base64
import binascii
import hmac
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


TASK_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
ALLOWED_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
FORBIDDEN_BINDS = {"0.0.0.0", "::"}
DEFAULT_LOGIN_CHECKS = {
    # "opencode auth list" sai 0 mesmo sem credencial ("No authenticated integrations").
    "opencode": ["sh", "-c", 'out=$(opencode auth list 2>&1) || exit 1; '
                 'case "$out" in *"No authenticated"*) exit 1;; esac'],
    "codex": ["codex", "login", "status"],
    "claude": ["claude", "auth", "status"],
}
# Restricao opcional de UMA chamada: nomes fechados, nunca texto livre no argv.
ALLOWED_PROVIDERS = frozenset({"zcode", "opencode", "codex", "claude", "muse"})
PROVIDER_RE = re.compile(r"[a-z]{1,16}")


def _parse_providers_field(value):
    """Campo `providers` do POST /v1/vision; ausente/null = cadeia padrao."""
    if value is None:
        return None
    if not isinstance(value, list) or not 1 <= len(value) <= 5:
        raise ValueError("providers invalidos")
    out = []
    for item in value:
        if (
            not isinstance(item, str)
            or not PROVIDER_RE.fullmatch(item)
            or item not in ALLOWED_PROVIDERS
            or item in out
        ):
            raise ValueError("providers invalidos")
        out.append(item)
    return out


def _parse_login_checks(raw):
    """Le WAV_LOGIN_CHECKS; JSON invalido ou formato errado usa o default."""
    if not raw:
        return dict(DEFAULT_LOGIN_CHECKS)
    try:
        obj = json.loads(raw)
    except Exception:
        return dict(DEFAULT_LOGIN_CHECKS)
    if not isinstance(obj, dict):
        return dict(DEFAULT_LOGIN_CHECKS)
    parsed = {}
    for name, argv in obj.items():
        if not isinstance(name, str) or not name:
            return dict(DEFAULT_LOGIN_CHECKS)
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(a, str) and a for a in argv)
        ):
            return dict(DEFAULT_LOGIN_CHECKS)
        parsed[name] = list(argv)
    return parsed


@dataclass
class Config:
    bind: str = "127.0.0.1"
    port: int = 9120
    token_file: str = "~/.config/worker-ask-vision/token"
    allowed_hosts: tuple = field(default=None)
    max_concurrency: int = 2
    max_body: int = 6 * 1024 * 1024
    worker_ask: str = "worker-ask"
    zcode_worker: str = "zcode-worker"
    orchestrator: str = "claude"
    no_muse: bool = True
    max_timeout: int = 120
    min_timeout: int = 10
    max_stdout: int = 262144
    login_checks: dict = field(
        default_factory=lambda: dict(DEFAULT_LOGIN_CHECKS)
    )
    login_check_ttl: int = 300

    def __post_init__(self):
        if self.allowed_hosts is None:
            hosts = ["127.0.0.1", "localhost"]
            if self.bind not in FORBIDDEN_BINDS:
                hosts.append(self.bind)
            self.allowed_hosts = tuple(dict.fromkeys(hosts))
        else:
            self.allowed_hosts = tuple(self.allowed_hosts)

    @classmethod
    def from_env(cls) -> "Config":
        def _int(name, default):
            raw = os.getenv(name)
            if raw is None or raw == "":
                return default
            try:
                return int(raw.strip())
            except ValueError:
                return default

        bind = os.getenv("WAV_BIND", "127.0.0.1")
        port = _int("WAV_PORT", 9120)
        token_file = os.getenv(
            "WAV_TOKEN_FILE", "~/.config/worker-ask-vision/token"
        )
        raw_hosts = os.getenv("WAV_ALLOWED_HOSTS")
        if raw_hosts is None:
            allowed = None
        else:
            extra = [
                h.strip() for h in raw_hosts.split(",") if h.strip()
            ]
            if extra:
                base = ["127.0.0.1", "localhost"]
                if bind not in FORBIDDEN_BINDS:
                    base.append(bind)
                allowed = tuple(dict.fromkeys(base + extra))
            else:
                allowed = None
        max_conc = _int("WAV_MAX_CONCURRENCY", 2)
        max_body = _int("WAV_MAX_BODY", 6 * 1024 * 1024)
        worker_ask = os.getenv("WAV_WORKER_ASK", "worker-ask")
        zcode_worker = os.getenv("WAV_ZCODE_WORKER", "zcode-worker")
        orch = os.getenv("WAV_ORCHESTRATOR", "claude")
        raw_no_muse = os.getenv("WAV_NO_MUSE")
        if raw_no_muse is None:
            no_muse = True
        else:
            no_muse = raw_no_muse.strip() != "0"
        max_timeout = _int("WAV_MAX_TIMEOUT", 120)
        login_checks = _parse_login_checks(
            os.getenv("WAV_LOGIN_CHECKS")
        )
        login_check_ttl = _int("WAV_LOGIN_CHECK_TTL", 300)
        return cls(
            bind=bind,
            port=port,
            token_file=token_file,
            allowed_hosts=allowed,
            max_concurrency=max_conc,
            max_body=max_body,
            worker_ask=worker_ask,
            zcode_worker=zcode_worker,
            orchestrator=orch,
            no_muse=no_muse,
            max_timeout=max_timeout,
            login_checks=login_checks,
            login_check_ttl=login_check_ttl,
        )


def load_token(path: str) -> str:
    p = os.path.expanduser(path)
    if os.path.islink(p):
        print("wav: token recusado: symlink nao permitido", file=sys.stderr)
        raise SystemExit(1)
    try:
        st = os.stat(p)
    except FileNotFoundError:
        print("wav: token nao encontrado", file=sys.stderr)
        raise SystemExit(1)
    except OSError:
        print("wav: token inacessivel", file=sys.stderr)
        raise SystemExit(1)
    if not stat.S_ISREG(st.st_mode):
        print("wav: token precisa ser arquivo regular", file=sys.stderr)
        raise SystemExit(1)
    if st.st_mode & 0o077 != 0:
        print(
            "wav: token com permissao aberta (use 0600)", file=sys.stderr
        )
        raise SystemExit(1)
    try:
        with open(p, "r", encoding="utf-8") as f:
            content = f.read().strip()
    except OSError:
        print("wav: token ilegivel", file=sys.stderr)
        raise SystemExit(1)
    if len(content) < 32:
        print("wav: token curto demais", file=sys.stderr)
        raise SystemExit(1)
    return content


def resolve_token(config: Config) -> str:
    """Token da env (se nao vazia) ou do arquivo; nunca imprime o conteudo."""
    raw = os.getenv("WORKER_ASK_VISION_TOKEN")
    if raw is not None:
        value = raw.strip()
        if value:
            if len(value) < 32:
                print(
                    "wav: WORKER_ASK_VISION_TOKEN curto demais "
                    "(minimo 32 caracteres)",
                    file=sys.stderr,
                )
                raise SystemExit(1)
            return value
    return load_token(config.token_file)


def validate_bind(bind: str) -> None:
    """Bind aberto so com WAV_ALLOW_ANY_BIND=1 (rede isolada de container)."""
    allow_any = (os.getenv("WAV_ALLOW_ANY_BIND") or "").strip() == "1"
    if bind in FORBIDDEN_BINDS and not allow_any:
        print(
            f"wav: bind {bind} recusado (use 127.0.0.1)",
            file=sys.stderr,
        )
        raise SystemExit(2)


def _parse_host(raw: str) -> str:
    h = (raw or "").strip().lower()
    if not h:
        return ""
    if h.startswith("["):
        end = h.find("]")
        if end == -1:
            return h
        return h[1:end].strip()
    return h.split(":")[0].strip()


def _check_cli(argv) -> str:
    """Roda a checagem de login sem capturar nem devolver a saida."""
    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
        )
    except FileNotFoundError:
        return "ausente"
    except subprocess.TimeoutExpired:
        return "erro"
    except OSError:
        return "erro"
    return "logado" if proc.returncode == 0 else "sem-login"


def build_server(config: Config, token: str) -> ThreadingHTTPServer:
    sem = threading.BoundedSemaphore(max(1, config.max_concurrency))
    allowed = {a.lower() for a in config.allowed_hosts}
    login_cache = {}
    login_lock = threading.Lock()

    def _clis_status():
        # A trava cobre a checagem inteira: concorrentes esperam e leem o cache.
        ttl = config.login_check_ttl
        with login_lock:
            now = time.monotonic()
            result = {}
            for name, argv in config.login_checks.items():
                cached = login_cache.get(name)
                if cached is not None and cached[0] > now:
                    result[name] = cached[1]
                    continue
                status = _check_cli(argv)
                login_cache[name] = (time.monotonic() + ttl, status)
                result[name] = status
            return result

    class Handler(BaseHTTPRequestHandler):
        server_version = "wav"
        sys_version = ""
        # Conexão lenta não segura thread nem vaga para sempre.
        timeout = 30

        def log_message(self, *args):
            pass

        def _send_json(self, code: int, obj: dict) -> None:
            try:
                body = json.dumps(obj).encode("utf-8")
            except Exception:
                body = b'{"error":"encode"}'
            try:
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _log(self, task: str, rc, ms: int, status: int) -> None:
            print(
                f"wav task={task} rc={rc} ms={ms} status={status}",
                file=sys.stderr,
            )

        def do_GET(self):
            start = time.monotonic()
            path, sep, query = self.path.partition("?")
            if path == "/healthz":
                if sep and query == "clis=1":
                    payload = {"ok": True, "clis": _clis_status()}
                else:
                    payload = {"ok": True}
                ms = int((time.monotonic() - start) * 1000)
                self._send_json(200, payload)
                self._log("-", "-", ms, 200)
                return
            ms = int((time.monotonic() - start) * 1000)
            self._send_json(404, {"error": "not found"})
            self._log("-", "-", ms, 404)

        def do_POST(self):
            if self.path == "/healthz":
                ms = 0
                self._send_json(404, {"error": "not found"})
                self._log("-", "-", ms, 404)
                return
            if self.path != "/v1/vision":
                ms = 0
                self._send_json(404, {"error": "not found"})
                self._log("-", "-", ms, 404)
                return
            self._handle_vision()

        def _method_not_allowed(self):
            self._send_json(405, {"error": "method not allowed"})
            self._log("-", "-", 0, 405)

        do_PUT = _method_not_allowed
        do_DELETE = _method_not_allowed
        do_PATCH = _method_not_allowed
        do_OPTIONS = _method_not_allowed
        do_HEAD = _method_not_allowed
        do_TRACE = _method_not_allowed

        def _handle_vision(self):
            start = time.monotonic()

            def finish(code: int, obj: dict, task="-", rc="-"):
                ms = int((time.monotonic() - start) * 1000)
                self._send_json(code, obj)
                self._log(task, rc, ms, code)
                return ms

            host = _parse_host(self.headers.get("Host", ""))
            if host not in allowed:
                finish(421, {"error": "bad host"})
                return
            auth = self.headers.get("Authorization", "")
            if not hmac.compare_digest(
                auth.encode("utf-8", "surrogateescape"),
                ("Bearer " + token).encode("utf-8"),
            ):
                finish(401, {"error": "unauthorized"})
                return
            raw_cl = self.headers.get("Content-Length")
            try:
                cl = int(raw_cl) if raw_cl is not None else None
            except ValueError:
                cl = None
            if cl is None or cl < 0 or cl > config.max_body:
                finish(413, {"error": "body too large"})
                return
            try:
                raw = self.rfile.read(cl) if cl > 0 else b""
            except Exception:
                finish(400, {"error": "bad body"})
                return
            try:
                obj = json.loads(raw.decode("utf-8"))
            except Exception:
                finish(400, {"error": "bad json"})
                return
            if not isinstance(obj, dict):
                finish(400, {"error": "bad json"})
                return
            task_id = obj.get("task_id")
            if (
                not isinstance(task_id, str)
                or not TASK_RE.match(task_id)
            ):
                finish(400, {"error": "bad task_id"})
                return
            spec = obj.get("spec")
            if (
                not isinstance(spec, str)
                or not spec.strip()
                or len(spec) > 16000
            ):
                finish(400, {"error": "bad spec"}, task=task_id)
                return
            img_b64 = obj.get("image_b64")
            img_bytes = None
            if img_b64 is not None and img_b64 != "":
                if not isinstance(img_b64, str):
                    finish(400, {"error": "bad image"}, task=task_id)
                    return
                try:
                    img_bytes = base64.b64decode(img_b64, validate=True)
                except (binascii.Error, ValueError):
                    finish(400, {"error": "bad image"}, task=task_id)
                    return
            raw_ext = obj.get("image_ext", ".png")
            if not isinstance(raw_ext, str):
                finish(400, {"error": "bad image_ext"}, task=task_id)
                return
            ext = raw_ext.lower()
            if ext not in ALLOWED_EXTS:
                finish(400, {"error": "bad image_ext"}, task=task_id)
                return
            if "timeout_seconds" in obj and obj["timeout_seconds"] is not None:
                tv = obj["timeout_seconds"]
                if isinstance(tv, bool) or not isinstance(tv, int):
                    finish(400, {"error": "bad timeout"}, task=task_id)
                    return
            else:
                tv = config.max_timeout
            timeout_s = min(max(tv, config.min_timeout), config.max_timeout)

            try:
                providers = _parse_providers_field(obj.get("providers"))
            except ValueError:
                finish(400, {"error": "bad providers"}, task=task_id)
                return

            fast = obj.get("fast")
            if fast is not None and not isinstance(fast, bool):
                finish(400, {"error": "bad fast"}, task=task_id)
                return
            question = obj.get("question")
            if fast is True:
                if not (providers is None or providers == ["zcode"]):
                    finish(400, {"error": "bad fast"}, task=task_id)
                    return
                if (
                    not isinstance(question, str)
                    or not question.strip()
                    or len(question) > 4000
                ):
                    finish(400, {"error": "bad question"}, task=task_id)
                    return

            if not sem.acquire(timeout=2):
                finish(503, {"error": "busy"}, task=task_id)
                return
            usage = None
            try:
                if fast is True:
                    rc, out, usage = _run_fast(
                        config, task_id, question, img_bytes, ext, timeout_s
                    )
                else:
                    rc, out = _run_worker(
                        config, task_id, spec, img_bytes, ext, timeout_s, providers
                    )
            finally:
                try:
                    sem.release()
                except ValueError:
                    pass
            ms = int((time.monotonic() - start) * 1000)
            tail = out[-config.max_stdout :] if out else b""
            try:
                text = tail.decode("utf-8", errors="replace")
            except Exception:
                text = ""
            resp = {"rc": rc, "stdout": text, "elapsed_ms": ms}
            if fast is True:
                resp["fast"] = True
                if usage:
                    resp["usage"] = usage
            self._send_json(200, resp)
            self._log(task_id, rc, ms, 200)

    server = ThreadingHTTPServer((config.bind, config.port), Handler)
    server.daemon_threads = True
    return server


def _run_worker(config: Config, task_id: str, spec: str, img_bytes, ext: str, timeout_s: int,
                providers=None):
    with tempfile.TemporaryDirectory(prefix="wav-") as tmpd:
        try:
            os.chmod(tmpd, 0o700)
        except OSError:
            pass
        indir = os.path.join(tmpd, "in")
        os.mkdir(indir)
        if img_bytes is not None:
            with open(os.path.join(indir, "image" + ext), "wb") as f:
                f.write(img_bytes)
        cmd = [
            config.worker_ask,
            "--cwd", indir,
            "--orchestrator", config.orchestrator,
            "--mode", "plan",
            "--timeout", str(timeout_s),
            "--step-timeout", str(timeout_s),
            "--task-id", task_id,
        ]
        if providers:
            cmd.extend(["--providers", ",".join(providers)])
        if config.no_muse:
            cmd.append("--no-muse")
        # O filho roda worker-ask e os CLIs de IA sobre imagem externa: nao
        # herda o token do servico nem as WAV_* de configuracao.
        env = {
            k: v
            for k, v in os.environ.items()
            if k != "WORKER_ASK_VISION_TOKEN" and not k.startswith("WAV_")
        }
        env["WORKER_ASK_MIN_TIMEOUT"] = str(timeout_s)
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                env=env,
            )
        except FileNotFoundError:
            return 127, b""
        except OSError:
            return 127, b""
        try:
            out, _ = proc.communicate(
                spec.encode("utf-8"), timeout=timeout_s + 15
            )
            return proc.returncode, out or b""
        except subprocess.TimeoutExpired:
            try:
                import signal as _sig

                os.killpg(proc.pid, _sig.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, _sig.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
                try:
                    proc.wait(timeout=3)
                except Exception:
                    pass
            try:
                proc.communicate(timeout=2)
            except Exception:
                pass
            return 124, b""


def _run_fast(config: Config, task_id: str, question: str, img_bytes, ext: str, timeout_s: int):
    with tempfile.TemporaryDirectory(prefix="wav-") as tmpd:
        try:
            os.chmod(tmpd, 0o700)
        except OSError:
            pass
        indir = os.path.join(tmpd, "in")
        os.mkdir(indir)
        img_path = None
        if img_bytes is not None:
            img_path = os.path.join(indir, "image" + ext)
            with open(img_path, "wb") as f:
                f.write(img_bytes)
        qpath = os.path.join(tmpd, "question.md")
        with open(qpath, "w", encoding="utf-8") as f:
            f.write(question)
        cmd = [
            config.zcode_worker,
            "--chat",
            "--mode",
            "plan",
            "--model",
            "glm-5.3-flash",
            "--effort",
            "low",
            "--cwd",
            indir,
            "--queue-timeout",
            "10",
            "--json",
            "--spec",
            qpath,
        ]
        if img_path is not None:
            cmd.extend(["--attach", os.path.abspath(img_path)])
        env = {
            k: v
            for k, v in os.environ.items()
            if k != "WORKER_ASK_VISION_TOKEN" and not k.startswith("WAV_")
        }
        env["ZCODE_WORKER_ALLOW_PAID"] = "1"
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                env=env,
            )
        except FileNotFoundError:
            return 127, b"", None
        except OSError:
            return 127, b"", None
        try:
            out, _ = proc.communicate(timeout=timeout_s + 15)
        except subprocess.TimeoutExpired:
            try:
                import signal as _sig

                os.killpg(proc.pid, _sig.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, _sig.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
                try:
                    proc.wait(timeout=3)
                except Exception:
                    pass
            try:
                proc.communicate(timeout=2)
            except Exception:
                pass
            return 124, b"", None
        if proc.returncode != 0:
            return proc.returncode, b"", None
        raw_out = out or b""
        lines = [ln for ln in raw_out.splitlines() if ln.strip()]
        if not lines:
            return 1, b"", None
        try:
            obj = json.loads(lines[-1].decode("utf-8", errors="strict"))
        except Exception:
            try:
                obj = json.loads(lines[-1])
            except Exception:
                return 1, b"", None
        if not isinstance(obj, dict):
            return 1, b"", None
        if obj.get("status") != "ok":
            return 1, b"", None
        response = obj.get("response")
        if not isinstance(response, str):
            return 1, b"", None
        answer = response.strip()
        contrato = {
            "schema_version": 1,
            "task_id": task_id,
            "execution_state": "completed",
            "changed_files": [],
            "validation_owner": "orchestrator",
            "approval": "pending",
            "answer": answer,
            "image_seen": img_bytes is not None,
            "fast": True,
        }
        texto = "```json\n" + json.dumps(contrato, ensure_ascii=False) + "\n```\n"
        usage = obj.get("usage")
        usage_out = None
        if isinstance(usage, dict):
            u = {}
            if isinstance(usage.get("inputTokens"), int) and not isinstance(
                usage.get("inputTokens"), bool
            ):
                u["input"] = usage.get("inputTokens")
            if isinstance(usage.get("outputTokens"), int) and not isinstance(
                usage.get("outputTokens"), bool
            ):
                u["output"] = usage.get("outputTokens")
            usage_out = u
        return 0, texto.encode("utf-8"), usage_out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="worker-ask vision bridge")
    parser.add_argument(
        "--check-config", action="store_true", help="valida config + token"
    )
    args = parser.parse_args(argv)
    config = Config.from_env()
    validate_bind(config.bind)
    token = resolve_token(config)
    if args.check_config:
        print("wav: config ok", file=sys.stderr)
        return 0
    server = build_server(config, token)

    def _on_term(signum, frame):
        raise KeyboardInterrupt()

    try:
        signal.signal(signal.SIGTERM, _on_term)
    except (ValueError, OSError):
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            server.server_close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
