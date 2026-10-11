# Stable surface

This document defines what downstream tools and agents may rely on, and the two
credential levels that guard it. Anything not listed here is an internal detail and
may change between minor releases.

## Two credential levels

crewhall has exactly two ways to talk to the daemon:

- **Agent operations** — token-authenticated per agent, peer-to-peer. An agent (or
  a process that descends from an agent's terminal) may *only* call these. The
  acting identity is derived from a token the daemon issued; it is never taken from
  a `from`/`sender` field the caller chooses.
- **Control plane** — everything else. It requires the **control token** (a `0600`
  file under the state directory, read automatically by the CLI, the Web UI and the
  in-process UI) **and** a caller that does not descend from an agent session. A
  prompt-injected agent can read the control-token file but is still refused by the
  process-tree check.

See [SECURITY.md](../SECURITY.md) for the threat model.

## CLI commands (top level)

```
crewhall list|create|send|sendline|key|enter|capture|read|read-until|resize|
          interrupt|kill|status|events|attach            # raw sessions
crewhall terminal new|ls|send|key|capture|close|attach   # web terminals
crewhall office setup|status|stop|restart|list|open|read|insert|command|close|
          users|chat|say|comments|comment|save           # OnlyOffice
crewhall host list|add|remove|test
crewhall agent identity|list|create|send|capture|state|wait|stop|new-session
crewhall message send|request|reply|requests
crewhall team create|up|set-workspace|list|info|remove|add-member|remove-member
crewhall daemon|demo|web|token|terminal-token|frontends|service
crewhall update|selftest|doctor|clean|bundle|adapter|ui
```

Added commands are MINOR; removing or renaming one is MAJOR (see
[RELEASING.md](../RELEASING.md)).

## Daemon operations (UNIX socket)

Operations are grouped by the credential they need:

- **Agent ops** (`team_send`, `request_create`, `request_reply`, `request_cancel`
  with an `agent`, `agent_identity`, `team_members`, `agent_new_session`,
  `team_new_session`, `agent_hook`, `agent_permission_request`,
  `conversation_publish`): token-authenticated; callable by agents.
- **Agent co-editing ops** (`office_collab_*` except `office_collab_list`, sent with
  the agent's `sender` + `token`, as `crewhall office` does inside an agent): allowed
  only for the chat the agent leads or whose crew it belongs to.
- **Control ops** (everything else: creating/killing agents and sessions, settings,
  teams, conversations, office, bundles, terminals, …): need the control token and a
  non-agent caller.

An `office_collab_*` op answers `{"disabled": true}` when OnlyOffice is off.

## MCP tools (for agents)

The stdio MCP server exposes: `whoami`, `list_teammates`, `send_message`,
`request`, `reply`, `handoff`, `new_session`, `publish_artifact`.

## Criticality and the road to 1.0

The project is **beta**. The stable surface above is frozen for the 1.x line once
the 1.0 criteria are met:

1. **Phase 0 complete**: the control-token + process-credential gate, the
   human-draft-safe input handling, and the durable FIFO delivery queue, all with
   tests.
2. **Green CI**: the full test suite passes on every supported Python (3.11–3.14).
3. **API stability**: the CLI commands, agent-token/control-token RPC surface and
   MCP tools above change only additively across two consecutive releases.
