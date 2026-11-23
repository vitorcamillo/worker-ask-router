import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "cli-update"
PKGS = "@a/one,two"

FAKE_NPM = r'''#!/usr/bin/env python3
import json, os, sys
ctl = json.load(open(os.environ["FAKE_CTL"]))
a = sys.argv[1:]
if a[0] == "view":
    print(ctl["latest"][a[1]]); sys.exit(0)
if a[0] == "root":
    print(ctl["root"]); sys.exit(0)
if a[0] == "install":
    open(ctl["log"], "a").write("install\n")
    if ctl.get("install_fail"):
        sys.exit(1)
    d = a[a.index("--prefix") + 1]
    for spec in [x for x in a if "@" in x[1:]]:
        name, ver = spec.rsplit("@", 1)
        pd = os.path.join(d, "lib/node_modules", name)
        os.makedirs(pd)
        b = os.path.basename(name)
        json.dump({"name": name, "version": ver, "bin": {b: "x.js"}}, open(pd + "/package.json", "w"))
        os.makedirs(d + "/bin", exist_ok=True)
        p = d + "/bin/" + b
        open(p, "w").write("#!/bin/sh\nexit %d\n" % (1 if ctl.get("smoke_fail") else 0))
        os.chmod(p, 0o755)
    sys.exit(0)
sys.exit(2)
'''


@pytest.fixture
def env(tmp_path):
    groot = tmp_path / "global"
    for n, v in (("@a/one", "1.0.0"), ("two", "2.0.0")):
        d = groot / n
        d.mkdir(parents=True)
        (d / "package.json").write_text(json.dumps({"version": v}))
    npm = tmp_path / "npm"
    npm.write_text(FAKE_NPM)
    npm.chmod(0o755)
    ctl = tmp_path / "ctl.json"
    e = {
        "PATH": os.environ["PATH"], "HOME": str(tmp_path),
        "WORKER_CLI_ROOT": str(tmp_path / "root"), "CLI_UPDATE_NPM": str(npm),
        "CLI_UPDATE_GLOBAL_ROOT": str(groot), "CLI_UPDATE_PACKAGES": PKGS,
        "FAKE_CTL": str(ctl),
    }
    e["_ctl"], e["_log"], e["_tmp"] = ctl, tmp_path / "log", tmp_path
    set_ctl(e, {"one": "1.1.0", "two": "2.0.0"})
    return e


def set_ctl(e, latest, **extra):
    d = {"latest": {"@a/one": latest["one"], "two": latest["two"]},
         "root": "", "log": str(e["_log"])}
    d.update(extra)
    Path(e["_ctl"]).write_text(json.dumps(d))


def run(e, *args, **over):
    ee = {k: v for k, v in e.items() if not k.startswith("_")}
    ee.update(over)
    return subprocess.run([sys.executable, str(SCRIPT), *args], env=ee, capture_output=True, text=True)


def root(e):
    return Path(e["WORKER_CLI_ROOT"])


def installs(e):
    return e["_log"].read_text().count("install") if e["_log"].exists() else 0


def releases(e):
    d = root(e) / "releases"
    return sorted(x.name for x in d.iterdir()) if d.exists() else []


def test_update_creates_current(env):
    r = run(env)
    assert r.returncode == 0, r.stderr
    assert "@a/one 1.0.0 -> 1.1.0" in r.stdout
    cur = root(env) / "current"
    assert cur.is_symlink() and not os.path.isabs(os.readlink(cur))
    assert (cur / "bin" / "one").exists()
    assert len(releases(env)) == 1


def test_up_to_date_noop(env):
    set_ctl(env, {"one": "1.0.0", "two": "2.0.0"})
    r = run(env)
    assert r.returncode == 0 and "atualizado" in r.stdout
    assert installs(env) == 0 and not (root(env) / "current").exists()


def test_install_fail(env):
    set_ctl(env, {"one": "1.1.0", "two": "2.0.0"}, install_fail=True)
    r = run(env)
    assert r.returncode == 1 and "falhou" in r.stderr
    assert not (root(env) / "current").exists() and releases(env) == []


def test_smoke_fail_keeps_current(env):
    assert run(env).returncode == 0
    before = os.readlink(root(env) / "current")
    set_ctl(env, {"one": "1.2.0", "two": "2.0.0"}, smoke_fail=True)
    r = run(env)
    assert r.returncode == 1
    assert os.readlink(root(env) / "current") == before
    assert len(releases(env)) == 1


def test_second_update_previous_and_prune(env):
    import time
    assert run(env).returncode == 0
    first = os.readlink(root(env) / "current")
    time.sleep(1.1)
    set_ctl(env, {"one": "1.2.0", "two": "2.0.0"})
    assert run(env).returncode == 0
    time.sleep(1.1)
    second = os.readlink(root(env) / "current")
    assert os.readlink(root(env) / "previous") == first
    set_ctl(env, {"one": "1.3.0", "two": "2.0.0"})
    assert run(env).returncode == 0
    assert os.readlink(root(env) / "previous") == second
    assert len(releases(env)) == 2 and first.split("/")[1] not in releases(env)


def test_rollback(env):
    import time
    assert run(env).returncode == 0
    first = os.readlink(root(env) / "current")
    time.sleep(1.1)
    set_ctl(env, {"one": "1.2.0", "two": "2.0.0"})
    assert run(env).returncode == 0
    second = os.readlink(root(env) / "current")
    assert run(env, "--rollback").returncode == 0
    assert os.readlink(root(env) / "current") == first
    assert os.readlink(root(env) / "previous") == second


def test_rollback_without_previous(env):
    assert run(env, "--rollback").returncode == 1


def test_check_does_not_change(env):
    r = run(env, "--check")
    assert r.returncode == 0 and "1.0.0 -> 1.1.0" in r.stdout
    assert installs(env) == 0 and not (root(env) / "current").exists()


def test_disabled(env):
    r = run(env, WORKER_AUTO_UPDATE="0")
    assert r.returncode == 0 and "desativado" in r.stdout
    assert installs(env) == 0


def test_invalid_package_name(env):
    r = run(env, CLI_UPDATE_PACKAGES="@a/one,Bad;rm")
    assert r.returncode == 1 and "invalido" in r.stderr
    assert installs(env) == 0


def test_no_downgrade(env):
    set_ctl(env, {"one": "0.9.0", "two": "2.0.0"})
    r = run(env)
    assert r.returncode == 0 and "atualizado" in r.stdout
    assert installs(env) == 0
