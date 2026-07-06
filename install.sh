#!/bin/bash
# Hermes Colab CLI + Pony Diffusion V6 XL + Z-Image-Turbo — one-command installer
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COLAB_DEST="$HOME/.hermes/scripts/colab"
BIN_DIR="$HOME/.local/bin"

mkdir -p "$COLAB_DEST" "$BIN_DIR"

echo "=== Hermes Colab CLI + Diffusion Models Installer ==="

# Python deps
echo "Installing Python dependencies..."
pip install --quiet google-colab-cli requests

# Colab CLI
cp "$SCRIPT_DIR/colab.py" "$COLAB_DEST/colab.py"
chmod +x "$COLAB_DEST/colab.py"
ln -sf "$COLAB_DEST/colab.py" "$BIN_DIR/colabctl"

# Pony CLI
cp "$SCRIPT_DIR/pony.py" "$BIN_DIR/pony"
chmod +x "$BIN_DIR/pony"

# Z-Image CLI
cp "$SCRIPT_DIR/zimage/zimage.py" "$BIN_DIR/zimage"
chmod +x "$BIN_DIR/zimage"

echo ""
echo "=== Done ==="
echo "colabctl: $(which colabctl)"
echo "pony:     $(which pony)"
echo "zimage:   $(which zimage)"
echo ""
echo "Next:"
echo "  1. Authenticate Colab: see references/auth_flow.md"
echo "  2. Deploy Z-Image-Turbo: zimage/AGENTS.md"
echo "  3. Deploy Pony Diffusion: see AGENTS.md → Pony Diffusion Deployment"
