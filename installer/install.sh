#!/usr/bin/env bash
# Fresh Linux host setup. Does not install engines, download weights or use sudo.
set -euo pipefail
umask 077
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR="${IGM_INSTALL_DIR:-$HOME/.local/share/inference-gateway-manager}"
SERVICE_NAME=inference-gateway-manager.service
say() { printf '\n==> %s\n' "$*"; }
ask() {
  local reply
  read -r -p "$1 [$2] " reply || { echo "Input ended; installation cancelled." >&2; exit 1; }
  ANSWER="${reply:-$2}"
}
ask_yes_no() {
  while true; do
    ask "$1 (y/n)" "${2:-no}"
    case "$ANSWER" in
      y|Y|yes|YES) ANSWER=yes; return ;;
      n|N|no|NO) ANSWER=no; return ;;
    esac
  done
}
[[ $EUID -ne 0 ]] || { echo "Run as the unprivileged engine-owning user, not root." >&2; exit 1; }
[[ $(uname -s) == Linux ]] || { echo "The inference host requires Linux." >&2; exit 1; }
command -v python3 >/dev/null || { echo "Python 3.11+ is required." >&2; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)'
say "Fresh Inference Gateway Manager installation"
echo "Engines and weights are installed separately. Existing installations are never overwritten."
ask "Install directory" "$INSTALL_DIR"; INSTALL_DIR="$ANSWER"
# Normalize before deriving any paths. The helper also rejects unsafe unit paths.
INSTALL_DIR="$(python3 -c 'import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); from setup_files import validate_path; print(validate_path(sys.argv[2]))' "$REPO_ROOT/installer" "$INSTALL_DIR")"
python3 "$REPO_ROOT/installer/setup_files.py" check --source "$REPO_ROOT" --target "$INSTALL_DIR"
VENV_DIR="$INSTALL_DIR/.venv"
CREDS_FILE="$INSTALL_DIR/.local/credentials.env"
CONFIG_FILE="$INSTALL_DIR/config.local.json"
STATE_DIR="$INSTALL_DIR/.local/state"

say "Engine selections"
ENGINE_LIST="$(python3 "$REPO_ROOT/installer/generate_config.py" --list-engines)"
mapfile -t ALL_ENGINES <<< "$ENGINE_LIST"
printf '  %s\n' "${ALL_ENGINES[@]}"
echo "ds4 keeps its dwarfstar ID; ds4a is the separate ds4fa fork."
ask "Engines (comma-separated IDs, all, or empty to choose individually)" ""
ENGINE_INPUT="$ANSWER"
SELECTED=()
if [[ "$ENGINE_INPUT" == all || "$ENGINE_INPUT" == ALL ]]; then
  SELECTED=("${ALL_ENGINES[@]}")
elif [[ -n "$ENGINE_INPUT" ]]; then
  IFS=',' read -ra TOKENS <<< "$ENGINE_INPUT"
  for token in "${TOKENS[@]}"; do
    token="${token//[[:space:]]/}"
    [[ -n "$token" && " ${ALL_ENGINES[*]} " == *" $token "* ]] || { echo "Unknown engine: $token" >&2; exit 1; }
    SELECTED+=("$token")
  done
else
  for engine in "${ALL_ENGINES[@]}"; do
    ask_yes_no "Configure $engine?" no
    if [[ "$ANSWER" == yes ]]; then SELECTED+=("$engine"); fi
  done
fi
[[ ${#SELECTED[@]} -gt 0 ]] || { echo "Select at least one engine." >&2; exit 1; }
ask "Models directory (created after confirmation)" "$HOME/models"; MODELS_DIR="$ANSWER"
MODELS_DIR="$(python3 -c 'import sys; from pathlib import Path; p=Path(sys.argv[1]).expanduser(); assert p.is_absolute(), "Models directory must be absolute"; print(p.resolve())' "$MODELS_DIR")"
[[ ! -e "$MODELS_DIR" || -d "$MODELS_DIR" ]] || { echo "Models path is not a directory." >&2; exit 1; }

say "Gateway access"
BIND_IP=127.0.0.1
NETWORK=127.0.0.0/8
EXTRA_HOST=""
ask_yes_no "Allow clients on your trusted LAN?" no
if [[ "$ANSWER" == yes ]]; then
  command -v ip >/dev/null && ip -brief address show scope global || true
  ask "Specific private interface IP to bind (no wildcard)" ""; BIND_IP="$ANSWER"
  ask "Allowed client subnet in CIDR notation (must include that IP)" ""; NETWORK="$ANSWER"
  ask "Additional gateway hostname clients will use (optional, no URL or port)" ""; EXTRA_HOST="$ANSWER"
fi

say "Startup and recovery"
echo "Choose none for manual startup, user for a user service, or system for prepared administrator commands."
echo "System mode supports boot-before-login and optional recovery; it does not run sudo."
while true; do
  ask "Service mode: none, user, system" none
  SERVICE_MODE="$ANSWER"
  [[ "$SERVICE_MODE" == none || "$SERVICE_MODE" == user || "$SERVICE_MODE" == system ]] && break
done
RESTART_GATEWAY=no
REBOOT_HOST=no
if [[ "$SERVICE_MODE" == system ]]; then
  echo "Recovery also requires the root-owned helper and exact sudoers permissions in docs/recovery.md."
  ask_yes_no "Configure gateway restart capability (requires administrator setup)?" no; RESTART_GATEWAY="$ANSWER"
  ask_yes_no "Configure host reboot capability (interrupts every host workload)?" no; REBOOT_HOST="$ANSWER"
else
  echo "Recovery API controls stay disabled; they require a system service and administrator setup."
fi
UNIT_FILE="$INSTALL_DIR/.local/$SERVICE_NAME"
if [[ "$SERVICE_MODE" == user ]]; then
  USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  USER_UNIT="$USER_UNIT_DIR/$SERVICE_NAME"
  [[ ! -e "$USER_UNIT" && ! -L "$USER_UNIT" ]] || { echo "Existing user unit; refusing to overwrite it." >&2; exit 1; }
fi
say "Installation summary"
printf 'Install: %s\nModels: %s\nEngines: %s\nBind IP: %s\nAllowed network: %s\nService: %s\nGateway restart: %s\nHost reboot: %s\n' \
  "$INSTALL_DIR" "$MODELS_DIR" "${SELECTED[*]}" "$BIND_IP" "$NETWORK" "$SERVICE_MODE" "$RESTART_GATEWAY" "$REBOOT_HOST"
printf 'Additional allowed host: %s\n' "${EXTRA_HOST:-none}"
echo "All example models stay disabled. Configure installed engine paths, register weights, then explicitly load."
echo "MCP belongs on a separate client computer; no host MCP extra is installed."
ask_yes_no "Proceed with installation?" no
[[ "$ANSWER" == yes ]] || { echo "Cancelled."; exit 0; }

# Validate all proposed config values before touching destination files or installing dependencies.
python3 - "$REPO_ROOT" "$MODELS_DIR" "$BIND_IP" "$NETWORK" "$RESTART_GATEWAY" "$REBOOT_HOST" "$EXTRA_HOST" "${SELECTED[@]}" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from installer.generate_config import validate_inputs
try:
    validate_inputs(sys.argv[8:], sys.argv[2], sys.argv[3], sys.argv[4], [sys.argv[7]] if sys.argv[7] else [])
except ValueError as error:
    raise SystemExit(str(error))
PY
say "Installing public files and gateway dependencies"
python3 "$REPO_ROOT/installer/setup_files.py" copy --source "$REPO_ROOT" --target "$INSTALL_DIR"
mkdir -p "$MODELS_DIR"
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install "$INSTALL_DIR"
CONFIG_ARGS=(--output "$CONFIG_FILE" --models-dir "$MODELS_DIR" --host "$BIND_IP" --allowed-network "$NETWORK")
if [[ -n "$EXTRA_HOST" ]]; then CONFIG_ARGS+=(--allowed-host "$EXTRA_HOST"); fi
for engine in "${SELECTED[@]}"; do CONFIG_ARGS+=(--engine "$engine"); done
if [[ "$RESTART_GATEWAY" == yes ]]; then CONFIG_ARGS+=(--restart-gateway); fi
if [[ "$REBOOT_HOST" == yes ]]; then CONFIG_ARGS+=(--reboot-host); fi
"$VENV_DIR/bin/python" "$INSTALL_DIR/installer/generate_config.py" "${CONFIG_ARGS[@]}"
"$VENV_DIR/bin/python" -m gateway init-keys --output "$CREDS_FILE"
mkdir -p "$STATE_DIR"

if [[ "$SERVICE_MODE" != none ]]; then
  UNIT_ARGS=()
  if [[ "$SERVICE_MODE" == system ]]; then UNIT_ARGS+=(--system); fi
  python3 "$INSTALL_DIR/installer/setup_files.py" unit --source "$INSTALL_DIR" --target "$INSTALL_DIR" --output "$UNIT_FILE" "${UNIT_ARGS[@]}"
  if [[ "$SERVICE_MODE" == user ]]; then
    mkdir -p "$USER_UNIT_DIR"
    # Exclusive creation refuses a unit that appeared after the initial check.
    python3 - "$UNIT_FILE" "$USER_UNIT" <<'PY'
import sys
from pathlib import Path
with Path(sys.argv[2]).open('x') as output:
    output.write(Path(sys.argv[1]).read_text())
PY
    systemctl --user daemon-reload
    systemctl --user enable "$SERVICE_NAME"
    echo "User unit enabled, not started. For boot without login, an administrator can run:"
    printf '  sudo loginctl enable-linger %q\n' "$(id -un)"
    printf 'After reviewing the config, start with: systemctl --user start %q\n' "$SERVICE_NAME"
  else
    echo "Prepared a system unit running as the current unprivileged account. Administrator commands:"
    printf '  sudo install -m 0644 %q /etc/systemd/system/%q\n' "$UNIT_FILE" "$SERVICE_NAME"
    echo "  sudo systemctl daemon-reload"
    printf '  sudo systemctl enable %q\n' "$SERVICE_NAME"
    echo "After reviewing config, storage available before login, permissions and conflicting services:"
    printf '  sudo systemctl start %q\n' "$SERVICE_NAME"
    echo "Recovery helper/sudoers setup is separate: docs/recovery.md. Do not run both system and user units."
  fi
fi
say "Next steps"
printf 'Configuration: %s\nPrivate keys: %s\n' "$CONFIG_FILE" "$CREDS_FILE"
echo "Review executable paths and model artifacts; register compatible weights in the dashboard."
echo "The control key manages lifecycle; give inference clients only the inference key. Never share the credential file."
if [[ "$BIND_IP" == *:* ]]; then ORIGIN="http://[$BIND_IP]:9000"; else ORIGIN="http://$BIND_IP:9000"; fi
printf 'Dashboard and MCP gateway URL: %s\nInference API base URL: %s/v1\n' "$ORIGIN" "$ORIGIN"
echo "Set up MCP on a separate client computer using docs/mcp-registration.md."
echo "Manual start (use only when no service owns this gateway):"
printf '  set -a; . %q; set +a\n' "$CREDS_FILE"
printf '  %q -m gateway serve --config %q --state-dir %q\n' "$VENV_DIR/bin/python" "$CONFIG_FILE" "$STATE_DIR"
