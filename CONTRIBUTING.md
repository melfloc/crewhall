# Contributing

The rules below are the project's contract; they are enforced by review and CI.

## Principles

1. **Security first.** If convenience and safety disagree, safety wins. A new
   capability is **off by default** and enabled explicitly in Settings.
2. **Closed by default.** No dangerous flags, no auto-accepting trust/permission
   dialogs, no secrets in logs, bundles, fixtures or error messages.
3. **Honest data.** If a value is not observable it is `n/d`; it is never
   invented.
4. **Additive and reversible.** Do not break documented CLI/ops/config. One
   atomic commit per change, with its own tests.
5. **Local only.** Never push, publish, deploy or touch another machine from a
   change.

## Development

- Python ≥ 3.11, no runtime dependencies (`dependencies = []`).
- Tests: `python -m unittest discover` **from the repository root**.
- Lint: `ruff check agent_terminal tests scripts` (must pass).
- Agents/adapters: read `ADAPTERS.md`; a new adapter is not done until the
  **contract kit** (`tests/adapter_contract.py`) passes and a real run is
  documented in `ADAPTERS.md` §8. Scaffold with
  `crewhall adapter new <kind>`.
- Versioning: `RELEASING.md` (SemVer; a feature is MINOR). Update
  `pyproject.toml`, `agent_terminal/__init__.py` and `CHANGELOG.md` together.

## Commits

`tipo(ámbito): resumen (X.Y.Z)` (Spanish summaries are fine; code and CHANGELOG
are English). Each commit should leave the suite green.

## Adding a provider

1. Capture the TUI's screens in isolation (`ADAPTERS.md` §5) and sanitize them.
2. Implement `harness/<kind>.py`; declare only the capabilities the TUI offers.
3. Register it, make the contract kit pass, and add sanitized fixtures.
4. Document signals and limits in `ADAPTERS.md` §8 and the `CHANGELOG`.
