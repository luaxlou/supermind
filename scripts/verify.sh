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

expected_files='.agents/plugins/marketplace.json
.gitignore
README.md
docs/product/2026-09-04-capability-memory-design.md
docs/product/2026-09-06-capability-memory-hardening-design.md
docs/product/plans/2026-09-04-capability-memory.md
docs/product/plans/2026-09-06-capability-memory-hardening.md
plugins/supermind/.codex-plugin/plugin.json
plugins/supermind/evaluation/retrieval-v1.json
plugins/supermind/model.lock.json
plugins/supermind/pyproject.toml
plugins/supermind/requirements.lock
plugins/supermind/scripts/capability-memory
plugins/supermind/skills/supermind/SKILL.md
plugins/supermind/skills/supermind/agents/openai.yaml
plugins/supermind/skills/supermind/references/actions.md
plugins/supermind/skills/supermind/references/capability-routing.md
plugins/supermind/skills/supermind/references/product-state.md
plugins/supermind/src/supermind_memory/__init__.py
plugins/supermind/src/supermind_memory/bootstrap.py
plugins/supermind/src/supermind_memory/cli.py
plugins/supermind/src/supermind_memory/compatibility.py
plugins/supermind/src/supermind_memory/config.py
plugins/supermind/src/supermind_memory/decision.py
plugins/supermind/src/supermind_memory/discovery.py
plugins/supermind/src/supermind_memory/embeddings.py
plugins/supermind/src/supermind_memory/explorer.py
plugins/supermind/src/supermind_memory/health.py
plugins/supermind/src/supermind_memory/lifecycle.py
plugins/supermind/src/supermind_memory/repository.py
plugins/supermind/src/supermind_memory/redaction.py
plugins/supermind/src/supermind_memory/retrieval_evaluation.py
plugins/supermind/src/supermind_memory/schema.py
plugins/supermind/src/supermind_memory/scoring.py
plugins/supermind/src/supermind_memory/search.py
plugins/supermind/src/supermind_memory/service.py
plugins/supermind/src/supermind_memory/source_resolution.py
plugins/supermind/src/supermind_memory/taxonomy.py
plugins/supermind/src/supermind_memory/types.py
plugins/supermind/src/supermind_memory/workflow.py
plugins/supermind/tests/conftest.py
plugins/supermind/tests/e2e/test_login_reuse.py
plugins/supermind/tests/integration/test_cli.py
plugins/supermind/tests/integration/test_discovery.py
plugins/supermind/tests/integration/test_explorer.py
plugins/supermind/tests/integration/test_health.py
plugins/supermind/tests/integration/test_repository.py
plugins/supermind/tests/integration/test_search.py
plugins/supermind/tests/integration/test_service.py
plugins/supermind/tests/unit/test_taxonomy_scoring.py
plugins/supermind/tests/unit/test_redaction.py
plugins/supermind/tests/unit/test_types_config.py
plugins/supermind/uv.lock
scripts/verify.sh'

actual_files="$(
  cd "$project_root"
  find . \
    \( -path './.git' -o -path './.superpowers' -o -path './.worktrees' -o -name .venv -o -name .pytest_cache -o -name __pycache__ \) -prune \
    -o -type f -print \
    | sed 's#^\./##' \
    | sort
)"
if ! diff -u <(printf '%s\n' "$expected_files" | sort) <(printf '%s\n' "$actual_files"); then
  echo "Unexpected or missing files detected." >&2
  exit 1
fi

if find "$project_root" \
  \( -path "$project_root/.git" -o -path "$project_root/.superpowers" -o -path "$project_root/.worktrees" -o -name .venv -o -name .pytest_cache -o -name __pycache__ \) -prune \
  -o -type l -print -quit \
  | grep -q .; then
  echo "Symlinks are not allowed in the release tree." >&2
  exit 1
fi

for reference in actions.md capability-routing.md product-state.md; do
  grep -Fq "references/$reference" "$skill_root/SKILL.md"
done

if [[ ! -x "$plugin_root/scripts/capability-memory" ]]; then
  echo "Capability Memory launcher must be executable." >&2
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "Release verification requires uv for locked dependency and test checks." >&2
  exit 1
fi

uv lock --check --project "$plugin_root"
if ! diff -u \
  <(tail -n +3 "$plugin_root/requirements.lock") \
  <(uv export --project "$plugin_root" --frozen --no-dev --no-emit-project --format requirements-txt | tail -n +3); then
  echo "requirements.lock does not match the frozen project lock." >&2
  exit 1
fi

uv run --project "$plugin_root" pytest "$plugin_root/tests" -q
uv run --project "$plugin_root" pytest "$plugin_root/tests/e2e/test_login_reuse.py" -q

if grep -RInE \
  --exclude-dir=.git \
  --exclude-dir=.superpowers \
  --exclude-dir=.worktrees \
  --exclude-dir=.venv \
  --exclude-dir=.pytest_cache \
  --exclude-dir=__pycache__ \
  --exclude=.git \
  --exclude=verify.sh \
  '(/Users/|/home/|[A-Za-z]:\\\\Users\\\\|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{20,}|AKIA[0-9A-Z]{16}|BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY|\[TODO:|TBD)' \
  "$project_root"; then
  echo "Private, secret-like, legacy, or unfinished content detected." >&2
  exit 1
fi

echo "Supermind release tree verified."
