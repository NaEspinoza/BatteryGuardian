#!/usr/bin/env python3
# battery_daemon.py
# Minimal, robust battery notifier daemon.
# - No dependencias externas (usa stdlib).
# - Notificaciones: notify-send, sonido (paplay/canberra/aplay/bell), Telegram (HTTP).
# - Configurable vía .env file or variables de entorno.
# - Lightweight daemon loop, handles SIGINT/SIGTERM cleanly.
# - Avoids repeated notifications until battery re-enters neutral range.

from __future__ import annotations
import os
import sys
import time
import signal
import errno
import shutil
import subprocess
import logging
from typing import List, Tuple, Optional, Dict
from urllib import request, parse
import json

# -----------------------
# Defaults & paths
# -----------------------
HOME = os.path.expanduser("~")
XDG_STATE_HOME = os.getenv("XDG_STATE_HOME", os.path.join(HOME, ".local", "state"))
XDG_DATA_HOME = os.getenv("XDG_DATA_HOME", os.path.join(HOME, ".local", "share"))
DEFAULT_STATE_DIR = os.path.join(XDG_STATE_HOME, "battery_guardian")
DEFAULT_LOGFILE = os.path.join(XDG_DATA_HOME, "battery_guardian.log")
DEFAULT_ENV_PATH = os.path.join(os.getenv("XDG_CONFIG_HOME", os.path.join(HOME, ".config")), "battery_guardian", ".env")

STATE_FILE = os.path.join(DEFAULT_STATE_DIR, "state")
LOCKFILE = os.path.join(DEFAULT_STATE_DIR, "lock")
PIDFILE = os.path.join(DEFAULT_STATE_DIR, "pid")

# -----------------------
# Simple env loader
# -----------------------
def load_env_file(path: str) -> Dict[str, str]:
    env: Dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                env[k] = v
    except FileNotFoundError:
        pass
    return env

# -----------------------
# Configuration
# -----------------------
def load_config(env_path: Optional[str]) -> Dict[str, str]:
    cfg = dict(os.environ)  # base from environment
    if env_path:
        cfg.update(load_env_file(env_path))
    # fallback defaults
    cfg.setdefault("HIGH", "80")
    cfg.setdefault("LOW", "20")
    cfg.setdefault("POLL_INTERVAL", "60")
    cfg.setdefault("STATE_DIR", DEFAULT_STATE_DIR)
    cfg.setdefault("LOGFILE", DEFAULT_LOGFILE)
    # Telegram optional
    cfg.setdefault("TELEGRAM_BOT_TOKEN", "")
    cfg.setdefault("TELEGRAM_CHAT_ID", "")
    return cfg

# -----------------------
# Logging
# -----------------------
def setup_logging(path: str, verbose: bool = False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(path),
            logging.StreamHandler(sys.stdout)
        ]
    )

# -----------------------
# Battery discovery & reading (handles multiple batteries)
# Weighted by energy_full/charge_full when available for accuracy.
# -----------------------
def find_battery_paths() -> List[str]:
    base = "/sys/class/power_supply"
    if not os.path.isdir(base):
        return []
    bats = []
    for entry in os.listdir(base):
        p = os.path.join(base, entry)
        # must have capacity file
        if os.path.isdir(p) and os.path.exists(os.path.join(p, "capacity")):
            bats.append(p)
    return bats

def read_int_file(path: str) -> Optional[int]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except Exception:
        return None

def read_float_file(path: str) -> Optional[float]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return float(f.read().strip())
    except Exception:
        return None

def battery_info() -> Tuple[int, str]:
    """
    Returns (percentage:int 0..100, status:str)
    Status values: Charging, Discharging, Full, Unknown
    """
    paths = find_battery_paths()
    if not paths:
        raise RuntimeError("No battery devices found under /sys/class/power_supply")

    total_energy = 0.0
    total_capacity_weight = 0.0
    statuses = []
    capacities = []

    for p in paths:
        # Try to get best measurement: energy_now/energy_full or charge_now/charge_full
        energy_now = read_float_file(os.path.join(p, "energy_now")) or read_float_file(os.path.join(p, "charge_now"))
        energy_full = read_float_file(os.path.join(p, "energy_full")) or read_float_file(os.path.join(p, "charge_full"))
        capacity = read_int_file(os.path.join(p, "capacity"))  # fallback percentage
        status = None
        for fn in ("status",):
            s = None
            try:
                with open(os.path.join(p, fn), "r", encoding="utf-8") as f:
                    s = f.read().strip()
            except Exception:
                s = None
            if s:
                status = s
                break
        if status:
            statuses.append(status)
        if energy_now is not None and energy_full:
            # weighted by energy_full
            try:
                pct = (energy_now / energy_full) * 100.0
            except Exception:
                pct = float(capacity) if capacity is not None else 0.0
            total_energy += pct * (energy_full)
            total_capacity_weight += energy_full
        elif capacity is not None:
            # fallback to capacity with weight 1
            capacities.append(capacity)
            total_energy += capacity
            total_capacity_weight += 1.0
        else:
            # ultimate fallback: 0
            total_energy += 0.0
            total_capacity_weight += 1.0

    overall_pct = 0
    if total_capacity_weight > 0:
        overall_pct = int(round(total_energy / total_capacity_weight))
    else:
        overall_pct = capacities[0] if capacities else 0

    # Determine overall status: Charging > Discharging > Full > Unknown
    overall_status = "Unknown"
    if any(s.lower() == "charging" for s in statuses):
        overall_status = "Charging"
    elif any(s.lower() == "discharging" for s in statuses):
        overall_status = "Discharging"
    elif any(s.lower() == "full" for s in statuses):
        overall_status = "Full"
    else:
        if statuses:
            overall_status = statuses[0]

    return overall_pct, overall_status

# -----------------------
# Notifications (popup, sound, telegram)
# -----------------------
def notify_send(message: str) -> bool:
    # Try to find DBUS session bus address
    bus = os.getenv("DBUS_SESSION_BUS_ADDRESS")
    if not bus:
        # try default path for user session
        candidate = f"unix:path=/run/user/{os.getuid()}/bus"
        if os.path.exists(f"/run/user/{os.getuid()}/bus"):
            os.environ["DBUS_SESSION_BUS_ADDRESS"] = candidate
    try:
        subprocess.run(["notify-send", "Battery Guardian", message], check=False)
        logging.debug("notify-send attempted")
        return True
    except FileNotFoundError:
        logging.debug("notify-send not found")
        return False
    except Exception as e:
        logging.debug("notify-send error: %s", e)
        return False

def notify_sound() -> bool:
    # prefer paplay, then canberra-gtk-play, then aplay, then bell
    sounds = [
        (["paplay", "/usr/share/sounds/freedesktop/stereo/complete.oga"], "paplay"),
        (["canberra-gtk-play", "-f", "/usr/share/sounds/freedesktop/stereo/complete.oga"], "canberra-gtk-play"),
        (["aplay", "/usr/share/sounds/alsa/Front_Center.wav"], "aplay"),
    ]
    for cmd, name in sounds:
        try:
            subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            logging.debug("sound attempted with %s", name)
            return True
        except FileNotFoundError:
            continue
        except Exception:
            continue
    # fallback: terminal bell
    try:
        sys.stdout.write("\a")
        sys.stdout.flush()
        logging.debug("terminal bell fallback")
        return True
    except Exception:
        return False

def notify_telegram(token: str, chat_id: str, text: str) -> bool:
    if not token or not chat_id:
        return False
    api = f"https://api.telegram.org/bot{token}/sendMessage"
    data = {"chat_id": chat_id, "text": text}
    try:
        data_encoded = parse.urlencode(data).encode()
        req = request.Request(api, data=data_encoded, method="POST")
        with request.urlopen(req, timeout=6) as resp:
            body = resp.read()
            # quick sanity check
            try:
                j = json.loads(body.decode("utf-8"))
                ok = j.get("ok", False)
                logging.debug("telegram response ok=%s", ok)
                return bool(ok)
            except Exception:
                logging.debug("telegram raw response: %s", body)
                return True
    except Exception as e:
        logging.debug("telegram send failed: %s", e)
        return False

# -----------------------
# State handling
# -----------------------
def read_state(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return "none"

def write_state(path: str, value: str):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(value)
    os.replace(tmp, path)

def clear_state(path: str):
    try:
        os.remove(path)
    except Exception:
        pass

# -----------------------
# Daemon & CLI
# -----------------------
RUNNING = True

def handle_signal(signum, frame):
    global RUNNING
    logging.info("Signal %s received, shutting down.", signum)
    RUNNING = False

signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)

def one_iteration(cfg: Dict[str, str], force: Optional[str] = None):
    """
    Performs one read and maybe-notify action.
    force: None | "force-high" | "force-low"
    """
    low = int(cfg.get("LOW", "20"))
    high = int(cfg.get("HIGH", "80"))
    token = cfg.get("TELEGRAM_BOT_TOKEN", "")
    chat = cfg.get("TELEGRAM_CHAT_ID", "")
    state_file = os.path.join(cfg.get("STATE_DIR"), "state")
    try:
        if force == "force-high":
            pct, status = high, "Charging"
            logging.info("Forced high test: %s%% %s", pct, status)
        elif force == "force-low":
            pct, status = low, "Discharging"
            logging.info("Forced low test: %s%% %s", pct, status)
        else:
            pct, status = battery_info()
            logging.info("Battery %d%% - %s", pct, status)
    except Exception as e:
        logging.error("Failed reading battery info: %s", e)
        return

    current_state = read_state(state_file)

    if status == "Charging" and pct >= high:
        if current_state != "notified_high":
            msg = f"Batería en {pct}% — desconecta el cargador (objetivo: {high}%)."
            notify_send(msg)
            notify_sound()
            notify_telegram(token, chat, msg)
            write_state(state_file, "notified_high")
            logging.info("Notified HIGH: %s", msg)
        else:
            logging.debug("HIGH already notified; skipping.")
    elif status == "Discharging" and pct <= low:
        if current_state != "notified_low":
            msg = f"Batería baja: {pct}% — conecta el cargador (umbral: {low}%)."
            notify_send(msg)
            notify_sound()
            notify_telegram(token, chat, msg)
            write_state(state_file, "notified_low")
            logging.info("Notified LOW: %s", msg)
        else:
            logging.debug("LOW already notified; skipping.")
    else:
        # if within neutral range, clear state so next crossing triggers notification again
        if low < pct < high:
            if current_state != "none":
                clear_state(state_file)
                logging.debug("In neutral range (%d%%) — state reset.", pct)
        else:
            logging.debug("No notification conditions met (%d%%, %s).", pct, status)

def daemon_loop(cfg: Dict[str, str]):
    global RUNNING
    poll = int(cfg.get("POLL_INTERVAL", "60"))
    logging.info("Starting daemon loop with interval %ds", poll)
    # Write pidfile
    try:
        os.makedirs(cfg.get("STATE_DIR"), exist_ok=True)
        with open(PIDFILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass

    try:
        while RUNNING:
            one_iteration(cfg)
            # Sleep in small increments to be responsive to signals
            slept = 0
            while RUNNING and slept < poll:
                time.sleep(1)
                slept += 1
    finally:
        try:
            os.remove(PIDFILE)
        except Exception:
            pass
        logging.info("Daemon exiting.")

def print_help():
    print("battery_daemon.py -- lightweight battery notifier")
    print("")
    print("Usage:")
    print("  battery_daemon.py [--config PATH] [--once] [--daemon] [--test-high] [--test-low] [--poll N] [--verbose]")
    print("")
    print("Options:")
    print("  --config PATH   Path to .env-style config file (default: ~/.config/battery_guardian/.env)")
    print("  --once          Run a single check and exit")
    print("  --daemon        Run as a background loop (default behavior if omitted)")
    print("  --test-high     Simulate HIGH notification (useful for testing)")
    print("  --test-low      Simulate LOW notification (useful for testing)")
    print("  --poll N        Override poll interval in seconds")
    print("  --verbose       Enable debug logging")
    print("  -h, --help      Show this help")

# -----------------------
# Entrypoint
# -----------------------
def main(argv: List[str]):
    env_path = DEFAULT_ENV_PATH
    once = False
    forced = None
    daemon = False
    verbose = False
    override_poll = None

    i = 1
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help"):
            print_help(); return 0
        if a == "--config":
            i += 1
            env_path = argv[i]
        elif a == "--once":
            once = True
        elif a == "--daemon":
            daemon = True
        elif a == "--test-high":
            forced = "force-high"
            once = True
        elif a == "--test-low":
            forced = "force-low"
            once = True
        elif a == "--poll":
            i += 1
            override_poll = argv[i]
        elif a == "--verbose":
            verbose = True
        else:
            print("Unknown option:", a)
            print_help()
            return 2
        i += 1

    cfg = load_config(env_path)
    if override_poll is not None:
        cfg["POLL_INTERVAL"] = str(int(override_poll))

    setup_logging(cfg.get("LOGFILE"), verbose=verbose)
    logging.info("Config loaded from %s", env_path)
    logging.debug("Config: %s", {k: cfg[k] for k in ("HIGH","LOW","POLL_INTERVAL","TELEGRAM_BOT_TOKEN","TELEGRAM_CHAT_ID","STATE_DIR","LOGFILE") if k in cfg})

    # Ensure state dir exists
    os.makedirs(cfg.get("STATE_DIR"), exist_ok=True)

    if once:
        one_iteration(cfg, force=forced)
        return 0

    # default behavior is daemon loop unless explicitly told once
    daemon_loop(cfg)
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except KeyboardInterrupt:
        logging.info("Interrupted by user")
        sys.exit(0)
