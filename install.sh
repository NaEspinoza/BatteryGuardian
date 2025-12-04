#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

# install.sh - instala battery_daemon.py como daemon user (systemd --user)
# Uso:
#   ./install.sh            # instalación estándar (no enable-linger)
#   ./install.sh --dry-run  # muestra acciones sin ejecutarlas
#   ./install.sh --force    # sobrescribe sin preguntar (hace backup antes)
#   ./install.sh --enable-linger  # intenta activar linger para que el servicio sobreviva logout (requiere sudo)

PROG_NAME="battery_daemon.py"
SERVICE_NAME="battery-guardian.service"
USER_CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/battery_guardian"
ENV_FILE="${USER_CONFIG_DIR}/.env"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/battery_guardian"
BIN_DIR="${HOME}/.local/bin"
TARGET_BIN="${BIN_DIR}/${PROG_NAME}"
SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"
SERVICE_PATH="${SYSTEMD_USER_DIR}/${SERVICE_NAME}"

DRY_RUN=0
FORCE=0
ENABLE_LINGER=0

timestamp() { date +"%Y%m%dT%H%M%S"; }

log() { printf '%s %s\n' "$(date --iso-8601=seconds)" "$*"; }

usage(){
  cat <<EOF
Instalador minimal para Battery Guardian (user systemd)
Opciones:
  --dry-run         Muestra acciones sin hacer cambios
  --force           Forzar instalación (hace backup de archivos existentes)
  --enable-linger   (opcional) Ejecuta 'sudo loginctl enable-linger $USER' para persistencia tras logout
  -h|--help         Muestra esta ayuda
EOF
}

# Parse args
while [[ ${#@} -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --force) FORCE=1; shift ;;
    --enable-linger) ENABLE_LINGER=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Opción desconocida: $1"; usage; exit 2 ;;
  esac
done

run() {
  if [[ $DRY_RUN -eq 1 ]]; then
    log "[DRY-RUN] $*"
  else
    log "$*"
    eval "$@"
  fi
}

abort() {
  echo "Aborting: $*" >&2
  exit 1
}

# Local repo detection: busca battery_daemon.py al lado del instalador si existe,
# sino usa el path instalado si ya está.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_CANDIDATE="${SCRIPT_DIR}/${PROG_NAME}"

if [[ -f "${SRC_CANDIDATE}" ]]; then
  SRC="${SRC_CANDIDATE}"
elif [[ -f "${PWD}/${PROG_NAME}" ]]; then
  SRC="${PWD}/${PROG_NAME}"
elif [[ -f "${TARGET_BIN}" ]]; then
  SRC="${TARGET_BIN}"
else
  abort "No se encontró ${PROG_NAME} en el repo ni en ${TARGET_BIN}. Coloca ${PROG_NAME} en el directorio del instalador o en el cwd."
fi

log "Fuente detectada: ${SRC}"
log "Objetivo bin: ${TARGET_BIN}"
log "Env target: ${ENV_FILE}"
log "Service target: ${SERVICE_PATH}"

# Pre-flight: comprobar systemctl --user disponible
if ! systemctl --user --version >/dev/null 2>&1; then
  log "Advertencia: systemctl --user parece no estar disponible o no funciona correctamente en este entorno."
  log "El servicio se instalará, pero la activación automática puede fallar."
fi

# Crear dirs
run "mkdir -p \"${BIN_DIR}\""
run "mkdir -p \"${USER_CONFIG_DIR}\""
run "mkdir -p \"${STATE_DIR}\""
run "mkdir -p \"${SYSTEMD_USER_DIR}\""
run "mkdir -p \"$(dirname "${ENV_FILE}")\""

# Backup existing binary if present
if [[ -f "${TARGET_BIN}" ]]; then
  BK="${TARGET_BIN}.bak.$(timestamp)"
  if [[ $FORCE -eq 1 ]]; then
    run "mv -f \"${TARGET_BIN}\" \"${BK}\""
    log "Backup del bin existente en ${BK}"
  else
    run "cp -a \"${TARGET_BIN}\" \"${BK}\""
    log "Backup del bin existente en ${BK}"
  fi
fi

# Install binary
run "install -m 755 \"${SRC}\" \"${TARGET_BIN}\""

# Create .env if missing (secure defaults / placeholders)
if [[ ! -f "${ENV_FILE}" ]]; then
  log "No existe ${ENV_FILE} — se crea con placeholders."
  if [[ $DRY_RUN -eq 1 ]]; then
    log "[DRY-RUN] crear ${ENV_FILE} con contenido de ejemplo."
  else
    cat > "${ENV_FILE}" <<'EOF'
# Battery Guardian configuration (secure file)
# Edit values below. Keep file permissions 600.
# TELEGRAM (opcional)
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# Umbrales (porcentaje)
HIGH=80
LOW=20

# Poll interval en segundos (daemon)
POLL_INTERVAL=60

# Rutas (valores por defecto recomendados)
STATE_DIR=${XDG_STATE_HOME:-$HOME/.local/state}/battery_guardian
LOGFILE=${XDG_DATA_HOME:-$HOME/.local/share}/battery_guardian.log
EOF
    run "chmod 600 \"${ENV_FILE}\""
    log "Archivo ${ENV_FILE} creado con permisos 600. Rellená TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID si querés notificaciones remotas."
  fi
else
  log "${ENV_FILE} ya existe — no será sobrescrito."
  if [[ $FORCE -eq 1 ]]; then
    BACK_ENV="${ENV_FILE}.bak.$(timestamp)"
    run "cp -a \"${ENV_FILE}\" \"${BACK_ENV}\""
    log "Backup de env existente en ${BACK_ENV}"
  fi
fi

# Create systemd user service
SERVICE_CONTENT="[Unit]
Description=Battery Guardian (user daemon)
After=network.target

[Service]
Type=simple
EnvironmentFile=%h/.config/battery_guardian/.env
ExecStart=%h/.local/bin/${PROG_NAME} --config %h/.config/battery_guardian/.env
Restart=on-failure
RestartSec=30
KillMode=process
# Limit resources modestly
LimitNOFILE=4096
# Optional: keep logs to journal; the script also writes to LOGFILE configured in .env

[Install]
WantedBy=default.target
"

# Backup existing service file
if [[ -f "${SERVICE_PATH}" ]]; then
  SVC_BK="${SERVICE_PATH}.bak.$(timestamp)"
  run "cp -a \"${SERVICE_PATH}\" \"${SVC_BK}\""
  log "Backup de unit existente en ${SVC_BK}"
fi

if [[ $DRY_RUN -eq 1 ]]; then
  log "[DRY-RUN] escribir unit en ${SERVICE_PATH}"
else
  printf '%s' "${SERVICE_CONTENT}" > "${SERVICE_PATH}"
  log "Unit escrita en ${SERVICE_PATH}"
fi

# Reload user systemd, enable and start service
run "systemctl --user daemon-reload"
run "systemctl --user enable --now ${SERVICE_NAME}"
log "Intentando habilitar y arrancar ${SERVICE_NAME} (user systemd)."

# Optionally enable linger so the service can survive logout (requires sudo)
if [[ $ENABLE_LINGER -eq 1 ]]; then
  if command -v sudo >/dev/null 2>&1; then
    log "Intentando enable-linger para el usuario actual (necesita sudo): sudo loginctl enable-linger \$USER"
    if [[ $DRY_RUN -eq 1 ]]; then
      log "[DRY-RUN] sudo loginctl enable-linger \$USER"
    else
      sudo loginctl enable-linger "${USER}" || log "No se pudo enable-linger (ver permisos)."
    fi
  else
    log "No se encontró sudo: no puedo activar linger automáticamente."
  fi
fi

# Final checks
log "Instalación completada. Estado del servicio:"
systemctl --user status ${SERVICE_NAME} --no-pager || true

cat <<EOF

Resumen:
- Binary: ${TARGET_BIN}
- Config: ${ENV_FILE}  (perm: 600)
- State dir: ${STATE_DIR}
- Systemd user unit: ${SERVICE_PATH}
- Para ver logs: journalctl --user -u ${SERVICE_NAME} -f
- Para probar manualmente: ${TARGET_BIN} --test-high  (o --test-low)
- Si querés que el servicio sobreviva al logout, ejecuta: sudo loginctl enable-linger \$USER  (opcional)

EOF

exit 0
