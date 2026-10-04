# opencode-serve

Repeatable Linux systemd-user foreground `opencode serve --service` on
127.0.0.1:4096 by default, with optional Tailscale Serve or a listener for an
external reverse proxy. Python
stdlib only (`serve.py`, `test_serve.py`). Nothing runs except on explicit
execution.

## Shell completions

Install completions for the shell named by `$SHELL`:

```sh
./serve.py completion --install --force
```

Use `completion bash`, `completion zsh`, or `completion fish` to select a shell
explicitly. Add `--install` to install for that shell; without it, the command
prints the completion script. `--force` replaces an existing completion file;
without it, installation refuses to overwrite one.

Completions work with direct invocation (`./serve.py`, `/path/to/serve.py`, or
`serve.py` on your `PATH`). They complete actions, flags, shell names, and file
or directory arguments. Password and port values have no suggestions. Invoking
the script through `python3 serve.py` uses Python's shell completion instead.

Bash and Zsh scripts are installed under
`${XDG_DATA_HOME:-~/.local/share}/opencode-serve/completions/`, with a managed
source block added to `~/.bashrc` or `${ZDOTDIR:-~}/.zshrc`. Existing shell config
text and file mode are preserved, and reinstalling updates that block without
duplicating it. Config files are replaced atomically; hardlinks and extended
metadata are not retained.
Fish loads `${XDG_CONFIG_HOME:-~/.config}/fish/conf.d/opencode-serve.fish` at
startup, which also enables completion for `./serve.py` outside `PATH`, and
needs no edits to `config.fish`. Open a new shell after installation. The command installs
only user completions; it does not install or start the server.

## Auth notes (opencode v2.0.22)

- `serve --service` reads the password from `<config-home>/opencode/service.json`
  only. Missing/invalid file → `run` refuses to start.
- Browser sign-in: user `opencode` + that password.
- Serve stdout/stderr is suppressed in `run()` because the server can print
  password material; journald stays clean by design. Debug via exit codes and a
  temp-HOME reproduction, not by re-enabling output.
- Password generated once (`secrets.token_urlsafe(32)`) unless you supply one,
  preserved on reinstall unless you explicitly set a replacement. Fetched
  secret values and passwords are stored verbatim JSON 0600, never
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
   - **Use Tailscale Serve?** — yes keeps the listener on `127.0.0.1` and
     configures tailnet HTTPS after a successful service/authentication check.
     No skips Tailscale and asks for a **Bind IP** (default `0.0.0.0`, or a
     specific private IPv4/IPv6 address for a proxy on another machine).
     Reinstallation defaults to the saved networking choice and bind address.
   - **opencode binary** (default `~/.opencode/bin/opencode`).
   - **Refs file** — 1Password `op://` references for provider keys
     (`{"ENV_NAME": "op://vault/item/field", ...}`). Empty answer means no
     refs; no file is needed when you supply no secrets. Relative paths
     resolve against your current directory. An existing install keeps its
     saved secret values when you answer empty (nothing is erased silently);
     naming a file replaces the set.
   - **Server password** — hidden input with confirmation. Enter keeps the
     existing password or generates one for a new install. Supplying a password
     replaces the existing password; spaces and special characters are preserved.
   - A summary before anything is written; Ctrl-C, EOF, or `n` cancels with
     zero changes. Cancel/EOF are safe at every prompt.
3. If you accept, it writes files, then offers: `systemctl --user
daemon-reload` + `enable --now` (or an explicit restart on reinstall),
   optional `loginctl enable-linger` (run yourself if it needs permission; the
   installer never uses sudo), and Tailscale Serve exposure when selected
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

`python3 serve.py install --root DIR --secrets FILE --opencode PATH --port PORT --password PASSWORD --use-ts true`
(any subset; passing any flag selects this mode). It only installs, exactly as
before:

`--password` (alias `--pasword`) sets or replaces the server password. It must
be nonempty and contain no NUL characters. Omit it to keep the existing password
or generate one for a new install. Command-line passwords can appear in shell
history and process listings; use the guided hidden prompt when entering one
manually. The option is only accepted for `install`.

`--use-ts {true|false}` selects the networking mode. True uses `127.0.0.1`;
false defaults to `0.0.0.0`. With false, `--hostname IP` selects a specific
IPv4/IPv6 bind address. Omitted networking options retain the previous settings;
new and legacy installs default to Tailscale/loopback. Explicit-flag installation
does not start the unit or create a Serve mapping; start the unit and configure
Tailscale Serve manually when selected. It can remove an existing managed
mapping when networking changes. Saved binary and port settings are also kept
when their flags are omitted. `run` reads the saved settings and does not accept
these networking flags.

1. Refs file `{"ENV_NAME": "op://vault/item/field", ...}`. Reserved names:
   `HOME PATH OP_SERVICE_ACCOUNT_TOKEN OPENCODE_DB OPENCODE_PASSWORD
OPENCODE_CONFIG OPENCODE_CONFIG_DIR`, plus anything starting `XDG_`,
   `OPENCODE_SERVE_`, or `_`. Relative `--secrets` paths resolve against the
   current directory, independent of `--root`.
2. Writes `<root>/` (`config/ data/ state/ cache/ runtime.json serve.py`,
   password `config/opencode/service.json`) and the unit
   `~/.config/systemd/user/opencode-serve.service`. Unit names are fixed;
   rerun install after moving root. First installation requires an empty,
   dedicated root; unrelated nonempty directories are refused. Operations on
   the shared unit and Serve configuration are serialized by a private lock at
   `~/.local/state/opencode-serve/operation.lock`. Fails closed: any fetch/validation error
   aborts before replacing working files. Secret values already fetched in
   `runtime.json` are replaced only when a refs file is given.
3. `systemctl --user daemon-reload && systemctl --user enable --now opencode-serve.service`
4. Password retrieval as above. To rotate: stop the unit, rerun the installer
   with `--password PASSWORD` (or enter a new password in guided setup), then
   start the unit. To generate a fresh random password, stop the unit, remove
   the password file, rerun the installer with your original options, then start
   the unit.
5. Linger (else unit dies at logout): `loginctl enable-linger $USER`.

## External proxy (Traefik, F5, or similar)

```sh
./serve.py install --use-ts false --hostname 192.168.1.20 --port 4096
systemctl --user daemon-reload
systemctl --user enable --now opencode-serve.service
# On an already running installation, apply changed settings with:
systemctl --user restart opencode-serve.service
```

Use an IP assigned to the OpenCode machine. Configure the remote proxy's
upstream as `http://192.168.1.20:4096` (IPv6 example: `http://[fd00::20]:4096`).
Omit `--hostname` to listen on all IPv4 interfaces. Terminate client HTTPS at
the proxy, preserve the Authorization header and streaming connections, and
restrict backend access to the proxy with your firewall. OpenCode's Basic
authentication still uses user `opencode` and the saved server password.
There is no Tailscale dependency for a new installation in this mode.

Changing the bind address or networking mode requires restarting the service.
If guided setup skips that restart, it does not expose changed settings through
Tailscale. Disabling Tailscale or changing the port removes an exact Serve
mapping previously created by guided setup; if that mapping has become shared
or cannot be inspected, the networking change fails so you can resolve it
manually. Untracked/manual Serve routes are preserved.

## Remove / uninstall

```sh
./serve.py remove --root ~/.local/share/opencode-serve
./serve.py remove --root ~/.local/share/opencode-serve --purge
```

The root defaults to `~/.local/share/opencode-serve`. Removal verifies unit
ownership, disables/stops the user service, confirms it is inactive, removes
the unit, and reloads systemd. It then removes the copied launcher while keeping
the database, `runtime.json` (including saved provider credentials), password,
and remaining config/state/cache files. Reinstall with your original options
to use the retained data. `--purge` deletes the complete installation directory,
including credentials and database; it also works after a prior removal.
Purge refuses to discard a managed or pending Serve recovery record until
cleanup is verified. Resolve the reported Serve state and retry removal.
Home/shared config directories, unrecognized roots, and root symlinks are
refused. Bridged host-config symlinks are never followed during deletion.

Removal disables a tracked Tailscale Serve mapping only when the entire current
Serve config matches that installation's endpoint. Shared, modified, manual,
or inaccessible Serve config is retained with a notice when tracked; inspect
`tailscale serve status` and clean up manually if needed. Removal does not
uninstall the OpenCode/Tailscale binaries, revoke user linger, remove shell
completions, or delete host OpenCode settings. The shared operation lock remains
so other installation roots keep using the same coordinator.

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

- `serve.py` — installer + launcher (`install` guided or flagged / `run`),
  uninstaller (`remove`, optional `--purge`), plus
  shell completion generation and installation (`completion`).
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
  Networking checks also use a temporary authenticated HTTP listener on
  `127.0.0.2` to verify probes of a selected bind address.
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

## Linting & Formatting

This project uses a self-contained lint/format bundle rooted in `scripts/style.sh`.
Run it directly, via the package-manager aliases below, or wire it into CI.

### One-command bootstrap

If you cloned this repo fresh and `scripts/style.sh` is missing (it ships
managed), re-run `formatters --sync` from opencode to reinstall the bundle.

### Daily commands

```bash
./scripts/style.sh lint                  # full lint pass (all detected languages)
./scripts/style.sh lint --fix            # auto-fix what is auto-fixable
./scripts/style.sh lint --modified       # staged + unstaged + untracked
./scripts/style.sh lint --staged         # only files staged in the git index
./scripts/style.sh lint --unstaged       # only unstaged + untracked changes
./scripts/style.sh lint --fix --modified # fast pre-push loop
./scripts/style.sh format                # format everything
./scripts/style.sh format --modified     # format modified files
./scripts/style.sh format --staged       # format only staged files
./scripts/style.sh format --unstaged     # format only unstaged + untracked
```

### Per-language tools

- **Python**: `ruff` (lint + import sort) + `black` (format). Config: `[tool.ruff]` and `[tool.black]` in `pyproject.toml`.

- **Docs**: `markdownlint` + `prettier` (`.markdownlint.json`, `.prettierrc`)
  for Markdown/YAML. In docs-only repos, Prettier also owns JSON/JSONC.

- **Shell**: `shellcheck --severity=warning` on `*.sh`.

### CI

To wire lint into CI, run `formatters --ci` (installs both `lint.yml` and
`lint-autofix.yml`) or pair it with `--no-autofix` to skip the autofix workflow.

### Pre-commit hook (optional)

A pre-commit hook is installed. It runs `./scripts/style.sh lint --staged --fix` and
`./scripts/style.sh format --staged` before every commit. To bypass once:
`git commit --no-verify`.

### Advanced

- **Upgrade the bundle**: re-run `formatters --sync` from opencode. This prunes
  stale managed files and copies the latest scripts.
- **Ignore paths**: add entries to `.prettierignore`, `.markdownlintignore`,
  `.gitignore`, or tool-native ignore keys (`ruff exclude`, `biome files.ignore`,
  `.golangci.yml issues.exclude-rules`).
- **Modified-only mode** reads `git diff --name-only HEAD` — untracked files are
  included when `scripts/lib/modified-files.sh` sees them with
  `git status --porcelain`.
