#!/usr/bin/env bash
# jarvS redeploy script -- run this on the Ubuntu mini PC after `git pull`.
#
# Unlike install.sh (which provisions a bare Ubuntu box from scratch:
# apt packages, Node, gh CLI, ollama tuning, first-time systemd units), this
# script assumes the box is already provisioned and just needs the working
# tree's latest code picked up: refreshed venv/deps, refreshed Ollama models,
# refreshed systemd units, and a clean restart. Safe to run every time you
# pull.
#
# Usage:
#   git pull
#   ./deploy.sh
#
# Env overrides (all optional):
#   JARVIS_HOME       repo checkout to operate on   (default: this script's directory)
#   JARVIS_PLAN_MODEL Ollama planning model          (default: dolphin-mistral:7b)
#   JARVIS_FAST_MODEL Ollama fast model              (default: huihui_ai/qwen3-abliterated:4b)
#   SKIP_SYSTEMD=1    skip the systemd install/restart step (code-only refresh)
set -euo pipefail

# ---------------------------------------------------------------- setup ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JHOME="${JARVIS_HOME:-$SCRIPT_DIR}"
PLAN_MODEL="${JARVIS_PLAN_MODEL:-dolphin-mistral:7b}"
FAST_MODEL="${JARVIS_FAST_MODEL:-huihui_ai/qwen3-abliterated:4b}"
RUN_USER="${SUDO_USER:-$(whoami)}"
cd "$JHOME"

ok()   { printf '  \033[32m\xE2\x9C\x93\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }
fail() { printf '  \033[31m\xE2\x9C\x97\033[0m %s\n' "$1"; exit 1; }
step() { printf '\n==> %s\n' "$1"; }

# --------------------------------------------------------- 1. validate ---
step "validating environment"

command -v python3 >/dev/null || fail "python3 not found -- install Python 3.10+"
PYVER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
python3 - <<'EOF' || fail "python3 is older than 3.10 (found $PYVER)"
import sys
sys.exit(0 if sys.version_info >= (3, 10) else 1)
EOF
ok "python3 $PYVER"

command -v node >/dev/null || fail "node not found -- install Node.js 22.x (see install.sh)"
NODEVER="$(node -v)"
ok "node $NODEVER"
command -v npm >/dev/null && ok "npm $(npm -v)" || warn "npm not found on PATH"

command -v ollama >/dev/null || fail "ollama not found -- install it from https://ollama.com first"
ok "ollama $(ollama -v 2>/dev/null | head -1 || echo present)"

# ------------------------------------------------- 2. venv & dependencies -
step "python virtualenv & dependencies"

if [ ! -d .venv ]; then
  python3 -m venv .venv
  ok "created .venv"
else
  ok ".venv already exists, reusing"
fi

./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -r requirements-dev.txt
ok "runtime and development requirements installed in .venv"

step "jarvS runtime (Rust)"
command -v cargo >/dev/null 2>&1 || fail "Rust is not installed -- install it from https://rustup.rs and re-run"
cargo build --release --locked
ok "built target/release/jarvs"

./.venv/bin/pip install -q kokoro-onnx soundfile \
  || warn "kokoro-onnx install failed -- voice falls back to piper"

./.venv/bin/playwright install --with-deps chromium \
  || warn "playwright chromium install failed -- browse_* tools will error until fixed"
ok "playwright chromium ready"

if [ ! -f .env ]; then
  cp env.example .env
  # env.example hardcodes /home/jarvis/jarvis as a placeholder; rewrite it to
  # wherever this checkout actually lives (casing and all) so EnvironmentFile
  # / WorkingDirectory / voice & gmail paths don't silently point nowhere.
  sed -i "s#/home/jarvis/jarvis#${JHOME}#g" .env
  chmod 600 .env
  ok "created .env from env.example -- fill it in"
fi
[ -f missions.yaml ] || { cp missions.example.yaml missions.yaml; ok "created missions.yaml from missions.example.yaml"; }

step "whatsapp bridge (Node/Baileys)"
if [ -d channels/bridge ]; then
  (
    cd channels/bridge
    npm install --omit=dev
  ) && ok "whatsapp bridge built" \
    || warn "WhatsApp bridge dependencies failed to install"
else
  warn "channels/bridge not found -- skipping whatsapp bridge build"
fi

# --------------------------------------------------- 3. ollama models ----
step "ollama models"

if ! ollama list >/dev/null 2>&1; then
  fail "ollama is not running -- start it (systemctl start ollama, or run 'ollama serve') and re-run"
fi
ok "ollama is running"

for model in "$PLAN_MODEL" "$FAST_MODEL"; do
  if ollama list | awk '{print $1}' | grep -qx "$model"; then
    ok "$model already pulled"
  else
    echo "    pulling $model ..."
    ollama pull "$model" || warn "failed to pull $model -- pull it manually before relying on it"
  fi
done

# ---------------------------------------------- 4. systemd installation --
if [ "${SKIP_SYSTEMD:-0}" = "1" ]; then
  step "skipping systemd install/restart (SKIP_SYSTEMD=1)"
else
  step "installing systemd units"

  TMP_UNITS="$(mktemp -d)"
  trap 'rm -rf "$TMP_UNITS"' EXIT

  for unit in jarvis-worker jarvis-dashboard jarvis-wa; do
    src="systemd/${unit}.service"
    [ -f "$src" ] || fail "$src not found -- run this script from the repo root"
    sed -e "s#/home/jarvis/jarvis#${JHOME}#g" \
        -e "s#^User=jarvis#User=${RUN_USER}#" \
        "$src" > "$TMP_UNITS/${unit}.service"
  done

  echo "    the following needs sudo to copy units into /etc/systemd/system/"
  sudo cp "$TMP_UNITS"/*.service /etc/systemd/system/
  sudo systemctl daemon-reload
  ok "units installed for user=${RUN_USER} home=${JHOME}"

  for unit in jarvis-worker jarvis-dashboard jarvis-wa; do
    sudo systemctl enable --now "$unit" 2>/dev/null || sudo systemctl restart "$unit"
  done
  ok "jarvis-worker, jarvis-dashboard, jarvis-wa enabled and (re)started"
fi

# --------------------------------------------------- 5. health checks ----
step "post-install health check"

if [ "${SKIP_SYSTEMD:-0}" != "1" ]; then
  for unit in jarvis-worker jarvis-dashboard jarvis-wa; do
    state="$(systemctl is-active "$unit" 2>/dev/null || echo unknown)"
    if [ "$state" = "active" ]; then
      ok "$unit: $state"
    else
      warn "$unit: $state -- check 'journalctl -u $unit -e'"
    fi
  done
fi

echo
echo "==> deploy finished. next steps:"
echo "   - syntax check:    $JHOME/.venv/bin/python $JHOME/worker.py --help"
echo "   - tests:           $JHOME/.venv/bin/python -m pytest -q $JHOME/tests"
echo "   - lint:            $JHOME/.venv/bin/ruff check $JHOME"
echo "   - GitHub tools:    gh auth login   (or set GITHUB_TOKEN in .env)"
echo "   - Gmail tools:     save an OAuth client as $JHOME/gmail_credentials.json,"
echo "                      then run $JHOME/.venv/bin/python $JHOME/gmail_setup.py yourself"
echo "   - WhatsApp:        first run must be interactive to scan the QR --"
echo "                      $JHOME/.venv/bin/python $JHOME/channels/wa.py"
echo "                      then put the printed jid into JARVIS_ALLOWED / JARVIS_OWNER_JID in .env"
echo "                      and restart: sudo systemctl restart jarvis-wa jarvis-worker"
echo "   - Dashboard:       http://<box-ip>:\${JARVIS_DASHBOARD_PORT:-8899} from inside your LAN"
