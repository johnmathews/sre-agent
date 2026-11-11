# Coverage floor and worktree-safe git hooks (W3)

## What

- `pyproject.toml`: `[tool.coverage.run] source = ["src"]` and `[tool.coverage.report] fail_under = 89,
  show_missing = true, skip_covered = true`.
- `make test` runs `coverage run -m pytest -rf` then `coverage report`, so CI's `check` job prints the table and fails
  below the floor. `-rf` names any failing test in the short summary.
- `scripts/install-hooks.sh` finds the hooks directory with `git rev-parse --path-format=absolute --git-path hooks`
  instead of `$(git rev-parse --show-toplevel)/.git/hooks`, which does not exist in a linked worktree (`.git` is a file
  there).
- `CLAUDE.md` Commands and Testing Strategy mention the floor.

## Why

F24: nothing stopped coverage from sliding. The baseline with `--source=src` is 90.27%, so the floor is 89 (W36 raises it
to floor(final) - 1). NF5: `make hooks` failed from a worktree.

## How verified

- `fail_under = 99`: `make test` printed `Coverage failure: total of 90 is less than fail-under=99` and exited 2.
- `fail_under = 89`: 1699 passed, `TOTAL 9915 965 90%`, exit 0.
- `make hooks` from the worktree installed both hooks into the main checkout's shared `.git/hooks`.
