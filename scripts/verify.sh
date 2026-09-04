#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
plugin_root="$project_root/plugins/supermind"
skill_root="$plugin_root/skills/supermind"
validator_python="${CODEX_PYTHON:-python3}"
validator=("$validator_python")

"$validator_python" -m json.tool "$project_root/.agents/plugins/marketplace.json" >/dev/null
"$validator_python" -m json.tool "$plugin_root/.codex-plugin/plugin.json" >/dev/null

if ! "$validator_python" -c 'import yaml' 2>/dev/null; then
  if command -v uv >/dev/null 2>&1; then
    validator=(uv run --quiet --with PyYAML python3)
  else
    echo "Validation needs PyYAML. Install it or provide uv, then run this script again." >&2
    exit 1
  fi
fi

"${validator[@]}" "${CODEX_HOME:-$HOME/.codex}/skills/.system/skill-creator/scripts/quick_validate.py" "$skill_root"
"${validator[@]}" "${CODEX_HOME:-$HOME/.codex}/skills/.system/plugin-creator/scripts/validate_plugin.py" "$plugin_root"

expected_files='\.agents/plugins/marketplace\.json
\.gitignore
README\.md
plugins/supermind/\.codex-plugin/plugin\.json
plugins/supermind/skills/supermind/SKILL\.md
plugins/supermind/skills/supermind/agents/openai\.yaml
plugins/supermind/skills/supermind/references/actions\.md
plugins/supermind/skills/supermind/references/product-state\.md
plugins/supermind/skills/supermind/references/superpowers-routing\.md
scripts/verify\.sh'

actual_files="$(cd "$project_root" && find . -type f -not -path './.git/*' -print | sed 's#^\./##' | sort)"
if ! diff -u <(printf '%s\n' "$expected_files" | sed 's/\\//g' | sort) <(printf '%s\n' "$actual_files"); then
  echo "Unexpected or missing files detected." >&2
  exit 1
fi

if find "$project_root" -type l -not -path '*/.git/*' | grep -q .; then
  echo "Symlinks are not allowed in the release tree." >&2
  exit 1
fi

for reference in actions.md product-state.md superpowers-routing.md; do
  grep -Fq "references/$reference" "$skill_root/SKILL.md"
done

if grep -RInE --exclude-dir=.git --exclude=verify.sh \
  '(/Users/|/home/|[A-Za-z]:\\\\Users\\\\|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY|\[TODO:|TBD)' \
  "$project_root"; then
  echo "Private, secret-like, legacy, or unfinished content detected." >&2
  exit 1
fi

echo "Supermind release tree verified."
