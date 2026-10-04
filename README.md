# opencode-serve

Repeatable Linux systemd-user foreground `opencode serve --service` on
127.0.0.1:4096 (customizable), plus manual Tailscale Serve. Python stdlib only
(`serve.py`, `test_serve.py`). Nothing runs except on explicit execution.

## Auth notes (opencode v2.0.22)

- `serve --service` reads the password from `<config-home>/opencode/service.json`
  only. Missing/invalid file → `run` refuses to start.
- Browser sign-in: user `opencode` + that password.
- Serve stdout/stderr is suppressed in `run()` because the server can print
  password material; journald stays clean by design. Debug via exit codes and a
  temp-HOME reproduction, not by re-enabling output.
- Fixed password generated once (`secrets.token_urlsafe(32)`), preserved on
  reinstall. Fetched secret values are stored verbatim JSON 0600, never
  interpolated.

## Install (manual — nothing auto-runs)

1. `mkdir -m 700 -p .opencode-serve.local && cp references.example.json .opencode-serve.local/references.json`
   and edit: `{"ENV_NAME": "op://vault/item/field", ...}`.
   Reserved names: `HOME PATH OP_SERVICE_ACCOUNT_TOKEN OPENCODE_DB
   OPENCODE_PASSWORD OPENCODE_CONFIG OPENCODE_CONFIG_DIR`, plus anything
   starting `XDG_`, `OPENCODE_SERVE_`, or `_`.
2. `op signin` (or service-account env) so `op read` works. Never put
   `OP_SERVICE_ACCOUNT_TOKEN` in the references file; it is install-time only
   and is explicitly stripped from the launched server's environment.
3. `python3 serve.py install` (flags: `--root DIR --secrets FILE --opencode PATH
   --port PORT`). Custom port: `--port 5123` (1..65535, default 4096); the port
   is persisted in `<root>/runtime.json` and used by `run`. Writes only
   `<root>/` (`config/ data/ state/ cache/ runtime.json serve.py`, password
   `config/opencode/service.json`) and the unit
   `~/.config/systemd/user/opencode-serve.service`. Unit names are fixed;
   rerun install after moving root. Fails closed: any fetch/validation error
   aborts before replacing working files.
4. `systemctl --user daemon-reload && systemctl --user enable --now opencode-serve.service`
5. Password: `python3 -c "import json;print(json.load(open('$HOME/.local/share/opencode-serve/config/opencode/service.json'))['password'])"`
   then browser sign-in as user `opencode`. To rotate: stop the unit, remove
   this password file, rerun the installer with your original options, then
   start the unit. A missing password file prevents startup.
6. Linger (else unit dies at logout): `loginctl enable-linger $USER`
   (user action, installer never touches it).

## Tailscale Serve (manual — inspect first, never overwrite blindly)

Prerequisite: HTTPS enabled on the tailnet (Tailscale admin console → DNS →
Enable HTTPS), else `tailscale serve` cannot issue a cert. Clients connect over
**HTTPS on the tailnet** (e.g. `https://<machine>.<tailnet>.ts.net`); Tailscale
Serve terminates TLS and forwards to the plain-HTTP loopback listener.

```sh
tailscale status --json | python3 -c "import json,sys; print(json.load(sys.stdin)['Self']['DNSName'].rstrip('.'))"
tailscale serve status        # READ existing config; do not `reset`
tailscale serve --bg http://127.0.0.1:4096   # use your --port if not default
```

Warnings: `--bg` on an occupied path remaps it; never `tailscale serve reset`
without backup. Anyone with tailnet access + password reaches full OpenCode.
Keep ACLs tight, rotate often.

## Layout

- `serve.py` — installer + launcher (`install` / `run`).
- `opencode-serve.service.in` — unit template (`@EXEC@` quoted for
  spaces/`%`/`$`; `Type=simple`, `Restart=on-failure`, `UMask=0077`,
  `PrivateTmp=yes` hardening — needs real HOME/XDG/network).
- `test_serve.py` — `python3 test_serve.py`: fake `op`/`opencode`, temp
  HOME/XDG/systemd dirs; covers stable reinstall, failed-fetch keeps working
  config, special values, port validation/persistence, run argv/env isolation,
  bootstrap-token stripping, password refusal, no host writes.
- `.gitignore` ignores `.opencode-serve.local/`, `*.log`, `__pycache__/`.

## Verified limits

- Symlinks share host `~/.config/opencode/*` (except `service.json`) for
  settings only. DB isolated via `OPENCODE_DB=<root>/data/server.db`.
  Existing provider/MCP login credentials and sessions are not shared; legacy
  data-directory `auth.json` and ambient shell API keys are not imported.
  Supply provider API keys through the 1Password references file, or authenticate
  integrations separately in the remote instance where supported.
- Not validated live: reboot persistence, real `op` vault fetch, Tailscale
  network path, browser sign-in. Binary auth probed only on ephemeral ports
  with isolated HOME.
- `opencode pair` is unused here and its behavior with this setup is unverified.
