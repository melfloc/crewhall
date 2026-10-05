# Security policy

## Reporting a vulnerability

Please report suspected vulnerabilities **privately**: use the repository's
"Report a vulnerability" (GitHub private advisory) or email the maintainer
listed in `pyproject.toml`. Do not open a public issue for a security bug.
Include: what you did, what happened, the version (`crewhall --version`)
and, if possible, a minimal reproduction.

We aim to acknowledge within a few days and to publish a fix and an advisory
once a patched release exists.

## Scope and threat model

crewhall runs coding-agent TUIs on **your** machine and exposes a small
control plane (a 0600 UNIX socket and an optional Web UI). The invariants we
care about:

- **Local control plane**: the daemon socket is `0600`; anything that can reach
  it can already run code as you.
- **Web UI**: authenticated (token/session) unless bound to localhost and the
  token requirement is off; Host/Origin allow-lists; strict security headers and
  CSP; per-IP login throttling; no inline scripts/styles.
- **Agents**: a new capability is **off by default** (MCP, worktrees). The
  daemon is authoritative for agent identity and Team membership; a message or
  request is never trusted from a `--from`.
- **Secrets**: access tokens and provider credentials are never written to logs,
  bundles, the audit log, activity, fixtures or error messages; sensitive values
  are shown masked in the UI.
- **Filesystem**: directory suggestions are confined to `security.fs_roots`
  (`realpath`, no `..`/symlink escape); worktree git calls are argv-only and
  confined to the state directory.
- **Audit**: privileged operations are appended to `state/audit.jsonl` (0600),
  with no secret values.

Out of scope: a host that is already compromised (root/another process running
as you), and the upstream agent CLIs themselves.

## Hardening checklist

`crewhall doctor` reports token/settings/audit file modes, local users,
writable provider binaries and permissive Codex/Claude configs. Settings →
Access lets you require the token on localhost and rotate it.
