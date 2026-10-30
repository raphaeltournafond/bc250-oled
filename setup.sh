#!/usr/bin/env bash
# bc250-oled: one-command setup and update, run on the BC-250 (Bazzite, CachyOS, SteamOS, other systemd distros).
#
#   bash setup.sh                                   install, or update with the options used last time
#   bash setup.sh --preset 1.3 --rotate 180          install/update with new options (remembered)
#   bash setup.sh --fan                              also load the board's fan sensor driver (fan RPM, VRM temp)
#   bash setup.sh --uninstall                        stop it and remove what setup added
#
# Display options: python -m bc250_oled run --help   (e.g. --preset, --size, --driver, --bus, --rotate, --contrast,
#                                                     --pages, --night 23:00-07:30, --alert 90)
# Other flags: --no-autostart, --reset-options, --help.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
STATE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/bc250-oled"
OPTIONS_FILE="$STATE_DIR/options"
SERVICE=bc250-oled
UNIT="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$SERVICE.service"
VENV="$SRC/.venv"
UDEV_RULE=/etc/udev/rules.d/60-bc250-oled.rules
MODULES=/etc/modules-load.d/bc250-oled.conf
MODPROBE=/etc/modprobe.d/bc250-oled.conf

say()  { printf '\033[92m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[93m!! \033[0m %s\n' "$*" >&2; }
fail() { printf '\033[91mxx \033[0m %s\n' "$*" >&2; exit 1; }
ask()  { # ask "question" -> 0 for yes (default), 1 for no; non-interactive runs answer yes
  [[ -t 0 ]] || return 0
  local reply; read -r -p "$1 [Y/n] " reply
  [[ -z "$reply" || "$reply" =~ ^[Yy] ]]
}

# --- arguments --------------------------------------------------------------------------------------------------------
AUTOSTART=1 UNINSTALL=0 RESET_OPTIONS=0 FAN=0
RUN_OPTS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-autostart) AUTOSTART=0; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    --reset-options) RESET_OPTIONS=1; shift ;;
    --fan) FAN=1; shift ;;
    -h|--help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    --no-shift|--no-splash) RUN_OPTS+=("$1"); shift ;;
    --*) RUN_OPTS+=("$1" "${2:?$1 needs a value}"); shift 2 ;;
    *) fail "Unexpected argument: $1 (see --help)" ;;
  esac
done
mkdir -p "$STATE_DIR"

# --- uninstall ----------------------------------------------------------------------------------------------------------
if [[ "$UNINSTALL" == 1 ]]; then
  systemctl --user disable --now "$SERVICE" 2>/dev/null || true
  rm -f "$UNIT"
  systemctl --user daemon-reload 2>/dev/null || true
  if [[ -e "$UDEV_RULE" || -e "$MODULES" || -e "$MODPROBE" ]] && ask "Remove the system files setup added ($UDEV_RULE, $MODULES, $MODPROBE)?"; then
    sudo rm -f "$UDEV_RULE" "$MODULES" "$MODPROBE"
    sudo udevadm control --reload 2>/dev/null || true
  fi
  rm -rf "$VENV"
  say "Uninstalled (your options stay in $STATE_DIR; delete this folder to remove the rest)."
  exit 0
fi

# --- 1. remember options -------------------------------------------------------------------------------------------------
[[ "$RESET_OPTIONS" == 1 ]] && rm -f "$OPTIONS_FILE"
if [[ ${#RUN_OPTS[@]} -gt 0 ]]; then
  printf '%q ' "${RUN_OPTS[@]}" > "$OPTIONS_FILE"
  say "Options saved for next time: ${RUN_OPTS[*]}"
elif [[ -s "$OPTIONS_FILE" ]]; then
  eval "RUN_OPTS=($(cat "$OPTIONS_FILE"))"
  say "Using your saved options: ${RUN_OPTS[*]}"
fi
for opt in "${RUN_OPTS[@]}"; do
  [[ "$opt" =~ ^[A-Za-z0-9.,:x_+-]*$ ]] || fail "Option value '$opt' has characters the service can't take"
done

# --- 2. Python libraries, in a venv next to this script ---------------------------------------------------------------------
command -v python3 >/dev/null || fail "python3 not found"
if ! "$VENV/bin/python" -c "import luma.oled, smbus2" 2>/dev/null; then
  say "Installing the Python libraries in $VENV (private to this folder, nothing system-wide)"
  python3 -m venv "$VENV" || fail "python3 -m venv failed"
  "$VENV/bin/python" -m pip install --quiet --upgrade pip || true
fi
"$VENV/bin/python" -m pip install --quiet -r "$SRC/requirements.txt" || fail "Could not install the Python libraries"
PYTHON="$VENV/bin/python"
run() { (cd "$SRC" && "$PYTHON" -m bc250_oled "$@"); }
run run "${RUN_OPTS[@]}" --help >/dev/null || fail "Invalid display options: ${RUN_OPTS[*]}"

# --- 3. I2C access (the only system changes; each one asked first) -----------------------------------------------------------
if [[ ! -e /dev/i2c-0 ]] || [[ ! -s "$MODULES" ]] || ! grep -q '^i2c-dev' "$MODULES" 2>/dev/null; then
  if ask "Load the kernel's I2C device driver (i2c-dev) now and at every boot? (writes $MODULES)"; then
    sudo modprobe i2c-dev
    echo i2c-dev | sudo tee "$MODULES" >/dev/null
  fi
fi
GROUP="$(id -gn)"
RULE="SUBSYSTEM==\"i2c-dev\", GROUP=\"$GROUP\", MODE=\"0660\""
if [[ "$(cat "$UDEV_RULE" 2>/dev/null)" != "$RULE" ]]; then
  if ask "Let your account use the I2C buses (/dev/i2c-*, group '$GROUP')? (writes $UDEV_RULE)"; then
    echo "$RULE" | sudo tee "$UDEV_RULE" >/dev/null
    sudo udevadm control --reload
    sudo udevadm trigger --subsystem-match=i2c-dev
    sleep 1
  fi
fi

# --- 4. fan sensor (optional) ---------------------------------------------------------------------------------------------
if [[ "$FAN" == 1 ]]; then
  if grep -qsE '^(nct6683|nct6686|nct6687)$' /sys/class/hwmon/hwmon*/name; then
    say "Fan sensor driver already loaded"
  elif ask "Load the Super I/O sensor driver for fan RPM (nct6683 with force=1: it only reads)? (writes $MODPROBE)"; then
    echo "options nct6683 force=1" | sudo tee "$MODPROBE" >/dev/null
    printf 'i2c-dev\nnct6683\n' | sudo tee "$MODULES" >/dev/null
    sudo modprobe nct6683 force=1 || warn "nct6683 did not load: the fan reading stays empty"
  fi
fi

# --- 5. find the display (read-only) ---------------------------------------------------------------------------------------
say "Looking for the display (read-only: one byte read at 0x3C and 0x3D on each bus)"
if ! run detect; then
  warn "No display found yet: see 'How to connect' in the README. The service keeps looking, so once it is wired"
  warn "it shows up by itself (or run: bash $SRC/setup.sh again)."
fi

# --- 6. start at boot -------------------------------------------------------------------------------------------------------
if [[ "$AUTOSTART" == 1 ]]; then
  say "Setting up the display service"
  mkdir -p "$(dirname "$UNIT")"
  cat > "$UNIT" <<EOF
[Unit]
Description=BC-250 stats on an I2C OLED display
StartLimitIntervalSec=0

[Service]
Type=simple
WorkingDirectory=$SRC
ExecStart=$PYTHON -m bc250_oled run ${RUN_OPTS[*]}
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable "$SERVICE" >/dev/null 2>&1
  systemctl --user restart "$SERVICE"
  loginctl enable-linger "$(id -un)" 2>/dev/null \
    || warn "Could not enable lingering: the display starts when you log in (sudo loginctl enable-linger $(id -un))"
  sleep 4
  if systemctl --user is-active --quiet "$SERVICE"; then
    say "Running."
  else
    warn "The service is not running. Its last log lines:"
    journalctl --user -u "$SERVICE" -n 30 --no-pager -o cat >&2 || true
  fi
fi

cat <<EOF

$(say "All set.")
  Log:            journalctl --user -u $SERVICE -f
  Update later:   git -C "$SRC" pull && bash "$SRC/setup.sh"
  Try a layout:   cd "$SRC" && .venv/bin/python -m bc250_oled preview --preset 0.91 --out preview.png
EOF
