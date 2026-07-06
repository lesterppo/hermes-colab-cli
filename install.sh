#!/bin/bash
# Hermes Colab CLI + Pony Diffusion V6 XL + Z-Image-Turbo + Qwen2.5-VL-3B — one-command installer
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COLAB_DEST="$HOME/.hermes/scripts/colab"
BIN_DIR="$HOME/.local/bin"

mkdir -p "$COLAB_DEST" "$BIN_DIR"

echo "=== Hermes Colab CLI + Model Deployments Installer ==="

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

# Qwen Chat CLI
cp "$SCRIPT_DIR/examples/qwen-vl/qwen-chat" "$BIN_DIR/qwen-chat"
chmod +x "$BIN_DIR/qwen-chat"

echo ""
echo "=== Done ==="
echo "colabctl:  $(which colabctl)"
echo "pony:      $(which pony)"
echo "zimage:    $(which zimage)"
echo "qwen-chat: $(which qwen-chat)"
echo ""
echo "Next:"
echo "  1. Authenticate Colab: see references/auth_flow.md"
echo "  2. Deploy Qwen2.5-VL-3B: qwen-chat reconnect"
echo "  3. Deploy Z-Image-Turbo: zimage/AGENTS.md"
echo "  4. Deploy Pony Diffusion: see AGENTS.md → Pony Diffusion Deployment"
