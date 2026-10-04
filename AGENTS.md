# opencode-serve — Agent Rules

Canonical rules for this repository. `.cursor/rules/project.mdc` and
`.github/copilot-instructions.md` mirror this content; keep them in sync.

## Project

Python stdlib-only installer/launcher for an isolated OpenCode foreground
server (`opencode serve --service`) as a systemd user unit: loopback with
optional Tailscale Serve, or a configurable bind IP for an external proxy.
Supports uninstall with data retention or explicit purge, and 1Password
secret refs. Two source
files: `serve.py`, `test_serve.py`. No runtime dependencies. No build step.

## Commands

- Test: `python3 test_serve.py` — self-asserting; must end `result: ALL PASS`.
- Lint/format: `./scripts/style.sh lint` / `./scripts/style.sh format`
  (ruff + black; docs linters run in CI via npx).
- Install git hooks: `bash scripts/install-lefthook.sh`.

## MUST

- Stdlib only. Never add a runtime dependency.
- Never re-enable serve stdout/stderr output — it can print password
  material. Debug via exit codes and a temp-HOME reproduction.
- The installer never runs `sudo`; it only prints recovery commands.
- Preserve percent-escaping (`%` → `%%`) when writing paths into systemd
  unit files; `test_serve.py` asserts this.
- Secrets are stored verbatim as JSON `0600`, never interpolated.
  `OP_SERVICE_ACCOUNT_TOKEN` is install-time only and stripped from the
  launched server's environment (`RESERVED` in `serve.py`).
- Conventional Commits for all changes (see `.gitmessage`; local
  `commit.template` is configured).

## Labels

`bug`, `enhancement`, `docs`, `chore` — see `.github/labels.md`.
