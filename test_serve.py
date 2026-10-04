#!/usr/bin/env python3
"""Isolated stdlib checks for serve.py. Run: python3 test_serve.py
Fakes `op` and `opencode` in a temp dir; never touches real HOME, XDG dirs,
systemd, tailscale, or external network. Simulates first install, repeat install with
changed secret refs, failed op fetch leaving working config, special-value
secrets, and unit rendering.
"""

import http.server
import importlib.util
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
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
    shim.write_text(
        f"#!{sys.executable}\n"
        "import json,sys\n"
        f"cfg=json.load(open({str(base / 'op_state.json')!r}))\n"
        "if cfg.get('fail'): sys.exit(1)\n"
        "out=cfg['values'].get(sys.argv[-1],'')\n"
        "if not out: sys.exit(1)\n"
        "sys.stdout.write(out)\n"
    )
    shim.chmod(0o755)
    return bindir


def do_install(mod, base, home, bindir, refs_obj, root, extra_args=()):
    env = dict(os.environ)
    env.update(
        {
            "HOME": str(home),
            "PATH": str(bindir),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local/share"),
            "XDG_STATE_HOME": str(home / ".local/state"),
            "XDG_CACHE_HOME": str(home / ".cache"),
        }
    )
    sec = base / "refs.json"
    sec.write_text(json.dumps(refs_obj))
    proc = subprocess.run(
        [
            sys.executable,
            str(REPO / "serve.py"),
            "install",
            "--root",
            str(root),
            "--secrets",
            str(sec),
            "--opencode",
            str(bindir / "opencode"),
            *extra_args,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return proc


def fake_run_bin(base):
    """Fake `opencode serve`: dump argv + env to run_probe.json, exit 0."""
    bindir = base / "runbin"
    bindir.mkdir()
    dump = base / "run_probe.json"
    (bindir / "opencode").write_text(
        f"#!{sys.executable}\n"
        "import json,os,sys\n"
        f"json.dump({{'argv': sys.argv, 'env': dict(os.environ)}}, open({str(dump)!r}, 'w'))\n"
    )
    (bindir / "opencode").chmod(0o755)
    return bindir


def do_run(mod, base, root, extra_env=None):
    env = {
        "PATH": "/usr/bin:/bin",
        "OP_SERVICE_ACCOUNT_TOKEN": "bootstrap-secret",
        "LANG": "C",
        "TMPDIR": "/tmp/opencode",
    }
    env.update(extra_env or {})
    return (
        subprocess.run(
            [sys.executable, str(REPO / "serve.py"), "run", "--root", str(root)],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        ),
        base / "run_probe.json",
    )


def main():
    os.makedirs("/tmp/opencode", exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="oc-serve-test-", dir="/tmp/opencode") as tmp:
        base = Path(tmp)
        home = make_fake_home(base)
        root = base / "srv"
        values = {"op://v/i/k": 's3cret value $x "q" %f \ttab'}
        bindir = fake_bin(base)
        (base / "op_state.json").write_text(json.dumps({"values": values, "fail": False}))

        proc = do_install(load(), base, home, bindir, {"TEST_SECRET": "op://v/i/k"}, root)
        check("install ok", proc.returncode == 0)
        svc = root / "config/opencode/service.json"
        check("password exists", svc.exists())
        pw = json.loads(svc.read_text())["password"]
        check("password nonempty", isinstance(pw, str) and len(pw) >= 32)
        check("service 0600", stat.S_IMODE(svc.stat().st_mode) == 0o600)
        check(
            "runtime env fetched",
            json.loads((root / "runtime.json").read_text())["env"]
            == {"TEST_SECRET": 's3cret value $x "q" %f \ttab'},
        )
        check("runtime mode 0600", stat.S_IMODE((root / "runtime.json").stat().st_mode) == 0o600)
        check(
            "host config bridged",
            (root / "config/opencode/opencode.json").is_symlink()
            and (root / "config/opencode/other.txt").is_symlink(),
        )
        check("no service.json bridge", not (root / "config/opencode/service.json").is_symlink())
        unit = (home / ".config/systemd/user/opencode-serve.service").read_text()
        check("unit rendered", "@EXEC@" not in unit and "serve.py" in unit and "4096" not in unit)

        # repeat install preserves password, updates secret value
        values2 = {"op://v/i/k": "second \nvalue"}
        (base / "op_state.json").write_text(json.dumps({"values": values2, "fail": False}))
        proc2 = do_install(load(), base, home, bindir, {"TEST_SECRET": "op://v/i/k"}, root)
        check("reinstall ok", proc2.returncode == 0)
        check("password preserved", json.loads(svc.read_text())["password"] == pw)
        check(
            "env updated",
            json.loads((root / "runtime.json").read_text())["env"]
            == {"TEST_SECRET": "second \nvalue"},
        )

        # failed fetch leaves working config
        (base / "op_state.json").write_text(
            json.dumps({"values": {"op://v/i/k": ""}, "fail": True})
        )
        proc3 = do_install(load(), base, home, bindir, {"TEST_SECRET": "op://v/i/k"}, root)
        check("fail closed rc", proc3.returncode == 1)
        check("fail silent stderr", "op://v" not in proc3.stderr and "second" not in proc3.stderr)
        check(
            "config kept after failure",
            json.loads((root / "runtime.json").read_text())["env"]
            == {"TEST_SECRET": "second \nvalue"}
            and json.loads(svc.read_text())["password"] == pw,
        )

        custom_root = base / "custom-password"
        custom = ' chosen $password "quoted" %value \\ unicode-é\n '
        custom_proc = do_install(
            load(), base, home, bindir, {}, custom_root, ("--password", custom)
        )
        custom_svc = custom_root / "config/opencode/service.json"
        check(
            "custom password verbatim and private",
            custom_proc.returncode == 0
            and json.loads(custom_svc.read_text())["password"] == custom
            and stat.S_IMODE(custom_svc.stat().st_mode) == 0o600
            and custom not in custom_proc.stdout + custom_proc.stderr
            and custom not in (custom_root / "runtime.json").read_text()
            and custom not in (home / ".config/systemd/user/opencode-serve.service").read_text(),
        )
        preserve_proc = do_install(load(), base, home, bindir, {}, custom_root)
        check(
            "custom password preserved on reinstall",
            preserve_proc.returncode == 0
            and json.loads(custom_svc.read_text())["password"] == custom,
        )
        alias_proc = do_install(
            load(), base, home, bindir, {}, custom_root, ("--pasword", "replacement-secret")
        )
        check(
            "password alias replaces existing password",
            alias_proc.returncode == 0
            and json.loads(custom_svc.read_text())["password"] == "replacement-secret",
        )
        empty_proc = do_install(load(), base, home, bindir, {}, custom_root, ("--password", ""))
        check(
            "empty explicit password refused without replacement",
            empty_proc.returncode == 1
            and json.loads(custom_svc.read_text())["password"] == "replacement-secret",
        )
        for invalid_saved in ('{"password": ""}', "not-json", "[]"):
            custom_svc.write_text(invalid_saved)
            preserved = do_install(load(), base, home, bindir, {}, custom_root)
            check(
                "invalid existing password refused without override",
                preserved.returncode == 1 and custom_svc.read_text() == invalid_saved,
            )
            repaired = do_install(
                load(), base, home, bindir, {}, custom_root, ("--password", "repaired-secret")
            )
            check(
                "explicit password repairs invalid existing file",
                repaired.returncode == 0
                and json.loads(custom_svc.read_text())["password"] == "repaired-secret"
                and stat.S_IMODE(custom_svc.stat().st_mode) == 0o600,
            )
        failed_custom = do_install(
            load(),
            base,
            home,
            bindir,
            {"TEST_SECRET": "op://v/i/k"},
            root,
            ("--password", "must-not-replace"),
        )
        check(
            "failed fetch preserves password despite override",
            failed_custom.returncode == 1
            and json.loads(svc.read_text())["password"] == pw
            and "must-not-replace" not in failed_custom.stdout + failed_custom.stderr,
        )
        for bad_password in ("", "nul\0secret"):
            try:
                load().do_install(
                    base / "invalid-password",
                    {},
                    str(bindir / "opencode"),
                    4096,
                    password=bad_password,
                )
                refused = False
            except ValueError:
                refused = True
            check(
                "invalid password rejected before writes",
                refused and not (base / "invalid-password").exists(),
            )

        # reserved-name refs rejected
        proc4 = do_install(
            load(), base, home, bindir, {"OP_SERVICE_ACCOUNT_TOKEN": "op://v/i/k"}, root
        )
        check("reserved rejected", proc4.returncode == 1)
        check(
            "reserved OPENCODE_DB",
            "OPENCODE_DB" in load().RESERVED and "OPENCODE_PASSWORD" in load().RESERVED,
        )
        # non-op:// rejected
        proc5 = do_install(load(), base, home, bindir, {"X": "plain"}, root)
        check("non-ref rejected", proc5.returncode == 1)

        # --port validated, persisted; run reads runtime port
        (base / "refs.json").write_text(json.dumps({"TEST_SECRET": "op://v/i/k"}))
        (base / "op_state.json").write_text(json.dumps({"values": values2, "fail": False}))
        proc7 = subprocess.run(
            [
                sys.executable,
                str(REPO / "serve.py"),
                "install",
                "--root",
                str(root),
                "--secrets",
                str(base / "refs.json"),
                "--opencode",
                str(bindir / "opencode"),
                "--port",
                "0",
            ],
            env=dict(
                os.environ, HOME=str(home), PATH=str(bindir), XDG_CONFIG_HOME=str(home / ".config")
            ),
            capture_output=True,
            text=True,
            timeout=120,
        )
        check("port 0 rejected", proc7.returncode != 0)
        proc8 = subprocess.run(
            [
                sys.executable,
                str(REPO / "serve.py"),
                "install",
                "--root",
                str(root),
                "--secrets",
                str(base / "refs.json"),
                "--opencode",
                str(bindir / "opencode"),
                "--port",
                "5123",
            ],
            env=dict(
                os.environ, HOME=str(home), PATH=str(bindir), XDG_CONFIG_HOME=str(home / ".config")
            ),
            capture_output=True,
            text=True,
            timeout=120,
        )
        check(
            "custom port ok",
            proc8.returncode == 0
            and json.loads((root / "runtime.json").read_text())["port"] == 5123,
        )

        # explicit install without --secrets: no implicit default file
        env_e = dict(
            os.environ, HOME=str(home), PATH=str(bindir), XDG_CONFIG_HOME=str(home / ".config")
        )
        proc9 = subprocess.run(
            [
                sys.executable,
                str(REPO / "serve.py"),
                "install",
                "--root",
                str(root),
                "--opencode",
                str(bindir / "opencode"),
                "--port",
                "5123",
            ],
            env=env_e,
            capture_output=True,
            text=True,
            timeout=120,
        )
        check(
            "explicit no-secrets preserves env",
            proc9.returncode == 0
            and json.loads((root / "runtime.json").read_text())["env"]
            == {"TEST_SECRET": "second \nvalue"},
        )
        proc10 = subprocess.run(
            [
                sys.executable,
                str(REPO / "serve.py"),
                "install",
                "--root",
                str(base / "fresh"),
                "--opencode",
                str(bindir / "opencode"),
            ],
            env=env_e,
            capture_output=True,
            text=True,
            timeout=120,
        )
        check(
            "explicit fresh no-secrets empty",
            proc10.returncode == 0
            and json.loads((base / "fresh" / "runtime.json").read_text())["env"] == {},
        )
        proc11 = subprocess.run(
            [
                sys.executable,
                str(REPO / "serve.py"),
                "install",
                "--root",
                str(root),
                "--secrets",
                str(base / "missing.json"),
                "--opencode",
                str(bindir / "opencode"),
            ],
            env=env_e,
            capture_output=True,
            text=True,
            timeout=120,
        )
        check("explicit missing secrets fails", proc11.returncode == 1)

        # launcher uses fetched values without interpolation (spot check env build)
        cfg = json.loads((root / "runtime.json").read_text())
        check(
            "runtime has binary+path+home",
            cfg["binary"] and cfg["path"] and cfg["home"] == str(home),
        )

        # run(): fake opencode records argv/env; stdout suppressed, stderr restored
        runbin = fake_run_bin(base)
        cfg = json.loads((root / "runtime.json").read_text())
        cfg["binary"] = str(runbin / "opencode")
        (root / "runtime.json").write_text(json.dumps(cfg) + "\n")
        proc_run, probe = do_run(load(), base, root)
        rec = json.loads(probe.read_text())
        check("run ok", proc_run.returncode == 0 and probe.exists())
        check(
            "run argv",
            rec["argv"][1:] == ["serve", "--service", "--hostname", "127.0.0.1", "--port", "5123"],
        )
        check(
            "run env isolation",
            rec["env"]["HOME"] == str(home)
            and rec["env"]["PATH"] == cfg["path"]
            and rec["env"]["XDG_CONFIG_HOME"] == str(root / "config")
            and rec["env"]["XDG_DATA_HOME"] == str(root / "data")
            and rec["env"]["OPENCODE_DB"] == str(root / "data/server.db"),
        )
        check("run secret passed", rec["env"]["TEST_SECRET"] == "second \nvalue")
        check("run strips bootstrap", "OP_SERVICE_ACCOUNT_TOKEN" not in rec["env"])
        check(
            "run password absent",
            "OPENCODE_PASSWORD" not in rec["env"] and pw not in json.dumps(rec["env"]),
        )
        check("run streams quiet", proc_run.stdout == "" and proc_run.stderr == "")

        # run refuses without/invalid password; stderr restored (diagnostic, no secret)
        svc.write_text(json.dumps({"password": ""}) + "\n")
        if probe.exists():
            probe.unlink()
        bad, _ = do_run(load(), base, root)
        check(
            "run empty password refused",
            bad.returncode == 1
            and not probe.exists()
            and "failed" in bad.stderr
            and pw not in bad.stderr,
        )
        svc.unlink()
        bad2, _ = do_run(load(), base, root)
        check(
            "run missing password refused",
            bad2.returncode == 1 and not probe.exists() and "failed" in bad2.stderr,
        )

        # exec failure (bad binary, valid password): stderr restored, diagnostic clean
        svc.write_text(json.dumps({"password": pw}) + "\n")
        svc.chmod(0o600)
        cfg["binary"] = str(base / "nonexistent-opencode")
        (root / "runtime.json").write_text(json.dumps(cfg) + "\n")
        bad3, _ = do_run(load(), base, root)
        check(
            "run exec failure stderr restored",
            bad3.returncode == 1 and "failed" in bad3.stderr and pw not in bad3.stderr,
        )
        cfg["binary"] = str(runbin / "opencode")
        (root / "runtime.json").write_text(json.dumps(cfg) + "\n")

        # path with spaces renders safely into unit
        root_sp = base / "sr v %2"
        (base / "op_state.json").write_text(json.dumps({"values": values2, "fail": False}))
        proc6 = do_install(load(), base, home, bindir, {"TEST_SECRET": "op://v/i/k"}, root_sp)
        unit_sp = (home / ".config/systemd/user/opencode-serve.service").read_text()
        check(
            "space path ok",
            proc6.returncode == 0 and f'"{str(root_sp).replace("%", "%%")}"' in unit_sp,
        )
        guided_checks(load(), base, home, bindir)
        completion_checks(load(), base)
        network_checks(load(), base, home, bindir)
        remove_checks(load(), base, home, bindir)
        tailscale_cleanup_checks(load(), base, home, bindir)
    print("result:", "ALL PASS" if not failures else f"FAILURES {failures}")
    return 1 if failures else 0


def network_checks(mod, base, home, bindir):
    runbin = base / "runbin"
    update_root = base / "network-only-update"
    do_install(mod, base, home, bindir, {}, update_root, ("--port", "5444"))
    env = dict(os.environ, HOME=str(home), PATH=str(bindir), XDG_CONFIG_HOME=str(home / ".config"))
    toggled = subprocess.run(
        [
            sys.executable,
            str(REPO / "serve.py"),
            "install",
            "--root",
            str(update_root),
            "--use-ts",
            "false",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    saved = json.loads((update_root / "runtime.json").read_text())
    check(
        "network-only update preserves custom binary and port",
        toggled.returncode == 0
        and saved["port"] == 5444
        and saved["binary"] == str(bindir / "opencode")
        and saved["use_ts"] is False,
    )
    for hostname in ("0.0.0.0", "192.0.2.10", "::"):
        root = base / ("network-" + hostname.replace(":", "_"))
        flags = ("--use-ts", "false", "--hostname", hostname)
        installed = do_install(mod, base, home, bindir, {}, root, flags)
        config = json.loads((root / "runtime.json").read_text())
        check(
            "proxy bind settings persisted",
            installed.returncode == 0
            and config["use_ts"] is False
            and config["hostname"] == hostname,
        )
        repeated = do_install(mod, base, home, bindir, {}, root)
        config = json.loads((root / "runtime.json").read_text())
        check(
            "networking preserved on reinstall",
            repeated.returncode == 0
            and config["hostname"] == hostname
            and config["use_ts"] is False,
        )
        config["binary"] = str(runbin / "opencode")
        (root / "runtime.json").write_text(json.dumps(config))
        launched, probe = do_run(mod, base, root)
        check(
            "launcher passes proxy bind IP",
            launched.returncode == 0 and json.loads(probe.read_text())["argv"][4] == hostname,
        )
        switched = do_install(mod, base, home, bindir, {}, root, ("--use-ts", "true"))
        config = json.loads((root / "runtime.json").read_text())
        check(
            "Tailscale switch restores loopback",
            switched.returncode == 0
            and config["use_ts"] is True
            and config["hostname"] == "127.0.0.1",
        )
    root = base / "proxy-default"
    installed = do_install(mod, base, home, bindir, {}, root, ("--use-ts", "false"))
    config = json.loads((root / "runtime.json").read_text())
    check(
        "no Tailscale defaults to all IPv4 interfaces",
        installed.returncode == 0 and config["hostname"] == "0.0.0.0",
    )
    for args in (
        ("--use-ts", "invalid"),
        ("--use-ts", "true", "--hostname", "0.0.0.0"),
        ("--use-ts", "false", "--hostname", "not-an-ip"),
    ):
        invalid_root = base / "invalid-network"
        rejected = do_install(mod, base, home, bindir, {}, invalid_root, args)
        check(
            "invalid networking rejected before runtime write",
            rejected.returncode != 0 and not (invalid_root / "runtime.json").exists(),
        )
    # Legacy runtime files still use the loopback listener.
    legacy = base / "legacy-network"
    do_install(mod, base, home, bindir, {}, legacy)
    config = json.loads((legacy / "runtime.json").read_text())
    config.pop("hostname")
    config.pop("use_ts")
    config["binary"] = str(runbin / "opencode")
    (legacy / "runtime.json").write_text(json.dumps(config))
    launched, probe = do_run(mod, base, legacy)
    check(
        "legacy runtime binds loopback",
        launched.returncode == 0 and json.loads(probe.read_text())["argv"][4] == "127.0.0.1",
    )
    check(
        "wildcard probes use local reachable addresses",
        mod.probe_hostname("0.0.0.0") == "127.0.0.1"
        and mod.probe_hostname("::") == "::1"
        and mod.probe_hostname("192.0.2.10") == "192.0.2.10",
    )

    class AuthHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(
                200
                if self.headers.get("Authorization") == "Basic " + mod.basic_token("ready-password")
                else 401
            )
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.2", 0), AuthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        check(
            "readiness probes selected bind address with authentication",
            mod.verify_service(
                server.server_port, "ready-password", attempts=1, hostname="127.0.0.2"
            ),
        )
        check(
            "listener preflight probes selected bind address",
            mod.port_listening(server.server_port, hostname="127.0.0.2"),
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def remove_checks(mod, base, home, bindir):
    statefile, calls = base / "systemctl-state.json", base / "systemctl-calls.jsonl"
    shim = bindir / "systemctl"
    shim.write_text(
        f"#!{sys.executable}\nimport json,sys\nfrom pathlib import Path\ns=Path({str(statefile)!r}); state=json.loads(s.read_text())\nwith open({str(calls)!r},'a') as log: log.write(json.dumps(sys.argv[1:])+'\\n')\nverb=sys.argv[2]\nif verb=='disable':\n if state.get('stop_fail'): sys.exit(1)\n if not state.get('still_active'): state['active']=False\n s.write_text(json.dumps(state))\nif verb=='is-active': sys.exit(0 if state.get('active') else 3)\nif verb=='daemon-reload' and state.get('reload_fail'): sys.exit(1)\n"
    )
    shim.chmod(0o755)
    env = dict(os.environ, HOME=str(home), PATH=str(bindir), XDG_CONFIG_HOME=str(home / ".config"))
    statefile.write_text(json.dumps({"active": True}))
    unit = home / ".config/systemd/user/opencode-serve.service"

    def cli(root, purge=False):
        return subprocess.run(
            [
                sys.executable,
                str(REPO / "serve.py"),
                "remove",
                "--root",
                str(root),
                *(["--purge"] if purge else []),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    root = base / "remove-data"
    do_install(mod, base, home, bindir, {}, root)
    db = root / "data/server.db"
    db.write_text("saved database")
    password = (root / "config/opencode/service.json").read_text()
    config = json.loads((root / "runtime.json").read_text())
    config["env"] = {"KEY": "saved-secret"}
    (root / "runtime.json").write_text(json.dumps(config))
    removed = cli(root)
    check(
        "remove stops and uninstalls while keeping data and credentials",
        removed.returncode == 0
        and not unit.exists()
        and not (root / "serve.py").exists()
        and db.read_text() == "saved database"
        and (root / "config/opencode/service.json").read_text() == password
        and json.loads((root / "runtime.json").read_text())["env"] == {"KEY": "saved-secret"},
    )
    check(
        "remove output does not expose credentials",
        "saved-secret" not in removed.stdout + removed.stderr
        and json.loads(password)["password"] not in removed.stdout + removed.stderr,
    )
    removed_twice = cli(root)
    check("remove is repeatable", removed_twice.returncode == 0 and db.exists())
    purged = cli(root, True)
    check(
        "purge after remove deletes retained install", purged.returncode == 0 and not root.exists()
    )
    check("remove absent installation is harmless", cli(root, True).returncode == 0)
    root = base / "purge-direct"
    do_install(mod, base, home, bindir, {}, root)
    host_setting = home / ".config/opencode/other.txt"
    before = host_setting.read_text()
    check(
        "direct purge does not follow bridged config symlinks",
        cli(root, True).returncode == 0
        and not root.exists()
        and host_setting.read_text() == before,
    )
    for failure in ({"active": True, "stop_fail": True}, {"active": True, "still_active": True}):
        root = base / "remove-stop-failure"
        do_install(mod, base, home, bindir, {}, root)
        statefile.write_text(json.dumps(failure))
        failed = cli(root, True)
        check(
            "unverified service stop prevents deletion",
            failed.returncode == 1
            and unit.exists()
            and (root / "serve.py").exists()
            and (root / "runtime.json").exists(),
        )
    statefile.write_text(json.dumps({"active": False}))
    first, second = base / "remove-first", base / "remove-second"
    do_install(mod, base, home, bindir, {}, first)
    do_install(mod, base, home, bindir, {}, second)
    before = unit.read_text()
    check(
        "remove refuses unit belonging to another root",
        cli(first, True).returncode == 1
        and unit.read_text() == before
        and first.exists()
        and second.exists(),
    )
    check("remove matching other root succeeds", cli(second, True).returncode == 0)
    check("orphan installation removable when unit inactive", cli(first, True).returncode == 0)
    unknown = base / "unknown-remove"
    unknown.mkdir()
    (unknown / "important").write_text("keep")
    check(
        "purge rejects unrecognized directory",
        cli(unknown, True).returncode == 1 and (unknown / "important").read_text() == "keep",
    )
    link = base / "remove-root-link"
    link.symlink_to(unknown, target_is_directory=True)
    check("purge rejects symlink root", cli(link, True).returncode == 1 and unknown.exists())
    check("purge protects HOME", cli(home, True).returncode == 1 and home.exists())
    root = base / "reload-failure"
    do_install(mod, base, home, bindir, {}, root)
    statefile.write_text(json.dumps({"active": False, "reload_fail": True}))
    check(
        "daemon reload failure retains install data",
        cli(root, True).returncode == 1 and root.exists() and (root / "serve.py").exists(),
    )
    statefile.write_text(json.dumps({"active": False}))
    check(
        "removal retries after reload failure",
        cli(root, True).returncode == 0 and not root.exists(),
    )
    occupied = base / "nonempty-install"
    occupied.mkdir()
    (occupied / "important").write_text("unrelated project")
    adopted = do_install(mod, base, home, bindir, {}, occupied)
    check(
        "first install refuses nonempty unrecognized root",
        adopted.returncode == 1
        and (occupied / "important").read_text() == "unrelated project"
        and not (occupied / "runtime.json").exists(),
    )
    victim = base / "lock-victim"
    victim.write_text("unrelated secret data")
    victim.chmod(0o640)
    linked = base / "linked-install-lock"
    linked.mkdir()
    (linked / ".install.lock").symlink_to(victim)
    rejected = do_install(mod, base, home, bindir, {}, linked)
    check(
        "install lock symlink cannot truncate or chmod its target",
        rejected.returncode == 1
        and victim.read_text() == "unrelated secret data"
        and stat.S_IMODE(victim.stat().st_mode) == 0o640,
    )
    locked = base / "linked-remove-lock"
    do_install(mod, base, home, bindir, {}, locked)
    (locked / ".install.lock").unlink()
    (locked / ".install.lock").symlink_to(victim)
    rejected = cli(locked, True)
    check(
        "remove lock symlink cannot alter its target",
        rejected.returncode == 1
        and locked.exists()
        and unit.exists()
        and victim.read_text() == "unrelated secret data"
        and stat.S_IMODE(victim.stat().st_mode) == 0o640,
    )
    (locked / ".install.lock").unlink()
    cli(locked, True)

    # A second root must wait while removal owns the shared user unit.
    saved_env = dict(os.environ)
    os.environ.update(env)
    child = None
    first, second = base / "concurrent-remove", base / "concurrent-install"
    marker = base / "lock-attempt"
    try:
        mod.do_install(first, {}, str(bindir / "opencode"), 4096, keep_env={})
        worker = f"""import importlib.util, os
from pathlib import Path
spec = importlib.util.spec_from_file_location("serve_worker", {str(REPO / "serve.py")!r})
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
original = mod.fcntl.flock
def noted(fd, operation):
    number = fd if isinstance(fd, int) else fd.fileno()
    Path({str(marker)!r}).write_text(os.readlink("/proc/self/fd/" + str(number)))
    return original(fd, operation)
mod.fcntl.flock = noted
mod.do_install(Path({str(second)!r}), {{}}, {str(bindir / "opencode")!r}, 4096, keep_env={{}})
"""
        with mod.operation_lock():
            child = subprocess.Popen(
                [sys.executable, "-c", worker],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            deadline = time.monotonic() + 5
            while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            check(
                "concurrent install waits on shared operation lock",
                marker.exists()
                and marker.read_text().endswith("/operation.lock")
                and child.poll() is None
                and not (second / "runtime.json").exists(),
            )
            mod.remove_installation(first, purge=True)
            check(
                "concurrent install cannot replace unit during removal",
                not unit.exists() and child.poll() is None,
            )
        child.communicate(timeout=30)
        check(
            "waiting install writes its unit after removal finishes",
            child.returncode == 0
            and (second / "runtime.json").exists()
            and str(second) in unit.read_text(),
        )
        mod.remove_installation(second, purge=True)
        fresh = base / "concurrent-fresh"
        marker.unlink()
        worker = (
            worker[: worker.index("mod.do_install(Path(")]
            + f"mod.remove_installation(Path({str(fresh)!r}), purge=True)\n"
        )
        with mod.operation_lock():
            child = subprocess.Popen(
                [sys.executable, "-c", worker],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            deadline = time.monotonic() + 5
            while (
                (not marker.exists() or not marker.read_text().endswith("/operation.lock"))
                and child.poll() is None
                and time.monotonic() < deadline
            ):
                time.sleep(0.02)
            check(
                "remove waits for fresh install before absence checks",
                marker.exists()
                and marker.read_text().endswith("/operation.lock")
                and child.poll() is None
                and not fresh.exists(),
            )
            mod.do_install(fresh, {}, str(bindir / "opencode"), 4096, keep_env={})
        child.communicate(timeout=30)
        check(
            "queued remove uninstalls the completed fresh install",
            child.returncode == 0 and not fresh.exists() and not unit.exists(),
        )
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.communicate(timeout=5)
        os.environ.clear()
        os.environ.update(saved_env)


def tailscale_cleanup_checks(mod, base, home, bindir):
    saved = guided_env(home)
    original_cmd, original_which = mod.cmd, mod.shutil.which
    dns = "host.tail123.ts.net"

    def exact(port=4096):
        return {
            "TCP": {"443": {"HTTPS": True}},
            "Web": {dns + ":443": {"Handlers": {"/": {"Proxy": f"http://127.0.0.1:{port}"}}}},
        }

    state = {"cfg": exact(), "cmds": []}

    def fake_cmd(argv):
        state["cmds"].append(argv)
        if argv == ["tailscale", "serve", "status", "--json"]:
            if state.get("inspect_fail_after_create") and state.get("created_called"):
                return subprocess.CompletedProcess(argv, 1, b"", b"")
            return subprocess.CompletedProcess(argv, 0, json.dumps(state["cfg"]).encode(), b"")
        if argv[:3] == ["tailscale", "serve", "--bg"]:
            state["created_called"] = True
            if state.get("create_fail"):
                return subprocess.CompletedProcess(argv, 1, b"", b"")
            state["cfg"] = exact(int(argv[-1].rsplit(":", 1)[-1]))
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        if argv == ["tailscale", "serve", "--https=443", "off"]:
            if state.get("off_fail"):
                return subprocess.CompletedProcess(argv, 1, b"", b"")
            state["cfg"] = None
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        return subprocess.CompletedProcess(argv, 3 if "is-active" in argv else 0, b"", b"")

    try:
        mod.cmd = fake_cmd
        mod.shutil.which = lambda name: (
            "/fake/tailscale" if name == "tailscale" else original_which(name)
        )
        root = base / "owned-serve"
        mod.do_install(root, {}, str(bindir / "opencode"), 4096, keep_env={}, use_ts=True)
        mod.record_serve(root, dns, 4096)
        config = json.loads((root / "runtime.json").read_text())
        check(
            "managed Tailscale ownership persisted",
            config["tailscale_serve"] == {"dns": dns, "port": 4096},
        )
        mod.do_install(root, {}, str(bindir / "opencode"), 4096, keep_env={}, use_ts=False)
        config = json.loads((root / "runtime.json").read_text())
        check(
            "disabling Tailscale removes exact managed mapping",
            config["use_ts"] is False
            and config["hostname"] == "0.0.0.0"
            and "tailscale_serve" not in config
            and ["tailscale", "serve", "--https=443", "off"] in state["cmds"],
        )
        mod.do_install(root, {}, str(bindir / "opencode"), 4096, keep_env={}, use_ts=True)
        mod.record_serve(root, dns, 4096)
        state["cfg"] = exact()
        state["cmds"] = []
        mod.remove_installation(root)
        check(
            "remove cleans up owned Serve endpoint",
            state["cfg"] is None
            and ["tailscale", "serve", "--https=443", "off"] in state["cmds"]
            and "tailscale_serve" not in json.loads((root / "runtime.json").read_text()),
        )
        mod.do_install(root, {}, str(bindir / "opencode"), 4096, keep_env={}, use_ts=True)
        mod.record_serve(root, dns, 4096)
        shared = exact()
        shared["Services"] = {"unrelated": {}}
        state["cfg"] = shared
        state["cmds"] = []
        before = (root / "runtime.json").read_text()
        try:
            mod.do_install(root, {}, str(bindir / "opencode"), 4096, keep_env={}, use_ts=False)
            refused = False
        except ValueError:
            refused = True
        check(
            "network switch preserves shared Serve config",
            refused
            and state["cfg"] == shared
            and not any("off" in c for c in state["cmds"])
            and (root / "runtime.json").read_text() == before,
        )
        mod.remove_installation(root)
        check(
            "remove leaves shared Serve config untouched",
            state["cfg"] == shared
            and not any("off" in c for c in state["cmds"])
            and not (root / "serve.py").exists(),
        )
        state["cfg"] = exact()
        state["off_fail"] = True
        config = json.loads((root / "runtime.json").read_text())
        check(
            "failed Serve cleanup retains ownership for retry",
            not mod.clear_owned_serve(config) and "tailscale_serve" in config,
        )
        try:
            mod.remove_installation(root, purge=True)
            refused = False
        except ValueError:
            refused = True
        check(
            "purge retains recovery metadata when Serve cleanup fails",
            refused
            and root.exists()
            and "tailscale_serve" in json.loads((root / "runtime.json").read_text()),
        )
        state.update(
            cfg=None,
            off_fail=False,
            create_fail=True,
            inspect_fail_after_create=False,
            created_called=False,
        )
        mod.do_install(root, {}, str(bindir / "opencode"), 4096, keep_env={}, use_ts=True)
        failed = mod.create_serve(root, dns, 4096)
        check(
            "failed Serve creation leaves no false ownership",
            failed.returncode != 0
            and "tailscale_serve" not in json.loads((root / "runtime.json").read_text()),
        )
        state.update(create_fail=False, inspect_fail_after_create=True, created_called=False)
        failed = mod.create_serve(root, dns, 4096)
        check(
            "unverified Serve creation retains pending recovery record",
            failed.returncode != 0
            and json.loads((root / "runtime.json").read_text())["tailscale_serve"]["pending"]
            is True,
        )
        try:
            mod.remove_installation(root, purge=True)
            refused = False
        except ValueError:
            refused = True
        check("purge refuses unverified pending Serve ownership", refused and root.exists())
        state.update(inspect_fail_after_create=False)
        mod.record_serve(root, dns, 4096)
        mod.remove_installation(root, purge=True)
        check(
            "purge succeeds after ownership and cleanup verified",
            not root.exists() and state["cfg"] is None,
        )
        check(
            "cleanup never uses tailscale reset or sudo",
            not any("reset" in c or "sudo" in c for c in state["cmds"]),
        )
    finally:
        mod.cmd, mod.shutil.which = original_cmd, original_which
        os.environ.clear()
        os.environ.update(saved)


def guided_env(home):
    saved = dict(os.environ)
    os.environ.update(
        {"HOME": str(home), "PATH": "/usr/bin:/bin", "XDG_CONFIG_HOME": str(home / ".config")}
    )
    return saved


def completion_checks(mod, base):
    home = base / "completion home ' $literal"
    home.mkdir()
    data, config = home / "data", home / "config"
    env = dict(
        os.environ,
        HOME=str(home),
        SHELL="/bin/zsh",
        XDG_DATA_HOME=str(data),
        XDG_CONFIG_HOME=str(config),
        ZDOTDIR=str(home / "zdot"),
    )

    def cli(*args, overrides=None):
        return subprocess.run(
            [sys.executable, str(REPO / "serve.py"), "completion", *args],
            env=dict(env, **(overrides or {})),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
        )

    for shell in ("bash", "zsh", "fish"):
        printed = cli(shell)
        check(
            f"{shell} completion generation",
            printed.returncode == 0 and printed.stdout == mod.COMPLETIONS[shell],
        )
        target = (
            config / "fish/conf.d/opencode-serve.fish"
            if shell == "fish"
            else data / f"opencode-serve/completions/serve.py.{shell}"
        )
        rc = None if shell == "fish" else home / ("zdot/.zshrc" if shell == "zsh" else ".bashrc")
        if rc is not None:
            rc.parent.mkdir(parents=True, exist_ok=True)
            rc.write_text("# existing config\nexport CUSTOM_SETTING=kept\n")
            rc.chmod(0o640)
        installed = cli(shell, "--install")
        check(
            f"{shell} completion installation",
            installed.returncode == 0 and target.read_text() == printed.stdout,
        )
        if rc is not None:
            check(
                f"{shell} config preserved",
                rc.read_text().startswith("# existing config\nexport CUSTOM_SETTING=kept\n")
                and stat.S_IMODE(rc.stat().st_mode) == 0o640,
            )
        target.write_text("existing custom completion\n")
        before_rc = rc.read_text() if rc else None
        refused = cli(shell, "--install")
        check(
            f"{shell} refuses overwrite without force",
            refused.returncode == 1
            and target.read_text() == "existing custom completion\n"
            and (rc is None or rc.read_text() == before_rc),
        )
        forced = cli(shell, "--install", "--force")
        check(
            f"{shell} forced completion replacement",
            forced.returncode == 0
            and target.read_text() == printed.stdout
            and (rc is None or rc.read_text().count("# >>> opencode-serve completion >>>") == 1),
        )
        executable = shutil.which(shell)
        if executable:
            parsed = subprocess.run(
                [executable, "-n", str(target)], capture_output=True, timeout=30
            )
            check(f"{shell} completion syntax", parsed.returncode == 0)
            if shell == "bash":
                activation = f"source {shlex.quote(str(rc))}; complete -p serve.py"
                command = [executable, "--norc", "-c", activation]
            elif shell == "zsh":
                activation = (
                    f"source {shlex.quote(str(rc))}; [[ ${{_comps[serve.py]}} == _opencode_serve ]]"
                )
                command = [executable, "-f", "-c", activation]
            else:
                command = [executable, "-c", "complete -C './serve.py comp'"]
            loaded = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
            check(
                f"{shell} installed completion loads",
                loaded.returncode == 0 and (shell != "fish" or "completion" in loaded.stdout),
            )

    auto = cli("--install", "--force")
    check(
        "completion install detects shell", auto.returncode == 0 and "Installed zsh" in auto.stdout
    )
    check(
        "completion explicit shell overrides detection",
        cli("bash", overrides={"SHELL": "/bin/unknown"}).returncode == 0,
    )
    check(
        "completion unsupported shell rejected",
        cli(overrides={"SHELL": "/bin/unknown"}).returncode == 2,
    )
    check("completion unset shell rejected", cli(overrides={"SHELL": ""}).returncode == 2)
    check("completion force requires install", cli("bash", "--force").returncode == 2)
    check(
        "completion writes no server files",
        not (home / ".local/share/opencode-serve/runtime.json").exists()
        and not (config / "systemd").exists(),
    )
    zrc = home / "zdot/.zshrc"
    zrc.write_text("# >>> opencode-serve completion >>>\nunterminated block\n")
    ztarget = data / "opencode-serve/completions/serve.py.zsh"
    before = ztarget.read_text()
    check(
        "completion invalid rc block leaves files unchanged",
        cli("zsh", "--install", "--force").returncode == 1 and ztarget.read_text() == before,
    )
    btarget = data / "opencode-serve/completions/serve.py.bash"
    original = home / "unrelated-completion"
    original.write_text("do not overwrite\n")
    btarget.unlink()
    btarget.symlink_to(original)
    check(
        "completion refuses symlink without force",
        cli("bash", "--install").returncode == 1 and original.read_text() == "do not overwrite\n",
    )
    check(
        "completion force replaces symlink without following it",
        cli("bash", "--install", "--force").returncode == 0
        and not btarget.is_symlink()
        and original.read_text() == "do not overwrite\n",
    )
    btarget.unlink()
    btarget.symlink_to(home, target_is_directory=True)
    check(
        "completion force replaces directory symlink",
        cli("bash", "--install", "--force").returncode == 0
        and not btarget.is_symlink()
        and home.is_dir(),
    )
    btarget.unlink()
    os.mkfifo(btarget)
    check(
        "completion force refuses FIFO without blocking",
        cli("bash", "--install", "--force").returncode == 1
        and stat.S_ISFIFO(btarget.stat().st_mode),
    )
    btarget.unlink()
    btarget.mkdir()
    check(
        "completion force refuses actual directory",
        cli("bash", "--install", "--force").returncode == 1 and btarget.is_dir(),
    )
    btarget.rmdir()
    check(
        "completion reinstall after special file checks", cli("bash", "--install").returncode == 0
    )
    saved_env = dict(os.environ)
    old_atomic = mod.atomic
    old_replace = mod.os.replace
    bashrc = home / ".bashrc"
    before_rc = bashrc.read_text()

    def failed_rc_write(path, text, mode=0o600):
        if path == bashrc:
            raise OSError("simulated config write failure")
        old_atomic(path, text, mode)

    try:
        os.environ.update(env)
        mod.atomic = failed_rc_write
        for existing in (True, False):
            if existing:
                btarget.write_text("keep prior completion\n")
                btarget.chmod(0o640)
            else:
                btarget.unlink()
            try:
                mod.install_completion("bash", True)
                failed = False
            except OSError:
                failed = True
            check(
                "completion config failure rolls back completion file",
                failed
                and bashrc.read_text() == before_rc
                and (
                    btarget.read_text() == "keep prior completion\n"
                    and stat.S_IMODE(btarget.stat().st_mode) == 0o640
                    if existing
                    else not btarget.exists()
                )
                and not list(btarget.parent.glob(".completion-backup-*")),
            )
        btarget.write_text("recoverable prior completion\n")

        def failed_restore(source, target):
            if Path(source).name.startswith(".completion-backup-"):
                raise OSError("simulated rollback failure")
            old_replace(source, target)

        mod.os.replace = failed_restore
        try:
            mod.install_completion("bash", True)
            failed = False
        except OSError as error:
            failed = "previous completion retained at" in str(error)
        backups = list(btarget.parent.glob(".completion-backup-*"))
        check(
            "completion retains backup on rollback failure",
            failed
            and len(backups) == 1
            and backups[0].read_text() == "recoverable prior completion\n",
        )
        mod.os.replace = old_replace
        if backups:
            old_replace(backups[0], btarget)
    finally:
        mod.atomic = old_atomic
        mod.os.replace = old_replace
        os.environ.clear()
        os.environ.update(saved_env)
    check(
        "completion reinstall after rollback", cli("bash", "--install", "--force").returncode == 0
    )

    bash = shutil.which("bash")
    if bash:

        def candidates(words):
            command = f"source {shlex.quote(str(btarget))}\nCOMP_WORDS=({shlex.join(words)})\nCOMP_CWORD={len(words)-1}\nshopt -s failglob\n_opencode_serve\nshopt -q failglob || exit 1\nprintf '%s\\n' \"${{COMPREPLY[@]}}\""
            result = subprocess.run(
                [bash, "--norc", "-c", command], capture_output=True, text=True, timeout=30
            )
            check("bash completion function runs", result.returncode == 0)
            return result.stdout.splitlines()

        check("bash completes actions", "completion" in candidates(["./serve.py", "comp"]))
        check(
            "bash completes password flags",
            "--password" in candidates(["./serve.py", "install", "--p"]),
        )
        check(
            "bash completes shell names", "fish" in candidates(["./serve.py", "completion", "fi"])
        )
        check(
            "bash suppresses password value suggestions",
            not any(candidates(["./serve.py", "install", "--password", ""])),
        )
        directory = home / "directory with spaces"
        directory.mkdir()
        check(
            "bash completes directory with spaces",
            str(directory)
            in candidates(["./serve.py", "install", "--root", str(home / "directory")]),
        )
        check(
            "bash unmatched directory safe with failglob",
            not any(
                candidates(["./serve.py", "install", "--root", str(home / "missing-directory")])
            ),
        )
        check("bash completes remove action", "remove" in candidates(["./serve.py", "rem"]))
        check("bash completes purge flag", "--purge" in candidates(["./serve.py", "remove", "--p"]))
        check(
            "bash completes use-ts boolean",
            "false" in candidates(["./serve.py", "install", "--use-ts", "fa"]),
        )


def guided_setup(
    mod,
    answers,
    dns="host.tail123.ts.net.",
    serve_cfg=None,
    start_ok=True,
    isatty=True,
    fail_status=False,
    deny_bg=False,
    deny_status=False,
    ready="good",
    listening=False,
    active_ok=True,
    password_answers=None,
    network_answers=None,
):
    import builtins

    state = {
        "installs": [],
        "cmds": [],
        "answers": list(answers),
        "started": False,
        "serve_cfg": serve_cfg,
    }
    orig_input, orig_stdin, orig_cmd, orig_install, orig_which = (
        builtins.input,
        sys.stdin,
        mod.cmd,
        mod.do_install,
        shutil.which,
    )
    orig_api, orig_sleep, orig_svc_pw = mod.api_get, mod.time.sleep, mod.service_password
    orig_listening = mod.port_listening
    orig_getpass = mod.getpass.getpass
    orig_record = mod.record_serve
    network_replies = iter(["y"] if network_answers is None else network_answers)
    password_replies = iter([""] if password_answers is None else password_answers)

    def fake_getpass(prompt=""):
        sys.stdout.write(prompt)
        reply = next(password_replies)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    def fake_api(port, token=None, timeout=5, hostname="127.0.0.1"):
        state.setdefault("probe_hosts", []).append(hostname)
        if ready == "down":
            return None, b""
        if ready == "open":
            return 200, b"{}"
        if ready == "bad" or token != mod.basic_token("pw"):
            return 401, b""
        return 200, b'{"ok": true}'

    def fake_input(prompt=""):
        sys.stdout.write(prompt)
        if prompt.startswith(("Use Tailscale Serve?", "Bind IP for proxy access")):
            reply = next(network_replies)
            if isinstance(reply, BaseException):
                raise reply
            print(reply)
            return reply
        if not state["answers"]:
            raise EOFError()
        reply = state["answers"].pop(0)
        if isinstance(reply, BaseException):
            raise reply
        print(reply)
        return reply

    class FakeStdin:
        def isatty(self):
            return isatty

    def fake_cmd(argv):
        state["cmds"].append(list(argv))
        prog = (argv[0],) + tuple(argv[1:])
        if prog[:2] == ("systemctl", "--user"):
            if prog[2] == "is-active":
                return subprocess.CompletedProcess(
                    argv, 0 if state["started"] and active_ok else 1, b"", b""
                )
            if prog[2] in ("enable", "restart"):
                state["started"] = start_ok
            return subprocess.CompletedProcess(argv, 0 if start_ok else 1, b"", b"")
        if prog[:2] == ("loginctl", "enable-linger"):
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        if list(argv)[:3] == ["tailscale", "status", "--json"]:
            if fail_status:
                return subprocess.CompletedProcess(argv, 1, b"", b"")
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"Self": {"DNSName": dns}}).encode(), b""
            )
        if list(argv)[:4] == ["tailscale", "serve", "status", "--json"]:
            if deny_status:
                return subprocess.CompletedProcess(
                    argv, 1, b"", b"Access denied: serve config denied"
                )
            if state["serve_cfg"] == "fail":
                return subprocess.CompletedProcess(argv, 1, b"", b"")
            body = "null" if state["serve_cfg"] is None else json.dumps(state["serve_cfg"])
            return subprocess.CompletedProcess(argv, 0, body.encode(), b"")
        if list(argv)[:2] == ["tailscale", "serve"]:
            if deny_bg:
                return subprocess.CompletedProcess(
                    argv, 1, b"", b"Access denied: serve config denied"
                )
            if "--bg" in argv:
                state["serve_cfg"] = {
                    "TCP": {"443": {"HTTPS": True}},
                    "Web": {dns.rstrip(".") + ":443": {"Handlers": {"/": {"Proxy": argv[-1]}}}},
                }
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        return subprocess.CompletedProcess(argv, 1, b"", b"")

    def fake_install(
        root, refs, binary_name, port, keep_env=None, password=None, use_ts=None, hostname=None
    ):
        root.mkdir(parents=True, exist_ok=True)
        mod.atomic(
            root / "runtime.json",
            json.dumps(
                {"port": port, "use_ts": use_ts, "hostname": hostname, "env": keep_env or {}}
            ),
        )
        state["installs"].append(
            {
                "root": str(root),
                "refs": dict(refs),
                "binary": binary_name,
                "port": port,
                "keep": keep_env,
                "password": password,
                "use_ts": use_ts,
                "hostname": hostname,
            }
        )

    builtins.input, sys.stdin, mod.cmd, mod.do_install = (
        fake_input,
        FakeStdin(),
        fake_cmd,
        fake_install,
    )
    mod.shutil.which = lambda name: (
        None if name == "tailscale" and serve_cfg == "no-tail" else orig_which(name)
    )
    mod.api_get = fake_api
    mod.time.sleep = lambda s: None
    mod.service_password = lambda root: "pw"
    mod.port_listening = lambda port, hostname="127.0.0.1": listening
    mod.getpass.getpass = fake_getpass
    mod.record_serve = lambda root, dns, port: state.setdefault("serve_markers", []).append(
        (dns, port)
    )
    return state, (
        orig_input,
        orig_stdin,
        orig_cmd,
        orig_install,
        orig_which,
        orig_api,
        orig_sleep,
        orig_svc_pw,
        orig_listening,
        orig_getpass,
        orig_record,
    )


def guided_teardown(mod, saved_env, state, origs):
    import builtins

    orig_input, orig_stdin, orig_cmd, orig_install, orig_which = origs[:5]
    orig_api, orig_sleep, orig_svc_pw, orig_listening, orig_getpass, orig_record = origs[5:]
    builtins.input, sys.stdin, mod.cmd, mod.do_install = (
        orig_input,
        orig_stdin,
        orig_cmd,
        orig_install,
    )
    mod.shutil.which = orig_which
    mod.api_get, mod.time.sleep = orig_api, orig_sleep
    mod.service_password, mod.port_listening = orig_svc_pw, orig_listening
    mod.getpass.getpass = orig_getpass
    mod.record_serve = orig_record
    os.environ.clear()
    os.environ.update(saved_env)
    return state


def guided_checks(mod, base, home, bindir):
    # Isolate the entire guided suite in a temp CWD; restore no matter what.
    oldcwd = os.getcwd()
    os.chdir(base)
    try:
        _guided_checks(mod, base, home, bindir)
    finally:
        os.chdir(oldcwd)


def _guided_checks(mod, base, home, bindir):
    import io
    from contextlib import redirect_stderr, redirect_stdout

    gsrv = base / "gsrv"
    sec = base / "grefs.json"
    sec.write_text(json.dumps({"K": "op://v/i/k"}))

    # success: custom root/port/binary/refs + start + verified + tailscale expose
    saved = guided_env(home)
    st, origs = guided_setup(
        mod,
        [str(gsrv), "5123", str(bindir / "opencode"), str(sec), "y", "y", "n", "y"],
        ready="good",
    )
    rc = mod.guided_install()
    guided_teardown(mod, saved, st, origs)
    bg = [c for c in st["cmds"] if c[:2] == ["tailscale", "serve"] and "--bg" in c]
    check(
        "guided success",
        rc == 0
        and len(st["installs"]) == 1
        and st["installs"][0]["port"] == 5123
        and st["installs"][0]["refs"] == {"K": "op://v/i/k"}
        and st["installs"][0]["password"] is None
        and bg == [["tailscale", "serve", "--bg", "http://127.0.0.1:5123"]],
    )
    for host in ("", "192.0.2.10", "::"):
        saved = guided_env(home)
        st, origs = guided_setup(
            mod,
            [str(base / "guided-proxy"), "", str(bindir / "opencode"), "", "y", "y", "n"],
            network_answers=["n", host],
            ready="good",
            serve_cfg="no-tail",
        )
        rc = mod.guided_install()
        guided_teardown(mod, saved, st, origs)
        expected = host or "0.0.0.0"
        check(
            "guided proxy mode needs no Tailscale",
            rc == 0
            and st["installs"][0]["use_ts"] is False
            and st["installs"][0]["hostname"] == expected
            and not any(c[0] == "tailscale" for c in st["cmds"])
            and expected in st["probe_hosts"],
        )
    saved = guided_env(home)
    st, origs = guided_setup(mod, [str(base / "cancel-network"), ""], network_answers=[EOFError()])
    rc = mod.guided_install()
    guided_teardown(mod, saved, st, origs)
    check(
        "guided networking cancellation before changes",
        rc == 1 and not st["installs"] and not st["cmds"],
    )

    # Hidden password input preserves whitespace and retries mismatched confirmation.
    saved = guided_env(home)
    chosen = ' interactive $secret "quoted" é '
    st, origs = guided_setup(
        mod,
        [str(base / "guided-password"), "", str(bindir / "opencode"), "", "y", "n", "n"],
        password_answers=["first-secret", "mismatch-secret", chosen, chosen],
    )
    buf = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(err):
        rc = mod.guided_install()
    guided_teardown(mod, saved, st, origs)
    check(
        "guided custom password hidden and confirmed",
        rc == 0
        and st["installs"][0]["password"] == chosen
        and "Passwords do not match" in buf.getvalue()
        and all(
            p not in buf.getvalue() + err.getvalue()
            for p in (chosen, "first-secret", "mismatch-secret")
        ),
    )
    for failure in (EOFError(), KeyboardInterrupt(), mod.getpass.GetPassWarning("no terminal")):
        saved = guided_env(home)
        st, origs = guided_setup(
            mod,
            [str(base / "password-cancel"), "", str(bindir / "opencode"), ""],
            password_answers=[failure],
        )
        rc = mod.guided_install()
        guided_teardown(mod, saved, st, origs)
        check(
            "guided password cancellation safe", rc == 1 and not st["installs"] and not st["cmds"]
        )

    # non-TTY refuses without changes
    saved = guided_env(home)
    st, origs = guided_setup(mod, [], isatty=False)
    rc = mod.guided_install()
    guided_teardown(mod, saved, st, origs)
    check("guided non-tty refuses", rc == 1 and not st["installs"] and not st["cmds"])

    # cancel at summary writes nothing
    saved = guided_env(home)
    st, origs = guided_setup(
        mod, [str(base / "cancel-root"), "", str(bindir / "opencode"), "", "n"]
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = mod.guided_install()
    guided_teardown(mod, saved, st, origs)
    check(
        "guided cancel safe",
        rc == 1
        and not st["installs"]
        and "nothing changed" in buf.getvalue()
        and not (base / "cancel-root").exists(),
    )

    # EOF safe
    saved = guided_env(home)
    st, origs = guided_setup(mod, [EOFError()])
    rc = mod.guided_install()
    guided_teardown(mod, saved, st, origs)
    check("guided EOF safe", rc == 1 and not st["installs"])

    # no refs -> empty refs, service not running skips tailscale
    saved = guided_env(home)
    st, origs = guided_setup(
        mod, [str(base / "norefs"), "", str(bindir / "opencode"), "", "y", "n", "n"]
    )
    rc = mod.guided_install()
    guided_teardown(mod, saved, st, origs)
    check(
        "guided no refs",
        rc == 0
        and st["installs"][0]["refs"] == {}
        and not [c for c in st["cmds"] if c[:1] == ["tailscale"]],
    )

    # cancel after writes/service changes says retained
    saved = guided_env(home)
    st, origs = guided_setup(
        mod,
        [str(base / "latecancel"), "", str(bindir / "opencode"), "", "y", KeyboardInterrupt()],
        ready="good",
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = mod.guided_install()
    guided_teardown(mod, saved, st, origs)
    check(
        "guided late cancel retained",
        rc == 1
        and len(st["installs"]) == 1
        and "retained" in buf.getvalue()
        and "nothing changed" not in buf.getvalue(),
    )

    # discovered refs path shown as suggestion only; blank still means no refs
    (base / ".opencode-serve.local").mkdir(exist_ok=True)
    (base / ".opencode-serve.local/references.json").write_text(json.dumps({"K": "op://v/i/k"}))
    saved = guided_env(home)
    st, origs = guided_setup(
        mod, [str(base / "discovered"), "", str(bindir / "opencode"), "", "y", "n", "n"]
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = mod.guided_install()
    guided_teardown(mod, saved, st, origs)
    check(
        "guided discovered suggestion only",
        rc == 0 and st["installs"][0]["refs"] == {} and "suggestion only" in buf.getvalue(),
    )

    # relative refs resolved against cwd (suite cwd is base)
    saved = guided_env(home)
    st, origs = guided_setup(
        mod, [str(base / "relroot"), "", str(bindir / "opencode"), "grefs.json", "y", "n", "n"]
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = mod.guided_install()
    guided_teardown(mod, saved, st, origs)
    check(
        "guided relative refs",
        rc == 0
        and st["installs"][0]["refs"] == {"K": "op://v/i/k"}
        and f"Resolved refs: {base / 'grefs.json'}" in buf.getvalue(),
    )

    # existing runtime secrets preserved on empty refs
    keep_root = base / "keeproot"
    (keep_root / "x").mkdir(parents=True, exist_ok=True)
    (keep_root / "runtime.json").write_text(json.dumps({"port": 4096, "env": {"OLD": "kept"}}))
    saved = guided_env(home)
    st, origs = guided_setup(mod, [str(keep_root), "", str(bindir / "opencode"), "", "y", "n", "n"])
    rc = mod.guided_install()
    guided_teardown(mod, saved, st, origs)
    check(
        "guided keeps secrets",
        rc == 0
        and st["installs"][0]["keep"] == {"OLD": "kept"}
        and st["installs"][0]["refs"] == {},
    )

    # failed start -> no tailscale call
    saved = guided_env(home)
    st, origs = guided_setup(
        mod,
        [str(base / "failstart"), "", str(bindir / "opencode"), "", "y", "y", "n"],
        start_ok=False,
    )
    rc = mod.guided_install()
    guided_teardown(mod, saved, st, origs)
    check(
        "guided failed start no proxy",
        rc == 0 and not [c for c in st["cmds"] if c[:1] == ["tailscale"]],
    )

    # identical serve target idempotent, no --bg
    dns = "host.tail123.ts.net."
    same = {
        "TCP": {"443": {"HTTPS": True}},
        "Web": {
            "host.tail123.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:4096/"}}}
        },
    }
    saved = guided_env(home)
    st, origs = guided_setup(
        mod,
        [str(base / "same"), "", str(bindir / "opencode"), "", "y", "y", "n", "y"],
        dns=dns,
        serve_cfg=same,
    )
    rc = mod.guided_install()
    guided_teardown(mod, saved, st, origs)
    check("guided identical idempotent", rc == 0 and not [c for c in st["cmds"] if "--bg" in c])

    # occupied/conflicting config refused, no --bg
    conflict = {
        "TCP": {"443": {"HTTPS": True}},
        "Web": {
            "host.tail123.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:9999/"}}}
        },
    }
    saved = guided_env(home)
    st, origs = guided_setup(
        mod,
        [str(base / "conflict"), "", str(bindir / "opencode"), "", "y", "y", "n", "y"],
        dns=dns,
        serve_cfg=conflict,
    )
    rc = mod.guided_install()
    guided_teardown(mod, saved, st, origs)
    check("guided conflict refused", rc == 0 and not [c for c in st["cmds"] if "--bg" in c])

    # nonempty funnel/sibling state refused conservatively
    funnel = {
        "TCP": {"443": {"HTTPS": True}},
        "Web": {
            "host.tail123.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:4096/"}}}
        },
        "AllowFunnel": {"host.tail123.ts.net:443": True},
    }
    saved = guided_env(home)
    st, origs = guided_setup(
        mod,
        [str(base / "funnel"), "", str(bindir / "opencode"), "", "y", "y", "n", "y"],
        dns=dns,
        serve_cfg=funnel,
    )
    rc = mod.guided_install()
    guided_teardown(mod, saved, st, origs)
    check("guided funnel refused", rc == 0 and not [c for c in st["cmds"] if "--bg" in c])

    # external failures: no tailscale, bad status, bad serve status
    saved = guided_env(home)
    st, origs = guided_setup(
        mod,
        [str(base / "e1"), "", str(bindir / "opencode"), "", "y", "y", "n", "y"],
        serve_cfg="no-tail",
    )
    rc = mod.guided_install()
    guided_teardown(mod, saved, st, origs)
    check("guided no tailscale safe", rc == 0 and not [c for c in st["cmds"] if "--bg" in c])
    saved = guided_env(home)
    st, origs = guided_setup(
        mod,
        [str(base / "e2"), "", str(bindir / "opencode"), "", "y", "y", "n", "y"],
        fail_status=True,
    )
    rc = mod.guided_install()
    guided_teardown(mod, saved, st, origs)
    check("guided bad status safe", rc == 0 and not [c for c in st["cmds"] if "--bg" in c])
    saved = guided_env(home)
    st, origs = guided_setup(
        mod,
        [str(base / "e3"), "", str(bindir / "opencode"), "", "y", "y", "n", "y"],
        serve_cfg="fail",
    )
    rc = mod.guided_install()
    guided_teardown(mod, saved, st, origs)
    check("guided bad serve status safe", rc == 0 and not [c for c in st["cmds"] if "--bg" in c])
    saved = guided_env(home)
    st, origs = guided_setup(
        mod, [str(base / "e4"), "", str(bindir / "opencode"), "", "y", "y", "n", "y"], deny_bg=True
    )
    buf = io.StringIO()
    with redirect_stderr(buf):
        rc = mod.guided_install()
    guided_teardown(mod, saved, st, origs)
    check(
        "guided serve denied hints sudo",
        rc == 0
        and ["tailscale", "serve", "--bg", "http://127.0.0.1:4096"] in st["cmds"]
        and "sudo tailscale serve --bg http://127.0.0.1:4096" in buf.getvalue()
        and "set --operator" in buf.getvalue(),
    )
    saved = guided_env(home)
    st, origs = guided_setup(
        mod,
        [str(base / "e5"), "", str(bindir / "opencode"), "", "y", "y", "n", "y"],
        deny_status=True,
    )
    buf = io.StringIO()
    with redirect_stderr(buf):
        rc = mod.guided_install()
    guided_teardown(mod, saved, st, origs)
    check(
        "guided status denied hints sudo",
        rc == 0
        and "sudo tailscale serve status" in buf.getvalue()
        and not [c for c in st["cmds"] if "--bg" in c],
    )

    # readiness gate: verify tests below use fake_api/service_password/port_listening
    def tails_called(cmds):
        return [c for c in cmds if c[:1] == ["tailscale"]]

    # systemctl rc0 but bad auth (valid password rejected) -> no tailscale write
    saved = guided_env(home)
    st, origs = guided_setup(
        mod,
        [str(base / "badauth"), "", str(bindir / "opencode"), "", "y", "y", "n", "y"],
        ready="bad",
    )
    rc = mod.guided_install()
    guided_teardown(mod, saved, st, origs)
    check("guided bad auth no proxy", rc == 0 and not tails_called(st["cmds"]))

    # systemctl rc0 but open endpoint (no auth required) -> no tailscale write
    saved = guided_env(home)
    st, origs = guided_setup(
        mod,
        [str(base / "openauth"), "", str(bindir / "opencode"), "", "y", "y", "n", "y"],
        ready="open",
    )
    rc = mod.guided_install()
    guided_teardown(mod, saved, st, origs)
    check("guided open endpoint no proxy", rc == 0 and not tails_called(st["cmds"]))

    # rc0 start but endpoint down -> no tailscale write
    saved = guided_env(home)
    st, origs = guided_setup(
        mod,
        [str(base / "notready"), "", str(bindir / "opencode"), "", "y", "y", "n", "y"],
        ready="down",
    )
    rc = mod.guided_install()
    guided_teardown(mod, saved, st, origs)
    check("guided not ready no proxy", rc == 0 and not tails_called(st["cmds"]))

    # preflight: unrelated listener on port while unit inactive -> start skipped, no tailscale
    saved = guided_env(home)
    st, origs = guided_setup(
        mod,
        [str(base / "occupied"), "", str(bindir / "opencode"), "", "y", "y", "n"],
        listening=True,
    )
    buf = io.StringIO()
    with redirect_stderr(buf):
        rc = mod.guided_install()
    guided_teardown(mod, saved, st, origs)
    check(
        "guided occupied port skips start",
        rc == 0
        and not [c for c in st["cmds"] if c[2:3] in (["enable"], ["restart"])]
        and not tails_called(st["cmds"])
        and "unrelated" in buf.getvalue(),
    )

    # active unit stays active, readiness verified post restart -> tailscale write ok
    saved = guided_env(home)
    st, origs = guided_setup(
        mod,
        [str(base / "stillactive"), "", str(bindir / "opencode"), "", "y", "y", "n", "y"],
        ready="good",
    )
    rc = mod.guided_install()
    guided_teardown(mod, saved, st, origs)
    bg = [c for c in st["cmds"] if c[:2] == ["tailscale", "serve"] and "--bg" in c]
    check("guided verified exposes", rc == 0 and len(bg) == 1)

    # verify_service direct: denied+200 dict passes; 200 non-dict body fails
    orig_api, orig_sleep = mod.api_get, mod.time.sleep
    mod.time.sleep = lambda s: None
    pw_token = mod.basic_token("pw")

    def api_ok(port, token=None, timeout=5, hostname="127.0.0.1"):
        return (200, b'{"a":1}') if token == pw_token else (401, b"")

    def api_nodict(port, token=None, timeout=5, hostname="127.0.0.1"):
        return (200, b"[1,2]") if token == pw_token else (401, b"")

    mod.api_get = api_ok
    ok_shape = mod.verify_service(1, "pw", attempts=1)
    mod.api_get = api_nodict
    bad_shape = mod.verify_service(1, "pw", attempts=1)
    mod.api_get, mod.time.sleep = orig_api, orig_sleep
    check("verify_service shape", ok_shape and not bad_shape)

    # serve_matches strictness incl. trailing-slash form
    dns_key = dns.rstrip(".")
    check(
        "serve_matches strict",
        mod.serve_matches(same, dns_key, 4096)
        and mod.serve_matches(
            {
                "TCP": {"443": {"HTTPS": True}},
                "Web": {f"{dns_key}:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:4096"}}}},
            },
            dns_key,
            4096,
        )
        and not mod.serve_matches(None, dns_key, 4096)
        and not mod.serve_matches({}, dns_key, 4096)
        and not mod.serve_matches(conflict, dns_key, 4096)
        and not mod.serve_matches(funnel, dns_key, 4096),
    )

    # explicit flags stay noninteractive: works with stdin closed even when no TTY
    (base / "op_state.json").write_text(json.dumps({"values": {"op://v/i/k": "v"}, "fail": False}))
    env = dict(os.environ, HOME=str(home), PATH=str(bindir), XDG_CONFIG_HOME=str(home / ".config"))
    with open(os.devnull) as null:
        proc = subprocess.run(
            [
                sys.executable,
                str(REPO / "serve.py"),
                "install",
                "--root",
                str(base / "explicit"),
                "--secrets",
                str(sec),
                "--opencode",
                str(bindir / "opencode"),
            ],
            env=env,
            stdin=null,
            capture_output=True,
            text=True,
            timeout=120,
        )
    check(
        "explicit flags noninteractive",
        proc.returncode == 0 and (base / "explicit/runtime.json").exists(),
    )
    default_binary = home / ".opencode/bin/opencode"
    default_binary.parent.mkdir(parents=True, exist_ok=True)
    default_binary.symlink_to(bindir / "opencode")
    with open(os.devnull) as null:
        proc = subprocess.run(
            [sys.executable, str(REPO / "serve.py"), "install", "--password", "only-flag-secret"],
            env=env,
            stdin=null,
            capture_output=True,
            text=True,
            timeout=120,
        )
    check(
        "password flag alone selects noninteractive install",
        proc.returncode == 0
        and json.loads(
            (home / ".local/share/opencode-serve/config/opencode/service.json").read_text()
        )["password"]
        == "only-flag-secret"
        and "only-flag-secret" not in proc.stdout + proc.stderr,
    )
    proc = subprocess.run(
        [sys.executable, str(REPO / "serve.py"), "run", "--password", "run-secret"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    check(
        "run rejects install-only password without printing it",
        proc.returncode == 2 and "run-secret" not in proc.stdout + proc.stderr,
    )


if __name__ == "__main__":
    sys.exit(main())
