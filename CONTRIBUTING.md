# Contributing

LoopWeave is currently preparing its first public architecture. Before opening
a change, read `docs/ARCHITECTURE.md` and keep the core protocol vendor-neutral.

Use synthetic fixtures, include focused tests, and run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B -m pytest -q
```

Do not include local terminal logs, runtime databases, task packets, private
workspaces, credentials, or absolute user paths in issues or pull requests.
