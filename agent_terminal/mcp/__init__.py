"""Native MCP server for agents (JSON-RPC 2.0 over stdio, stdlib only).

The server is launched by the agent itself and takes its identity from the
environment (``CREWHALL_AGENT_ID`` / ``CREWHALL_TOKEN``). It makes
no authorization decision: every call goes to the daemon over the 0600 socket,
where identity and Team membership are enforced. It exposes only the minimum
surface (whoami, list teammates, send a message, new session for itself).
"""
