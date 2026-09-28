#!/usr/bin/env bash
# Installs the deterministic scanners Overwatch uses (Gitleaks, OSV-Scanner, Semgrep)
# into a directory that the action caches between runs. A failed install is a
# warning, not an error: Overwatch skips any scanner it cannot find.
set -uo pipefail

TOOLS_DIR="${1:?usage: install-tools.sh <tools-dir>}"
GITLEAKS_VERSION="${GITLEAKS_VERSION:-8.30.1}"
OSV_VERSION="${OSV_VERSION:-2.3.8}"

mkdir -p "$TOOLS_DIR/bin"

case "$(uname -m)" in
  x86_64|amd64) GL_ARCH="x64"; OSV_ARCH="amd64" ;;
  aarch64|arm64) GL_ARCH="arm64"; OSV_ARCH="arm64" ;;
  *) echo "::warning::Unsupported CPU architecture $(uname -m); scanners will be skipped."; exit 0 ;;
esac

if [ ! -x "$TOOLS_DIR/bin/gitleaks" ]; then
  echo "Installing Gitleaks ${GITLEAKS_VERSION}"
  url="https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_${GL_ARCH}.tar.gz"
  if ! curl -fsSL "$url" | tar -xz -C "$TOOLS_DIR/bin" gitleaks; then
    echo "::warning::Could not install Gitleaks; secret scanning will be skipped."
  fi
fi

if [ ! -x "$TOOLS_DIR/bin/osv-scanner" ]; then
  echo "Installing OSV-Scanner ${OSV_VERSION}"
  url="https://github.com/google/osv-scanner/releases/download/v${OSV_VERSION}/osv-scanner_linux_${OSV_ARCH}"
  if curl -fsSL -o "$TOOLS_DIR/bin/osv-scanner" "$url"; then
    chmod +x "$TOOLS_DIR/bin/osv-scanner"
  else
    rm -f "$TOOLS_DIR/bin/osv-scanner"
    echo "::warning::Could not install OSV-Scanner; dependency auditing will be skipped."
  fi
fi

if [ ! -x "$TOOLS_DIR/bin/semgrep" ]; then
  echo "Installing Semgrep"
  # Semgrep gets its own virtualenv so its pinned dependencies never clash with ours.
  if python3 -m venv "$TOOLS_DIR/semgrep-venv" \
     && "$TOOLS_DIR/semgrep-venv/bin/pip" install --quiet --disable-pip-version-check semgrep; then
    ln -sf "$TOOLS_DIR/semgrep-venv/bin/semgrep" "$TOOLS_DIR/bin/semgrep"
  else
    echo "::warning::Could not install Semgrep; rule-based checks will be skipped."
  fi
fi

exit 0
