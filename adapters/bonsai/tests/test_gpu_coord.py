"""Testes offline da coordenação GPU (jogo > Whisper > Bonsai).

Nada real: sem systemctl, sem DBus e sem segredos — stop/start/estado/
GameMode são falsos injetados e o estado vive em diretório temporário. A
contenção de flock é simulada com um segundo descritor no mesmo arquivo.
Rodar com: python3 -m unittest discover -s tests -v
"""

import fcntl
import inspect
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import gpu_coord as gc  # noqa: E402


class BaseTemp(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state_dir = Path(tmp.name) / "state"
        self.lock = self.state_dir / "whisper.lock"
        self.phase = self.state_dir / "whisper-phase.json"
        self.cancel = self.state_dir / "cancel-token"

    def escrever_fase(self, conteudo):
        self.phase.parent.mkdir(parents=True, exist_ok=True)
        self.phase.write_text(conteudo, encoding="utf-8")

    def segurar_lock(self):
        """Contenção real de flock (outro descritor, mesmo arquivo)."""
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # LIFO: unlock roda antes do close (flock em fd fechado dá EBADF).
        self.addCleanup(os.close, fd)
        self.addCleanup(fcntl.flock, fd, fcntl.LOCK_UN)

    def fazer_lease(self, estado="inactive", gamemode=("ok", 0)):
        """Lease com stop/start/estado/GameMode falsos e ganchos de corrida."""
        f = {
            "eventos": [],
            "fase_no_start": [],
            "estado": estado,
            "gamemode": gamemode,
            "stop_ok": True,
            "start_ok": True,
            "no_stop": None,
            "during_start": None,
        }

        def stopper():
            f["eventos"].append("stop")
            if f["stop_ok"]:
                f["estado"] = "inactive"
            if f["no_stop"]:
                f["no_stop"]()
            return f["stop_ok"]

        def starter():
            f["eventos"].append("start")
            try:
                fase = json.loads(self.phase.read_text())["phase"]
            except (OSError, ValueError, KeyError):
                fase = None
            f["fase_no_start"].append(fase)
            if f["during_start"]:
                f["during_start"]()
            return f["start_ok"]

        lease = gc.WhisperLease(
            state_dir=self.state_dir,
            stopper=stopper,
            starter=starter,
            state_checker=lambda: f["estado"],
            gamemode_checker=lambda: f["gamemode"],
        )
        return lease, f


class TestUnavailable(unittest.TestCase):
    def test_code_e_message_acessiveis_e_seguros(self):
        exc = gc.Unavailable("whisper_busy", "Whisper já está usando a GPU.")
        self.assertIsInstance(exc, Exception)
        self.assertEqual(exc.code, "whisper_busy")
        self.assertEqual(exc.message, "Whisper já está usando a GPU.")
        self.assertEqual(str(exc), "Whisper já está usando a GPU.")


class TestWhisperBusy(BaseTemp):
    def test_sem_lock_e_livre_mesmo_com_marcador_stale(self):
        self.escrever_fase('{"phase": "whisper"}')
        self.assertFalse(gc.whisper_busy(self.lock, self.phase))
        self.escrever_fase('{"phase": "restoring", "pid": 424242}')
        self.assertFalse(gc.whisper_busy(self.lock, self.phase))

    def test_lock_held_com_fase_whisper_esta_ocupado(self):
        self.segurar_lock()
        self.escrever_fase('{"phase": "whisper"}')
        self.assertTrue(gc.whisper_busy(self.lock, self.phase))

    def test_lock_held_em_restoring_nao_bloqueia_bonsai(self):
        self.segurar_lock()
        self.escrever_fase(json.dumps({"phase": "restoring", "pid": os.getpid()}))
        self.assertFalse(gc.whisper_busy(self.lock, self.phase))

    def test_restoring_de_pid_morto_fail_closed(self):
        self.segurar_lock()
        self.escrever_fase('{"phase": "restoring", "pid": 424242}')
        with unittest.mock.patch.object(gc.os, "kill", side_effect=ProcessLookupError):
            self.assertTrue(gc.whisper_busy(self.lock, self.phase))

    def test_json_de_tipo_errado_fail_closed(self):
        self.segurar_lock()
        for conteudo in ("[]", '"restoring"', "7"):
            self.escrever_fase(conteudo)
            self.assertTrue(gc.whisper_busy(self.lock, self.phase))

    def test_so_blockingioerror_permite_examinar_fase(self):
        self.escrever_fase(json.dumps({"phase": "restoring", "pid": os.getpid()}))
        with unittest.mock.patch.object(gc.fcntl, "flock", side_effect=BlockingIOError):
            self.assertFalse(gc.whisper_busy(self.lock, self.phase))

    def test_outro_oserror_no_flock_fail_closed(self):
        with unittest.mock.patch.object(gc.fcntl, "flock", side_effect=OSError("EIO")):
            self.assertTrue(gc.whisper_busy(self.lock, self.phase))

    def test_lock_held_sem_fase_ou_corrompida_e_fail_closed(self):
        self.segurar_lock()
        self.assertTrue(gc.whisper_busy(self.lock, self.phase))
        self.escrever_fase('{"phase": ')
        self.assertTrue(gc.whisper_busy(self.lock, self.phase))

    def test_erro_de_caminho_e_fail_closed(self):
        self.state_dir.mkdir(parents=True)
        (self.state_dir / "bloqueio").write_text("arquivo", encoding="utf-8")
        ruim = self.state_dir / "bloqueio" / "whisper.lock"
        self.assertTrue(gc.whisper_busy(ruim, self.phase))

    def test_lock_criado_com_permissao_600(self):
        self.assertFalse(gc.whisper_busy(self.lock, self.phase))
        self.assertEqual(self.lock.stat().st_mode & 0o777, 0o600)


class TestCancelResume(BaseTemp):
    def test_token_unico_atomico_e_600(self):
        um = gc.cancel_resume(self.cancel)
        dois = gc.cancel_resume(self.cancel)
        self.assertEqual(self.cancel.read_text(encoding="utf-8"), dois)
        self.assertNotEqual(um, dois)
        self.assertEqual(self.cancel.stat().st_mode & 0o777, 0o600)

    def test_main_grava_token(self):
        with unittest.mock.patch.object(gc, "CANCEL_PATH", self.cancel):
            self.assertEqual(gc.main(["cancel-resume"]), 0)
        self.assertTrue(self.cancel.exists())

    def test_main_arg_invalido_nao_grava(self):
        self.assertEqual(gc.main(["outro"]), 2)
        self.assertFalse(self.cancel.exists())


class TestLeaseEntrada(BaseTemp):
    def test_caminho_feliz_para_e_restaura_em_restoring(self):
        lease, f = self.fazer_lease(estado="active")
        with lease:
            self.assertEqual(f["eventos"], ["stop"])
            self.assertEqual(json.loads(self.phase.read_text())["phase"], "whisper")
            self.assertTrue(gc.whisper_busy(self.lock, self.phase))
        self.assertEqual(f["eventos"], ["stop", "start"])
        self.assertEqual(f["fase_no_start"], ["restoring"])
        self.assertFalse(self.phase.exists())
        self.assertFalse(gc.whisper_busy(self.lock, self.phase))

    def test_bonsai_ja_parado_nao_para_nem_reinicia(self):
        lease, f = self.fazer_lease(estado="inactive")
        with lease:
            pass
        self.assertEqual(f["eventos"], [])
        self.assertFalse(self.phase.exists())
        self.assertFalse(gc.whisper_busy(self.lock, self.phase))

    def test_gamemode_ativo_recusa_sem_tocar_nada(self):
        lease, f = self.fazer_lease(estado="active", gamemode=("active", 1))
        with self.assertRaises(gc.Unavailable) as captura:
            lease.__enter__()
        self.assertEqual(captura.exception.code, "gamemode_active")
        self.assertEqual(f["eventos"], [])
        self.assertFalse(self.phase.exists())
        self.assertFalse(gc.whisper_busy(self.lock, self.phase))

    def test_gamemode_desconhecido_recusa_fail_closed(self):
        lease, _ = self.fazer_lease(gamemode=("unknown", None))
        with self.assertRaises(gc.Unavailable) as captura:
            lease.__enter__()
        self.assertEqual(captura.exception.code, "gamemode_unknown")

    def test_estados_desconhecidos_nunca_liberam_gpu(self):
        for estado in ("unknown", "deactivating", "reloading"):
            with self.subTest(estado=estado):
                lease, f = self.fazer_lease(estado=estado)
                with self.assertRaises(gc.Unavailable) as captura:
                    lease.__enter__()
                self.assertEqual(captura.exception.code, "bonsai_state_unknown")
                self.assertEqual(f["eventos"], [])

    def test_fase_whisper_antes_do_snapshot_de_estado(self):
        observado = []

        def state_checker():
            observado.append(json.loads(self.phase.read_text(encoding="utf-8")))
            return "inactive"

        lease = gc.WhisperLease(
            state_dir=self.state_dir,
            stopper=lambda: True,
            starter=lambda: True,
            state_checker=state_checker,
            gamemode_checker=lambda: ("ok", 0),
        )
        with lease:
            pass
        self.assertEqual(observado, [{"phase": "whisper", "pid": os.getpid()}])

    def test_lock_ocupado_recusa_whisper_busy_sem_parar_bonsai(self):
        self.segurar_lock()
        lease, f = self.fazer_lease(estado="active")
        with self.assertRaises(gc.Unavailable) as captura:
            lease.__enter__()
        self.assertEqual(captura.exception.code, "whisper_busy")
        self.assertEqual(f["eventos"], [])

    def test_aquisicao_re_tenta_bloqueio_curto(self):
        lease = gc.WhisperLease(state_dir=self.state_dir)
        with unittest.mock.patch.object(
            gc.fcntl, "flock", side_effect=[BlockingIOError, None, None]
        ) as flock, unittest.mock.patch.object(gc.time, "sleep") as dormir:
            lease._acquire_lock()
            lease._cleanup()
        self.assertEqual(flock.call_count, 3)
        dormir.assert_called_once()

    def test_estado_do_bonsai_ilegivel_e_fail_closed(self):
        lease, f = self.fazer_lease(estado=None)
        with self.assertRaises(gc.Unavailable) as captura:
            lease.__enter__()
        self.assertEqual(captura.exception.code, "bonsai_state_unknown")
        self.assertEqual(f["eventos"], [])

    def test_stop_falho_recusa_e_limpa(self):
        lease, f = self.fazer_lease(estado="active")
        f["stop_ok"] = False
        with self.assertRaises(gc.Unavailable) as captura:
            lease.__enter__()
        self.assertEqual(captura.exception.code, "bonsai_stop_failed")
        self.assertFalse(self.phase.exists())
        self.assertFalse(gc.whisper_busy(self.lock, self.phase))

    def test_stop_sem_confirmacao_de_unidade_morta_recusa(self):
        eventos = []
        lease = gc.WhisperLease(
            state_dir=self.state_dir,
            stopper=lambda: eventos.append("stop") or True,  # "para" sem efeito
            starter=lambda: True,
            state_checker=lambda: "active",
            gamemode_checker=lambda: ("ok", 0),
        )
        with self.assertRaises(gc.Unavailable) as captura:
            lease.__enter__()
        self.assertEqual(captura.exception.code, "bonsai_stop_failed")
        self.assertEqual(eventos, ["stop"])
        self.assertFalse(self.phase.exists())

    def test_gamemode_ativou_logo_apos_o_stop_recusa(self):
        lease, f = self.fazer_lease(estado="active")
        f["no_stop"] = lambda: f.update(gamemode=("active", 1))
        with self.assertRaises(gc.Unavailable) as captura:
            lease.__enter__()
        self.assertEqual(captura.exception.code, "gamemode_active")
        self.assertFalse(self.phase.exists())
        self.assertFalse(gc.whisper_busy(self.lock, self.phase))


class TestLeaseSaida(BaseTemp):
    def test_cancel_restore_do_job_impede_reinicio(self):
        lease, f = self.fazer_lease(estado="active")
        with lease as precisa:
            precisa.cancel_restore()
        self.assertEqual(f["eventos"], ["stop"])
        self.assertFalse(self.phase.exists())
        self.assertFalse(gc.whisper_busy(self.lock, self.phase))

    def test_stop_manual_durante_o_job_impede_reinicio(self):
        lease, f = self.fazer_lease(estado="active")
        with lease:
            gc.cancel_resume(self.cancel)
        self.assertEqual(f["eventos"], ["stop"])
        self.assertFalse(self.phase.exists())

    def test_gamemode_ativou_durante_o_job_impede_reinicio(self):
        lease, f = self.fazer_lease(estado="active")
        with lease:
            f["gamemode"] = ("active", 1)
        self.assertEqual(f["eventos"], ["stop"])
        self.assertFalse(self.phase.exists())
        self.assertFalse(gc.whisper_busy(self.lock, self.phase))

    def test_stop_manual_durante_starter_para_bonsai_antes_de_liberar(self):
        lease, f = self.fazer_lease(estado="active")
        f["during_start"] = lambda: gc.cancel_resume(self.cancel)
        with lease:
            pass
        self.assertEqual(f["eventos"], ["stop", "start", "stop"])
        self.assertFalse(self.phase.exists())

    def test_gamemode_ativado_durante_starter_para_bonsai_antes_de_liberar(self):
        lease, f = self.fazer_lease(estado="active")
        f["during_start"] = lambda: f.update(gamemode=("active", 1))
        with lease:
            pass
        self.assertEqual(f["eventos"], ["stop", "start", "stop"])
        self.assertFalse(self.phase.exists())

    def test_excecao_no_corpo_propaga_limpa_e_restaura(self):
        lease, f = self.fazer_lease(estado="active")
        with self.assertRaises(RuntimeError):
            with lease:
                raise RuntimeError("filho morreu")
        self.assertEqual(f["eventos"], ["stop", "start"])
        self.assertFalse(self.phase.exists())
        self.assertFalse(gc.whisper_busy(self.lock, self.phase))

    def test_starter_falho_nao_lanca_e_limpa(self):
        lease, f = self.fazer_lease(estado="active")
        f["start_ok"] = False
        with lease:
            pass
        self.assertEqual(f["eventos"], ["stop", "start"])
        self.assertFalse(self.phase.exists())
        self.assertFalse(gc.whisper_busy(self.lock, self.phase))


class TestArgvFixos(unittest.TestCase):
    def test_units_e_argv_somente_do_bonsai(self):
        self.assertEqual(
            gc.STOP_BONSAI_ARGV,
            ["systemctl", "--user", "stop", "bonsai-llama.service"],
        )
        self.assertEqual(
            gc.START_BONSAI_ARGV,
            ["systemctl", "--user", "start", "bonsai-llama.service"],
        )
        self.assertEqual(
            gc.BONSAI_STATE_ARGV,
            [
                "systemctl", "--user", "show", "bonsai-llama.service",
                "--property=ActiveState,MainPID",
            ],
        )

    def test_stop_e_start_usam_argv_fixo_e_timeout_10(self):
        with unittest.mock.patch.object(gc.subprocess, "run") as fake:
            fake.return_value = unittest.mock.Mock(returncode=0)
            self.assertTrue(gc._stop_bonsai())
            self.assertTrue(gc._start_bonsai())
        argvs = [chamada.args[0] for chamada in fake.call_args_list]
        self.assertEqual(argvs, [gc.STOP_BONSAI_ARGV, gc.START_BONSAI_ARGV])
        for chamada in fake.call_args_list:
            self.assertEqual(chamada.kwargs.get("timeout"), 10.0)
            self.assertIs(chamada.kwargs.get("check"), False)

    def test_erro_ou_timeout_de_systemctl_sao_tratados(self):
        with unittest.mock.patch.object(
            gc.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd="systemctl", timeout=10),
        ):
            self.assertFalse(gc._stop_bonsai())
            self.assertIsNone(gc._bonsai_state())
        with unittest.mock.patch.object(
            gc.subprocess, "run", side_effect=OSError("sem systemctl")
        ):
            self.assertFalse(gc._start_bonsai())

    def test_estado_vira_string_ou_none(self):
        with unittest.mock.patch.object(gc.subprocess, "run") as fake:
            fake.return_value = unittest.mock.Mock(returncode=0, stdout="active\n123\n")
            self.assertEqual(gc._bonsai_state(), "active")
            fake.return_value = unittest.mock.Mock(returncode=0, stdout="inactive\n0\n")
            self.assertEqual(gc._bonsai_state(), "inactive")
            fake.return_value = unittest.mock.Mock(returncode=3, stdout="inactive\n0\n")
            self.assertIsNone(gc._bonsai_state())

    def test_estado_parser_exige_pid_coerente_e_estado_conhecido(self):
        casos = {
            "active\n0\n": None,
            "inactive\n9\n": None,
            "deactivating\n0\n": None,
            "active\nabc\n": None,
            "ActiveState=failed\nMainPID=0\n": "failed",
            "ActiveState=active\nMainPID=77\n": "active",
            "MainPID=77\nActiveState=active\n": "active",
        }
        for stdout, esperado in casos.items():
            with self.subTest(stdout=stdout):
                self.assertEqual(gc._parse_bonsai_state(stdout), esperado)


class TestSemBypass(unittest.TestCase):
    def test_modulo_nao_le_ambiente(self):
        fonte = inspect.getsource(gc)
        self.assertNotIn("environ", fonte)


if __name__ == "__main__":
    unittest.main()
