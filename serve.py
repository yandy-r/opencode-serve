#!/usr/bin/env python3
"""Install or launch isolated OpenCode foreground server. Python stdlib only."""

import argparse
import base64
import contextlib
import fcntl
import http.client
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

RESERVED = {
    "HOME",
    "PATH",
    "OP_SERVICE_ACCOUNT_TOKEN",
    "OPENCODE_DB",
    "OPENCODE_PASSWORD",
    "OPENCODE_CONFIG",
    "OPENCODE_CONFIG_DIR",
}

DEFAULT_PORT = 4096


def default_root():
    return Path.home() / ".local/share/opencode-serve"


def default_secrets():
    return Path(".opencode-serve.local/references.json")


def default_opencode():
    return str(Path.home() / ".opencode/bin/opencode")


class _Cancel(Exception):
    pass


def cancelled(dirty):
    if dirty:
        print("Cancelled; existing writes and service changes are retained.")
    else:
        print("Cancelled; nothing changed.")
    return 1


def private_dir(path):
    if path.is_symlink():
        raise ValueError("private directory cannot be a symlink")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def atomic(path, data, mode=0o600):
    fd, temp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def quote(value):
    # systemd: double-quoted arguments, literal specifiers and dollar signs.
    value = str(value)
    if any(ord(c) < 32 for c in value):
        raise ValueError("control character in systemd path")
    return (
        '"'
        + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
        + '"'
    )


def port_value(raw):
    try:
        port = int(raw)
    except (TypeError, ValueError):
        raise ValueError("port must be 1..65535") from None
    if not 1 <= port <= 65535:
        raise ValueError("port must be 1..65535")
    return port


def executable(name):
    path = shutil.which(name)
    if not path:
        raise ValueError("required executable missing")
    return str(Path(path).absolute())


def validated_refs(refs):
    if not isinstance(refs, dict):
        raise ValueError("references must be a JSON object")
    for name, ref in refs.items():
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
            or name in RESERVED
            or name.startswith("XDG_")
            or name.startswith("OPENCODE_SERVE_")
            or name.startswith("_")
            or not isinstance(ref, str)
            or not ref.startswith("op://")
            or "\0" in ref
        ):
            raise ValueError("invalid or reserved environment reference")
    return refs


def do_install(root, refs, binary_name, port, keep_env=None):
    root = Path(root).absolute()
    home = Path.home()
    host_config = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "opencode"
    unit_dir = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "systemd/user"
    refs = validated_refs(refs)
    binary = executable(binary_name)
    op = executable("op") if refs else None
    private_dir(root)
    # One writer; fetch all values before replacing any working runtime files.
    with (root / ".install.lock").open("w") as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        if keep_env is not None:
            fetched = dict(keep_env)
        else:
            fetched = {}
            for name, ref in refs.items():
                result = subprocess.run(
                    [op, "read", "--no-newline", ref],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    timeout=60,
                )
                if result.returncode:
                    raise ValueError("secret fetch failed; previous runtime unchanged")
                value = result.stdout.decode("utf-8")  # No stripping or newline translation.
                if not value or "\0" in value:
                    raise ValueError("empty or NUL secret; previous runtime unchanged")
                fetched[name] = value
        for sub in ("config/opencode", "data", "state", "cache"):
            private_dir(root / sub)
        service = root / "config/opencode/service.json"
        if service.exists():
            saved = json.loads(service.read_text())
            if (
                not isinstance(saved, dict)
                or not isinstance(saved.get("password"), str)
                or not saved["password"]
            ):
                raise ValueError("existing service password invalid; refusing replacement")
        else:
            atomic(service, json.dumps({"password": secrets.token_urlsafe(32)}) + "\n")
        service.chmod(0o600)
        # Config entries are shared intentionally; service.json never shared.
        if host_config.is_dir():
            for source in host_config.iterdir():
                if source.name == "service.json":
                    continue
                target = root / "config/opencode" / source.name
                if not target.exists() and not target.is_symlink():
                    target.symlink_to(source.absolute(), target_is_directory=source.is_dir())
        config = {
            "binary": binary,
            "path": os.environ.get("PATH", os.defpath),
            "home": str(home),
            "port": port,
            "env": fetched,
        }
        atomic(root / "runtime.json", json.dumps(config) + "\n")
        atomic(root / "serve.py", Path(__file__).read_text())
        template = Path(__file__).with_name("opencode-serve.service.in").read_text()
        unit = template.replace(
            "@EXEC@",
            " ".join(
                quote(x)
                for x in [executable(sys.executable), root / "serve.py", "run", "--root", root]
            ),
        )
        unit_dir.mkdir(parents=True, exist_ok=True)
        atomic(unit_dir / "opencode-serve.service", unit)


def install(args):
    root = (args.root or default_root()).absolute()
    refs, keep = {}, None
    if args.secrets is not None:
        refs = validated_refs(json.loads(args.secrets.read_text()))
    else:
        try:
            saved_env = json.loads((root / "runtime.json").read_text()).get("env")
            if isinstance(saved_env, dict) and saved_env:
                keep = saved_env
        except (OSError, ValueError):
            pass
    do_install(
        root, refs, args.opencode or default_opencode(), args.port or DEFAULT_PORT, keep_env=keep
    )


def ask(text, default=None):
    hint = "" if default in (None, "") else f" [{default}]"
    try:
        answer = input(f"{text}{hint}: ").strip()
    except EOFError:
        raise _Cancel() from None
    return answer if answer else ("" if default is None else str(default))


def ask_yes(text, default_yes):
    hint = "Y/n" if default_yes else "y/N"
    try:
        answer = input(f"{text} [{hint}]: ").strip().lower()
    except EOFError:
        raise _Cancel() from None
    if not answer:
        return default_yes
    if answer in ("y", "yes"):
        return True
    if answer in ("n", "no"):
        return False
    print("Answer y or n.")
    return ask_yes(text, default_yes)


def cmd(argv):
    try:
        return subprocess.run(argv, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None


def denied(result):
    return result is not None and result.stderr is not None and b"Access denied" in result.stderr


API_INFO = "/api/info"  # documented V2 endpoint; 200 + JSON object when authed


def api_get(port, token=None, timeout=5):
    """GET /api/info on loopback. http.client: no proxy env, no redirects."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        headers = {"Authorization": "Basic " + token} if token else {}
        conn.request("GET", API_INFO, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read()
    except OSError:
        return None, b""
    finally:
        conn.close()


def basic_token(password):
    return base64.b64encode(("opencode:" + password).encode()).decode()


def port_listening(port):
    return api_get(port)[0] is not None


def verify_service(port, password, attempts=10):
    """Bounded: wrong/no Basic auth denied AND correct auth 200 JSON object."""
    good, bad = basic_token(password), basic_token(password + "-wrong")
    for _ in range(attempts):
        if api_get(port, bad)[0] in (401, 403) and api_get(port)[0] in (401, 403):
            status, body = api_get(port, good)
            if status == 200:
                try:
                    return isinstance(json.loads(body), dict)
                except ValueError:
                    return False
        time.sleep(1)
    return False


def service_password(root):
    try:
        saved = json.loads((Path(root) / "config/opencode/service.json").read_text())
    except (OSError, ValueError):
        return None
    password = saved.get("password") if isinstance(saved, dict) else None
    return password if isinstance(password, str) and password else None


def serve_matches(cfg, dns, port):
    if not isinstance(cfg, dict):
        return False
    for key in ("Services", "AllowFunnel", "Foreground"):
        if cfg.get(key):
            return False
    if set(cfg) - {"TCP", "Web"}:
        return False
    if cfg.get("TCP") != {"443": {"HTTPS": True}}:
        return False
    web = cfg.get("Web")
    if not isinstance(web, dict) or len(web) != 1:
        return False
    key = next(iter(web))
    if key != f"{dns}:443":
        return False
    handlers = web[key].get("Handlers") if isinstance(web[key], dict) else None
    if not isinstance(handlers, dict) or len(handlers) != 1:
        return False
    handler = handlers.get("/")
    if not isinstance(handler, dict) or set(handler) != {"Proxy"}:
        return False
    return handler["Proxy"] in (f"http://127.0.0.1:{port}/", f"http://127.0.0.1:{port}")


def guided_install():
    dirty = False
    if not sys.stdin.isatty():
        print(
            "opencode-serve: guided install needs a terminal; rerun in a terminal"
            " or pass explicit flags: install --root DIR --secrets FILE --opencode PATH --port PORT",
            file=sys.stderr,
        )
        return 1
    try:
        print("Guided setup. Writes only the install root and the user unit.")
        print(
            "DB is isolated at <root>/data/server.db; provider keys come only"
            " from the refs file. Host logins are not shared."
        )
        root_raw = ask("Install root", default_root())
        root = Path(root_raw).expanduser()
        root = root if root.is_absolute() else Path.cwd() / root
        root = root.absolute()
        if root_raw and not Path(root_raw).expanduser().is_absolute():
            print(f"Resolved root: {root} (relative to current dir, not the root itself)")
        runtime = root / "runtime.json"
        port_default = DEFAULT_PORT
        if runtime.exists():
            with contextlib.suppress(OSError, ValueError):
                port_default = port_value(json.loads(runtime.read_text()).get("port", DEFAULT_PORT))
        while True:
            try:
                port = port_value(ask("Port", port_default))
                break
            except ValueError:
                print("Port must be 1..65535.")
        binary = ask("opencode binary", default_opencode())
        default_ref = default_secrets()
        if default_ref.exists():
            # Suggestion only; Enter always means no refs.
            print(f"Refs file found (suggestion only): {default_ref.absolute()}")
        ref_raw = ask("Refs file (Enter for none)")
        refs, ref_shown = {}, "(none)"
        if ref_raw:
            ref_path = Path(ref_raw).expanduser()
            ref_path = ref_path if ref_path.is_absolute() else Path.cwd() / ref_path
            ref_shown = str(ref_path.absolute())
            print(f"Resolved refs: {ref_shown}")
            try:
                refs = validated_refs(json.loads(ref_path.read_text()))
            except (OSError, ValueError):
                print(f"Cannot use refs file {ref_shown}; check path and JSON.", file=sys.stderr)
                return 1
        keep = None
        if not refs and runtime.exists():
            try:
                saved_env = json.loads(runtime.read_text()).get("env")
                if isinstance(saved_env, dict) and saved_env:
                    keep = saved_env
                    print(
                        f"Keeping {len(keep)} existing runtime secret(s); empty refs will not erase them."
                    )
            except (OSError, ValueError):
                pass
        names = ", ".join(sorted(refs)) if refs else ("(kept)" if keep else "(none)")
        print(f"Root: {root}\nPort: {port}\nBinary: {binary}\nRefs: {ref_shown}\nSecrets: {names}")
        if not ask_yes("Write files?", True):
            print("Cancelled; nothing changed.")
            return 1
        try:
            if binary and not Path(binary).exists() and not shutil.which(binary):
                print("opencode binary not found; check the path.", file=sys.stderr)
                return 1
            do_install(root, refs, binary, port, keep_env=keep)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            print(
                "opencode-serve: failed; check inputs, private-file permissions and CLI availability",
                file=sys.stderr,
            )
            return 1
        dirty = True  # files + unit written
        print("Installed opencode-serve.service; not started yet.")
        running = verified = False
        if ask_yes(
            "Start/restart service now? (daemon-reload, then enable --now; restarts the unit on reinstall)",
            True,
        ):
            reloaded = cmd(["systemctl", "--user", "daemon-reload"])
            if reloaded is None or reloaded.returncode:
                print(
                    "systemctl daemon-reload failed; run: systemctl --user daemon-reload",
                    file=sys.stderr,
                )
            else:
                active = cmd(["systemctl", "--user", "is-active", "opencode-serve.service"])
                was_active = active is not None and active.returncode == 0
                if not was_active and port_listening(port):
                    # Never proxy or restart over an unrelated listener.
                    print(
                        f"Port {port} already answers and the unit is inactive; unrelated"
                        f" listener suspected. Start skipped; free the port or pick another"
                        f" port and reinstall.",
                        file=sys.stderr,
                    )
                else:
                    verb = ["restart"] if was_active else ["enable", "--now"]
                    done = cmd(["systemctl", "--user", *verb, "opencode-serve.service"])
                    if done is None or done.returncode:
                        print(
                            "Service start failed; not exposing via Tailscale."
                            " Run: systemctl --user status opencode-serve.service",
                            file=sys.stderr,
                        )
                    else:
                        active = cmd(["systemctl", "--user", "is-active", "opencode-serve.service"])
                        password = service_password(root)
                        if (
                            active is not None
                            and active.returncode == 0
                            and password
                            and verify_service(port, password)
                        ):
                            running = verified = True
                        else:
                            print(
                                "Service start reported success but the unit is not active or"
                                f" {API_INFO} did not reject bad auth and accept the real password"
                                f" on 127.0.0.1:{port}; skipping Tailscale."
                                " Run: systemctl --user status opencode-serve.service",
                                file=sys.stderr,
                            )
        else:
            active = cmd(["systemctl", "--user", "is-active", "opencode-serve.service"])
            running = active is not None and active.returncode == 0
            if running:
                password = service_password(root)
                verified = bool(password and verify_service(port, password))
            print("Skipped service start.")
        if ask_yes(
            "Enable linger so the unit survives logout? (loginctl enable-linger, may need permission)",
            False,
        ):
            user = os.environ.get("USER") or Path.home().name
            lingered = cmd(["loginctl", "enable-linger", user])
            if lingered is None or lingered.returncode:
                print(
                    f"loginctl failed (no sudo attempted); run yourself: loginctl enable-linger {user}",
                    file=sys.stderr,
                )
        if verified:
            if ask_yes(
                "Expose on tailnet via Tailscale Serve? (tailnet-only HTTPS;"
                " anyone with tailnet access plus password gets full coding access)",
                False,
            ):
                if not shutil.which("tailscale"):
                    print("tailscale not found; install it first.", file=sys.stderr)
                else:
                    status = cmd(["tailscale", "status", "--json"])
                    dns = None
                    if status is not None and status.returncode == 0:
                        try:
                            dns = (
                                json.loads(status.stdout.decode())
                                .get("Self", {})
                                .get("DNSName", "")
                                .rstrip(".")
                            )
                        except (ValueError, AttributeError):
                            dns = None
                    if not dns:
                        print(
                            "Cannot read tailnet DNS name; run: tailscale status --json",
                            file=sys.stderr,
                        )
                    else:
                        current = cmd(["tailscale", "serve", "status", "--json"])
                        if current is None or current.returncode:
                            print(
                                "Cannot read Serve state; run: tailscale serve status",
                                file=sys.stderr,
                            )
                            if denied(current):
                                print(
                                    "Access denied suggests Serve is root-managed; check with:"
                                    " sudo tailscale serve status",
                                    file=sys.stderr,
                                )
                        else:
                            try:
                                cfg = json.loads(
                                    (current.stdout or b"null").decode().strip() or "null"
                                )
                            except ValueError:
                                cfg = "invalid"
                            if cfg in (None, {}):
                                exposed = cmd(
                                    ["tailscale", "serve", "--bg", f"http://127.0.0.1:{port}"]
                                )
                                if exposed is None or exposed.returncode:
                                    if denied(exposed):
                                        print(
                                            "Tailscale permission denied; no sudo attempted. Run yourself:\n"
                                            f"  sudo tailscale serve --bg http://127.0.0.1:{port}\n"
                                            "Optional: sudo tailscale set --operator=$USER grants this user"
                                            " broader Tailscale control; choose only if intended.",
                                            file=sys.stderr,
                                        )
                                    else:
                                        print(
                                            "tailscale serve failed; check HTTPS certificates and permissions;"
                                            " run: tailscale serve status",
                                            file=sys.stderr,
                                        )
                                else:
                                    print(f"Serving at https://{dns}")
                            elif cfg != "invalid" and serve_matches(cfg, dns, port):
                                print(f"Already serving at https://{dns}; nothing changed.")
                            else:
                                print(
                                    "Existing Serve config found; refusing to overwrite it."
                                    " Inspect with: tailscale serve status (never reset without backup)",
                                    file=sys.stderr,
                                )
                                # ponytail: no raw-config merge; add only for an explicit --force path.
        else:
            if running:
                print("Service running but endpoint not verified; skipping Tailscale exposure.")
            else:
                print("Service not running; skipping Tailscale exposure.")
        print("Password stays private. As user `opencode`, get it with:")
        print(
            f"  python3 -c \"import json;print(json.load(open('{root}/config/opencode/service.json'))['password'])\""
        )
        print("Serve state: tailscale serve status")
        return 0
    except _Cancel:
        return cancelled(dirty)
    except KeyboardInterrupt:
        print()
        return cancelled(dirty)


def run(root):
    config = json.loads((root / "runtime.json").read_text())
    port = port_value(config.get("port", 4096))
    password = service_password(root)
    if not password:
        raise ValueError("server password missing; refusing unauthenticated startup")
    env = {
        "HOME": config["home"],
        "PATH": config["path"],
        "LANG": "C.UTF-8",
        "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_DATA_HOME": str(root / "data"),
        "XDG_STATE_HOME": str(root / "state"),
        "XDG_CACHE_HOME": str(root / "cache"),
        "OPENCODE_DB": str(root / "data/server.db"),
    }
    env.update(config["env"])
    env.pop("OP_SERVICE_ACCOUNT_TOKEN", None)
    # Both streams suppressed: OpenCode can print passwords; no journal leaks.
    with open(os.devnull, "wb") as sink:
        os.dup2(sink.fileno(), 1)
        saved_err = os.dup(2)
        try:
            os.dup2(sink.fileno(), 2)
            os.umask(0o077)
            os.chdir(config["home"])
            os.execve(
                config["binary"],
                [
                    config["binary"],
                    "serve",
                    "--service",
                    "--hostname",
                    "127.0.0.1",
                    "--port",
                    str(port),
                ],
                env,
            )
        finally:
            os.dup2(saved_err, 2)
            os.close(saved_err)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["install", "run"])
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--secrets", type=Path, default=None)
    parser.add_argument("--opencode", default=None)
    parser.add_argument(
        "--port",
        type=port_value,
        default=None,
        help="install-time listen port, 1..65535 (run uses runtime.json)",
    )
    args = parser.parse_args()
    if args.action == "install" and any(
        getattr(args, name) is not None for name in ("root", "secrets", "opencode", "port")
    ):
        try:
            os.umask(0o077)
            install(args)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            # Do not print exception strings: JSON / subprocess errors may contain secrets.
            print(
                "opencode-serve: failed; check inputs, private-file permissions and CLI availability",
                file=sys.stderr,
            )
            return 1
        print("Installed opencode-serve.service; not started. See README for manual activation.")
        return 0
    try:
        os.umask(0o077)
        if args.action == "install":
            return guided_install()
        else:
            run((args.root or default_root()).absolute())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        # Do not print exception strings: JSON / subprocess errors may contain secrets.
        print(
            "opencode-serve: failed; check inputs, private-file permissions and CLI availability",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
