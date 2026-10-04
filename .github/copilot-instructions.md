# GitHub Copilot / code-agent PR guidance — opencode-serve

This file is read by GitHub Copilot's coding agent when it opens or edits a
pull request. It mirrors the canonical agent rules in
[`AGENTS.md`](../AGENTS.md) — keep them in sync. The points below are the
PR-workflow essentials re-stated for agents that land here first.

## Project

Python stdlib-only installer/launcher for an isolated OpenCode foreground
server (`opencode serve --service`) as a systemd user unit: loopback with
optional Tailscale Serve, or a configurable bind IP for an external proxy.
Supports uninstall with data retention or explicit purge, and 1Password
secret refs. Two source
files: `serve.py`, `test_serve.py`. No runtime dependencies. No build step.

Commands: `python3 test_serve.py` (must end `result: ALL PASS`),
`./scripts/style.sh lint` / `./scripts/style.sh format`
(ruff + black; docs linters run in CI via npx).

Project rules: never add a runtime dependency; never re-enable serve
stdout/stderr output (password material — debug via exit codes and a
temp-HOME reproduction); the installer never runs `sudo`; preserve
percent-escaping (`%` → `%%`) in systemd unit paths; secrets stored verbatim
as JSON `0600`, never interpolated.

## PR title — Conventional Commits, enforced

PR titles are validated by
[`.github/workflows/pr-title.yml`](workflows/pr-title.yml) and the check is
required to merge. Use:

```text
<type>[optional scope]: <description>
```

- Allowed types: `feat`, `fix`, `docs`, `refactor`, `perf`, `test`, `build`,
  `ci`, `chore`, `style`. Use Conventional Commits for all changes (see
  [`.gitmessage`](../.gitmessage); local `commit.template` is configured).
- **Never open a PR with `[WIP]`, `[Draft]`, `Draft:`, `WIP:`, or
  `Initial plan` in the title.** The workflow rejects these prefixes. For
  work-in-progress, use GitHub's native **Draft PR** status instead.
- The PR title becomes the **squash-merge commit subject verbatim** and
  appears in `git log`. Write it exactly as it should read.

### Fixing a rejected title

Edit the existing PR in place — **do not open a replacement PR**:

```text
gh pr edit <number> --title "<new-title>"
```

Or use the GitHub UI. The workflow re-runs automatically on edit.

> Safety net: [`workflows/pr-title-autofix.yml`](workflows/pr-title-autofix.yml)
> runs in parallel and will (1) strip `[WIP]` / `[Draft]` / `Draft:` / `WIP:` /
> `Initial plan` prefixes from the PR title server-side, then convert the PR
> to draft; and (2) normalize a leading Conventional Commit type that was
> written without a colon or with capitalization — e.g. `Refactor X` becomes
> `refactor: X`, `Feat(auth): X` becomes `feat(auth): X`. It exists because
> some coding-agent runtime tokens lack `pull_requests:write` and cannot edit
> their own title after the fact. **Do not rely on it** — produce a clean
> Conventional Commit title up-front; the autofix workflow is a last-resort
> guardrail, not an excuse to bypass these rules.

### Work-in-progress

Use GitHub's native **Draft PR** status (the "Draft" toggle when opening, or
`gh pr ready --undo`). Never encode work-in-progress by prefixing the title
with `[WIP]` or `Draft:` — both are rejected by the title workflow.

## PR body

- Follow [`pull_request_template.md`](pull_request_template.md). Fill every
  checklist item honestly; don't leave stubs.
- Always link the issue: `Closes #…` for standalone issues.
- Label PRs using the repo's taxonomy only — `bug`, `enhancement`, `docs`,
  `chore` (see [`labels.md`](labels.md)). **Never invent ad-hoc labels.**

## Scope

- Keep PRs **small and focused** — one logical change per PR. Omnibus PRs
  (multiple unrelated features, refactors, and bug fixes in one branch) are
  harder to review and harder to revert. Split into smaller PRs with clear
  dependencies when the scope grows.

## Commits inside the PR

This repository's convention is **squash-only merges**, so the PR title is
what lands on the default branch. Interior commits may be informal during
development; the final PR title is the contract.

## Security

- **Never** commit `.env`, `.env.encrypted`, tokens, API keys, service
  credentials, or any secret material.
- Configuration must come from environment variables or a secret-management
  system — never hard-coded.
- If you think you may have committed a secret, **stop and tell a
  maintainer** before opening a PR; rotate the credential and force-push
  the branch only after coordinating.

## Everything else

For the full rules (`MUST` list, test/lint commands, labels), read
[`AGENTS.md`](../AGENTS.md) and [`README.md`](../README.md) (docs beyond
that live only in [`docs/`](../docs/)).
