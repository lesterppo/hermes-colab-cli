#!/bin/bash
# install.sh — one-command setup for Hermes Colab CLI
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HERMES_SCRIPTS="${HOME}/.hermes/scripts/colab"
HERMES_SKILLS="${HOME}/.hermes/skills/devops/colab-cli"

echo "=== Hermes Colab CLI Installer ==="

# 1. Install official google-colab-cli
echo "[1/3] Installing google-colab-cli..."
pip install google-colab-cli 2>/dev/null || pip3 install google-colab-cli

# 2. Copy CLI script
echo "[2/3] Installing colab.py to ${HERMES_SCRIPTS}..."
mkdir -p "${HERMES_SCRIPTS}"
cp "${SCRIPT_DIR}/colab.py" "${HERMES_SCRIPTS}/colab.py"
chmod +x "${HERMES_SCRIPTS}/colab.py"

# 3. Copy skill
echo "[3/3] Installing skill to ${HERMES_SKILLS}..."
mkdir -p "${HERMES_SKILLS}/references"
cp "${SCRIPT_DIR}/SKILL.md" "${HERMES_SKILLS}/SKILL.md"
cp "${SCRIPT_DIR}/references/auth_flow.md" "${HERMES_SKILLS}/references/auth_flow.md"

echo ""
echo "Done! Next steps:"
echo "  1. Authenticate — see references/auth_flow.md"
echo "  2. Test: python3 ${HERMES_SCRIPTS}/colab.py whoami"
echo "  3. Use:  python3 ${HERMES_SCRIPTS}/colab.py --help"
