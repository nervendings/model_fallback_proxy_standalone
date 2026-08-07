#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${repo_root}"

python_bin="${PYTHON_BIN:-${repo_root}/.venv/bin/python}"
if [[ ! -x "${python_bin}" ]]; then
    python_bin="$(command -v python3)"
fi

echo "[1/5] shell syntax"
bash -n scripts/runtime/run_proxy.sh scripts/runtime/run_watcher.sh

echo "[2/5] Python syntax"
"${python_bin}" -m compileall -q src tests

echo "[3/5] unit tests"
PYTHONPATH="${repo_root}/src" "${python_bin}" -m unittest discover -s tests -q

echo "[4/5] repository whitespace"
git diff --check

echo "[5/5] tracked-file secret/path scan"
mapfile -t scan_files < <(
    rg --files \
        -g '*.py' -g '*.sh' -g '*.service' -g '*.md' -g '*.toml' \
        -g '*.ini' -g '*.example' -g '*.txt' -g '*.json'
)
if ((${#scan_files[@]})) && rg -n -i --hidden \
    --glob '!configs/environment.example' \
    --glob '!README.md' \
    --glob '!docs/**' \
    '(api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password\s*=|BEGIN (RSA|OPENSSH|PRIVATE) KEY|/home/[A-Za-z0-9._-]+/mnt/|/home/[A-Za-z0-9._-]+/tool/)' \
    "${scan_files[@]}"; then
    echo "quality gate failed: possible secret or private path" >&2
    exit 1
fi

echo "quality gate: PASS"
