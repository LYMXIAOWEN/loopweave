# AGENTS

## Scope

These rules apply to the LoopWeave repository.

## Source of truth

- Product code lives in `src/`.
- Tests live in `tests/`.
- Public architecture and operating documentation lives in `docs/`.
- Runtime state must never become source material.

## Required checks

Run these from the repository root before handing off a change:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B -m pytest -q
python3 -m compileall -q src tests
```

When Ruff is installed, also run:

```bash
ruff check src tests
```

## Product boundaries

- Do not claim Windows support until native ConPTY behavior passes the agreed
  Windows acceptance matrix.
- Do not add a terminal-agent allowlist to the core protocol.
- Bind terminal sessions explicitly; do not guess a target from a process name.
- Agent-specific hooks are optional adapters, not product admission gates.
- Preserve explicit human review and stop points.

## Repository hygiene

- Never commit `runs/`, `var/`, project workspaces, transcripts, SQLite state,
  sockets, logs, task packets, or review drafts.
- Never commit absolute user paths, personal names, task IDs, or credentials.
- Use synthetic fixtures in tests.
- Public commands, Python packages, protocol markers, and environment variables
  must use the LoopWeave identity only. Do not add historical product names or
  compatibility aliases to the public tree.
