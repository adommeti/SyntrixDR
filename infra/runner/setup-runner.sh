#!/usr/bin/env bash
# One-time preparation of the self-hosted GitHub Actions runner (Linux x64, labels: self-hosted, linux, syntrixdr-linux).
# Run ONCE as a sudo-capable admin on the runner host, then never again from CI — workflows must not need sudo.
#   sudo bash infra/runner/setup-runner.sh <runner-service-user>
# Installs: Docker Engine + compose plugin (runner user in the docker group), build/system deps for Python wheels,
# Playwright/Chromium system libraries, libpq, and the user-local tool directory the workflow uses.
set -euo pipefail
RUNNER_USER="${1:?runner service user (the account the actions runner runs as)}"

if [ "$(id -u)" -ne 0 ]; then echo "run with sudo"; exit 1; fi
. /etc/os-release
case "${ID_LIKE:-$ID}" in *debian*|*ubuntu*) ;; *) echo "Debian/Ubuntu family expected (found $ID); adapt package names."; exit 1 ;; esac

apt-get update -y
apt-get install -y --no-install-recommends \
  ca-certificates curl git gnupg lsb-release make jq unzip zip build-essential pkg-config \
  libpq-dev libffi-dev libssl-dev python3-dev \
  fonts-liberation fonts-noto-color-emoji

# --- Docker Engine + compose plugin (official repo) --------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/${ID}/gpg" | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/${ID} $(lsb_release -cs) stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker
usermod -aG docker "$RUNNER_USER"

# --- Playwright / Chromium system libraries (so CI can `playwright install chromium` without sudo) ----------
apt-get install -y --no-install-recommends \
  libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
  libxfixes3 libxrandr2 libgbm1 libasound2 libpango-1.0-0 libcairo2 libatspi2.0-0 libx11-xcb1 libxcb-dri3-0 libxshmfence1

# --- tool cache used by ci.yml (TOOL_BIN, uv tools, Playwright browsers) ----------------------------------------
# ci.yml creates it itself at "$GITHUB_WORKSPACE/../_tools" (= <runner-dir>/_work/SyntrixDR/_tools); nothing to do here.

# --- sanity -----------------------------------------------------------------------------------------------------
RUNNER_HOME="$(getent passwd "$RUNNER_USER" | cut -d: -f6)"
RUNNER_DIR="$(dirname "$(find "$RUNNER_HOME" -maxdepth 2 -name svc.sh 2>/dev/null | head -1)" 2>/dev/null || true)"
sg docker -c "docker info >/dev/null" 2>/dev/null && echo "docker: OK" || echo "docker: group membership applies after the runner service restarts"
echo "Restart the runner service so the docker group applies:  cd ${RUNNER_DIR:-<runner-dir>} && sudo ./svc.sh stop && sudo ./svc.sh start"
echo "Runner labels expected by ci.yml: self-hosted, linux, syntrixdr-linux"
