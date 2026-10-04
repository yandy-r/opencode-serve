#!/usr/bin/env python3
"""Isolated stdlib checks for serve.py. Run: python3 test_serve.py
Fakes `op` and `opencode` in a temp dir; never touches real HOME, XDG dirs,
systemd, tailscale, or network. Simulates first install, repeat install with
changed secret refs, failed op fetch leaving working config, special-value
secrets, and unit rendering.
"""
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).parent
failures = []


def check(name, condition):
    print(("PASS" if condition else "FAIL"), name)
    if not condition:
        failures.append(name)


def load():
    spec = importlib.util.spec_from_file_location("opencode_serve", REPO / "serve.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_fake_home(base):
    home = base / "home"
    (home / ".config/opencode").mkdir(parents=True)
    (home / ".config/opencode/opencode.json").write_text('{" providers": {}}')
    (home / ".config/opencode/other.txt").write_text("host")
    return home


def fake_bin(base):
    bindir = base / "bin"
    bindir.mkdir()
    (bindir / "opencode").write_text("#!/bin/sh\nexit 0\n")
    (bindir / "opencode").chmod(0o755)
    # `op` shim reads op_state.json {"fail":bool,"values":{ref:val}}
    shim = bindir / "op"
    shim.write_text(f"#!{sys.executable}\n"
                    "import json,sys\n"
                    f"cfg=json.load(open({str(base / 'op_state.json')!r}))\n"
                    "if cfg.get('fail'): sys.exit(1)\n"
                    "out=cfg['values'].get(sys.argv[-1],'')\n"
                    "if not out: sys.exit(1)\n"
                    "sys.stdout.write(out)\n")
    shim.chmod(0o755)
    return bindir


def do_install(mod, base, home, bindir, refs_obj, root):
    env = dict(os.environ)
    env.update({"HOME": str(home), "PATH": str(bindir),
                "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_DATA_HOME": str(home / ".local/share"),
                "XDG_STATE_HOME": str(home / ".local/state"),
                "XDG_CACHE_HOME": str(home / ".cache")})
    sec = base / "refs.json"
    sec.write_text(json.dumps(refs_obj))
    proc = subprocess.run([sys.executable, str(REPO / "serve.py"), "install",
                           "--root", str(root), "--secrets", str(sec),
                           "--opencode", str(bindir / "opencode")],
                          env=env, capture_output=True, text=True, timeout=120)
    return proc


def fake_run_bin(base):
    """Fake `opencode serve`: dump argv + env to run_probe.json, exit 0."""
    bindir = base / "runbin"
    bindir.mkdir()
    dump = base / "run_probe.json"
    (bindir / "opencode").write_text(
        f"#!{sys.executable}\n"
        "import json,os,sys\n"
        f"json.dump({{'argv': sys.argv, 'env': dict(os.environ)}}, open({str(dump)!r}, 'w'))\n")
    (bindir / "opencode").chmod(0o755)
    return bindir


def do_run(mod, base, root, extra_env=None):
    env = {"PATH": "/usr/bin:/bin", "OP_SERVICE_ACCOUNT_TOKEN": "bootstrap-secret",
           "LANG": "C", "TMPDIR": "/tmp/opencode"}
    env.update(extra_env or {})
    return subprocess.run([sys.executable, str(REPO / "serve.py"), "run",
                           "--root", str(root)], env=env,
                          capture_output=True, text=True, timeout=120), base / "run_probe.json"


def main():
    with tempfile.TemporaryDirectory(prefix="oc-serve-test-", dir="/tmp/opencode") as tmp:
        base = Path(tmp)
        home = make_fake_home(base)
        root = base / "srv"
        values = {"op://v/i/k": "s3cret value $x \"q\" %f \ttab"}
        bindir = fake_bin(base)
        (base / "op_state.json").write_text(json.dumps({"values": values, "fail": False}))

        proc = do_install(load(), base, home, bindir,
                          {"TEST_SECRET": "op://v/i/k"}, root)
        check("install ok", proc.returncode == 0)
        svc = root / "config/opencode/service.json"
        check("password exists", svc.exists())
        pw = json.loads(svc.read_text())["password"]
        check("password nonempty", isinstance(pw, str) and len(pw) >= 32)
        check("service 0600", stat.S_IMODE(svc.stat().st_mode) == 0o600)
        check("runtime env fetched", json.loads((root / "runtime.json").read_text())["env"]
              == {"TEST_SECRET": "s3cret value $x \"q\" %f \ttab"})
        check("runtime mode 0600", stat.S_IMODE((root / "runtime.json").stat().st_mode) == 0o600)
        check("host config bridged", (root / "config/opencode/opencode.json").is_symlink()
              and (root / "config/opencode/other.txt").is_symlink())
        check("no service.json bridge", not (root / "config/opencode/service.json").is_symlink())
        unit = (home / ".config/systemd/user/opencode-serve.service").read_text()
        check("unit rendered", "@EXEC@" not in unit and "serve.py" in unit and "4096" not in unit)

        # repeat install preserves password, updates secret value
        values2 = {"op://v/i/k": "second \nvalue"}
        (base / "op_state.json").write_text(json.dumps({"values": values2, "fail": False}))
        proc2 = do_install(load(), base, home, bindir, {"TEST_SECRET": "op://v/i/k"}, root)
        check("reinstall ok", proc2.returncode == 0)
        check("password preserved", json.loads(svc.read_text())["password"] == pw)
        check("env updated", json.loads((root / "runtime.json").read_text())["env"]
              == {"TEST_SECRET": "second \nvalue"})

        # failed fetch leaves working config
        (base / "op_state.json").write_text(json.dumps({"values": {"op://v/i/k": ""}, "fail": True}))
        proc3 = do_install(load(), base, home, bindir, {"TEST_SECRET": "op://v/i/k"}, root)
        check("fail closed rc", proc3.returncode == 1)
        check("fail silent stderr", "op://v" not in proc3.stderr and "second" not in proc3.stderr)
        check("config kept after failure", json.loads((root / "runtime.json").read_text())["env"]
              == {"TEST_SECRET": "second \nvalue"} and json.loads(svc.read_text())["password"] == pw)

        # reserved-name refs rejected
        proc4 = do_install(load(), base, home, bindir,
                           {"OP_SERVICE_ACCOUNT_TOKEN": "op://v/i/k"}, root)
        check("reserved rejected", proc4.returncode == 1)
        check("reserved OPENCODE_DB", "OPENCODE_DB" in load().RESERVED
              and "OPENCODE_PASSWORD" in load().RESERVED)
        # non-op:// rejected
        proc5 = do_install(load(), base, home, bindir, {"X": "plain"}, root)
        check("non-ref rejected", proc5.returncode == 1)

        # --port validated, persisted; run reads runtime port
        (base / "refs.json").write_text(json.dumps({"TEST_SECRET": "op://v/i/k"}))
        (base / "op_state.json").write_text(json.dumps({"values": values2, "fail": False}))
        proc7 = subprocess.run([sys.executable, str(REPO / "serve.py"), "install",
                                "--root", str(root), "--secrets", str(base / "refs.json"),
                                "--opencode", str(bindir / "opencode"), "--port", "0"],
                               env=dict(os.environ, HOME=str(home),
                                        PATH=str(bindir),
                                        XDG_CONFIG_HOME=str(home / ".config")),
                               capture_output=True, text=True, timeout=120)
        check("port 0 rejected", proc7.returncode != 0)
        proc8 = subprocess.run([sys.executable, str(REPO / "serve.py"), "install",
                                "--root", str(root), "--secrets", str(base / "refs.json"),
                                "--opencode", str(bindir / "opencode"), "--port", "5123"],
                               env=dict(os.environ, HOME=str(home),
                                        PATH=str(bindir),
                                        XDG_CONFIG_HOME=str(home / ".config")),
                               capture_output=True, text=True, timeout=120)
        check("custom port ok", proc8.returncode == 0
              and json.loads((root / "runtime.json").read_text())["port"] == 5123)

        # launcher uses fetched values without interpolation (spot check env build)
        cfg = json.loads((root / "runtime.json").read_text())
        check("runtime has binary+path+home", cfg["binary"] and cfg["path"] and cfg["home"] == str(home))

        # run(): fake opencode records argv/env; stdout suppressed, stderr restored
        runbin = fake_run_bin(base)
        cfg = json.loads((root / "runtime.json").read_text())
        cfg["binary"] = str(runbin / "opencode")
        (root / "runtime.json").write_text(json.dumps(cfg) + "\n")
        proc_run, probe = do_run(load(), base, root)
        rec = json.loads(probe.read_text())
        check("run ok", proc_run.returncode == 0 and probe.exists())
        check("run argv", rec["argv"][1:] == ["serve", "--service", "--hostname",
                                              "127.0.0.1", "--port", "5123"])
        check("run env isolation", rec["env"]["HOME"] == str(home)
              and rec["env"]["PATH"] == cfg["path"]
              and rec["env"]["XDG_CONFIG_HOME"] == str(root / "config")
              and rec["env"]["XDG_DATA_HOME"] == str(root / "data")
              and rec["env"]["OPENCODE_DB"] == str(root / "data/server.db"))
        check("run secret passed", rec["env"]["TEST_SECRET"] == "second \nvalue")
        check("run strips bootstrap", "OP_SERVICE_ACCOUNT_TOKEN" not in rec["env"])
        check("run password absent", "OPENCODE_PASSWORD" not in rec["env"]
              and pw not in json.dumps(rec["env"]))
        check("run streams quiet", proc_run.stdout == "" and proc_run.stderr == "")

        # run refuses without/invalid password; stderr restored (diagnostic, no secret)
        svc.write_text(json.dumps({"password": ""}) + "\n")
        if probe.exists():
            probe.unlink()
        bad, _ = do_run(load(), base, root)
        check("run empty password refused", bad.returncode == 1
              and not probe.exists() and "failed" in bad.stderr and pw not in bad.stderr)
        svc.unlink()
        bad2, _ = do_run(load(), base, root)
        check("run missing password refused", bad2.returncode == 1
              and not probe.exists() and "failed" in bad2.stderr)

        # exec failure (bad binary, valid password): stderr restored, diagnostic clean
        svc.write_text(json.dumps({"password": pw}) + "\n")
        svc.chmod(0o600)
        cfg["binary"] = str(base / "nonexistent-opencode")
        (root / "runtime.json").write_text(json.dumps(cfg) + "\n")
        bad3, _ = do_run(load(), base, root)
        check("run exec failure stderr restored", bad3.returncode == 1
              and "failed" in bad3.stderr and pw not in bad3.stderr)
        cfg["binary"] = str(runbin / "opencode")
        (root / "runtime.json").write_text(json.dumps(cfg) + "\n")

        # path with spaces renders safely into unit
        root_sp = base / "sr v %2"
        (base / "op_state.json").write_text(json.dumps({"values": values2, "fail": False}))
        proc6 = do_install(load(), base, home, bindir, {"TEST_SECRET": "op://v/i/k"}, root_sp)
        unit_sp = (home / ".config/systemd/user/opencode-serve.service").read_text()
        check("space path ok", proc6.returncode == 0 and '"%s"' % str(root_sp).replace("%", "%%") in unit_sp)
    print("result:", "ALL PASS" if not failures else f"FAILURES {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
