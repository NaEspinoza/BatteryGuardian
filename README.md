# Battery Guardian — Notificador de batería (minimal, ligero)

**Autor:** Nazareno Espinoza
**Licencia:** Apache License 2.0

Un proyecto pequeño y funcional para notificar cuando la batería cruza umbrales (por ejemplo **80%** para desconectar y **20%** para conectar). Diseñado para ser fácil de integrar, seguro y con consumo mínimo de recursos: daemon en Python (solo stdlib) + integración opcional con **Telegram** para notificaciones fiables cuando la pantalla está apagada.

---

## Características clave

* Daemon ligero sin dependencias externas (solo Python stdlib).
* Notificaciones en cascada: `notify-send` (GUI), sonido local y **Telegram** (fallback fiable).
* Estado anti-spam: notifica una vez por cruce de umbral y espera volver al rango neutral.
* Configuración por archivo `.env` o variables de entorno.
* Instalación y gestión con `systemd --user` (unidad y timer disponibles).

---

## Estructura mínima del repo

```
battery-guardian/
├─ battery_daemon.py        # Daemon principal
├─ install.sh               # Instalador (crea bin, .env, systemd user unit)
├─ .env.example             # Ejemplo de configuración
├─ systemd/                 # (opcional) unidades .service / .timer
└─ README.md
```

---

## Requisitos

* Ubuntu / Linux con `/sys/class/power_supply` (laptops estándar).
* Python 3.8+ (viene por defecto en Ubuntu 24.04).
* `notify-send` para notificaciones GUI (paquete `libnotify-bin`), opcional.
* (Opcional) Token y chat_id de Telegram para recibir alertas aunque la sesión gráfica esté apagada.

---

## Instalación rápida (ejemplo)

Desde el directorio del proyecto:

```bash
# marcar instalador ejecutable y simular
chmod +x install.sh
./install.sh --dry-run

# instalar de verdad
./install.sh

# (opcional) permitir que el servicio siga activo tras logout
# requiere sudo:
sudo loginctl enable-linger "$USER"
```

Después de instalar, verifica el servicio user-systemd:

```bash
systemctl --user daemon-reload
systemctl --user enable --now battery-guardian.service
systemctl --user status battery-guardian.service --no-pager
journalctl --user -u battery-guardian.service -f
```

---

## Configuración (.env)

Copia el ejemplo y edítalo:

```bash
mkdir -p ~/.config/battery_guardian
cp .env.example ~/.config/battery_guardian/.env
chmod 600 ~/.config/battery_guardian/.env
```

Ejemplo de contenido mínimo (`~/.config/battery_guardian/.env`):

```env
# Telegram (opcional)
TELEGRAM_BOT_TOKEN=123456:ABCdefGh...
TELEGRAM_CHAT_ID=987654321

# Umbrales
HIGH=80
LOW=20

# Poll interval (segundos)
POLL_INTERVAL=60

# Rutas (valores por defecto)
STATE_DIR=${XDG_STATE_HOME:-$HOME/.local/state}/battery_guardian
LOGFILE=${XDG_DATA_HOME:-$HOME/.local/share}/battery_guardian.log
```

**Importante:** Mantener permisos `600` en el archivo `.env` para proteger tokens.

---

## Uso — comandos útiles

Probar notificaciones sin instalar el servicio:

```bash
# simular alerta de alto (test)
~/.local/bin/battery_daemon.py --config ~/.config/battery_guardian/.env --test-high

# simular alerta de bajo (test)
~/.local/bin/battery_daemon.py --config ~/.config/battery_guardian/.env --test-low

# ejecutar una sola vez (check real)
~/.local/bin/battery_daemon.py --once

# ejecutar en modo daemon (loop)
~/.local/bin/battery_daemon.py
```

Logs por defecto en `~/.local/share/battery_guardian.log` o en el `LOGFILE` indicado en `.env`. Para ver logs del servicio (systemd user):

```bash
journalctl --user -u battery-guardian.service -f
```

---

## Cómo funciona (resumen técnico)

1. El daemon lee `/sys/class/power_supply/*` y calcula el porcentaje de batería.
2. Decide el **estado** (`Charging` / `Discharging` / `Full`) a partir de los archivos `status`.
3. Si se cumplen condiciones (≥ `HIGH` mientras carga, o ≤ `LOW` mientras descarga), ejecuta notificaciones en cascada:

   * `notify-send` (si hay sesión gráfica activa),
   * reproducción de sonido (paplay/canberra/aplay o campana de terminal),
   * envío HTTP a la API de Telegram si `TELEGRAM_BOT_TOKEN` y `CHAT_ID` están configurados.
4. Guarda un `state` (`notified_high` / `notified_low`) para evitar spam hasta que la carga vuelva al rango neutral (`LOW < pct < HIGH`).
5. Bucles en `POLL_INTERVAL` segundos, con manejo de señales SIGINT/SIGTERM para cierre ordenado.

---

## Troubleshooting rápido

* **No se muestran notificaciones con pantalla apagada:** configurar Telegram en `.env` (es el más fiable).
* **`systemctl --user` no funciona en SSH sin sesión:** habilitar linger (`sudo loginctl enable-linger $USER`) o usar otra estrategia (tmux/cron).
* **Permisos .env:** `chmod 600 ~/.config/battery_guardian/.env`.
* **No se encuentra batería:** comprobar `/sys/class/power_supply` y ejecutar `~/.local/bin/battery_daemon.py --once` para ver errores.

---

## Seguridad y buenas prácticas

* No subir `.env` al repositorio. Usa `.env.example`.
* Para producción, considera usar un secret manager o `gnome-keyring` en lugar de archivo plano.
* El código está deliberadamente sin dependencias externas para facilitar auditoría y despliegue.

---

## Licencia

Este proyecto está licenciado bajo la **Apache License 2.0**.
Ver el texto completo en: [https://www.apache.org/licenses/LICENSE-2.0](https://www.apache.org/licenses/LICENSE-2.0)
