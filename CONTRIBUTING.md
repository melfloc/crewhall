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
- Lint: `ruff check crewhall tests scripts` (must pass). Install it with
  `python -m pip install ruff` (or `pipx install ruff`). The active rule set is
  in `pyproject.toml` (`E`, `F`, `W`, `B` bugbear, `UP`, `BLE` blind-except,
  `SIM`). A new blind `except Exception` needs an explicit `# noqa: BLE001`
  with a reason.
- Types: `mypy` runs in CI (`python -m pip install mypy`); the strict scope is
  `messaging`, `team`, `requests`, `harness/*` and `persistence`, the rest of
  the package is checked non-strict.
- Coverage: `coverage run -m unittest discover && coverage report` (CI enforces
  the thresholds in `.github/workflows/ci.yml`; `python -m pip install coverage`).
- Agents/adapters: read `ADAPTERS.md`; a new adapter is not done until the
  **contract kit** (`tests/adapter_contract.py`) passes and a real run is
  documented in `ADAPTERS.md` §8. Scaffold with
  `crewhall adapter new <kind>`.
- Versioning: `RELEASING.md` (SemVer; a feature is MINOR). Update
  `pyproject.toml`, `crewhall/__init__.py` and `CHANGELOG.md` together.

### Browser tests (optional, slow)

`tests/test_web_ui_browser.py` and `tests/test_terminal_web_browser.py` drive a
real Chromium through Playwright. They **skip automatically** when Playwright or
a Chromium binary is missing, so they never run in the default suite. They are
slow (each test launches a browser, ~2 s/test; the UI file is ~250 s):

```bash
python -m pip install playwright && playwright install chromium
python -m unittest tests.test_web_ui_browser tests.test_terminal_web_browser
```

`CHROMIUM` may point at `chromium`, `chromium-browser` or `google-chrome` on
`PATH`. Every browser test has a **hard per-test timeout**
(`HardTimeoutMixin.TEST_TIMEOUT`, default 120 s in `tests/support.py`): a stuck
navigation fails the test instead of hanging the suite. Raise it locally for a
slow machine, or run them with `-v` to see progress.

## Commits

`tipo(ámbito): resumen (X.Y.Z)` (Spanish summaries are fine; code and CHANGELOG
are English). Each commit should leave the suite green.

## Adding a provider

1. Capture the TUI's screens in isolation (`ADAPTERS.md` §5) and sanitize them.
2. Implement `harness/<kind>.py`; declare only the capabilities the TUI offers.
3. Register it, make the contract kit pass, and add sanitized fixtures.
4. Document signals and limits in `ADAPTERS.md` §8 and the `CHANGELOG`.
