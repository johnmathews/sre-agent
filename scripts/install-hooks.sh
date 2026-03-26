#!/usr/bin/env bash
# Install git hooks for the SRE assistant repository.
set -euo pipefail

HOOKS_DIR="$(git rev-parse --show-toplevel)/.git/hooks"

# --- Pre-commit: fast lint + format check (~1s) ---
cat > "$HOOKS_DIR/pre-commit" << 'HOOK'
#!/usr/bin/env bash
# Pre-commit hook: catch lint/format errors before they reach CI.
# Installed by: make hooks
set -euo pipefail

echo "Running lint check..."
if ! uv run ruff check src/ tests/ --quiet; then
    echo ""
    echo "Pre-commit hook FAILED — lint errors found."
    echo "Run 'make format' to fix, then re-commit."
    exit 1
fi

if ! uv run ruff format --check src/ tests/ --quiet; then
    echo ""
    echo "Pre-commit hook FAILED — formatting issues found."
    echo "Run 'make format' to fix, then re-commit."
    exit 1
fi
HOOK

chmod +x "$HOOKS_DIR/pre-commit"
echo "Installed pre-commit hook at $HOOKS_DIR/pre-commit"

# --- Pre-push: full check (lint + typecheck + test) ---
cat > "$HOOKS_DIR/pre-push" << 'HOOK'
#!/usr/bin/env bash
# Pre-push hook: run make check before pushing.
# Installed by: make hooks
set -euo pipefail

echo "Running make check before push..."
if ! make check; then
    echo ""
    echo "Pre-push hook FAILED — push blocked."
    echo "Fix the issues above, then try again."
    exit 1
fi
HOOK

chmod +x "$HOOKS_DIR/pre-push"
echo "Installed pre-push hook at $HOOKS_DIR/pre-push"
