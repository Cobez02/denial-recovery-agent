#!/usr/bin/env sh
# Scan staged changes for secrets before a commit. Used as a plain git hook when pre-commit is not
# installed: copy or symlink it to .git/hooks/pre-commit. With pre-commit, .pre-commit-config.yaml
# runs the same scan.
set -eu

if command -v gitleaks >/dev/null 2>&1; then
  exec gitleaks protect --staged --redact --config .gitleaks.toml
fi

cat >&2 <<'MSG'
gitleaks is not installed; refusing to commit unscanned changes.

Install it, then try again:
  Windows : winget install gitleaks     (or: scoop install gitleaks)
  macOS   : brew install gitleaks
  Linux   : https://github.com/gitleaks/gitleaks/releases (put the binary on PATH)
Or use pre-commit for everything: uv run pre-commit install
MSG
exit 1
