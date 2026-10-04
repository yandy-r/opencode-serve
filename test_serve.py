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
import shutil
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


def do_install(mod, base, home, bindir, refs_obj, root):
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
    print("result:", "ALL PASS" if not failures else f"FAILURES {failures}")
    return 1 if failures else 0


def guided_env(home):
    saved = dict(os.environ)
    os.environ.update(
        {"HOME": str(home), "PATH": "/usr/bin:/bin", "XDG_CONFIG_HOME": str(home / ".config")}
    )
    return saved


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
):
    import builtins

    state = {"installs": [], "cmds": [], "answers": list(answers), "started": False}
    orig_input, orig_stdin, orig_cmd, orig_install, orig_which = (
        builtins.input,
        sys.stdin,
        mod.cmd,
        mod.do_install,
        shutil.which,
    )
    orig_api, orig_sleep, orig_svc_pw = mod.api_get, mod.time.sleep, mod.service_password
    orig_listening = mod.port_listening

    def fake_api(port, token=None, timeout=5):
        if ready == "down":
            return None, b""
        if ready == "open":
            return 200, b"{}"
        if ready == "bad" or token != mod.basic_token("pw"):
            return 401, b""
        return 200, b'{"ok": true}'

    def fake_input(prompt=""):
        sys.stdout.write(prompt)
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
            if serve_cfg == "fail":
                return subprocess.CompletedProcess(argv, 1, b"", b"")
            body = "null" if serve_cfg is None else json.dumps(serve_cfg)
            return subprocess.CompletedProcess(argv, 0, body.encode(), b"")
        if list(argv)[:2] == ["tailscale", "serve"]:
            if deny_bg:
                return subprocess.CompletedProcess(
                    argv, 1, b"", b"Access denied: serve config denied"
                )
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        return subprocess.CompletedProcess(argv, 1, b"", b"")

    def fake_install(root, refs, binary_name, port, keep_env=None):
        state["installs"].append(
            {
                "root": str(root),
                "refs": dict(refs),
                "binary": binary_name,
                "port": port,
                "keep": keep_env,
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
    mod.port_listening = lambda port: listening
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
    )


def guided_teardown(mod, saved_env, state, origs):
    import builtins

    orig_input, orig_stdin, orig_cmd, orig_install, orig_which = origs[:5]
    orig_api, orig_sleep, orig_svc_pw, orig_listening = origs[5:]
    builtins.input, sys.stdin, mod.cmd, mod.do_install = (
        orig_input,
        orig_stdin,
        orig_cmd,
        orig_install,
    )
    mod.shutil.which = orig_which
    mod.api_get, mod.time.sleep = orig_api, orig_sleep
    mod.service_password, mod.port_listening = orig_svc_pw, orig_listening
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
        and bg == [["tailscale", "serve", "--bg", "http://127.0.0.1:5123"]],
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
        and st["cmds"][-1] == ["tailscale", "serve", "--bg", "http://127.0.0.1:4096"]
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

    def api_ok(port, token=None, timeout=5):
        return (200, b'{"a":1}') if token == pw_token else (401, b"")

    def api_nodict(port, token=None, timeout=5):
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


if __name__ == "__main__":
    sys.exit(main())
