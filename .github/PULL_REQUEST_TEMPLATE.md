## What and why

## Checklist
- [ ] `python -m unittest discover` passes from the repository root
- [ ] `ruff check agent_terminal tests scripts` is clean
- [ ] `python scripts/privacy_scan.py` is clean (no personal paths, hosts, emails, tokens)
- [ ] New capabilities are off by default and documented
- [ ] `CHANGELOG.md` updated (and version bumped if user-visible)
