#!/bin/bash
# Hermes Colab CLI + Pony Diffusion V6 XL — one-command installer
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COLAB_DEST="$HOME/.hermes/scripts/colab"
BIN_DIR="$HOME/.local/bin"

mkdir -p "$COLAB_DEST" "$BIN_DIR"

echo "=== Hermes Colab CLI + Pony Diffusion Installer ==="

# Python deps
echo "Installing Python dependencies..."
pip install --quiet google-colab-cli diffusers[torch] "transformers==4.48.0" \
    accelerate xformers safetensors fastapi uvicorn python-multipart

# Colab CLI
cp "$SCRIPT_DIR/colab.py" "$COLAB_DEST/colab.py"
chmod +x "$COLAB_DEST/colab.py"
ln -sf "$COLAB_DEST/colab.py" "$BIN_DIR/colabctl"

# Pony CLI
cp "$SCRIPT_DIR/pony.py" "$BIN_DIR/pony"
chmod +x "$BIN_DIR/pony"

echo ""
echo "=== Done ==="
echo "colabctl: $(which colabctl)"
echo "pony:     $(which pony)"
echo ""
echo "Next:"
echo "  1. Authenticate: see references/auth_flow.md"
echo "  2. Deploy Pony Diffusion: see AGENTS.md → Pony Diffusion Deployment"
