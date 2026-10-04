# opencode-serve

Repeatable Linux systemd-user foreground `opencode serve --service` on
127.0.0.1:4096 (customizable), plus manual or guided Tailscale Serve. Python
stdlib only (`serve.py`, `test_serve.py`). Nothing runs except on explicit
execution.

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

## Quick start (guided, one command)

1. `op signin` (or service-account env) so `op read` works, if you use secret
   refs. Never put `OP_SERVICE_ACCOUNT_TOKEN` in the references file; it is
   install-time only and is explicitly stripped from the launched server's
   environment.
2. Run `python3 serve.py install` with no flags in a terminal. It asks, with
   defaults shown in brackets:
   - **Install root** (default `~/.local/share/opencode-serve`). Relative
     paths resolve against your current directory, not the install root.
   - **Port** (default 4096; an existing install reuses its saved port).
   - **opencode binary** (default `~/.opencode/bin/opencode`).
   - **Refs file** — 1Password `op://` references for provider keys
     (`{"ENV_NAME": "op://vault/item/field", ...}`). Empty answer means no
     refs; no file is needed when you supply no secrets. Relative paths
     resolve against your current directory. An existing install keeps its
     saved secret values when you answer empty (nothing is erased silently);
     naming a file replaces the set.
   - A summary before anything is written; Ctrl-C, EOF, or `n` cancels with
     zero changes. Cancel/EOF are safe at every prompt.
3. If you accept, it writes files, then offers: `systemctl --user
   daemon-reload` + `enable --now` (or an explicit restart on reinstall),
   optional `loginctl enable-linger` (run yourself if it needs permission; the
   installer never uses sudo), and optional Tailscale Serve exposure
   (tailnet-only HTTPS via `tailscale serve --bg http://127.0.0.1:PORT`). If
   Serve is root-managed, a failed `serve` prints only the matching `sudo`
   recovery command (never run by the installer); `sudo tailscale set
   --operator=$USER` is shown as optional broader access.
4. The database is isolated: the server uses `<root>/data/server.db` and only
   the refs-file keys; host logins and ambient API keys are not shared.
5. Password stays private; the installer never prints it. Retrieve it with
   `python3 -c "import json;print(json.load(open('<root>/config/opencode/service.json'))['password'])"`
   then browser sign-in as user `opencode`.

Guided Tailscale Serve checks first: it reads `tailscale status --json`
(`Self.DNSName`, trailing dot stripped — no hostname guessing) and
`tailscale serve status --json`. It only adds config when Serve is empty,
treats an identical existing proxy (`<dns>:443` → `http://127.0.0.1:PORT`) as
done, and refuses to touch any other non-empty state (no reset, no overwrite
of Funnel/TCP/other mappings). It never exposes when the service failed to
start. Any tailnet user plus the password gets full coding access; keep ACLs
tight.

Without a terminal, bare `python3 serve.py install` fails fast with the flag
line instead of hanging or changing services.

## Automated path (explicit flags — noninteractive)

`python3 serve.py install --root DIR --secrets FILE --opencode PATH --port PORT`
(any subset; passing any flag selects this mode). It only installs, exactly as
before:

1. Refs file `{"ENV_NAME": "op://vault/item/field", ...}`. Reserved names:
   `HOME PATH OP_SERVICE_ACCOUNT_TOKEN OPENCODE_DB OPENCODE_PASSWORD
   OPENCODE_CONFIG OPENCODE_CONFIG_DIR`, plus anything starting `XDG_`,
   `OPENCODE_SERVE_`, or `_`. Relative `--secrets` paths resolve against the
   current directory, independent of `--root`.
2. Writes only `<root>/` (`config/ data/ state/ cache/ runtime.json serve.py`,
   password `config/opencode/service.json`) and the unit
   `~/.config/systemd/user/opencode-serve.service`. Unit names are fixed;
   rerun install after moving root. Fails closed: any fetch/validation error
   aborts before replacing working files. Secret values already fetched in
   `runtime.json` are replaced only when a refs file is given.
3. `systemctl --user daemon-reload && systemctl --user enable --now opencode-serve.service`
4. Password retrieval as above. To rotate: stop the unit, remove the password
   file, rerun the installer with your original options, then start the unit.
5. Linger (else unit dies at logout): `loginctl enable-linger $USER`.

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

- `serve.py` — installer + launcher (`install` guided or flagged / `run`).
- `opencode-serve.service.in` — unit template (`@EXEC@` quoted for
  spaces/`%`/`$`; `Type=simple`, `Restart=on-failure`, `UMask=0077`,
  `PrivateTmp=yes` hardening — needs real HOME/XDG/network).
- `test_serve.py` — `python3 test_serve.py`: fake `op`/`opencode`, temp
  HOME/XDG/systemd dirs; covers stable reinstall, failed-fetch keeps working
  config, special values, port validation/persistence, run argv/env isolation,
  bootstrap-token stripping, password refusal, no host writes, plus guided
  flows (success, cancel/EOF, non-TTY refuse, no/custom/relative refs, secret
  preservation, failed start skips exposure, idempotent vs conflicting Serve
  state, external failures, explicit-flag parity) using mocked
  systemctl/tailscale/loginctl only.
- `.gitignore` ignores `.opencode-serve.local/`, `*.log`, `__pycache__/`.

## Verified limits

- Symlinks share host `~/.config/opencode/*` (except `service.json`) for
  settings only. DB isolated via `OPENCODE_DB=<root>/data/server.db`.
  Existing provider/MCP login credentials and sessions are not shared; legacy
  data-directory `auth.json` and ambient shell API keys are not imported.
  Supply provider API keys through the 1Password references file, or
  authenticate integrations separately in the remote instance where supported.
- Not validated live: reboot persistence, real `op` vault fetch, Tailscale
  network path, browser sign-in. Binary auth probed only on ephemeral ports
  with isolated HOME.
- `opencode pair` is unused here and its behavior with this setup is unverified.
