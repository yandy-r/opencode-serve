#!/usr/bin/env python3
"""Install or launch isolated OpenCode foreground server. Python stdlib only."""

import argparse
import base64
import contextlib
import fcntl
import getpass
import http.client
import ipaddress
import json
import os
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import warnings
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
_OPERATION_MUTEX = threading.RLock()
_HELD_OPERATION_LOCK = None

COMPLETIONS = {
    "bash": r"""# opencode-serve Bash completion (source this file).
_opencode_serve() {
    local cur prev action options candidate
    cur=${COMP_WORDS[COMP_CWORD]}
    prev=${COMP_WORDS[COMP_CWORD-1]}
    action=${COMP_WORDS[1]}
    COMPREPLY=()
    case "$prev" in
        --password|--pasword|--port|--hostname) return ;;
        --use-ts)
            while IFS= read -r candidate; do COMPREPLY+=("$candidate"); done < <(compgen -W 'true false' -- "$cur")
            return ;;
        --root|--secrets|--opencode)
            while IFS= read -r -d '' candidate; do COMPREPLY+=("$candidate"); done < <(
                unset GLOBIGNORE
                shopt -s nullglob
                shopt -u failglob
                for candidate in "$cur"*; do
                    if [[ "$prev" == --root ]]; then
                        [[ -d "$candidate" ]] || continue
                    else
                        [[ -e "$candidate" || -L "$candidate" ]] || continue
                    fi
                    printf '%s\0' "$candidate"
                done
            )
            compopt -o filenames 2>/dev/null || :
            return ;;
    esac
    options='--help -h'
    case "$action" in
        install) options+=' --root --secrets --opencode --port --password --pasword --use-ts --hostname' ;;
        run) options+=' --root' ;;
        remove) options+=' --root --purge' ;;
        completion) options+=' bash zsh fish --install --force' ;;
        *) options+=' install run completion remove' ;;
    esac
    while IFS= read -r candidate; do COMPREPLY+=("$candidate"); done < <(compgen -W "$options" -- "$cur")
}
complete -F _opencode_serve serve.py
""",
    "zsh": r"""# opencode-serve Zsh completion (source this file).
_opencode_serve() {
    local context state state_descr line
    typeset -A opt_args
    _arguments -C \
        '(-h --help)'{-h,--help}'[Show help]' \
        '1:action:(install run completion remove)' \
        '*::argument:->arguments'
    case "$state" in
        arguments)
            case "$line[1]" in
                install)
                    _arguments \
                        '--root[Install root]:directory:_directories' \
                        '--secrets[1Password references JSON]:file:_files' \
                        '--opencode[OpenCode executable]:executable:_files' \
                        '--port[Listen port]:port:' \
                        '--use-ts[Use Tailscale networking]:boolean:(true false)' \
                        '--hostname[Bind IP for external proxy]:IP address:' \
                        '(--password --pasword)'{--password,--pasword}'[Set server password]:password:' \
                        '(-h --help)'{-h,--help}'[Show help]' ;;
                run)
                    _arguments '--root[Install root]:directory:_directories' \
                        '(-h --help)'{-h,--help}'[Show help]' ;;
                remove)
                    _arguments '--root[Install root]:directory:_directories' \
                        '--purge[Delete installation data and credentials]' \
                        '(-h --help)'{-h,--help}'[Show help]' ;;
                completion)
                    _arguments '1:shell:(bash zsh fish)' \
                        '--install[Install user shell completions]' \
                        '--force[Replace existing completion file]' \
                        '(-h --help)'{-h,--help}'[Show help]' ;;
            esac ;;
    esac
}
if (( ! $+functions[compdef] )); then
    autoload -Uz compinit
    compinit
fi
compdef _opencode_serve serve.py
""",
    "fish": r"""# opencode-serve Fish completion.
complete -c serve.py -f
complete -c serve.py -n '__fish_use_subcommand' -a 'install run completion remove'
complete -c serve.py -s h -l help -d 'Show help'
complete -c serve.py -n '__fish_seen_subcommand_from install run remove' -l root -r -a '(__fish_complete_directories)' -d 'Install root'
complete -c serve.py -n '__fish_seen_subcommand_from install' -l secrets -r -F -d '1Password references JSON'
complete -c serve.py -n '__fish_seen_subcommand_from install' -l opencode -r -F -d 'OpenCode executable'
complete -c serve.py -n '__fish_seen_subcommand_from install' -l port -x -d 'Listen port'
complete -c serve.py -n '__fish_seen_subcommand_from install' -l use-ts -x -a 'true false' -d 'Use Tailscale networking'
complete -c serve.py -n '__fish_seen_subcommand_from install' -l hostname -x -d 'Bind IP for external proxy'
complete -c serve.py -n '__fish_seen_subcommand_from remove' -l purge -d 'Delete installation data and credentials'
complete -c serve.py -n '__fish_seen_subcommand_from install' -l password -x -d 'Set server password'
complete -c serve.py -n '__fish_seen_subcommand_from install' -l pasword -x -d 'Set server password'
complete -c serve.py -n '__fish_seen_subcommand_from completion; and not __fish_seen_subcommand_from bash zsh fish' -a 'bash zsh fish'
complete -c serve.py -n '__fish_seen_subcommand_from completion' -l install -d 'Install user shell completions'
complete -c serve.py -n '__fish_seen_subcommand_from completion' -l force -d 'Replace existing completion file'
""",
}


def install_completion(shell, force):
    home = Path.home()
    if shell == "fish":
        config = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
        # Eager loading also supports ./serve.py when its directory is not on PATH.
        destination = config / "fish/conf.d/opencode-serve.fish"
        rc = None
    else:
        data = Path(os.environ.get("XDG_DATA_HOME", home / ".local/share"))
        destination = data / "opencode-serve/completions" / f"serve.py.{shell}"
        rc = (
            Path(os.environ.get("ZDOTDIR", home)) / ".zshrc" if shell == "zsh" else home / ".bashrc"
        ).resolve()
    destination = destination.absolute()
    if destination.exists() and not destination.is_symlink() and not destination.is_file():
        raise ValueError(
            "completion path is not a regular file; choose a different data/config directory"
        )
    if (destination.exists() or destination.is_symlink()) and not force:
        raise ValueError("completion file exists; use --force to replace it")
    rc_text = None
    if rc is not None:
        previous = rc.read_text() if rc.exists() else ""
        start, end = "# >>> opencode-serve completion >>>", "# <<< opencode-serve completion <<<"
        block = f"{start}\nif [ -r {shlex.quote(str(destination))} ]; then\n    . {shlex.quote(str(destination))}\nfi\n{end}"
        if start in previous or end in previous:
            if previous.count(start) != 1 or previous.count(end) != 1:
                raise ValueError(
                    "invalid completion block in shell config; fix it before installing"
                )
            before, rest = previous.split(start)
            if end not in rest:
                raise ValueError(
                    "invalid completion block in shell config; fix it before installing"
                )
            _, after = rest.split(end)
            rc_text = before + block + after
        else:
            rc_text = (
                previous + ("\n" if previous and not previous.endswith("\n") else "") + block + "\n"
            )
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if destination.exists() or destination.is_symlink():
        fd, name = tempfile.mkstemp(prefix=".completion-backup-", dir=destination.parent)
        os.close(fd)
        backup = Path(name)
        try:
            backup.unlink()
            shutil.copy2(destination, backup, follow_symlinks=False)
        except OSError:
            backup.unlink(missing_ok=True)
            raise
    try:
        atomic(destination, COMPLETIONS[shell], mode=0o644)
        if rc is not None:
            rc.parent.mkdir(parents=True, exist_ok=True)
            mode = rc.stat().st_mode & 0o777 if rc.exists() else 0o600
            atomic(rc, rc_text, mode=mode)
    except (OSError, ValueError):
        try:
            if backup is not None:
                os.replace(backup, destination)
            else:
                destination.unlink(missing_ok=True)
        except OSError as error:
            recovery = f"previous completion retained at {backup}" if backup else str(destination)
            raise OSError(f"completion rollback failed; inspect {recovery}") from error
        raise
    if backup is not None:
        try:
            backup.unlink(missing_ok=True)
        except OSError:
            print(f"Completion installed; old backup retained at {backup}", file=sys.stderr)
    print(f"Installed {shell} completions: {destination}")
    print("Open a new shell to load the completions.")


def completion_main(argv):
    parser = argparse.ArgumentParser(description="Print or install shell completions.")
    parser.add_argument("shell", nargs="?", choices=tuple(COMPLETIONS), help="default: $SHELL")
    parser.add_argument("--install", action="store_true", help="install for the current user")
    parser.add_argument("--force", action="store_true", help="replace an existing completion file")
    args = parser.parse_args(argv)
    if args.force and not args.install:
        parser.error("--force requires --install")
    shell = args.shell or Path(os.environ.get("SHELL", "")).name
    if shell not in COMPLETIONS:
        parser.error("specify bash, zsh or fish, or set $SHELL to a supported shell")
    if not args.install:
        print(COMPLETIONS[shell], end="")
        return 0
    try:
        install_completion(shell, args.force)
    except (OSError, ValueError) as error:
        print(f"opencode-serve: completion install failed: {error}", file=sys.stderr)
        return 1
    return 0


def default_root():
    return Path.home() / ".local/share/opencode-serve"


def default_secrets():
    return Path(".opencode-serve.local/references.json")


def default_opencode():
    return str(Path.home() / ".opencode/bin/opencode")


class _Cancel(Exception):
    pass


class RootRefusal(ValueError):
    """Unrecognized nonempty install root. Message carries only escaped paths, safe to print."""


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


def network_settings(use_ts, hostname=None):
    if not isinstance(use_ts, bool):
        raise ValueError("use_ts must be true or false")
    hostname = hostname if hostname is not None else ("127.0.0.1" if use_ts else "0.0.0.0")
    if not isinstance(hostname, str):
        raise ValueError("hostname must be an IP address")
    hostname = str(ipaddress.ip_address(hostname))
    if use_ts and hostname != "127.0.0.1":
        raise ValueError("Tailscale Serve requires the 127.0.0.1 listener")
    return hostname


def probe_hostname(hostname):
    return {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(hostname, hostname)


def runtime_config(root):
    path = root / "runtime.json"
    if not path.exists():
        return {}
    config = json.loads(path.read_text())
    if not isinstance(config, dict):
        raise ValueError("runtime must be a JSON object")
    return config


@contextlib.contextmanager
def file_lock(path):
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("lock path must be a regular file")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("lock path must be a regular file")
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


@contextlib.contextmanager
def operation_lock():
    """Serialize the shared user unit and Serve mutations, including nested calls."""
    global _HELD_OPERATION_LOCK
    path = Path.home() / ".local/state/opencode-serve/operation.lock"
    with _OPERATION_MUTEX:
        if _HELD_OPERATION_LOCK is not None:
            if path != _HELD_OPERATION_LOCK:
                raise ValueError("home changed during a service operation")
            yield
            return
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with file_lock(path):
            _HELD_OPERATION_LOCK = path
            try:
                yield
            finally:
                _HELD_OPERATION_LOCK = None


def recognized_install(root, config):
    if config.get("install_root") == str(root):
        return True
    launcher = root / "serve.py"
    return (
        launcher.is_file()
        and not launcher.is_symlink()
        and launcher.read_text().startswith(
            '#!/usr/bin/env python3\n"""Install or launch isolated OpenCode foreground server. Python stdlib only."""'
        )
        and all(key in config for key in ("binary", "home", "env", "port"))
    )


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


def do_install(
    root, refs, binary_name, port, keep_env=None, password=None, use_ts=None, hostname=None
):
    if password is not None and (not isinstance(password, str) or not password or "\0" in password):
        raise ValueError("password must be a nonempty string without NUL")
    root = Path(root).absolute()
    home = Path.home()
    host_config = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "opencode"
    unit_dir = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "systemd/user"
    refs = validated_refs(refs)
    binary = executable(binary_name)
    op = executable("op") if refs else None
    with operation_lock():
        if root.exists() and not recognized_install(root, runtime_config(root)):
            # `completion --install` writes <root>/completions for the default root; its
            # contents stay untouched. Everything else unexpected keeps the refusal.
            unexpected = [
                p.name
                for p in root.iterdir()
                if p.name != ".install.lock"
                and not (p.name == "completions" and p.is_dir() and not p.is_symlink())
            ]
            if unexpected:
                raise RootRefusal(
                    "first install requires an empty, dedicated directory; refusing root "
                    f"{ascii(str(root))}: unexpected entries: "
                    + ", ".join(ascii(name) for name in sorted(unexpected))
                )
        private_dir(root)
        # One writer; fetch all values before replacing any working runtime files.
        with file_lock(root / ".install.lock"):
            previous = runtime_config(root)
            old_mode = previous.get("use_ts", True)
            use_ts = old_mode if use_ts is None else use_ts
            if hostname is None and use_ts == old_mode:
                hostname = previous.get("hostname")
            hostname = network_settings(use_ts, hostname)
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
            if service.exists() and password is None:
                saved = json.loads(service.read_text())
                if (
                    not isinstance(saved, dict)
                    or not isinstance(saved.get("password"), str)
                    or not saved["password"]
                ):
                    raise ValueError("existing service password invalid; refusing replacement")
            owned_serve = previous.get("tailscale_serve")
            if owned_serve and (not use_ts or previous.get("port") != port):
                if not clear_owned_serve(previous):
                    raise ValueError(
                        "cannot change networking until the managed Serve mapping is removed"
                    )
                owned_serve = None
            if password is not None or not service.exists():
                atomic(
                    service,
                    json.dumps(
                        {
                            "password": (
                                password if password is not None else secrets.token_urlsafe(32)
                            )
                        }
                    )
                    + "\n",
                )
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
                "hostname": hostname,
                "use_ts": use_ts,
                "install_root": str(root),
            }
            if owned_serve:
                config["tailscale_serve"] = owned_serve
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
    with operation_lock():
        root = (args.root or default_root()).absolute()
        previous = runtime_config(root)
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
            root,
            refs,
            args.opencode or previous.get("binary") or default_opencode(),
            port_value(args.port if args.port is not None else previous.get("port", DEFAULT_PORT)),
            keep_env=keep,
            password=args.password,
            use_ts=args.use_ts,
            hostname=args.hostname,
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


def ask_password():
    while True:
        try:
            # Refuse getpass's echoed-input fallback if terminal access fails.
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                password = getpass.getpass("Server password (Enter to keep existing or generate): ")
                if not password:
                    return None
                if "\0" in password:
                    print("Password cannot contain NUL.")
                    continue
                confirmation = getpass.getpass("Confirm server password: ")
        except (EOFError, getpass.GetPassWarning):
            raise _Cancel() from None
        if password == confirmation:
            return password
        print("Passwords do not match; try again.")


def cmd(argv):
    try:
        return subprocess.run(argv, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None


def denied(result):
    return result is not None and result.stderr is not None and b"Access denied" in result.stderr


API_INFO = "/api/info"  # documented V2 endpoint; 200 + JSON object when authed


def api_get(port, token=None, timeout=5, hostname="127.0.0.1"):
    """GET /api/info directly. http.client: no proxy env, no redirects."""
    conn = http.client.HTTPConnection(probe_hostname(hostname), port, timeout=timeout)
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


def port_listening(port, hostname="127.0.0.1"):
    return api_get(port, hostname=hostname)[0] is not None


def verify_service(port, password, attempts=10, hostname="127.0.0.1"):
    """Bounded: wrong/no Basic auth denied AND correct auth 200 JSON object."""
    good, bad = basic_token(password), basic_token(password + "-wrong")
    for _ in range(attempts):
        if api_get(port, bad, hostname=hostname)[0] in (401, 403) and api_get(
            port, hostname=hostname
        )[0] in (401, 403):
            status, body = api_get(port, good, hostname=hostname)
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


def record_serve(root, dns, port):
    with operation_lock(), file_lock(root / ".install.lock"):
        config = runtime_config(root)
        if not config.get("use_ts", True) or config.get("port") != port:
            raise ValueError("installation changed during Tailscale setup")
        config["tailscale_serve"] = {"dns": dns, "port": port}
        atomic(root / "runtime.json", json.dumps(config) + "\n")


def create_serve(root, dns, port):
    with operation_lock(), file_lock(root / ".install.lock"):
        config = runtime_config(root)
        if config.get("removed") or not config.get("use_ts", True) or config.get("port") != port:
            raise ValueError("installation changed during Tailscale setup")
        current = cmd(["tailscale", "serve", "status", "--json"])
        try:
            empty = (
                current is not None
                and current.returncode == 0
                and json.loads(current.stdout) in (None, {})
            )
        except (ValueError, TypeError):
            empty = False
        if not empty:
            raise ValueError("Serve state changed; refusing to overwrite")
        # Durable pending state permits recovery if the process dies during the RPC.
        config["tailscale_serve"] = {"dns": dns, "port": port, "pending": True}
        atomic(root / "runtime.json", json.dumps(config) + "\n")
        argv = ["tailscale", "serve", "--bg", f"http://127.0.0.1:{port}"]
        result = cmd(argv)
        current = cmd(["tailscale", "serve", "status", "--json"])
        try:
            cfg = (
                json.loads(current.stdout)
                if current is not None and current.returncode == 0
                else "unknown"
            )
        except (ValueError, TypeError):
            cfg = "unknown"
        if cfg in (None, {}):
            config.pop("tailscale_serve")
            atomic(root / "runtime.json", json.dumps(config) + "\n")
        elif serve_matches(cfg, dns, port):
            config["tailscale_serve"] = {"dns": dns, "port": port}
            atomic(root / "runtime.json", json.dumps(config) + "\n")
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        else:
            print(
                "Serve setup unverified; pending recovery metadata retained. Inspect tailscale serve status before retrying.",
                file=sys.stderr,
            )
        return (
            result
            if result is not None and result.returncode
            else subprocess.CompletedProcess(argv, 1, b"", b"")
        )


def clear_owned_serve(config):
    with operation_lock():
        owned = config.get("tailscale_serve")
        if not owned:
            return True
        if not isinstance(owned, dict) or not isinstance(owned.get("dns"), str):
            raise ValueError("invalid managed Tailscale metadata")
        port = port_value(owned.get("port"))
        if not shutil.which("tailscale"):
            print(
                "Managed Serve mapping retained: tailscale unavailable; inspect tailscale serve status.",
                file=sys.stderr,
            )
            return False
        current = cmd(["tailscale", "serve", "status", "--json"])
        try:
            if current is None or current.returncode:
                raise ValueError("Serve state unavailable")
            cfg = json.loads(current.stdout)
        except (ValueError, TypeError):
            print(
                "Cannot inspect managed Serve mapping; inspect tailscale serve status.",
                file=sys.stderr,
            )
            return False
        if cfg in (None, {}):
            return True
        if owned.get("pending"):
            print(
                "Pending Serve setup retained; inspect tailscale serve status and resolve it before purging.",
                file=sys.stderr,
            )
            return False
        if not serve_matches(cfg, owned["dns"], port):
            print(
                "Shared or changed Serve config retained; inspect tailscale serve status.",
                file=sys.stderr,
            )
            return False
        removed = cmd(["tailscale", "serve", "--https=443", "off"])
        if removed is None or removed.returncode:
            print(
                "Cannot remove managed Serve mapping; run: tailscale serve --https=443 off",
                file=sys.stderr,
            )
            return False
        current = cmd(["tailscale", "serve", "status", "--json"])
        try:
            cleared = (
                current is not None
                and current.returncode == 0
                and json.loads(current.stdout) in (None, {})
            )
        except (ValueError, TypeError):
            cleared = False
        if not cleared:
            print("Serve removal unverified; inspect tailscale serve status.", file=sys.stderr)
        return cleared


def remove_installation(root, purge=False):
    with operation_lock():
        root = Path(root).absolute()
        home = Path.home()
        config_home = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
        unit = config_home / "systemd/user/opencode-serve.service"
        if (
            not root.exists()
            and not root.is_symlink()
            and not unit.exists()
            and not unit.is_symlink()
        ):
            print("opencode-serve is not installed; nothing to remove.")
            return
        if root.is_symlink():
            raise ValueError("refusing a symlink install root")
        protected = {home.resolve(), config_home.resolve(), (home / ".local").resolve()}
        protected.update(
            Path(os.environ.get(name, home / default)).resolve()
            for name, default in (
                ("XDG_DATA_HOME", ".local/share"),
                ("XDG_STATE_HOME", ".local/state"),
                ("XDG_CACHE_HOME", ".cache"),
            )
        )
        if root.resolve() in protected or root.resolve() in home.resolve().parents:
            raise ValueError("refusing removal from a home or shared config/data directory")
        config = runtime_config(root)
        if root.exists() and not recognized_install(root, config):
            raise ValueError("directory is not a recognized opencode-serve installation")
        with contextlib.ExitStack() as stack:
            if root.exists():
                stack.enter_context(file_lock(root / ".install.lock"))
                config = runtime_config(root)
            if unit.exists() or unit.is_symlink():
                exec_lines = [
                    line for line in unit.read_text().splitlines() if line.startswith("ExecStart=")
                ]
                suffix = " " + " ".join(
                    quote(x) for x in (root / "serve.py", "run", "--root", root)
                )
                if len(exec_lines) != 1 or not exec_lines[0].endswith(suffix):
                    raise ValueError("user unit belongs to another installation; nothing removed")
                stopped = cmd(["systemctl", "--user", "disable", "--now", "opencode-serve.service"])
                active = cmd(["systemctl", "--user", "is-active", "opencode-serve.service"])
                if (
                    stopped is None
                    or stopped.returncode
                    or active is None
                    or active.returncode not in (3, 4)
                ):
                    raise ValueError("service stop unverified; no files removed")
            elif root.exists() and not config.get("removed"):
                active = cmd(["systemctl", "--user", "is-active", "opencode-serve.service"])
                if active is None or active.returncode not in (3, 4):
                    raise ValueError("unit is missing and shutdown is unverified; no files removed")
            if config:
                # A failed/shared Serve cleanup never removes other apps' mappings.
                cleared = clear_owned_serve(config)
                if purge and not cleared:
                    raise ValueError("purge refused until managed Tailscale cleanup is verified")
                config["removed"] = True
                if cleared:
                    config.pop("tailscale_serve", None)
                atomic(root / "runtime.json", json.dumps(config) + "\n")
            unit.unlink(missing_ok=True)
            reloaded = cmd(["systemctl", "--user", "daemon-reload"])
            if reloaded is None or reloaded.returncode:
                raise ValueError("unit removed; run systemctl --user daemon-reload before retrying")
            if root.exists():
                if purge:
                    shutil.rmtree(root)
                else:
                    (root / "serve.py").unlink(missing_ok=True)
            print(
                "Uninstalled opencode-serve."
                + (
                    " Installation data deleted."
                    if purge
                    else f" Data and credentials retained at {root}."
                )
            )


def remove_main(argv):
    parser = argparse.ArgumentParser(
        description="Stop and uninstall the server; retain data unless --purge."
    )
    parser.add_argument("--root", type=Path, default=default_root())
    parser.add_argument(
        "--purge", action="store_true", help="delete the complete installation directory"
    )
    args = parser.parse_args(argv)
    try:
        remove_installation(args.root, args.purge)
    except (OSError, ValueError):
        print(
            "opencode-serve: removal failed; check the install root, unit ownership, and systemctl --user status opencode-serve.service",
            file=sys.stderr,
        )
        return 1
    return 0


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
        print("Guided setup. Writes the install root, user unit, and a per-user operation lock.")
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
        previous = runtime_config(root)
        use_ts = ask_yes(
            "Use Tailscale Serve? (yes: loopback + tailnet HTTPS; no: external reverse proxy)",
            previous.get("use_ts", True),
        )
        hostname = "127.0.0.1"
        if not use_ts:
            default_host = (
                previous.get("hostname") if previous.get("use_ts", True) is False else None
            )
            while True:
                try:
                    hostname = network_settings(
                        False, ask("Bind IP for proxy access", default_host or "0.0.0.0")
                    )
                    break
                except ValueError:
                    print("Enter a valid IPv4 or IPv6 bind address.")
            print("Use HTTPS at your proxy and restrict backend access to the proxy's addresses.")
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
        password = ask_password()
        names = ", ".join(sorted(refs)) if refs else ("(kept)" if keep else "(none)")
        print(
            f"Root: {root}\nBind: {hostname}:{port}\nTailscale: {use_ts}\nBinary: {binary}\nRefs: {ref_shown}\nSecrets: {names}"
        )
        if not ask_yes("Write files?", True):
            print("Cancelled; nothing changed.")
            return 1
        with operation_lock():
            try:
                if binary and not Path(binary).exists() and not shutil.which(binary):
                    print("opencode binary not found; check the path.", file=sys.stderr)
                    return 1
                do_install(
                    root,
                    refs,
                    binary,
                    port,
                    keep_env=keep,
                    password=password,
                    use_ts=use_ts,
                    hostname=hostname,
                )
            except RootRefusal as error:
                # Message is built only from escaped root/entry names; no exception text.
                print(f"opencode-serve: {error}", file=sys.stderr)
                return 1
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
                    if not was_active and port_listening(port, hostname=hostname):
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
                            active = cmd(
                                ["systemctl", "--user", "is-active", "opencode-serve.service"]
                            )
                            password = service_password(root)
                            if (
                                active is not None
                                and active.returncode == 0
                                and password
                                and verify_service(port, password, hostname=hostname)
                            ):
                                running = verified = True
                            else:
                                print(
                                    "Service start reported success but the unit is not active or"
                                    f" {API_INFO} did not reject bad auth and accept the real password"
                                    f" on {probe_hostname(hostname)}:{port}; skipping exposure."
                                    " Run: systemctl --user status opencode-serve.service",
                                    file=sys.stderr,
                                )
            else:
                active = cmd(["systemctl", "--user", "is-active", "opencode-serve.service"])
                running = active is not None and active.returncode == 0
                if running:
                    password = service_password(root)
                    old_hostname = network_settings(
                        previous.get("use_ts", True), previous.get("hostname")
                    )
                    if old_hostname != hostname or previous.get("port", DEFAULT_PORT) != port:
                        print(
                            "Networking changed; restart the service before using the new listener."
                        )
                    else:
                        verified = bool(
                            password and verify_service(port, password, hostname=hostname)
                        )
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
            if use_ts and verified:
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
                                exposed = create_serve(root, dns, port)
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
                                owned = runtime_config(root).get("tailscale_serve", {})
                                if (
                                    owned.get("pending")
                                    and owned.get("dns") == dns
                                    and owned.get("port") == port
                                ):
                                    record_serve(root, dns, port)
                                print(f"Already serving at https://{dns}; nothing changed.")
                            else:
                                print(
                                    "Existing Serve config found; refusing to overwrite it."
                                    " Inspect with: tailscale serve status (never reset without backup)",
                                    file=sys.stderr,
                                )
                                # ponytail: no raw-config merge; add only for an explicit --force path.
            elif not use_ts:
                upstream = "<server LAN IP>" if hostname in ("0.0.0.0", "::") else hostname
                if ":" in upstream:
                    upstream = f"[{upstream}]"
                print(f"Tailscale disabled. Proxy upstream: http://{upstream}:{port}")
            else:
                if running:
                    print("Service running but endpoint not verified; skipping Tailscale exposure.")
                else:
                    print("Service not running; skipping Tailscale exposure.")
            print("Password stays private. As user `opencode`, get it with:")
            print(
                f"  python3 -c \"import json;print(json.load(open('{root}/config/opencode/service.json'))['password'])\""
            )
            if use_ts:
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
    hostname = network_settings(config.get("use_ts", True), config.get("hostname"))
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
                    hostname,
                    "--port",
                    str(port),
                ],
                env,
            )
        finally:
            os.dup2(saved_err, 2)
            os.close(saved_err)


def main():
    if sys.argv[1:2] == ["completion"]:
        return completion_main(sys.argv[2:])
    if sys.argv[1:2] == ["remove"]:
        return remove_main(sys.argv[2:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["install", "run", "completion", "remove"])
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--secrets", type=Path, default=None)
    parser.add_argument("--opencode", default=None)
    parser.add_argument(
        "--use-ts",
        choices=("true", "false"),
        default=None,
        help="install networking: true = Tailscale/loopback, false = external proxy",
    )
    parser.add_argument(
        "--hostname",
        default=None,
        help="install bind IP (default: 127.0.0.1 with Tailscale, 0.0.0.0 without)",
    )
    parser.add_argument(
        "--password",
        "--pasword",
        default=None,
        help="install-time server password; replaces an existing password (default: keep or generate)",
    )
    parser.add_argument(
        "--port",
        type=port_value,
        default=None,
        help="install-time listen port, 1..65535 (run uses runtime.json)",
    )
    args = parser.parse_args()
    if args.action in ("completion", "remove"):
        parser.error("use completion or remove as the first argument")
    args.use_ts = None if args.use_ts is None else args.use_ts == "true"
    if args.action == "run" and (args.use_ts is not None or args.hostname is not None):
        parser.error("--use-ts and --hostname are install-time options; run uses runtime.json")
    if args.action == "run" and args.password is not None:
        parser.error("--password is only supported for install")
    if args.action == "install" and any(
        getattr(args, name) is not None
        for name in ("root", "secrets", "opencode", "port", "password", "use_ts", "hostname")
    ):
        try:
            os.umask(0o077)
            install(args)
        except RootRefusal as error:
            # Message is built only from escaped root/entry names; no exception text.
            print(f"opencode-serve: {error}", file=sys.stderr)
            return 1
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
