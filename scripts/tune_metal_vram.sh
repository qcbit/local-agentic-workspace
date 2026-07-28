#!/usr/bin/env bash
set -euo pipefail

echo "==================================================="
echo " macOS Apple Silicon Metal VRAM Tuning Utility"
echo " Target: MackBook Pro M1 Max (32 Unified Memory)"
echo "==================================================="

# Verify MacOS and Apple Silicon
if [[ "$(uname)" != "Darwin" ]]; then
    echo "ERROR: This script is designed exclusively for MacOS." >&2
    exit 1
fi

ARCH=$(uname -m)
if [[ "$ARCH" != "arm64" ]]; then
    echo "WARNING: Non-Apple Silicon architecture detected ($ARCH)."
    echo "This script is optimized for Apple Silicon M-series chips."
fi

# Check current wired memory limit
CURRENT_LIMIT=$(sysctl -n iogpu.wired_mem_limit 2>/dev/null || echo "Not Set")
echo "Current iogpu.wired_mem_limit: $CURRENT_LIMIT"

# Apply recommended 28GB VRAM limit
# Optimal VRAM headroom for 14B/32B quantized models leaving 4GB for base macOS operations
TARGET_MB=28672  # 28GB in MB
echo "Applying recommended wired memory limit: ${TARGET_MB}MB (~28GB)"
if sudo sysctl iogpu.wired_mem_limit=$TARGET_MB; then
    echo "Successfully set iogpu.wired_mem_limit to ${TARGET_MB}MB."
else
    echo "ERROR: Failed to set iogpu.wired_mem_limit. You may need to run this script with elevated privileges." >&2
    exit 1
fi

echo "Verification check:"
sysctl iogpu.wried_mem_limit
echo "==================================================="
