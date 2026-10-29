# Manual tests

Some guarantees cannot be checked in the default suite because they need a real
external service or a real agent CLI. This file documents how to run them.

## E2 - OnlyOffice document chat survives a reopen

**Goal.** The co-authoring chat of a document opened in OnlyOffice must keep its
history when the document is closed and reopened, and reopening must not resend
old messages to the agent.

**Setup.** A real OnlyOffice Document Server reachable from crewhall
(`crewhall office setup`, then `crewhall doctor` shows OnlyOffice healthy) and
the live co-editing level enabled (`office.collab_enabled`).

**Steps.**
1. In the Web UI, open a chat and create/upload a `.docx` artifact.
2. Open it in the editor (the artifact panel's *Edit* action) and, in the
   document's chat panel, send "hola".
3. Confirm the message reaches the chat agent (it appears as
   `[OnlyOffice chat · user · file.docx] hola` and the agent replies).
4. Close the editor tab, then reopen the same document.
5. Send a second message "¿sigues ahí?".

**Expected.** On reopen, the editor shows the previous chat history (the messages
are still there) and the agent receives **only** the new message. Old messages are
not re-forwarded: the co-editor forwards each chat message once, and comment ids
persist across reopens so the forwarding is idempotent.

**Result / status.** To be verified against a real Document Server before the 1.0
release; record the DS version and the outcome here. If a version does not keep
the chat history, persist the chat markers the same way comments are persisted
(`office.collab` comment ids).

## E3 - A co-editor is bounded, closes on demand and leaves no Chromium behind

**Goal.** Opening a document for reading must not start a headless editor; the
agent's co-editor must be limited to `office.collab_max_editors` (1 by default),
close when the panel closes or after `office.collab_idle_s`, and leave no
`chromium --user-data-dir=.../crewhall-collab-*` process behind.

**Setup.** A healthy OnlyOffice Document Server, `office.collab_enabled=true`, and
Chromium installed (the same setup as E2).

**Steps.**
1. Open a chat artifact with **Open** (not **Live**). Run `crewhall office list`:
   it must be empty and `pgrep -af 'user-data-dir=.*crewhall-collab'` must print
   nothing.
2. Click **Live**. `crewhall office list` must show one editor with `pid`,
   `memory_kb` and `idle_s`.
3. Click **Live** on a second artifact. With the default limit, the first editor
   must be gone from the list and only the second one remains.
4. Close the editor panel (or switch chats). `crewhall office list` must be empty
   and the Chromium profile directory must no longer exist.
5. For the idle path, set `office.collab_idle_s` to 60 in a test profile, open
   **Live**, leave it untouched, and confirm it is gone after a minute.

**Expected.** At most one headless Chromium at a time; each one is closed on
demand/idle and its whole process group is reaped. A single editor can use ~1 GB
with the stock Document Server, which is exactly why the old unlimited registry
reached several GB.

## D1 - A real agent cannot read the control token

**Goal.** An agent running under crewhall must not be able to read
`$XDG_RUNTIME_DIR/crewhall/control-token`.

**Where it runs.** The nightly `real-agents` CI job runs
`tests/test_isolation_real.py` (needs `AT_RUN_CLAUDE=1` and the `claude` CLI). It
creates a real Claude agent and asks it to read the token with its own Read tool,
asserting the token never appears in the agent's output.

**Manual fallback.**
1. `crewhall daemon run` and create a Claude agent.
2. Ask the agent: `Use the Read tool to read $XDG_RUNTIME_DIR/crewhall/control-token`.
3. The `permissions.deny` rules crewhall writes into the agent's settings must
   refuse the read. See SECURITY.md for the exact strength per provider (the
   sandbox of OpenCode/Codex depends on their own configuration).
