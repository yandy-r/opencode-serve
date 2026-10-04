#!/usr/bin/env python3
"""Install or launch isolated OpenCode foreground server. Python stdlib only."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile


RESERVED = {"HOME", "PATH", "OP_SERVICE_ACCOUNT_TOKEN", "OPENCODE_DB",
            "OPENCODE_PASSWORD", "OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR"}


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
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'


def port_value(raw):
    try:
        port = int(raw)
    except (TypeError, ValueError):
        raise ValueError("port must be 1..65535")
    if not 1 <= port <= 65535:
        raise ValueError("port must be 1..65535")
    return port


def executable(name):
    path = shutil.which(name)
    if not path:
        raise ValueError("required executable missing")
    return str(Path(path).absolute())


def install(args):
    root = args.root.absolute()
    home = Path.home()
    host_config = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "opencode"
    unit_dir = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "systemd/user"
    refs = json.loads(args.secrets.read_text())
    if not isinstance(refs, dict):
        raise ValueError("references must be a JSON object")
    for name, ref in refs.items():
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
                or name in RESERVED or name.startswith("XDG_") or name.startswith("OPENCODE_SERVE_")
                or name.startswith("_") or not isinstance(ref, str) or not ref.startswith("op://")
                or "\0" in ref):
            raise ValueError("invalid or reserved environment reference")
    binary = executable(args.opencode)
    python = executable(sys.executable)
    op = executable("op") if refs else None
    private_dir(root)
    # One writer; fetch all values before replacing any working runtime files.
    with (root / ".install.lock").open("w") as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        fetched = {}
        for name, ref in refs.items():
            result = subprocess.run([op, "read", "--no-newline", ref],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60)
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
            if not isinstance(saved, dict) or not isinstance(saved.get("password"), str) or not saved["password"]:
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
        config = {"binary": binary, "path": os.environ.get("PATH", os.defpath),
                  "home": str(home), "port": args.port, "env": fetched}
        atomic(root / "runtime.json", json.dumps(config) + "\n")
        atomic(root / "serve.py", Path(__file__).read_text())
        template = Path(__file__).with_name("opencode-serve.service.in").read_text()
        unit = template.replace("@EXEC@", " ".join(quote(x) for x in
                                [python, root / "serve.py", "run", "--root", root]))
        unit_dir.mkdir(parents=True, exist_ok=True)
        atomic(unit_dir / "opencode-serve.service", unit)
    print("Installed opencode-serve.service; not started. See README for manual activation.")


def run(root):
    config = json.loads((root / "runtime.json").read_text())
    port = port_value(config.get("port", 4096))
    try:
        saved = json.loads((root / "config/opencode/service.json").read_text())
    except (OSError, ValueError):
        raise ValueError("server password missing; refusing unauthenticated startup")
    password = saved.get("password") if isinstance(saved, dict) else None
    if not isinstance(password, str) or not password:
        raise ValueError("server password missing; refusing unauthenticated startup")
    env = {"HOME": config["home"], "PATH": config["path"], "LANG": "C.UTF-8",
           "XDG_CONFIG_HOME": str(root / "config"), "XDG_DATA_HOME": str(root / "data"),
           "XDG_STATE_HOME": str(root / "state"), "XDG_CACHE_HOME": str(root / "cache"),
           "OPENCODE_DB": str(root / "data/server.db")}
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
            os.execve(config["binary"], [config["binary"], "serve", "--service", "--hostname",
                                         "127.0.0.1", "--port", str(port)], env)
        finally:
            os.dup2(saved_err, 2)
            os.close(saved_err)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["install", "run"])
    parser.add_argument("--root", type=Path, default=Path.home() / ".local/share/opencode-serve")
    parser.add_argument("--secrets", type=Path, default=Path(".opencode-serve.local/references.json"))
    parser.add_argument("--opencode", default=str(Path.home() / ".opencode/bin/opencode"))
    parser.add_argument("--port", type=port_value, default=4096,
                        help="install-time listen port, 1..65535 (run uses runtime.json)")
    args = parser.parse_args()
    try:
        os.umask(0o077)
        if args.action == "install":
            install(args)
        else:
            run(args.root.absolute())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        # Do not print exception strings: JSON / subprocess errors may contain secrets.
        print("opencode-serve: failed; check inputs, private-file permissions and CLI availability", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
