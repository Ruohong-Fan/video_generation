#!/usr/bin/env bash
set -euo pipefail

# Install and verify the Jimeng CLI (dreamina command)

echo "=== Jimeng CLI Setup ==="

# Check if already installed
if command -v dreamina &>/dev/null; then
  echo "✓ dreamina is already installed: $(which dreamina)"
  dreamina --version 2>/dev/null || true
else
  echo "Installing Jimeng CLI..."
  curl -s https://jimeng.jianying.com/cli | bash

  # Reload PATH in case the installer added a new directory
  export PATH="$HOME/.local/bin:$HOME/bin:$PATH"

  if command -v dreamina &>/dev/null; then
    echo "✓ dreamina installed successfully"
  else
    echo "ERROR: Installation completed but 'dreamina' not found in PATH." >&2
    echo "Try opening a new terminal or add the install directory to your PATH." >&2
    exit 1
  fi
fi

echo ""
echo "=== Next Steps ==="
echo "1. Log in:          dreamina login"
echo "2. Check credits:   dreamina user_credit"
echo "3. List commands:   dreamina --help"
echo ""
echo "Or use the helper scripts in scripts/:"
echo "  bash scripts/text2video.sh  \"your prompt here\""
echo "  bash scripts/image2video.sh path/to/image.png \"camera motion\""
echo "  bash scripts/text2image.sh  \"your prompt here\""
