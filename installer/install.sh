#!/usr/bin/env bash
# Interactive installer for Inference Gateway Manager.
# Run from the repository root: bash installer/install.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR="${IGM_INSTALL_DIR:-/opt/inference-gateway-manager}"
SERVICE_USER="${IGM_SERVICE_USER:-${USER:-$(id -un)}}"
SERVICE_NAME="inference-gateway-manager.service"
MODELS_DIR=""
VENV_DIR="$INSTALL_DIR/.venv"
STATE_DIR="$INSTALL_DIR/.local/state"
CREDS_FILE="$INSTALL_DIR/.local/credentials.env"
CONFIG_FILE="$INSTALL_DIR/config.local.json"

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
ask() { # ask "prompt" default -> sets ANSWER
  local prompt="$1" default="$2" reply
  read -r -p "$prompt [$default] " reply || true
  ANSWER="${reply:-$default}"
}
ask_yes_no() { # sets ANSWER=yes/no
  local prompt="$1" default="${2:-no}" reply
  while true; do
    read -r -p "$prompt (y/n) [$default] " reply || reply="$default"
    case "${reply:-$default}" in
      y|Y|yes|YES) ANSWER=yes; return ;;
      n|N|no|NO) ANSWER=no; return ;;
    esac
  done
}

[[ $EUID -ne 0 ]] || { echo "Run as the unprivileged engine-owning user, not root." >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || { echo "Python 3.11+ is required" >&2; exit 1; }
[[ -f "$REPO_ROOT/examples/config.example.json" ]] || { echo "Run from a full repository checkout" >&2; exit 1; }

say "Inference Gateway Manager installer"
echo "Repository: $REPO_ROOT"
echo "Install target: $INSTALL_DIR"

ask "Install directory" "$INSTALL_DIR"; INSTALL_DIR="$ANSWER"
VENV_DIR="$INSTALL_DIR/.venv"
STATE_DIR="$INSTALL_DIR/.local/state"
CREDS_FILE="$INSTALL_DIR/.local/credentials.env"
CONFIG_FILE="$INSTALL_DIR/config.local.json"

if [[ -d "$INSTALL_DIR/.local" || -f "$CONFIG_FILE" ]]; then
  ask_yes_no "$INSTALL_DIR already contains an installation. Reconfigure in place (keeps keys, state and config you keep)?" "no"
  [[ "$ANSWER" == yes ]] || { echo "Aborting to avoid overwriting an existing installation." >&2; exit 1; }
fi

# 1. Engine selection
ALL_ENGINES=(llama strata kyojin dwarfstar gufo)
say "Engine selection"
echo "Available engines:"
printf '  %s\n' "${ALL_ENGINES[@]}"
read -r -p "Engines to configure (comma-separated, 'all', or empty for interactive prompts): " ENGINE_INPUT || ENGINE_INPUT=""
SELECTED=()
if [[ "$ENGINE_INPUT" == all || "$ENGINE_INPUT" == ALL ]]; then
  SELECTED=("${ALL_ENGINES[@]}")
else
  IFS=',' read -ra TOKENS <<< "$ENGINE_INPUT"
  if [[ ${#TOKENS[@]} -gt 0 && -n "${TOKENS[0]:-}" ]]; then
    for token in "${TOKENS[@]}"; do
      token="$(echo "$token" | xargs)"
      [[ " ${ALL_ENGINES[*]} " == *" $token "* ]] || { echo "Unknown engine: $token" >&2; exit 1; }
      SELECTED+=("$token")
    done
  else
    for engine in "${ALL_ENGINES[@]}"; do
      ask_yes_no "Configure $engine?" "no"
      [[ "$ANSWER" == yes ]] && SELECTED+=("$engine")
    done
    [[ ${#SELECTED[@]} -gt 0 ]] || { echo "At least one engine must be selected." >&2; exit 1; }
  fi
fi
echo "Selected engines: ${SELECTED[*]}"

# 2. Models directory
say "Models directory"
echo "IGM does not download models; it serves files already on this host."
while true; do
  ask "Directory where model files live (created if missing)" "/opt/models"
  MODELS_DIR="$ANSWER"
  if [[ -e "$MODELS_DIR" && ! -d "$MODELS_DIR" ]]; then
    echo "$MODELS_DIR exists and is not a directory." >&2
    continue
  fi
  mkdir -p "$MODELS_DIR" 2>/dev/null || { echo "Cannot create $MODELS_DIR (need write permission)." >&2; exit 1; }
  break
done

# 3. Recovery options
say "Recovery options"
echo "Both controls are opt-in, control-credential only, and confirm the exact action."
ask_yes_no "Expose gateway restart?" "no"; RESTART_GATEWAY="$ANSWER"
ask_yes_no "Expose host reboot (requires additional sudo setup per docs/recovery.md)?" "no"; REBOOT_HOST="$ANSWER"

# 4. MCP
say "Remote MCP adapter"
echo "The MCP adapter runs on client computers, not this host; this only installs the optional mcp extra."
ask_yes_no "Install the optional mcp Python extra (igm-mcp)?" "no"; INSTALL_MCP="$ANSWER"

# 5. System service / boot
say "Boot behaviour"
ask_yes_no "Install a systemd user service and enable it to start on power-on?" "yes"; INSTALL_SERVICE="$ANSWER"

# Summary
say "Summary"
echo "  Install dir:    $INSTALL_DIR"
echo "  Models dir:     $MODELS_DIR"
echo "  Engines:        ${SELECTED[*]}"
echo "  Gateway restart: $RESTART_GATEWAY"
echo "  Host reboot:    $REBOOT_HOST"
echo "  MCP extra:      $INSTALL_MCP"
echo "  Boot service:  $INSTALL_SERVICE (user service as '$SERVICE_USER')"
ask_yes_no "Proceed with installation?" "yes"
[[ "$ANSWER" == yes ]] || { echo "Aborted." >&2; exit 1; }

# Install
say "Installing files to $INSTALL_DIR"
if [[ "$(cd "$REPO_ROOT" && pwd)" != "$(cd "$INSTALL_DIR" && pwd)" ]]; then
  mkdir -p "$INSTALL_DIR"
  if command -v rsync >/dev/null; then
    rsync -a --exclude .git --exclude .venv --exclude .local --exclude .pytest_cache \
          "$REPO_ROOT"/ "$INSTALL_DIR"/
  else
    cp -a "$REPO_ROOT"/. "$INSTALL_DIR"/
    rm -rf "$INSTALL_DIR/.git" "$INSTALL_DIR/.venv" "$INSTALL_DIR/.local" "$INSTALL_DIR/.pytest_cache"
  fi
fi
mkdir -p "$INSTALL_DIR/examples"

say "Creating virtual environment and installing the gateway"
python3 -m venv "$VENV_DIR"
if [[ "$INSTALL_MCP" == yes ]]; then
  "$VENV_DIR/bin/pip" install --upgrade pip
  "$VENV_DIR/bin/pip" install "$INSTALL_DIR[mcp]"
else
  "$VENV_DIR/bin/pip" install --upgrade pip
  "$VENV_DIR/bin/pip" install "$INSTALL_DIR"
fi

say "Generating configuration"
CONFIG_ARGS=(--output "$CONFIG_FILE" --models-dir "$MODELS_DIR")
for engine in "${SELECTED[@]}"; do CONFIG_ARGS+=(--engine "$engine"); done
[[ "$RESTART_GATEWAY" == yes ]] && CONFIG_ARGS+=(--restart-gateway)
[[ "$REBOOT_HOST" == yes ]] && CONFIG_ARGS+=(--reboot-host)
cd "$INSTALL_DIR"
"$VENV_DIR/bin/python" installer/generate_config.py "${CONFIG_ARGS[@]}"

say "Generating credentials"
if [[ -f "$CREDS_FILE" ]]; then
  echo "Keeping existing $CREDS_FILE"
else
  mkdir -p "$INSTALL_DIR/.local"
  "$VENV_DIR/bin/python" -m gateway init-keys --output "$CREDS_FILE"
fi
mkdir -p "$STATE_DIR"

# Systemd user service
if [[ "$INSTALL_SERVICE" == yes ]]; then
  say "Installing systemd user service"
  UNIT_SRC="$INSTALL_DIR/examples/inference-gateway-manager.service"
  UNIT_DIR="$HOME/.config/systemd/user"
  UNIT_FILE="$UNIT_DIR/$SERVICE_NAME"
  mkdir -p "$UNIT_DIR"
  sed -e "s|/opt/inference-gateway-manager|$INSTALL_DIR|g" "$UNIT_SRC" > "$UNIT_FILE"
  systemctl --user daemon-reload
  systemctl --user enable "$SERVICE_NAME"
  echo "Enabled $SERVICE_NAME (user)."
  if ! loginctl show-user "$SERVICE_USER" 2>/dev/null | grep -q '^Linger=yes'; then
    echo "Note: to start at power-on without an interactive login, run:"
    echo "  sudo loginctl enable-linger $SERVICE_USER"
  fi
  ask_yes_no "Start the gateway service now?" "yes"
  if [[ "$ANSWER" == yes ]]; then
    systemctl --user start "$SERVICE_NAME"
    systemctl --user --no-pager status "$SERVICE_NAME" || true
  fi
else
  say "Skipping service installation"
  echo "Start manually with:"
  echo "  set -a; . $CREDS_FILE; set +a"
  echo "  $VENV_DIR/bin/python -m gateway serve --config $CONFIG_FILE --state-dir $STATE_DIR"
fi

say "Next steps"
echo "  1. Edit $CONFIG_FILE to set real engine paths, argv, model names and LAN access."
echo "  2. Review $CREDS_FILE (owner-only readable) for control/inference keys."
echo "  3. Copy your model files into $MODELS_DIR."
echo "  4. LAN use: bind a specific interface address, allowed_hosts and allowed_networks per README."
if [[ "$REBOOT_HOST" == yes ]]; then
  echo "  5. Host reboot needs the sudo helper from docs/recovery.md before it will work."
fi
echo "Dashboard: http://127.0.0.1:9000/"
