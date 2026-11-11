#!/usr/bin/env bash
# jarvS installer. Run as the jarvis user (which has passwordless sudo),
# from inside the repo checkout (it installs into that same directory).
set -euo pipefail

# Default to wherever this script actually lives, not a guessed path --
# a hardcoded "$HOME/jarvis" broke EnvironmentFile/WorkingDirectory on any
# box checked out somewhere else (e.g. "$HOME/Jarvis", capital J).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JHOME="${JARVIS_HOME:-$SCRIPT_DIR}"
PLAN_MODEL="${JARVIS_PLAN_MODEL:-dolphin-mistral:7b}"
FAST_MODEL="${JARVIS_FAST_MODEL:-huihui_ai/qwen3-abliterated:4b}"
echo "==> installing into $JHOME"

# --- system packages -------------------------------------------------------
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-pip qrencode ffmpeg git curl jq

# --- node 22 for the whatsapp bridge ---------------------------------------
if ! command -v node >/dev/null || [ "$(node -v | cut -c2-3)" -lt 22 ]; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
  sudo apt-get install -y -qq nodejs
fi

# --- gh CLI (Phase 2: GitHub tools) -----------------------------------------
if ! command -v gh >/dev/null; then
  curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | \
    sudo dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg
  sudo chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | \
    sudo tee /etc/apt/sources.list.d/github-cli.list >/dev/null
  sudo apt-get update -qq
  sudo apt-get install -y -qq gh
fi

# --- python env ------------------------------------------------------------
mkdir -p "$JHOME"/{logs,journal,voices,code}
cd "$JHOME"
python3 -m venv .venv
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -r requirements-dev.txt
# kokoro voice is optional; do not fail the whole install over it
./.venv/bin/pip install -q kokoro-onnx soundfile || echo "!! kokoro-onnx install failed - voice falls back to piper"

# --- jarvS runtime (Rust) --------------------------------------------------
# The worker service runs `jarvs daemon`, which mediates every agent tool call.
if ! command -v cargo >/dev/null 2>&1; then
  echo "!! Rust is not installed. Install it from https://rustup.rs, then re-run this script."
  exit 1
fi
cargo build --release --locked

# --- browser (Phase 2: browse_open/browse_click/browse_read) ---------------
./.venv/bin/playwright install --with-deps chromium || \
  echo "!! playwright chromium install failed - browse_* tools will error until fixed"

# --- whatsapp bridge (Baileys) ---------------------------------------------
echo "==> installing WhatsApp bridge dependencies"
(
  cd "$JHOME/channels/bridge"
  npm install --omit=dev
) || echo "!! WhatsApp bridge dependencies failed to install; see channels/bridge/package.json"

# --- ollama tuning ---------------------------------------------------------
sudo mkdir -p /etc/systemd/system/ollama.service.d
sudo tee /etc/systemd/system/ollama.service.d/jarvis.conf >/dev/null <<'EOF'
[Service]
Environment="OLLAMA_NUM_PARALLEL=1"
Environment="OLLAMA_MAX_LOADED_MODELS=1"
Environment="OLLAMA_KEEP_ALIVE=5m"
EOF
sudo systemctl daemon-reload
sudo systemctl restart ollama
sleep 3

# --- models ----------------------------------------------------------------
ollama pull "$PLAN_MODEL"
if [ "$FAST_MODEL" != "$PLAN_MODEL" ]; then
  ollama pull "$FAST_MODEL"
fi
# --- piper voice (optional, for voice replies) -----------------------------
if [ ! -f "$JHOME/voices/en_US-amy-medium.onnx" ]; then
  echo "==> fetching piper voice (optional)"
  curl -fsSL -o "$JHOME/voices/en_US-amy-medium.onnx" \
    https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/medium/en_US-amy-medium.onnx || true
  curl -fsSL -o "$JHOME/voices/en_US-amy-medium.onnx.json" \
    https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/medium/en_US-amy-medium.onnx.json || true
fi

# --- env -------------------------------------------------------------------
if [ ! -f "$JHOME/.env" ]; then
  cp "$JHOME/env.example" "$JHOME/.env"
  # env.example hardcodes /home/jarvis/jarvis as a placeholder; rewrite it to
  # wherever this box actually has Jarvis checked out (casing and all).
  sed -i "s#/home/jarvis/jarvis#$JHOME#g" "$JHOME/.env"
fi
chmod 600 "$JHOME/.env"
[ -f "$JHOME/missions.yaml" ] || cp "$JHOME/missions.example.yaml" "$JHOME/missions.yaml"

# --- systemd units ---------------------------------------------------------
sudo tee /etc/systemd/system/jarvis-worker.service >/dev/null <<EOF
[Unit]
Description=jarvS worker
After=network-online.target ollama.service
Wants=ollama.service

[Service]
Type=simple
User=$USER
WorkingDirectory=$JHOME
EnvironmentFile=$JHOME/.env
Environment=PYTHONUNBUFFERED=1
Environment=JARVS_PYTHON_DIR=$JHOME
Environment=JARVIS_HOME=$JHOME
ExecStart=$JHOME/target/release/jarvs daemon
Restart=always
RestartSec=10
TimeoutStopSec=30
KillMode=mixed

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/jarvis-wa.service >/dev/null <<EOF
[Unit]
Description=jarvS WhatsApp channel
After=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$JHOME
EnvironmentFile=$JHOME/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=$JHOME/.venv/bin/python $JHOME/channels/wa.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/jarvis-dashboard.service >/dev/null <<EOF
[Unit]
Description=jarvS read-only dashboard
After=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$JHOME
EnvironmentFile=$JHOME/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=$JHOME/.venv/bin/uvicorn dashboard:app --host 127.0.0.1 --port \${JARVIS_DASHBOARD_PORT}
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
echo
echo "==> installed. next:"
echo "   1. $JHOME/.venv/bin/python $JHOME/worker.py --help"
echo "   2. $JHOME/.venv/bin/python -m pytest -q $JHOME/tests"
echo "   3. $JHOME/.venv/bin/ruff check $JHOME"
echo "   4. $JHOME/.venv/bin/python $JHOME/channels/wa.py   # scan QR"
echo "   5. temporarily set JARVIS_LOG_UNKNOWN_JIDS=1, message Jarvis, then copy"
echo "      the logged direct JID into JARVIS_ALLOWED and JARVIS_OWNER_JID."
echo "      Set JARVIS_LOG_UNKNOWN_JIDS=0, restart jarvis-wa, then test 'ping'."
echo "   6. sudo systemctl enable --now jarvis-wa jarvis-worker"
echo "   7. optional: sudo systemctl enable --now jarvis-dashboard"
echo "   8. optional: save a Gmail OAuth client as $JHOME/gmail_credentials.json,"
echo "      then run $JHOME/.venv/bin/python $JHOME/gmail_setup.py yourself (interactive)"
echo "   9. optional SSH lateral movement: make sure this user's ~/.ssh has keys"
echo "      that already work, passwordless, against whatever hosts you want"
echo "      ssh_command() to reach -- nothing here generates or distributes keys"
