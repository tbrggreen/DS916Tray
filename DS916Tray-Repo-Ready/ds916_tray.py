"""
DS916 Tray Renderer
Runs in the Windows system tray, streams themes to the DS916 screen.
Compile with: pyinstaller --onefile --windowed --icon=icon.ico --name=DS916Tray ds916_tray.py
"""
import sys, os, json, struct, time, io, threading, winreg, ctypes, logging, re
import urllib.request
import urllib.parse
from logging.handlers import RotatingFileHandler
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, colorchooser
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont
import serial
import serial.tools.list_ports
import pystray
from pystray import MenuItem as Item
import PIL.Image as PILImage

# ── Paths ────────────────────────────────────────────────────────────────────
APP_NAME    = 'DS916Tray'
CONFIG_DIR  = os.path.join(os.environ.get('APPDATA',''), APP_NAME)
THEMES_DIR  = os.path.join(CONFIG_DIR, 'Themes')
LANGUAGES_DIR = os.path.join(CONFIG_DIR, 'Languages')
CONFIG_FILE = os.path.join(CONFIG_DIR, 'config.json')
LOG_FILE    = os.path.join(CONFIG_DIR, 'ds916_tray_log.txt')
os.makedirs(CONFIG_DIR, exist_ok=True)
os.makedirs(THEMES_DIR, exist_ok=True)
os.makedirs(LANGUAGES_DIR, exist_ok=True)

# ── Logging ──────────────────────────────────────────────────────────────────
# Three levels, configurable in Settings -> General:
#   Off     - logging.CRITICAL+1 (nothing written at all, not even errors)
#   Normal  - INFO and above (startup, theme loads, connection status,
#             restarts, config changes -- the kind of thing you'd want in a
#             support request, without being noisy)
#   Verbose - DEBUG and above (every sensor read attempt, per-frame timing,
#             RTSS scan details -- for actively diagnosing a problem)
#
# RotatingFileHandler caps file size so logs can never grow unbounded: once
# ds916_tray_log.txt hits ~1MB it's rotated to ds916_tray_log.txt.1 (one backup kept),
# so total on-disk log size is bounded to roughly 2MB no matter how long the
# app has been running.
LOG_LEVEL_MAP = {'off': logging.CRITICAL + 1, 'normal': logging.INFO, 'verbose': logging.DEBUG}

log = logging.getLogger('ds916tray')
log.setLevel(logging.DEBUG)  # handlers below do the actual filtering

_file_handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=1, encoding='utf-8')
_file_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
log.addHandler(_file_handler)

# Also mirror to stdout when running from a console (python ds916_tray.py
# directly) -- harmless no-op when run as the windowed .exe with no console.
_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setFormatter(logging.Formatter('%(message)s'))
log.addHandler(_console_handler)

def set_log_level(level_name):
    """level_name: 'off', 'normal', or 'verbose'. Applied to both handlers
    so the file and console always show the same configured verbosity."""
    lvl = LOG_LEVEL_MAP.get(level_name, logging.INFO)
    _file_handler.setLevel(lvl)
    _console_handler.setLevel(lvl)

# ── Default config ────────────────────────────────────────────────────────────
DEFAULT_CFG = {
    'com_port':   'COM3',
    'fps':        12,
    'screen_mode': 'auto',          # auto, gaming, work, idle
    'gaming_gpu_threshold': 45,
    'language': 'English',      # selected UI language
    'weather_city': 'Москва',
    'weather_latitude': 55.7558,
    'weather_longitude': 37.6173,
    'weather_timezone': 'Europe/Moscow',
    'rotate_display': True,         # legacy compatibility
    'display_orientation': 'rotated_portrait',  # album, rotated_album, portrait, rotated_portrait
    'decorative_animation': True,   # weather and music equalizer animations
    'theme_path': '',
    'autostart':  True,
    'hwinfo_path': '',
    'rtss_process': '',              # empty = auto-detect active 3D app; or an exact exe name (e.g. "game.exe") to pin a specific process
    'log_level': 'normal',           # 'off', 'normal', or 'verbose' -- see Logging section above
    'hwinfo_auto_restart': False,    # opt-in: if True, periodically check HWiNFO64's uptime and restart it before the 12h shared-memory limit -- see check_hwinfo_restart_needed(). Off by default so we never restart HWiNFO64 without explicit permission, and so Pro license holders (no 12h limit) aren't restarted needlessly.
    # Note: there is no persisted sensor index map here. Standard sensor
    # keys (CPU_USAGE, GPU_TEMP, etc.) are resolved fresh by NAME on every
    # single read inside read_sharedmem() -- nothing is ever cached across
    # restarts, so there is nothing that can go stale if HWiNFO's internal
    # sensor ordering shifts (driver update, new device added, etc.).
    # CUSTOM_N entries for sensors with no standard name come from the
    # currently loaded theme's own sensorMap instead of a global config.
}

def load_cfg():
    try:
        with open(CONFIG_FILE) as f: c=json.load(f)
        # Merge with defaults for any missing keys
        for k,v in DEFAULT_CFG.items():
            if k not in c: c[k]=v
        # Drop any leftover sensor_map from an older config version -- it's
        # no longer used for anything and keeping it around risks confusion
        # (and was the source of a real bug: a stale cached index could
        # silently persist forever across HWiNFO restarts/reorderings).
        c.pop('sensor_map', None)
        return c
    except: return dict(DEFAULT_CFG)

def save_cfg(cfg):
    with open(CONFIG_FILE,'w') as f: json.dump(cfg,f,indent=2)

cfg = load_cfg()
set_log_level(cfg.get('log_level', 'normal'))
log.info('=' * 60)
log.info(f'DS916 Tray starting (log level: {cfg.get("log_level", "normal")})')

# ── DS916 Protocol ────────────────────────────────────────────────────────────
HEADER_TPL = bytearray([
    0x00,0x3c,0x00,0x00,0x00,0x06,0x00,0x00,
    0x00,0x00,0x00,0x00,
    0x00,0x00,0x00,0x00,0x00,
    0x4f,0x54,0x06,
    0x00,0x00,0x00,0x00,
    0x1b,
    0x00,0x00,0x00,0x00,
    0x00,0x00,0x00,0x00,
    0x1b,0x00,
    0x00,0x00,0x00,0x00,
    0x89,0xb3,0xff,0xff,0x00,
    0x00,0x00,0x00,0x09,0x00,0x00,
    0x01,0x00,0x03,0x00,0x02,0x03,
    0x00,0x00,0x00,0x00,
])

def make_frame(jpeg, first=False):
    h = bytearray(HEADER_TPL)
    h[0] = 0x03 if first else 0x00
    struct.pack_into('<I', h, 56, len(jpeg))
    struct.pack_into('<I', h,  8, len(jpeg)+60)
    return bytes(h)+jpeg

# ── HWiNFO Reader ─────────────────────────────────────────────────────────────
_shm_handle = None
_shm_data   = None
_hwinfo_names_logged = False

def try_open_sharedmem():
    """Try to open HWiNFO shared memory using pure ctypes."""
    global _shm_handle, _shm_data
    log.debug('Attempting to open HWiNFO shared memory...')
    try:
        import ctypes
        import ctypes.wintypes

        kernel32      = ctypes.windll.kernel32
        FILE_MAP_READ = 0x0004

        # Declare complete Win32 signatures. On 64-bit Windows, HANDLE and
        # mapped addresses must remain pointer-sized; explicit argtypes also
        # prevent ctypes from converting handles/offsets through default c_int.
        kernel32.OpenFileMappingW.argtypes = [
            ctypes.wintypes.DWORD, ctypes.wintypes.BOOL, ctypes.wintypes.LPCWSTR
        ]
        kernel32.OpenFileMappingW.restype = ctypes.wintypes.HANDLE
        kernel32.MapViewOfFile.argtypes = [
            ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD,
            ctypes.wintypes.DWORD, ctypes.wintypes.DWORD, ctypes.c_size_t
        ]
        kernel32.MapViewOfFile.restype = ctypes.c_void_p
        kernel32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
        kernel32.UnmapViewOfFile.restype = ctypes.wintypes.BOOL
        kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
        kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
        kernel32.GetLastError.restype = ctypes.wintypes.DWORD

        # Open the named file mapping
        win_handle = None
        for name in ('Global\\HWiNFO_SENS_SM2', 'HWiNFO_SENS_SM2'):
            h = kernel32.OpenFileMappingW(FILE_MAP_READ, False, name)
            if h:
                win_handle = h
                log.debug(f'  Opened mapping: "{name}" handle={h}')
                break

        if not win_handle:
            raise OSError(
                'OpenFileMappingW failed — HWiNFO64 not running or '
                'Shared Memory Support not enabled.\n'
                '  In HWiNFO64: Settings → General → Shared Memory Support')

        # Map the complete existing section (dwNumberOfBytesToMap=0).
        # HWiNFO controls the section size, so avoid guessing a fixed 1 MiB.
        SM_SIZE = 0
        ptr = kernel32.MapViewOfFile(win_handle, FILE_MAP_READ, 0, 0, SM_SIZE)
        if not ptr:
            kernel32.CloseHandle(win_handle)
            raise OSError(f'MapViewOfFile failed (error {kernel32.GetLastError()})')
        log.debug(f'  MapViewOfFile ptr=0x{ptr:X}')

        # Read using ctypes.string_at — this is the correct way to read
        # from a raw memory address in Python on Windows
        sig_bytes = ctypes.string_at(ptr, 4)
        sig = struct.unpack('<I', sig_bytes)[0]
        log.debug(f'  Signature: 0x{sig:08X} (WIFH=0x57494648, SiWH=0x53695748)')

        VALID_SIGS = {0x57494648, 0x53695748}
        if sig not in VALID_SIGS:
            kernel32.UnmapViewOfFile(ptr)
            kernel32.CloseHandle(win_handle)
            raise OSError(f'Unknown signature 0x{sig:08X} — HWiNFO still loading?')

        # Store both so we can read later and keep alive
        # Decode layout using reverse-engineered struct (github.com/namazso/hwinfosharedmem.h)
        hdr   = ctypes.string_at(ptr, 48)
        off_e = struct.unpack_from('<I', hdr, 0x20)[0]
        sz_e  = struct.unpack_from('<I', hdr, 0x24)[0]
        n_e   = struct.unpack_from('<I', hdr, 0x28)[0]
        log.info(f'HWiNFO shared-memory header: readings={n_e}, offset={off_e}, entry_size={sz_e}')
        if off_e <= 0 or sz_e < 292 or n_e <= 0:
            kernel32.UnmapViewOfFile(ptr)
            kernel32.CloseHandle(win_handle)
            raise OSError(
                f'Unexpected HWiNFO reading-section layout: offset={off_e}, '
                f'entry_size={sz_e}, count={n_e}'
            )
        # Store 7-tuple: kernel32, win_handle, ptr, SM_SIZE, off_e, sz_e, n_e
        _shm_handle = (kernel32, win_handle, ptr, SM_SIZE, off_e, sz_e, n_e)
        _shm_data   = True
        log.info('HWiNFO shared memory connected OK')
        return True

    except Exception as e:
        log.warning(f'HWiNFO shared memory open failed: {e}')
        _shm_handle = None
        _shm_data   = None
        return False


# Canonical HWiNFO sensor names for each standard sensor key. Looked up by
# NAME on every single read (like the RTSS reader already does for process
# names) rather than caching a numeric index anywhere — this is what
# actually eliminates the staleness problem: there's nothing to go stale
# if nothing is ever persisted across HWiNFO restarts/reorderings.
STANDARD_SENSOR_NAMES = {
    'CPU_USAGE':    ['Total CPU Usage'],
    'CPU_TEMP':     ['CPU (Tctl/Tdie)', 'CPU Package', 'CPU Temperature'],
    'CPU_FAN':      ['CPU1', 'CPU Fan', 'CPU_OPT'],
    'CPU_FREQ':     ['CPU Clock', 'Core Clocks (avg)'],
    'CPU_POWER':    ['CPU Package Power', 'CPU Power'],
    'CPU_VOLTAGE':  ['CPU Core Voltage', 'Vcore'],
    'GPU_USAGE':    ['GPU Core Load', 'GPU Usage', 'GPU Load', 'GPU Utilization'],
    'GPU_TEMP':     ['GPU Temperature', 'GPU Temp'],
    'GPU_FAN1':     ['GPU Fan1', 'GPU Fan 1', 'GPU Fan'],   # AMD cards commonly report a single 'GPU Fan' rather than separate Fan1/Fan2 - falls back here
    'GPU_FAN2':     ['GPU Fan2', 'GPU Fan 2'],
    'GPU_FREQ':     ['GPU Clock'],
    'GPU_POWER':    ['GPU Power', 'Total Board Power (TBP)', ('GPU Core Power (VDDCR_GFX)', 1)],   # 'Total Board Power (TBP)' confirmed via HWiNFO's GPU sensor group on AMD RX 9070 XT -- a real measured total power draw, not a sum HWiNFO computes itself. Falls back to the GFX core rail only if TBP isn't available at all (underreports total power in that case, since it excludes SoC/memory rails).
    'VRAM_USAGE':   ['GPU Memory Load'],   # percentage-based VRAM usage (NVIDIA-style naming) - AMD does not expose a reliable percentage equivalent; see note in read_sharedmem()
    'VRAM_USED':    ['GPU Memory Used', ('GPU D3D Memory Dedicated', 1/1024), ('GPU Memory Usage', 1/1024)],   # 'GPU Memory Usage' is a CONFIRMED-BUGGY AMD driver value -- HWiNFO's own author (Martin) states on the HWiNFO forum (hwinfo.com/forum/threads/abnormal-reporting-of-gpu-memory-usage.9461/) that this is unreliable on AMD GPUs and recommends watching the GPU D3D Memory values instead. 'GPU D3D Memory Dedicated' (in MB, converted to GB here) is the correct, AMD-recommended replacement and is tried first; the buggy sensor is kept only as a last-resort fallback for systems where D3D Memory Dedicated isn't exposed at all.
    'RAM_USAGE':    ['Physical Memory Load'],
    'RAM_USED_GB':  [('Physical Memory Used', 1/1024)],
    'RAM_FREE_GB':  [('Physical Memory Available', 1/1024)],
    'RAM_TOTAL':    [('Physical Memory Total', 1/1024)],
    'DISK_USAGE':   ['Disk Usage'],
    'DISK_USED':    ['Disk Used'],
    'DISK_FREE':    ['Disk Free'],
    'DISK_TEMP':    ['Drive Temperature'],
    'DISK_READ':    ['Read Rate', 'Disk Read Rate'],
    'DISK_WRITE':   ['Write Rate', 'Disk Write Rate'],
    'MB_TEMP':      ['Motherboard'],
    'CHASSIS_FAN1': ['Chassis1', 'Chassis Fan 1', 'CHA_FAN1'],
    'CHASSIS_FAN2': ['Chassis2', 'Chassis Fan 2', 'CHA_FAN2'],
    'CHASSIS_FAN3': ['Chassis3', 'Chassis Fan 3', 'CHA_FAN3'],
    'NET_DOWN':     ['Current DL rate', 'Download rate'],
    'NET_UP':       ['Current UP rate', 'Upload rate'],
    'NET_PING':     ['Ping'],
    'BATTERY':      ['Battery Charge Level'],
    # FRAMERATE intentionally not auto-mapped — HWiNFO's PresentMon
    # tracking is unreliable without an HWiNFO Pro license; users who
    # want to try it anyway can wire up a CUSTOM_N key manually.
}

def read_sharedmem(sensor_map=None):
    """Read sensor values from HWiNFO shared memory, resolving every
    standard sensor key (and any CUSTOM_N keys) fresh by NAME on every
    call. No numeric index is ever cached in config.json — if HWiNFO's
    internal sensor ordering shifts after a restart, driver update, or
    new device being added, this re-resolves correctly on the very next
    read with no stale state possible.

    sensor_map is accepted for backwards compatibility (CUSTOM_N entries
    from older saved themes may still carry a literal index) but standard
    keys always resolve by name, ignoring any cached index for them.

    Layout from: github.com/namazso/hwinfosharedmem.h
    HWiNFOEntry: type(4) sensor_index(4) id(4) name_orig(128) name_user(128) unit(16) value(8d)
    Value is a double at offset 0x11C = 284 within each entry.
    """
    global _shm_handle
    data = {}
    if not _shm_handle: return data
    try:
        import ctypes
        kernel32, win_handle, ptr, SM_SIZE, off_e, sz_e, n_e = _shm_handle
        ptr = int(ptr)
        # A double at offset 0x11C occupies bytes 284..291.
        if not ptr or off_e <= 0 or sz_e < 292 or n_e <= 0:
            log.warning(
                'HWiNFO reading section looks invalid: ptr=%r offset=%r entry_size=%r count=%r',
                ptr, off_e, sz_e, n_e
            )
            return data

        # Re-verify the signature on every read. If HWiNFO64 restarts (e.g.
        # after the free version's 12-hour shared memory limit kicks in and
        # the user restarts it), the OLD mapping we have open becomes stale
        # — Windows may have destroyed and recreated the section under the
        # same name. Without this check we'd keep silently failing forever
        # on a dead handle, since _shm_handle would never go back to None
        # and try_open_sharedmem() would never be called again.
        VALID_SIGS = {0x57494648, 0x53695748}
        sig_bytes = ctypes.string_at(ptr, 4)
        sig = struct.unpack('<I', sig_bytes)[0]
        if sig not in VALID_SIGS:
            log.info('HWiNFO shared memory signature is now invalid (stale handle after a restart) - reconnecting next read')
            try:
                kernel32.UnmapViewOfFile(ptr)
                kernel32.CloseHandle(win_handle)
            except Exception:
                pass
            _shm_handle = None
            return data

        VALUE_OFFSET = 0x11C  # 284 — double at this offset within HWiNFOEntry

        # Build name -> (index, value) for every entry in ONE pass, then
        # resolve every standard key against it by name. This is the same
        # cost as before (one scan of all entries per read) but eliminates
        # any persisted index entirely.
        name_to_val = {}
        for idx in range(n_e):
            entry_ptr = ptr + off_e + idx * sz_e
            name_bytes = ctypes.string_at(entry_ptr + 0x0C, 128)
            name = name_bytes.rstrip(b'\x00').decode('ascii', 'replace').strip()
            if not name: continue
            val_bytes = ctypes.string_at(entry_ptr + VALUE_OFFSET, 8)
            val = struct.unpack('<d', val_bytes)[0]
            name_to_val[name] = val

        # Log the actual names once per process. This makes it possible to
        # distinguish a layout/offset issue from sensor names that simply
        # differ on this HWiNFO version or hardware.
        global _hwinfo_names_logged
        if not _hwinfo_names_logged:
            log.info('HWiNFO sensor names sample: %s', ', '.join(list(name_to_val.keys())[:30]))
            _hwinfo_names_logged = True

        for key, candidates in STANDARD_SENSOR_NAMES.items():
            for cand in candidates:
                # Candidates are either a plain sensor name (string), or a
                # (name, multiplier) tuple when a vendor/driver reports the
                # same metric in different units than this key expects --
                # e.g. AMD's 'GPU Memory Usage' is in MB while VRAM_USED
                # expects GB, so it's listed as ('GPU Memory Usage', 1/1024).
                if isinstance(cand, tuple):
                    cname, multiplier = cand
                else:
                    cname, multiplier = cand, 1
                if cname in name_to_val:
                    data[key] = name_to_val[cname] * multiplier
                    break

        # VRAM_USAGE (percentage): no direct sensor exists for this on most
        # AMD cards (and isn't always reliable on NVIDIA either). If we
        # weren't able to resolve it directly above, compute it ourselves
        # from VRAM_USED (GB) and the card's known total capacity, looked
        # up once at startup via detect_gpu_vram_capacity(). If the card
        # isn't in our known-capacity database, VRAM_USAGE simply stays
        # unavailable rather than guessing.
        if 'VRAM_USAGE' not in data and 'VRAM_USED' in data and _detected_vram_gb:
            data['VRAM_USAGE'] = min(100.0, (data['VRAM_USED'] / _detected_vram_gb) * 100.0)

        # CUSTOM_N keys (manually wired sensors not covered by the standard
        # name table above) still use whatever literal index was saved with
        # the theme/sensor_map, since there's no name to re-resolve against
        # for an arbitrary user-picked index.
        if sensor_map:
            for skey, idx in sensor_map.items():
                if not skey.startswith('CUSTOM_') or idx is None: continue
                if idx >= n_e: continue
                entry_ptr = ptr + off_e + idx * sz_e
                val_bytes = ctypes.string_at(entry_ptr + VALUE_OFFSET, 8)
                data[skey] = struct.unpack('<d', val_bytes)[0]

    except Exception as e:
        log.error(f'Shared memory read error: {e}')
        # Any unexpected failure also resets the handle, rather than
        # leaving a possibly-broken mapping in place indefinitely.
        _shm_handle = None
    return data


# ── GPU VRAM capacity lookup ───────────────────────────────────────────────────
# HWiNFO does not expose total VRAM capacity as a polled sensor (it's static
# hardware info, not something that changes frame to frame) -- there's no
# sensor name for it on either NVIDIA or AMD cards in anything we've found.
# Since VRAM_USAGE (a 0-100% sensor) needs a total to divide VRAM_USED by,
# we keep a small lookup table of known card model -> VRAM capacity (GB) and
# match it against the GPU's device name string from HWiNFO's sensor groups
# (e.g. "dGPU [#0]: AMD Radeon RX 9070 XT: PowerColor Radeon RX 9070 XT").
# This only needs to run once at startup, not on every read, since a card's
# VRAM capacity never changes while the system is running.
#
# Keys are matched as case-insensitive substrings against the device name,
# longest/most-specific match wins (so "RX 9070 XT" doesn't accidentally
# match against a plain "RX 9070" entry or vice versa).
GPU_VRAM_GB = {
    # AMD RDNA4 / RDNA3
    'RX 9070 XT':  16,
    'RX 9070 GRE': 12,
    'RX 9070':     16,
    'RX 9060 XT':  16,   # also exists in an 8GB variant -- can't disambiguate by name alone
    'RX 7900 XTX': 24,
    'RX 7900 XT':  20,
    'RX 7900 GRE': 16,
    'RX 7800 XT':  16,
    'RX 7700 XT':  12,
    'RX 7600 XT':  16,
    'RX 7600':     8,
    # NVIDIA RTX 40 / 30 series
    'RTX 4090':    24,
    'RTX 4080 SUPER': 16,
    'RTX 4080':    16,
    'RTX 4070 TI SUPER': 16,
    'RTX 4070 TI': 12,
    'RTX 4070 SUPER': 12,
    'RTX 4070':    12,
    'RTX 4060 TI': 8,    # also has a 16GB variant -- can't disambiguate by name alone
    'RTX 4060':    8,
    'RTX 3090 TI': 24,
    'RTX 3090':    24,
    'RTX 3080 TI': 12,
    'RTX 3080':    10,   # also has a 12GB variant
    'RTX 3070 TI': 8,
    'RTX 3070':    8,
    'RTX 3060 TI': 8,
    'RTX 3060':    12,   # also has an 8GB variant
    '2080 TI':     11,
    '2080 SUPER':  8,
    '2080':        8,
    '2070 SUPER':  8,
    '2070':        8,
    '2060 SUPER':  8,
    '2060':        6,
}

_detected_vram_gb = None  # cached once at startup; None means "not yet checked" or "no match found"

def detect_gpu_vram_capacity():
    """Match the GPU's device name (from discovered sensor groups) against
    GPU_VRAM_GB to find its known total VRAM capacity. Called once at
    startup -- result is cached in _detected_vram_gb for the rest of the
    session, since a card's VRAM capacity is static hardware info that
    will never change while the app is running."""
    global _detected_vram_gb
    sensors_path = os.path.join(CONFIG_DIR, 'hwinfo_sensors.json')
    if not os.path.exists(sensors_path):
        return None
    try:
        with open(sensors_path, encoding='utf-8') as f:
            disc = json.load(f)
        device_names = disc.get('device_names', [])
        gpu_name = None
        for d in device_names:
            name = d.get('name', '')
            if 'gpu' in name.lower():
                gpu_name = name
                break
        if not gpu_name:
            log.debug('VRAM capacity lookup: no GPU device name found in discovered sensors')
            return None

        # Find the longest matching key (most specific match wins, so
        # "RX 9070 XT" is preferred over a hypothetical shorter "RX 9070"
        # match against the same name)
        best_match = None
        best_len = 0
        upper_name = gpu_name.upper()
        for key, gb in GPU_VRAM_GB.items():
            if key.upper() in upper_name and len(key) > best_len:
                best_match = key
                best_len = len(key)
                _detected_vram_gb = gb

        if best_match:
            log.info(f'VRAM capacity detected: "{gpu_name}" matched "{best_match}" -> {_detected_vram_gb} GB')
        else:
            log.info(f'VRAM capacity lookup: GPU "{gpu_name}" not found in known card database -- '
                      f'VRAM_USAGE percentage will be unavailable (VRAM_USED in GB still works normally)')
        return _detected_vram_gb
    except Exception as e:
        log.warning(f'VRAM capacity lookup error: {e}')
        return None


def discover_sensors():
    """Scan all HWiNFO shared memory entries and save to hwinfo_sensors.json.
    Also reads the sensor group (device name) section so device names like
    'AMD Ryzen 5 5600X' and 'ASRock B550M Steel Legend' are available in
    the theme builder's + Sensors picker as static label elements.
    Called automatically on startup and available from tray menu."""
    if not _shm_handle:
        log.warning('Cannot discover sensors - shared memory not available')
        return False
    try:
        import ctypes
        kernel32, win_handle, ptr, SM_SIZE, off_e, sz_e, n_e = _shm_handle
        ptr = int(ptr)

        TYPE_NAMES = {0:'Other',1:'Temperature',2:'Voltage',3:'Fan',
                      4:'Current',5:'Power',6:'Clock',7:'Usage',8:'Other'}

        # Header layout (_HWiNFO_SENSORS_SHARED_MEM2):
        # dwSignature(4) dwVersion(4) dwRevision(4) poll_time(8=long) = 20 bytes
        # dwOffsetOfSensorSection(4)@0x14, dwSizeOfSensorElement(4)@0x18, dwNumSensorElements(4)@0x1C
        # dwOffsetOfReadingSection(4)@0x20, dwSizeOfReadingElement(4)@0x24, dwNumReadingElements(4)@0x28
        hdr = ctypes.string_at(ptr, 48)
        off_s = struct.unpack_from('<I', hdr, 0x14)[0]  # sensor (device group) section
        sz_s  = struct.unpack_from('<I', hdr, 0x18)[0]
        n_s   = struct.unpack_from('<I', hdr, 0x1C)[0]

        # Read device group names from the sensor section
        # _HWiNFO_SENSORS_SENSOR_ELEMENT: dwSensorID(4) dwSensorInst(4) szSensorNameOrig(128) szSensorNameUser(128)
        device_names = []
        for i in range(n_s):
            entry = ctypes.string_at(ptr + off_s + i * sz_s, min(sz_s, 264))
            name = entry[8:8+128].rstrip(b'\x00').decode('ascii','replace').strip()
            if name:
                device_names.append({'index': i, 'name': name})

        # dwSensorIndex linking each reading to its device group is at offset 0x04
        sensors = []
        for i in range(n_e):
            entry = ctypes.string_at(ptr + off_e + i * sz_e, sz_e)
            stype        = struct.unpack_from('<I', entry, 0x00)[0]
            sensor_idx   = struct.unpack_from('<I', entry, 0x04)[0]
            name_orig    = entry[0x0C:0x0C+128].rstrip(b'\x00').decode('ascii','replace').strip()
            unit         = entry[0x10C:0x10C+16].rstrip(b'\x00').decode('ascii','replace').strip()
            val          = struct.unpack_from('<d', entry, 0x11C)[0]
            if not name_orig: continue
            sensors.append({
                'index':        i,
                'sensor_index': sensor_idx,
                'type':         TYPE_NAMES.get(stype, 'Other'),
                'name':         name_orig,
                'unit':         unit,
                'sample':       round(val, 3),
            })

        out = {'generated': str(datetime.now()), 'device_names': device_names, 'sensors': sensors}
        sensors_path = os.path.join(CONFIG_DIR, 'hwinfo_sensors.json')
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(sensors_path, 'w', encoding='utf-8') as f:
            json.dump(out, f, indent=2)
        log.info(f'Sensor discovery: {len(sensors)} sensors, {len(device_names)} device groups saved to {sensors_path}')
        return sensors_path
    except Exception as e:
        log.error(f'Sensor discovery error: {e}')
        return False

# ── Supplementary system metrics (independent of HWiNFO) ──────────────────────
# Keep slow disk/network/ping operations off the frame-rendering thread.
try:
    import psutil as _psutil
except Exception:
    _psutil = None

_system_metrics_lock = threading.Lock()
_system_metrics = {}
_system_metrics_thread_started = False
_weather_last_fetch = 0.0
_weather_last_success = 0.0
_weather_cache = {'WEATHER_UPDATED_TEXT': 'Обновлено: —', 'WEATHER_TEMP': '—', 'WEATHER_FEELS_TEXT': 'Ощущается как: — °C', 'WEATHER_DESC': 'Загрузка погоды…', 'WEATHER_DETAIL': 'Подключение к сервису', 'WEATHER_CODE': -1, 'WEATHER_IS_DAY': 1, 'WEATHER_SUN_TEXT': 'Рассвет —  |  Закат —'}

_WEATHER_CODES_RU = {
    0: 'Ясно', 1: 'Преимущественно ясно', 2: 'Переменная облачность', 3: 'Пасмурно',
    45: 'Туман', 48: 'Изморозевый туман', 51: 'Морось', 53: 'Морось', 55: 'Сильная морось',
    56: 'Ледяная морось', 57: 'Сильная ледяная морось', 61: 'Небольшой дождь', 63: 'Дождь',
    65: 'Сильный дождь', 66: 'Ледяной дождь', 67: 'Сильный ледяной дождь', 71: 'Небольшой снег',
    73: 'Снег', 75: 'Сильный снег', 77: 'Снежная крупа', 80: 'Ливень', 81: 'Ливень', 82: 'Сильный ливень',
    85: 'Снегопад', 86: 'Сильный снегопад', 95: 'Гроза', 96: 'Гроза с градом', 99: 'Сильная гроза'
}

def _fetch_weather():
    """Fetch current weather from Open-Meteo; retain last good values on errors."""
    global _weather_cache, _weather_last_fetch, _weather_last_success
    lat = float(cfg.get('weather_latitude', 48.708))
    lon = float(cfg.get('weather_longitude', 44.514))
    timezone = str(cfg.get('weather_timezone', 'Europe/Moscow'))
    city_name = str(cfg.get('weather_city', 'Москва'))
    url = ('https://api.open-meteo.com/v1/forecast?latitude=' + urllib.parse.quote(str(lat)) +
           '&longitude=' + urllib.parse.quote(str(lon)) +
           '&current=temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m,is_day'
           '&daily=sunrise,sunset&forecast_days=1&timezone=' + urllib.parse.quote(timezone))
    req = urllib.request.Request(url, headers={'User-Agent': 'DS916Tray/1.0', 'Accept': 'application/json'})
    _weather_last_fetch = time.monotonic()
    try:
        log.info('Weather request started: Open-Meteo (%s)', city_name)
        with urllib.request.urlopen(req, timeout=8) as response:
            status = getattr(response, 'status', 200)
            raw = response.read()
        log.debug('Weather HTTP response: status=%s bytes=%s', status, len(raw))
        data = json.loads(raw.decode('utf-8-sig'))
        cur = data.get('current') or {}
        temp = cur.get('temperature_2m')
        code = cur.get('weather_code')
        if temp is None or code is None:
            raise ValueError('response JSON has no current.temperature_2m/weather_code')
        desc = _WEATHER_CODES_RU.get(int(code), 'Погодные условия')
        feels = cur.get('apparent_temperature')
        wind = cur.get('wind_speed_10m')
        daily = data.get('daily') or {}
        def sun_time(key):
            vals = daily.get(key) or []
            if not vals or not vals[0]: return '--:--'
            return str(vals[0]).split('T')[-1][:5]
        sun_text = f"Рассвет {sun_time('sunrise')}  •  Закат {sun_time('sunset')}"
        detail = []
        if feels is not None:
            detail.append(f'ощущается {round(float(feels))}°')
        if wind is not None:
            detail.append(f'ветер {float(wind) / 3.6:.1f} м/с')
        new_cache = {
            'WEATHER_UPDATED_TEXT': 'Обновлено: ' + datetime.now().strftime('%H:%M'),
            'WEATHER_TEMP': f'{float(temp):.0f}',
            'WEATHER_TEMP_TEXT': f'{float(temp):.0f} °C',
            'WEATHER_FEELS_TEXT': f'Ощущается как: {round(float(feels))} °C' if feels is not None else 'Ощущается как: — °C',
            'WEATHER_DESC': desc,
            'WEATHER_DETAIL': (f'Ветер {float(wind) / 3.6:.1f} м/с' if wind is not None else city_name),
            'WEATHER_CITY': city_name,
            'WEATHER_CODE': int(code),
            'WEATHER_IS_DAY': int(cur.get('is_day', 1) or 0),
            'WEATHER_SUN_TEXT': sun_text
        }
        _weather_cache = new_cache
        _weather_last_success = time.monotonic()
        log.info('Weather updated successfully for %s: %s, %s; detail=%s',
                 city_name, new_cache['WEATHER_TEMP_TEXT'], desc, new_cache['WEATHER_DETAIL'])
        return True
    except Exception as e:
        # Retry failed initial fetches soon; do not erase last known good weather.
        log.warning('Weather update failed (%s): %s', type(e).__name__, e)
        if _weather_last_success == 0:
            _weather_cache = {
                'WEATHER_UPDATED_TEXT': 'Обновлено: —',
                'WEATHER_TEMP': '—',
                'WEATHER_TEMP_TEXT': '— °C',
                'WEATHER_FEELS_TEXT': 'Ощущается как: — °C',
                'WEATHER_DESC': 'Нет данных',
                'WEATHER_DETAIL': 'Нет соединения с погодным сервисом',
                'WEATHER_CODE': -1,
                'WEATHER_IS_DAY': 1,
                'WEATHER_SUN_TEXT': 'Рассвет —  •  Закат —'
            }
        return False



def _collect_system_metrics():
    """Collect network rates, disk free space and ping in a background thread."""
    global _system_metrics
    previous_net = None
    previous_time = None
    last_net_log = 0.0
    last_ping_log = 0.0
    log.info('System metrics worker started (psutil=%s)', _psutil is not None)
    while True:
        values = {}
        now_mono = time.monotonic()
        try:
            if _psutil is not None:
                net = _psutil.net_io_counters()
                if previous_net is not None and previous_time is not None:
                    dt = max(0.001, now_mono - previous_time)
                    values['NET_DOWN'] = max(0.0, (net.bytes_recv - previous_net.bytes_recv) / dt / 1024 / 1024)
                    values['NET_UP'] = max(0.0, (net.bytes_sent - previous_net.bytes_sent) / dt / 1024 / 1024)
                    for raw_key, text_key in (('NET_DOWN', 'NET_DOWN_TEXT'), ('NET_UP', 'NET_UP_TEXT')):
                        rate = values[raw_key]
                        values[text_key] = (f'{rate * 1024:.0f} KB/s' if rate < 1.0 and rate > 0 else ('0 KB/s' if rate == 0 else f'{rate:.2f} MB/s'))
                    if now_mono - last_net_log >= 30:
                        log.debug('Network rates: down=%.3f MB/s up=%.3f MB/s', values['NET_DOWN'], values['NET_UP'])
                        last_net_log = now_mono
                previous_net, previous_time = net, now_mono
            else:
                values['SYSTEM_METRICS_NOTE'] = 'psutil not installed'
                if now_mono - last_net_log >= 30:
                    log.warning('Network rates unavailable: psutil is not installed in this Python environment/build')
                    last_net_log = now_mono
        except Exception as e:
            log.debug('Network metrics error: %r', e)

        # Fixed drives: use C: and D: when present. Values are GB, not MB.
        for drive, key in (('C:\\', 'DISK_C'), ('D:\\', 'DISK_D'), ('F:\\', 'DISK_F')):
            try:
                total, used, free = __import__('shutil').disk_usage(drive)
                values[key + '_FREE_GB'] = free / (1024 ** 3)
                values[key + '_TOTAL_GB'] = total / (1024 ** 3)
                values[key + '_USED_PCT'] = used * 100.0 / total if total else 0.0
            except Exception as e:
                log.debug('Disk metrics unavailable for %s: %r', drive, e)

        # Decode Windows ping output using OEM code page, then parse RU/EN output.
        try:
            import subprocess, re
            creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            result = subprocess.run(
                ['ping', '-n', '1', '-w', '900', '1.1.1.1'],
                capture_output=True, text=False, timeout=1.5,
                creationflags=creationflags
            )
            raw_output = (result.stdout or b'') + b'\n' + (result.stderr or b'')
            encodings = []
            try:
                encodings.append('cp%d' % ctypes.windll.kernel32.GetOEMCP())
            except Exception:
                pass
            encodings.extend(['cp866', 'cp1251', 'utf-8', 'mbcs'])
            decoded_outputs = []
            for encoding in encodings:
                try:
                    decoded = raw_output.decode(encoding, errors='replace')
                    if decoded not in decoded_outputs:
                        decoded_outputs.append(decoded)
                except Exception:
                    continue
            parsed_ping = None
            if result.returncode == 0:
                patterns = [
                    r'time\s*[=<]\s*(\d+)\s*(?:ms|мс|мсек)',
                    r'время\s*[=<]\s*(\d+)\s*(?:ms|мс|мсек)',
                ]
                for decoded in decoded_outputs:
                    for pattern in patterns:
                        match = re.search(pattern, decoded, re.IGNORECASE)
                        if match:
                            parsed_ping = float(match.group(1))
                            break
                    if parsed_ping is not None:
                        break
                if parsed_ping is None:
                    for decoded in decoded_outputs:
                        if re.search(r'(?:time|время)\s*<\s*1\s*(?:ms|мс|мсек)', decoded, re.IGNORECASE):
                            parsed_ping = 0.5
                            break
            if parsed_ping is not None:
                values['NET_PING'] = parsed_ping
            if now_mono - last_ping_log >= 30:
                sample = next((x for x in decoded_outputs if 'Ответ от' in x or 'Reply from' in x),
                              decoded_outputs[0] if decoded_outputs else repr(raw_output))
                log.debug('Ping probe: returncode=%s, parsed=%s, encodings=%s, output=%r',
                          result.returncode, parsed_ping, encodings, sample[-300:])
                last_ping_log = now_mono
        except Exception as e:
            if now_mono - last_ping_log >= 30:
                log.warning('Ping check failed: %r', e)
                last_ping_log = now_mono

        # Retry every 60s until first success; refresh every 15 minutes afterwards.
        weather_interval = 900 if _weather_last_success > 0 else 60
        if now_mono - _weather_last_fetch >= weather_interval:
            _fetch_weather()
        values.update(_weather_cache)
        with _system_metrics_lock:
            _system_metrics = values
        time.sleep(2.0)


def _start_system_metrics_worker():
    global _system_metrics_thread_started
    if not _system_metrics_thread_started:
        _system_metrics_thread_started = True
        threading.Thread(target=_collect_system_metrics, name='DS916-SystemMetrics', daemon=True).start()


def read_sensors():
    """Read all sensor values from HWiNFO64's shared memory. Returns an
    empty dict if HWiNFO64 isn't running or shared memory isn't enabled —
    callers should treat missing keys as 'sensor unavailable', not an error."""
    # CUSTOM_N entries (manually wired sensors HWiNFO doesn't have a
    # standard name for) come from the currently loaded THEME's own
    # sensorMap, not a persisted global config -- standard keys resolve
    # by name fresh on every read inside read_sharedmem() itself and need
    # no map passed in at all.
    custom_map = {}
    if _current_theme:
        for k, v in _current_theme.get('sensorMap', {}).items():
            if k.startswith('CUSTOM_') and v is not None:
                custom_map[k] = v
    if _shm_handle is None:
        try_open_sharedmem()
    d = read_sharedmem(custom_map) if _shm_handle is not None else {}

    # RTSS is optional and independent of HWiNFO — merge in FPS values if available
    rtss_vals = read_rtss_framerate()
    if rtss_vals is not None:
        d.update(rtss_vals)

    _start_system_metrics_worker()
    with _system_metrics_lock:
        d.update(_system_metrics)

    # Reliable RAM fallback from psutil. HWiNFO's memory readings can be
    # absent or expressed differently depending on its version/localization.
    if _psutil is not None:
        try:
            vm = _psutil.virtual_memory()
            d.setdefault('RAM_USED_GB', vm.used / (1024 ** 3))
            d.setdefault('RAM_FREE_GB', vm.available / (1024 ** 3))
            d.setdefault('RAM_TOTAL', vm.total / (1024 ** 3))
            d.setdefault('RAM_USAGE', vm.percent)
        except Exception as e:
            log.debug('RAM fallback error: %r', e)

    # Theme uses RTSS_FRAMETIME while the reader's canonical key is
    # RTSS_FRAMETIME_MS. Keep both for compatibility with existing themes.
    if 'RTSS_FRAMETIME_MS' in d:
        d['RTSS_FRAMETIME'] = d['RTSS_FRAMETIME_MS']
        d['RTSS_FRAMETIME_TEXT'] = f"{d['RTSS_FRAMETIME_MS']:.2f} ms"
    elif 'RTSS_FPS' in d and d['RTSS_FPS'] > 0:
        # Fallback only when RTSS doesn't expose dwFrameTime; explicitly label it as estimated.
        d['RTSS_FRAMETIME_TEXT'] = f"~{1000.0 / d['RTSS_FPS']:.2f} ms"

    return d

# ── RTSS Reader (optional — FPS via RivaTuner Statistics Server) ──────────────
# Independent of HWiNFO. RTSS hooks directly into the game's D3D/OpenGL/Vulkan
# present calls, so its per-process framerate is attributed correctly without
# needing HWiNFO Pro to exclude background applications.
_rtss_handle = None       # (kernel32, win_handle, ptr, size) once mapped
_rtss_unavailable_logged = False
_rtss_last_attempt = 0     # time.time() of the last connection attempt
_rtss_retry_interval = 10  # seconds between reconnect attempts while unavailable

def try_open_rtss():
    """Try to open RTSS shared memory using the same pure-ctypes approach
    that works for HWiNFO. Safe to call repeatedly — RTSS may not be
    running, may be started later, or may be closed; we just keep retrying
    on each read rather than treating one failure as permanent."""
    global _rtss_handle, _rtss_unavailable_logged
    try:
        import ctypes
        kernel32      = ctypes.windll.kernel32
        FILE_MAP_READ       = 0x0004
        FILE_MAP_ALL_ACCESS  = 0x000F001F

        kernel32.OpenFileMappingW.restype = ctypes.c_void_p
        kernel32.MapViewOfFile.restype    = ctypes.c_void_p
        kernel32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.argtypes     = [ctypes.c_void_p]

        # The access mask passed to OpenFileMappingW constrains what
        # MapViewOfFile can later request against that same handle — so if
        # MapViewOfFile fails with ERROR_ACCESS_DENIED, retrying with a
        # different access right on the SAME handle won't help; we need a
        # fresh OpenFileMappingW call with a different requested access
        # right. Try every (access, name) combination as a full
        # open+map+verify cycle, closing and moving on between attempts.
        #
        # IMPORTANT: request size 0 here, not a guessed fixed size. Passing
        # a size larger than the section RTSS actually created can itself
        # cause MapViewOfFile to fail with ERROR_ACCESS_DENIED (5) — the
        # same symptom as a real permissions problem, but with a totally
        # different cause. 0 means "map the whole existing section,
        # whatever size it actually is" and is the standard safe approach
        # when you don't control the size the mapping was created with.
        ptr = None
        win_handle = None
        opened_name = None
        opened_access = None
        last_err = None

        for access in (FILE_MAP_ALL_ACCESS, FILE_MAP_READ):
            for name in ('RTSSSharedMemoryV2', 'Global\\RTSSSharedMemoryV2'):
                h = kernel32.OpenFileMappingW(access, False, name)
                if not h:
                    continue
                p = kernel32.MapViewOfFile(h, access, 0, 0, 0)
                if p:
                    ptr, win_handle, opened_name, opened_access = p, h, name, access
                    break
                last_err = kernel32.GetLastError()
                try:
                    log.debug('  RTSS MapViewOfFile failed (error %s) on "%s" with access=0x%X - trying next combination' % (last_err, name, access))
                except Exception as log_err:
                    log.debug('  RTSS MapViewOfFile failed, and logging itself raised: %r' % (log_err,))
                kernel32.CloseHandle(h)
            if ptr:
                break

        if not win_handle:
            # RTSS not running — this is a normal, expected state since RTSS
            # is an optional feature. Log once, not every frame.
            if not _rtss_unavailable_logged:
                if last_err is not None:
                    log.info('RTSS shared memory could not be mapped (last error %s) - '
                          'this is optional, FPS sensor will be unavailable. Make sure '
                          'RTSS (RivaTuner Statistics Server) is installed and running.' % (last_err,), flush=True)
                else:
                    log.info('RTSS shared memory not found (RTSS not running - this is '
                          'optional, FPS sensor will be unavailable)', flush=True)
                _rtss_unavailable_logged = True
            _rtss_handle = None
            return False

        sig_bytes = ctypes.string_at(ptr, 4)
        sig = struct.unpack('<I', sig_bytes)[0]
        # 'RTSS' as a little-endian DWORD per the SDK header
        RTSS_SIG = struct.unpack('<I', b'SSTR')[0]  # confirmed via live testing: RTSS writes the signature bytes in this order in memory
        if sig != RTSS_SIG:
            log.debug('  RTSS signature mismatch: got 0x%08X, expected 0x%08X (0xDEAD means RTSS is shutting down)' % (sig, RTSS_SIG))
            kernel32.UnmapViewOfFile(ptr)
            kernel32.CloseHandle(win_handle)
            _rtss_handle = None
            return False

        version = struct.unpack_from('<I', ctypes.string_at(ptr+4, 4))[0]
        if version < 0x00020000:
            # Older v1.x struct doesn't have per-app entries we need
            log.warning('  RTSS version 0x%08X is older than v2.0 - per-app data unavailable' % (version,))
            kernel32.UnmapViewOfFile(ptr)
            kernel32.CloseHandle(win_handle)
            _rtss_handle = None
            return False

        _rtss_handle = (kernel32, win_handle, ptr, None)
        _rtss_unavailable_logged = False
        log.info('RTSS shared memory connected OK (mapping="%s", version=0x%08X)' % (opened_name, version))
        return True

    except Exception as e:
        if not _rtss_unavailable_logged:
            log.debug('RTSS shared memory unavailable: %r' % (e,))
            _rtss_unavailable_logged = True
        _rtss_handle = None
        return False


def list_rtss_apps():
    """Return a list of (process_id, exe_name, framerate) for all active
    3D applications currently tracked by RTSS. Used by Settings to let the
    user pick a specific process instead of relying on auto-detection."""
    global _rtss_handle
    apps = []
    if _rtss_handle is None:
        if not try_open_rtss():
            return apps
    try:
        import ctypes
        kernel32, win_handle, ptr, size = _rtss_handle

        # Re-verify signature hasn't gone stale (RTSS could have shut down
        # since we last opened the mapping)
        hdr = ctypes.string_at(ptr, 36)
        sig = struct.unpack_from('<I', hdr, 0)[0]
        RTSS_SIG = struct.unpack('<I', b'SSTR')[0]  # confirmed via live testing: RTSS writes the signature bytes in this order in memory
        if sig != RTSS_SIG:
            try:
                kernel32.UnmapViewOfFile(ptr)
                kernel32.CloseHandle(win_handle)
            except Exception:
                pass
            _rtss_handle = None
            return apps

        # v2.0 header layout (RTSS_SHARED_MEMORY):
        # dwSignature(4) dwVersion(4) dwAppEntrySize(4) dwAppArrOffset(4)
        # dwAppArrSize(4) dwOSDEntrySize(4) dwOSDArrOffset(4) dwOSDArrSize(4)
        # dwOSDFrame(4) = 36 bytes, then arrOSD[8], then arrApp[256]
        app_entry_size = struct.unpack_from('<I', hdr, 8)[0]
        app_arr_offset = struct.unpack_from('<I', hdr, 12)[0]

        if app_entry_size <= 0 or app_entry_size > 100000:
            return apps  # sanity check — malformed/unexpected layout

        for i in range(256):
            entry_ptr = ptr + app_arr_offset + i*app_entry_size
            # dwProcessID(4) at offset 0, szName[MAX_PATH=260] at offset 4
            pid_bytes = ctypes.string_at(entry_ptr, 4)
            pid = struct.unpack('<I', pid_bytes)[0]
            if pid == 0:
                continue  # empty slot
            name_bytes = ctypes.string_at(entry_ptr + 4, 260)
            name = name_bytes.split(b'\x00', 1)[0].decode('utf-8', errors='replace')
            if not name:
                continue

            # dwStatFramerateAvg (offset 308) turned out to be tied to RTSS's
            # benchmark/stat-recording session lifecycle — it can spike
            # absurdly high right as a session starts, then drop to 0 once
            # that recording window ends, since it's not continuously live.
            # dwStatFrameTimeBufFramerate (offset 5024, v2.5+) is the value
            # backing RTSS's own ring buffer of recent frametimes — it
            # updates continuously every frame with no recording-session
            # concept, which is what working third-party readers like
            # CapFrameX use. Stored in units of 0.1 FPS. Fall back to the
            # older instantaneous dwFrameTime-based calc on RTSS versions
            # too old to have this field at all.
            if app_entry_size >= 5028:
                favg_bytes = ctypes.string_at(entry_ptr + 5024, 4)
                fps = struct.unpack('<I', favg_bytes)[0] / 10.0
            else:
                frametime_bytes = ctypes.string_at(entry_ptr + 280, 4)
                frame_time_us = struct.unpack('<I', frametime_bytes)[0]
                fps = (1000000.0 / frame_time_us) if frame_time_us > 0 else 0.0

            apps.append((pid, name, round(fps, 1)))

    except Exception as e:
        log.error('RTSS app list error: %r' % (e,))
    return apps


def read_rtss_framerate():
    """Return the current framerate (float) from RTSS, or None if RTSS
    isn't running / no active 3D app is detected. Optional feature —
    callers should treat None as 'sensor unavailable', not an error.

    Selection behavior:
      - cfg['rtss_process'] set (non-empty) -> match that exe name exactly
      - otherwise -> auto-pick the active app: the entry with the most
        recently updated dwTime1, which corresponds to whichever hooked
        3D application most recently rendered a frame (i.e. the one
        currently in the foreground / actively rendering)
    """
    global _rtss_handle, _rtss_last_attempt
    if _rtss_handle is None:
        now = time.time()
        if now - _rtss_last_attempt < _rtss_retry_interval:
            return None  # still cooling down since the last failed attempt
        _rtss_last_attempt = now
        if not try_open_rtss():
            return None
    try:
        import ctypes
        kernel32, win_handle, ptr, size = _rtss_handle

        hdr = ctypes.string_at(ptr, 36)
        sig = struct.unpack_from('<I', hdr, 0)[0]
        RTSS_SIG = struct.unpack('<I', b'SSTR')[0]  # confirmed via live testing: RTSS writes the signature bytes in this order in memory
        if sig != RTSS_SIG:
            # Signature flipped to 0xDEAD (or similar) — RTSS shut down while
            # we were holding the mapping open. Drop our handle so the next
            # call to try_open_rtss() actually attempts a fresh connection
            # instead of silently reusing a dead one forever.
            try:
                kernel32.UnmapViewOfFile(ptr)
                kernel32.CloseHandle(win_handle)
            except Exception:
                pass
            _rtss_handle = None
            return None

        app_entry_size = struct.unpack_from('<I', hdr, 8)[0]
        app_arr_offset = struct.unpack_from('<I', hdr, 12)[0]
        if app_entry_size <= 0 or app_entry_size > 100000:
            return None

        target_name = cfg.get('rtss_process', '').strip().lower()

        best_pid = None
        best_time1 = -1
        best_fps = None
        best_entry_ptr = None

        for i in range(256):
            entry_ptr = ptr + app_arr_offset + i*app_entry_size
            pid_bytes = ctypes.string_at(entry_ptr, 4)
            pid = struct.unpack('<I', pid_bytes)[0]
            if pid == 0:
                continue

            name_bytes = ctypes.string_at(entry_ptr + 4, 260)
            name = name_bytes.split(b'\x00', 1)[0].decode('utf-8', errors='replace')

            time1_bytes = ctypes.string_at(entry_ptr + 272, 4)
            time1 = struct.unpack('<I', time1_bytes)[0]

            # dwStatFrameTimeBufFramerate (offset 5024, v2.5+): backed by
            # RTSS's continuously-updating ring buffer of recent
            # frametimes, with no recording-session lifecycle — unlike
            # dwStatFramerateAvg, this won't spike then drop to zero.
            # Stored in units of 0.1 FPS. Fall back to the older
            # instantaneous dwFrameTime-based calc on RTSS versions too old
            # to have this field at all.
            if app_entry_size >= 5028:
                favg_bytes = ctypes.string_at(entry_ptr + 5024, 4)
                fps = struct.unpack('<I', favg_bytes)[0] / 10.0
            else:
                frametime_bytes = ctypes.string_at(entry_ptr + 280, 4)
                frame_time_us = struct.unpack('<I', frametime_bytes)[0]
                fps = (1000000.0 / frame_time_us) if frame_time_us > 0 else 0.0

            if target_name:
                if name.lower() == target_name:
                    return _build_rtss_result(entry_ptr, app_entry_size, fps, ctypes)
                continue

            # Auto mode: pick whichever app most recently rendered a frame
            if time1 > best_time1:
                best_time1 = time1
                best_pid = pid
                best_fps = fps
                best_entry_ptr = entry_ptr

        if target_name:
            return None  # configured process not currently found/running
        if best_fps is None:
            return None
        return _build_rtss_result(best_entry_ptr, app_entry_size, best_fps, ctypes)

    except Exception as e:
        log.debug('RTSS read error: %r' % (e,))
        return None

def _build_rtss_result(entry_ptr, app_entry_size, fps, ctypes):
    """Build the RTSS sensor dict from an app entry pointer.
    Returns dict with RTSS_FPS, RTSS_FPS_MIN, RTSS_FPS_MAX, RTSS_FPS_AVG."""
    result = {'RTSS_FPS': round(fps, 1)}
    try:
        # dwFrameTime at offset 280 is microseconds per frame in RTSS v2.
        if app_entry_size >= 284:
            frame_time_us = struct.unpack('<I', ctypes.string_at(entry_ptr + 280, 4))[0]
            if 0 < frame_time_us < 1000000:
                result['RTSS_FRAMETIME_MS'] = frame_time_us / 1000.0
    except Exception:
        pass
    try:
        # dwStatFramerateMin @304, dwStatFramerateAvg @308, dwStatFramerateMax @312
        # These are session-based averages but still useful for display when available.
        # They reset with each benchmark session but give min/max context when non-zero.
        if app_entry_size >= 316:
            fmin = struct.unpack('<I', ctypes.string_at(entry_ptr + 304, 4))[0]
            favg = struct.unpack('<I', ctypes.string_at(entry_ptr + 308, 4))[0]
            fmax = struct.unpack('<I', ctypes.string_at(entry_ptr + 312, 4))[0]
            # Only expose these if they have plausible non-zero values
            if fmin > 0: result['RTSS_FPS_MIN'] = float(fmin)
            if favg > 0: result['RTSS_FPS_AVG'] = float(favg)
            if fmax > 0: result['RTSS_FPS_MAX'] = float(fmax)
    except Exception:
        pass
    return result

# ── Font loader ───────────────────────────────────────────────────────────────
_font_cache = {}
_custom_font_files = {}  # family name -> temp file path (extracted from theme)

def _find_windows_font(family, bold=False):
    """Search Windows font directories for a font matching the family name."""
    import glob, re

    # Known exact mappings for common fonts
    KNOWN = {
        'Consolas':    ('consolab.ttf', 'consola.ttf'),
        'Arial':       ('arialbd.ttf',  'arial.ttf'),
        'Segoe UI':    ('segoeuib.ttf', 'segoeui.ttf'),
        'Courier New': ('courbd.ttf',   'cour.ttf'),
        'Tahoma':      ('tahomabd.ttf', 'tahoma.ttf'),
        'Verdana':     ('verdanab.ttf', 'verdana.ttf'),
        'Impact':      ('impact.ttf',   'impact.ttf'),
        'Georgia':     ('georgiab.ttf', 'georgia.ttf'),
        'Calibri':     ('calibrib.ttf', 'calibri.ttf'),
        'Times New Roman': ('timesbd.ttf', 'times.ttf'),
        'Comic Sans MS':   ('comicbd.ttf', 'comic.ttf'),
        'Trebuchet MS':    ('trebucbd.ttf','trebuc.ttf'),
        'Palatino Linotype':('palabd.ttf','pala.ttf'),
        'Century Gothic':  ('gothicb.ttf','gothic.ttf'),
    }

    font_dirs = [
        'C:/Windows/Fonts/',
        os.path.expanduser('~/AppData/Local/Microsoft/Windows/Fonts/'),
    ]

    # Try known mapping first
    if family in KNOWN:
        b, r = KNOWN[family]
        fname = b if bold else r
        for d in font_dirs:
            p = d + fname
            if os.path.exists(p): return p
        # Try the other variant
        fname2 = r if bold else b
        for d in font_dirs:
            p = d + fname2
            if os.path.exists(p): return p

    # Dynamic search: look for files matching the family name
    safe = re.sub(r'[^a-z0-9]', '', family.lower())
    for d in font_dirs:
        candidates = glob.glob(d + '*.ttf') + glob.glob(d + '*.otf')
        scored = []
        for path in candidates:
            base = re.sub(r'[^a-z0-9]', '', os.path.basename(path).lower())
            if safe in base:
                # Prefer bold variants when bold=True
                is_bold = any(x in base for x in ['bold','bd','b'])
                score = (is_bold == bold) * 2 + (safe == base.replace('.ttf','').replace('.otf',''))
                scored.append((score, path))
        if scored:
            scored.sort(reverse=True)
            return scored[0][1]

    return None

def get_font(family='Consolas', size=32, bold=False):
    key = (family, size, bold)
    if key in _font_cache: return _font_cache[key]

    font = None

    # 1. Try custom embedded font first (extracted from theme)
    if family in _custom_font_files:
        try:
            font = ImageFont.truetype(_custom_font_files[family], size)
        except Exception as e:
            log.warning(f'Custom font load error ({family}): {e}')

    # 2. Search Windows Fonts
    if not font:
        path = _find_windows_font(family, bold)
        if path:
            try:
                font = ImageFont.truetype(path, size)
            except Exception as e:
                log.warning(f'Font load error ({path}): {e}')

    # 3. Fallback to Consolas
    if not font:
        try:
            fb = 'C:/Windows/Fonts/' + ('consolab.ttf' if bold else 'consola.ttf')
            font = ImageFont.truetype(fb, size)
        except:
            font = ImageFont.load_default()

    if family not in _font_cache or font:
        _font_cache[key] = font
    return font

def load_custom_fonts_from_theme(theme):
    """Extract embedded font data URLs from theme and write to temp files."""
    import tempfile, base64
    for cf in theme.get('customFonts', []):
        family = cf.get('family','')
        data_url = cf.get('data','')
        filename = cf.get('filename','font.ttf')
        if not family or not data_url or ',' not in data_url:
            continue
        try:
            _, b64 = data_url.split(',', 1)
            font_bytes = base64.b64decode(b64)
            ext = os.path.splitext(filename)[1] or '.ttf'
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
            tmp.write(font_bytes)
            tmp.close()
            _custom_font_files[family] = tmp.name
            # Clear cache entries for this family so they get reloaded
            for k in list(_font_cache.keys()):
                if k[0] == family: del _font_cache[k]
            log.debug(f'Custom font extracted: "{family}" -> {tmp.name}')
        except Exception as e:
            log.warning(f'Custom font extract error ({family}): {e}')

# ── Colour helpers ────────────────────────────────────────────────────────────
def parse_color(c):
    """Parse #rrggbbaa (builder format) → (r,g,b,a) tuple."""
    if not c or len(c)<7: return (255,255,255,255)
    c=c.lstrip('#')
    if len(c)==6:  return (int(c[0:2],16),int(c[2:4],16),int(c[4:6],16),255)
    if len(c)==8:  return (int(c[0:2],16),int(c[2:4],16),int(c[4:6],16),int(c[6:8],16))
    return (255,255,255,255)

def color_rgb(c):
    r,g,b,a = parse_color(c)
    return (r,g,b)

def color_rgba(c):
    return parse_color(c)

# ── Graph history buffers ──────────────────────────────────────────────────────
_graph_history = {}  # elem_id -> deque of values

def graph_push(eid, value, maxlen=120):
    from collections import deque
    if eid not in _graph_history:
        _graph_history[eid] = deque(maxlen=maxlen)
    _graph_history[eid].append(value)

def graph_get(eid):
    return list(_graph_history.get(eid, []))

# ── Renderer ──────────────────────────────────────────────────────────────────
def render_frame(theme, sensors):
    W = theme.get('width', 462)
    H = theme.get('height', 1920)
    bg = color_rgb(theme.get('background','#111114ff'))
    img = Image.new('RGB', (W, H), bg)

    # Composite background image if present
    bg_pil = theme.get('_background_image')
    if bg_pil:
        try:
            resized = bg_pil.resize((W, H), Image.LANCZOS).convert('RGB')
            img.paste(resized, (0, 0))
        except Exception as e:
            log.warning(f'Background image error: {e}')

    draw = ImageDraw.Draw(img, 'RGBA')
    now = datetime.now()

    def sv(key, default=0):
        return sensors.get(key, default)

    # Render only elements assigned to the active auto-screen; untagged legacy themes remain unchanged.
    active_mode = sensors.get('_DISPLAY_MODE', 'gaming')
    elements = [e for e in theme.get('elements', []) if e.get('screenMode') in (None, 'all', active_mode)]
    elements = sorted(elements, key=lambda e: e.get('z',0))

    for el in elements:
        if not el.get('visible', True): continue
        x   = int(el.get('x', 0))
        y   = int(el.get('y', 0))
        w   = int(el.get('w', 100))
        h   = int(el.get('h', 30))
        typ = el.get('type','')

        if typ in ('text','static','clock','date','weekday'):
            fs    = int(el.get('fontSize', 32))
            fam   = el.get('fontFamily', 'Consolas')
            bold  = el.get('bold', False)
            color = color_rgb(el.get('color','#ffffffff'))
            align = el.get('align','left')
            pre   = el.get('prefix','')
            unit  = el.get('unit','')

            if typ=='clock':
                fmt  = el.get('clockFormat','12h')
                secs = el.get('clockSeconds', True)
                if fmt=='12h':
                    if secs:
                        # With seconds: zero-pad hour so AM/PM stays stable
                        text = now.strftime('%I:%M:%S %p')
                    else:
                        # No seconds: strip leading zero, no shifting issue
                        text = now.strftime('%I:%M %p').lstrip('0')
                else:
                    text = now.strftime('%H:%M:%S' if secs else '%H:%M')
            elif typ=='date':
                dfmt = el.get('dateFormat','DD-MM-YYYY')
                # Russian month names are explicit: independent of Windows locale.
                months = ['января','февраля','марта','апреля','мая','июня','июля','августа','сентября','октября','ноября','декабря']
                months_short = ['янв','фев','мар','апр','май','июн','июл','авг','сен','окт','ноя','дек']
                text = dfmt.replace('YYYY',now.strftime('%Y'))\
                           .replace('MMMM',months[now.month-1])\
                           .replace('MMM',months_short[now.month-1])\
                           .replace('MM',now.strftime('%m'))\
                           .replace('DD',now.strftime('%d'))\
                           .replace('D',str(now.day))
            elif typ=='weekday':
                wfmt = el.get('weekdayFormat','full')
                weekdays = ['понедельник','вторник','среда','четверг','пятница','суббота','воскресенье']
                weekdays_short = ['ПН','ВТ','СР','ЧТ','ПТ','СБ','ВС']
                text = weekdays[now.weekday()] if wfmt=='full' else weekdays_short[now.weekday()]
            elif typ=='static':
                text = el.get('customText','Label')
                # Weather heading follows the city selected in Settings.
                if isinstance(text, str) and re.match(r'^ПОГОДА\s*/\s*', text, re.IGNORECASE):
                    live_city = sensors.get('WEATHER_CITY')
                    if isinstance(live_city, str) and live_city.strip():
                        text = re.sub(r'^(ПОГОДА\s*/\s*).+$', lambda m: m.group(1) + live_city.strip().upper(), text, flags=re.IGNORECASE)
            else:  # sensor value text
                sensor_key = el.get('sensorKey','')
                raw = sensors.get(sensor_key, None)
                unit = el.get('unit','')
                if raw is None:
                    val_str = '—'
                elif isinstance(raw, str):
                    val_str = raw
                elif isinstance(raw, float):
                    whole_units = {'%', 'RPM', '\u00B0C', 'C', 'MHz', 'W'}
                    if unit.strip() in whole_units or raw == int(raw):
                        val_str = str(int(round(raw)))
                    else:
                        val_str = f'{raw:.1f}'
                else:
                    val_str = str(raw)
                text = pre + val_str + (' ' if unit and val_str and val_str != '—' else '') + (unit if val_str != '—' else '')

            font = get_font(fam, fs, bold)
            # Player metadata uses the untruncated source value for width measurement
            # and pixel-based scrolling. Sensor display formatting can otherwise
            # shorten long strings before the marquee gets a chance to run.
            player_field = None
            if el.get('id') == 'work_player_title':
                player_field = 'title'
            elif el.get('id') == 'work_player_artist':
                player_field = 'artist'
            full_value = None
            if player_field:
                source_key = 'NOW_PLAYING_TITLE' if player_field == 'title' else 'NOW_PLAYING_ARTIST'
                full_value = str(sensors.get(source_key, text) or '')
                text = full_value
            try:
                bbox = draw.textbbox((0,0), text, font=font)
                tw = bbox[2]-bbox[0]
                th = bbox[3]-bbox[1]
            except Exception:
                tw, th = fs*len(text)//2, fs
            if player_field and tw > w:
                now_mono = time.monotonic()
                if not hasattr(render_frame, '_pixel_marquee'):
                    render_frame._pixel_marquee = {}
                marquee = render_frame._pixel_marquee
                if marquee.get(player_field, {}).get('value') != full_value:
                    marquee[player_field] = {'value': full_value, 'started': now_mono}
                elapsed = max(0.0, now_mono - marquee[player_field]['started'] - 1.5)
                speed = 24.0  # pixels per second; deliberately calm for the narrow screen
                gap_px = max(42, int(fs * 1.8))
                cycle_px = max(1, tw + gap_px)
                offset = (elapsed * speed) % cycle_px
                # Render to a temporary viewport so glyphs can move by fractional pixels
                # over time without clipping the rest of the UI.
                layer = Image.new('RGBA', (w, h), (0, 0, 0, 0))
                ld = ImageDraw.Draw(layer)
                text_y = max(0, (h - th) // 2 - bbox[1])
                ld.text((int(round(-offset)), text_y), text, font=font, fill=(*color, 255))
                ld.text((int(round(tw + gap_px - offset)), text_y), text, font=font, fill=(*color, 255))
                img.paste(layer, (x, y), layer)
            else:
                if align=='center':   tx = x + (w-tw)//2
                elif align=='right':  tx = x + w - tw
                else:                 tx = x
                draw.text((tx, y), text, font=font, fill=color)

        elif typ=='bar':
            raw_val = float(sv(el.get('sensorKey','CPU_USAGE'), 0))
            max_val = float(el.get('maxValue', 100))
            pct     = max(0.0, min(1.0, raw_val/max_val if max_val else 0))
            rad     = int(el.get('cornerRadius', 0))
            thick   = int(el.get('borderThickness', 0))
            bg_c    = color_rgba(el.get('bgColor','#1a1a2299'))
            fill_c  = color_rgba(el.get('fillColor','#00b4ffff'))
            bord_c  = color_rgba(el.get('borderColor','#00000000'))
            style   = el.get('barStyle','solid')

            if style == 'segmented':
                # Discrete LED-style blocks — each segment is either fully lit or unlit
                segs    = int(el.get('segmentCount', 12))
                gap_pct = float(el.get('segmentGap', 18)) / 100.0
                lit_count = round(pct * segs)
                seg_w_full = w / segs
                seg_w = seg_w_full * (1 - gap_pct)
                seg_rad = min(rad, 3)
                for i in range(segs):
                    sx = x + i*seg_w_full
                    lit = i < lit_count
                    color = fill_c if lit else bg_c
                    if seg_rad > 0:
                        draw.rounded_rectangle([sx, y, sx+seg_w, y+h], radius=seg_rad, fill=color)
                    else:
                        draw.rectangle([sx, y, sx+seg_w, y+h], fill=color)

            elif style == 'gapped':
                # Continuous fill with thin vertical gap lines overlaid
                segs  = int(el.get('segmentCount', 16))
                gap_w = max(1, int(el.get('segmentGap', 2)))
                if rad > 0:
                    draw.rounded_rectangle([x,y,x+w,y+h], radius=rad, fill=bg_c)
                else:
                    draw.rectangle([x,y,x+w,y+h], fill=bg_c)
                fw = int(w*pct)
                if fw > 0:
                    if rad > 0:
                        draw.rounded_rectangle([x,y,x+fw,y+h], radius=rad, fill=fill_c)
                    else:
                        draw.rectangle([x,y,x+fw,y+h], fill=fill_c)
                # Overlay gap lines using the background color, cutting through both fill and empty zones
                seg_w_full = w / segs
                for i in range(1, segs):
                    gx = x + i*seg_w_full
                    draw.rectangle([gx-gap_w/2, y, gx+gap_w/2, y+h], fill=bg_c)
                if thick > 0:
                    draw.rectangle([x,y,x+w,y+h], outline=bord_c, width=thick)

            else:  # solid
                if rad > 0:
                    draw.rounded_rectangle([x,y,x+w,y+h], radius=rad, fill=bg_c)
                else:
                    draw.rectangle([x,y,x+w,y+h], fill=bg_c)
                fw = int(w*pct)
                if fw > 0:
                    if rad > 0:
                        draw.rounded_rectangle([x,y,x+fw,y+h], radius=rad, fill=fill_c)
                    else:
                        draw.rectangle([x,y,x+fw,y+h], fill=fill_c)
                if thick > 0:
                    draw.rectangle([x,y,x+w,y+h], outline=bord_c, width=thick)

        elif typ=='ring':
            raw_val = float(sv(el.get('sensorKey','CPU_USAGE'), 0))
            max_val = float(el.get('maxValue', 100))
            pct     = max(0.0, min(1.0, raw_val/max_val if max_val else 0))
            rw      = int(el.get('ringWidth', 14))
            arc_c   = color_rgb(el.get('arcColor','#00b4ffff'))
            trk_c   = color_rgb(el.get('trackColor','#1a1a3399'))
            diam    = min(w, h)
            margin  = rw//2 + 2
            box     = [x+margin, y+margin, x+diam-margin, y+diam-margin]
            ring_style = el.get('ringStyle', 'solid')

            if ring_style == 'segmented':
                # Discrete arc blocks all the way around; lit segments use arc_c,
                # unlit use trk_c. Matches the builder's SVG segmented preview.
                segs    = int(el.get('segmentCount', 24))
                gap_deg = float(el.get('segmentGap', 6))
                seg_deg = 360.0 / segs
                arc_deg = max(0.5, seg_deg - gap_deg)
                lit_count = round(pct * segs)
                for i in range(segs):
                    start_a = -90 + i*seg_deg + gap_deg/2
                    end_a   = start_a + arc_deg
                    lit     = i < lit_count
                    color   = arc_c if lit else trk_c
                    draw.arc(box, start_a, end_a, fill=color, width=rw)
            else:
                # Track
                draw.arc(box, 0, 360, fill=trk_c, width=rw)
                # Arc
                start_a = -90
                end_a   = start_a + int(360*pct)
                if pct > 0:
                    draw.arc(box, start_a, end_a, fill=arc_c, width=rw)

            # Label
            if el.get('showLabel', True):
                lfs   = int(el.get('labelFontSize', 28))
                lfam  = el.get('labelFontFamily','Consolas')
                lbold = el.get('labelBold', True)
                lcolor= color_rgb(el.get('labelColor','#ffffffff'))
                unit  = el.get('unit','')
                label = f'{int(raw_val)}{unit}'
                lfont = get_font(lfam, lfs, lbold)
                try:
                    bbox = draw.textbbox((0,0), label, font=lfont)
                    tw,th = bbox[2]-bbox[0], bbox[3]-bbox[1]
                except: tw=th=lfs
                cx = x + diam//2 - tw//2
                cy = y + diam//2 - th//2
                draw.text((cx,cy), label, font=lfont, fill=lcolor)

        elif typ=='linegraph':
            eid     = el.get('id','')
            hist_s  = int(el.get('historySeconds', 60))
            maxlen  = max(10, hist_s * cfg.get('fps',6))

            # Build series list: series 1 = left axis, series 2/3 = right axis
            max_val  = float(el.get('maxValue', 100))
            max_val2 = float(el.get('maxValue2', 100))
            series_defs = [
                (el.get('sensorKey',''),  el.get('lineColor','#00b4ffff'),  max_val,  el.get('fillColor')),
            ]
            if el.get('sensorKey2'):
                series_defs.append((el.get('sensorKey2'), el.get('lineColor2','#ff3df0ff'), max_val2, None))
            if el.get('sensorKey3'):
                series_defs.append((el.get('sensorKey3'), el.get('lineColor3','#5effc0ff'), max_val2, None))

            lw   = int(el.get('lineWidth',2))
            rad  = int(el.get('cornerRadius',4))
            bg_c = color_rgba(el.get('bgColor','#0a0a1499'))
            gc   = color_rgba(el.get('gridColor','#ffffff22'))
            show_grid = el.get('showGrid', True)

            if rad>0: draw.rounded_rectangle([x,y,x+w,y+h],radius=rad,fill=bg_c)
            else:     draw.rectangle([x,y,x+w,y+h],fill=bg_c)

            if show_grid:
                for gi in range(1,4):
                    gy = y + h*gi//4
                    draw.line([(x,gy),(x+w,gy)], fill=gc, width=1)

            for si, (skey, lcolor, smax, fillcolor) in enumerate(series_defs):
                if not skey: continue
                raw_val = float(sv(skey, 0))
                hist_key = f'{eid}_{si}'
                if hist_key not in _graph_history:
                    from collections import deque
                    _graph_history[hist_key] = deque(maxlen=maxlen)
                _graph_history[hist_key].append(raw_val)
                pts = list(_graph_history[hist_key])
                if len(pts) < 2: continue

                lc = color_rgb(lcolor)
                n = len(pts)
                def gx(i): return x + int(i/(n-1)*w)
                def gy_v(v, smax=smax): return y+h - int(max(0,min(1,v/smax if smax else 0))*h*0.88+h*0.06)
                coords = [(gx(i), gy_v(v)) for i,v in enumerate(pts)]

                # Only series 1 gets a fill-under (matches builder preview behavior)
                if si == 0 and fillcolor:
                    fc = color_rgba(fillcolor)
                    poly = [(x,y+h)] + coords + [(x+w,y+h)]
                    draw.polygon(poly, fill=fc)

                draw.line(coords, fill=lc, width=lw)

        elif typ=='monthcalendar':
            # Compact Russian monthly calendar with today's date highlighted.
            months_ru = ['ЯНВАРЬ','ФЕВРАЛЬ','МАРТ','АПРЕЛЬ','МАЙ','ИЮНЬ','ИЮЛЬ','АВГУСТ','СЕНТЯБРЬ','ОКТЯБРЬ','НОЯБРЬ','ДЕКАБРЬ']
            import calendar
            accent = color_rgba(el.get('accentColor', '#ff6680ff'))
            fg = color_rgba(el.get('color', '#e8f2ffff'))
            muted = color_rgba(el.get('mutedColor', '#8fa9caff'))
            draw.text((x, y), f"{months_ru[now.month-1]} {now.year}", font=get_font('Consolas', int(el.get('titleFontSize', 24)), True), fill=accent)
            headers = ['ПН','ВТ','СР','ЧТ','ПТ','СБ','ВС']
            colw = w / 7
            header_y = y + int(el.get('headerOffset', 38))
            hf = get_font('Consolas', int(el.get('headerFontSize', 17)), True)
            df = get_font('Consolas', int(el.get('fontSize', 21)), True)
            for i, hd in enumerate(headers):
                bb = draw.textbbox((0,0), hd, font=hf); tw=bb[2]-bb[0]
                draw.text((int(x+i*colw+(colw-tw)/2), header_y), hd, font=hf, fill=muted)
            weeks = calendar.monthcalendar(now.year, now.month)
            row_h = int(el.get('rowHeight', 34)); start_y = header_y + 27
            for ri, week in enumerate(weeks):
                for ci, day in enumerate(week):
                    if not day: continue
                    cx = int(x + ci*colw + colw/2); cy = start_y + ri*row_h
                    if day == now.day:
                        rr = int(el.get('highlightRadius', 15))
                        draw.ellipse((cx-rr, cy-rr+2, cx+rr, cy+rr+2), fill=accent)
                        txtc = color_rgba('#07101fff')
                    else:
                        txtc = fg
                    label = str(day); bb=draw.textbbox((0,0),label,font=df); tw=bb[2]-bb[0]
                    draw.text((int(cx-tw/2), cy-12), label, font=df, fill=txtc)

        elif typ=='weathericon':
            # Compact animated weather glyph for the gaming footer row.
            import math
            phase = time.monotonic()
            wc = int(sensors.get('WEATHER_CODE', -1) or -1)
            day = bool(int(sensors.get('WEATHER_IS_DAY', 1) or 0))
            cx, cy = x + w//2, y + h//2
            if wc in (0, 1, 2, 3, -1):
                if day:
                    draw.ellipse((cx-15,cy-22,cx+15,cy+8),fill=(255,190,55,255))
                    for a in range(8):
                        ang=a*math.pi/4+phase*0.12
                        draw.line((cx+int(19*math.cos(ang)),cy-7+int(19*math.sin(ang)),cx+int(26*math.cos(ang)),cy-7+int(26*math.sin(ang))),fill=(255,208,90,255),width=2)
                else:
                    draw.ellipse((cx-15,cy-19,cx+15,cy+11),fill=(215,230,255,255))
                    draw.ellipse((cx-5,cy-24,cx+22,cy+4),fill=(25,16,26,255))
            if wc in (2,3,45,48,51,53,55,56,57,61,63,65,66,67,80,81,82,71,73,75,77,85,86,95,96,99,-1):
                cloud=(205,218,235,255) if wc not in (45,48) else (155,170,190,255)
                draw.ellipse((cx-28,cy-2,cx-5,cy+20),fill=cloud)
                draw.ellipse((cx-16,cy-14,cx+10,cy+21),fill=cloud)
                draw.ellipse((cx+1,cy-5,cx+27,cy+20),fill=cloud)
                draw.rounded_rectangle((cx-23,cy+4,cx+21,cy+21),radius=6,fill=cloud)
            if wc in (51,53,55,56,57,61,63,65,66,67,80,81,82,95,96,99):
                for i in range(3):
                    xx=cx-13+i*13
                    yy=cy+24+int((phase*32+i*9)%9)
                    draw.line((xx,yy,xx-4,yy+7),fill=(80,180,255,255),width=2)
            elif wc in (71,73,75,77,85,86):
                for i in range(4):
                    xx=cx-15+i*10
                    yy=cy+24+int((phase*18+i*7)%8)
                    draw.ellipse((xx-2,yy-2,xx+2,yy+2),fill=(245,250,255,255))

        elif typ=='rect':
            _rect_fill = el.get('fillColor', '#00000000')
            if el.get('id') in ('idle_weather_card', 'work_date_card'):
                _wc = int(sensors.get('WEATHER_CODE', -1) or -1)
                _day = bool(int(sensors.get('WEATHER_IS_DAY', 1) or 0))
                if _wc in (95, 96, 99): _rect_fill = '#4a246fff' if _day else '#24183fff'
                elif _wc in (71, 73, 75, 77, 85, 86): _rect_fill = '#34536fff' if _day else '#1d2d46ff'
                elif _wc in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82): _rect_fill = '#126b8aff' if _day else '#10354fff'
                elif _wc in (45, 48): _rect_fill = '#65758aff' if _day else '#343e55ff'
                elif _wc in (2, 3): _rect_fill = '#386cb0ff' if _day else '#202c64ff'
                elif _wc in (0, 1): _rect_fill = '#1672d4ff' if _day else '#111e5aff'
            fill_c = color_rgba(_rect_fill)
            rad    = int(el.get('cornerRadius',0))
            if rad>0: draw.rounded_rectangle([x,y,x+w,y+h],radius=rad,fill=fill_c)
            else:     draw.rectangle([x,y,x+w,y+h],fill=fill_c)

        elif typ=='image':
            # Image layers are loaded at theme load time
            img_data = el.get('_pil_image')
            if img_data:
                try:
                    resized = img_data.resize((w,h), Image.LANCZOS)
                    img.paste(resized, (x,y))
                except: pass
        elif typ == 'image_sensor':
            # Album art arrives as bytes from the Windows media session.
            raw_art = sensors.get(el.get('sensorKey', 'NOW_PLAYING_ART_BYTES'))
            if raw_art:
                try:
                    import hashlib
                    cache_key = hashlib.md5(raw_art).hexdigest()
                    cache = getattr(render_frame, '_album_art_cache', None)
                    if cache is None or cache[0] != cache_key:
                        with Image.open(io.BytesIO(raw_art)) as source_art:
                            art = source_art.convert('RGB')
                        side = max(w, h)
                        # Center-crop square cover, then scale to the display box.
                        aw, ah = art.size
                        crop_side = min(aw, ah)
                        left = (aw - crop_side) // 2
                        top = (ah - crop_side) // 2
                        art = art.crop((left, top, left + crop_side, top + crop_side))
                        art = art.resize((w, h), Image.LANCZOS)
                        render_frame._album_art_cache = (cache_key, art)
                    else:
                        art = cache[1]
                    img.paste(art, (x, y))
                except Exception as e:
                    log.debug('Album art render failed: %r', e)
            else:
                # Clean placeholder when the media app does not publish a thumbnail.
                import math
                cx, cy = x + w//2, y + h//2
                draw.rounded_rectangle((x, y, x+w, y+h), radius=10, fill=(25, 25, 32, 255), outline=(255, 50, 65, 220), width=2)
                draw.ellipse((cx-w//4, cy-h//4, cx+w//4, cy+h//4), outline=(255, 50, 65, 230), width=3)
                draw.ellipse((cx-w//10, cy-h//10, cx+w//10, cy+h//10), fill=(255, 50, 65, 230))
        elif typ == 'equalizer':
            # Vertical bars rise from a shared baseline. On pause, preserve the
            # last animated levels rather than collapsing them to a flat line.
            import math
            playing_now = bool(sensors.get('NOW_PLAYING_PLAYING', False)) and cfg.get('decorative_animation', True)
            phase = time.monotonic() * float(el.get('speed', 5.0))
            count = max(5, int(el.get('barCount', 17)))
            gap = max(2, int(el.get('barGap', 5)))
            bar_w = max(2, (w - gap * (count - 1)) // count)
            color = color_rgba(el.get('fillColor', '#ff3344ff'))
            dim_color = color_rgba(el.get('bgColor', '#4b2028ff'))
            max_h = h
            track_key = str(sensors.get('NOW_PLAYING_TITLE', ''))
            state = getattr(render_frame, '_eq_state', None)
            if not isinstance(state, dict) or state.get('count') != count:
                state = {'count': count, 'playing': playing_now, 'track': track_key,
                         'levels': [max(3, int(max_h * (0.18 + 0.65 * ((math.sin(i*1.31)+1)/2)))) for i in range(count)]}
            if playing_now:
                # Smooth pseudo-spectrum animation; each bar remains visibly vertical.
                levels = []
                for i in range(count):
                    wave = (math.sin(phase + i * 0.77) + 1) / 2
                    wave2 = (math.sin(phase * 0.67 - i * 0.43) + 1) / 2
                    bh = max(3, int(max_h * (0.14 + 0.82 * (0.62*wave + 0.38*wave2))))
                    levels.append(bh)
                state['levels'] = levels
                state['playing'] = True
                state['track'] = track_key
            else:
                # If a new track appears while paused, seed a distinct static pattern.
                if state.get('playing') or state.get('track') != track_key:
                    if state.get('track') != track_key:
                        state['levels'] = [
                            max(3, int(max_h * (0.18 + 0.65 * ((math.sin(i*1.31 + len(track_key)*0.17)+1)/2))))
                            for i in range(count)
                        ]
                    state['playing'] = False
                    state['track'] = track_key
                levels = state['levels']
            render_frame._eq_state = state
            for i, bh in enumerate(levels):
                bx = x + i * (bar_w + gap)
                by = y + max_h - bh
                draw.rounded_rectangle((bx, by, bx+bar_w, y+max_h),
                                       radius=max(1, bar_w//3),
                                       fill=color)

    # Large animated weather illustration in the lower weather panel. No decorative particle strip.
    if active_mode in ('idle', 'work'):
        try:
            import math
            anim = ImageDraw.Draw(img, 'RGBA')
            # Freeze decorative weather artwork in place when animation is disabled.
            phase = time.monotonic() if cfg.get('decorative_animation', True) else 0.0
            code_raw = sensors.get('WEATHER_CODE', -1)
            code = int(code_raw) if code_raw is not None else -1
            is_day = bool(int(sensors.get('WEATHER_IS_DAY', 1) or 0))
            cx, cy = W // 2, (1740 if active_mode == 'work' else 1420)
            # Larger sun/moon, scaled for the tall 462x1920 display.
            if code in (0, 1, 2, 3, -1):
                if is_day:
                    anim.ellipse((cx-72, cy-72, cx+72, cy+72), fill=(255,190,55,245))
                    for a in range(12):
                        ang=a*math.pi/6+phase*0.10
                        anim.line((cx+int(84*math.cos(ang)),cy+int(84*math.sin(ang)),cx+int(106*math.cos(ang)),cy+int(106*math.sin(ang))),fill=(255,208,90,230),width=5)
                else:
                    anim.ellipse((cx-66,cy-66,cx+66,cy+66),fill=(215,230,255,245))
                    anim.ellipse((cx-28,cy-88,cx+88,cy+28),fill=(7,21,47,255))
            cloudy = code in (2,3,45,48,51,53,55,56,57,61,63,65,66,67,80,81,82,71,73,75,77,85,86,95,96,99,-1)
            if cloudy:
                cloud=(178,202,226,245) if code not in (45,48) else (145,163,181,235)
                ox,oy=cx,cy+24
                anim.ellipse((ox-108,oy-36,ox-22,oy+48),fill=cloud)
                anim.ellipse((ox-66,oy-88,ox+42,oy+50),fill=cloud)
                anim.ellipse((ox+10,oy-56,ox+105,oy+47),fill=cloud)
                anim.rounded_rectangle((ox-88,oy+1,ox+78,oy+52),radius=18,fill=cloud)
            if code in (51,53,55,56,57,61,63,65,66,67,80,81,82,95,96,99):
                for i in range(7):
                    xx=cx-72+i*24
                    yy=cy+100+int((phase*110+i*17)%75)
                    anim.line((xx,yy,xx-10,yy+23),fill=(80,180,255,235),width=5)
            elif code in (71,73,75,77,85,86):
                for i in range(9):
                    xx=cx-88+i*22+int(5*math.sin(phase+i))
                    yy=cy+105+int((phase*36+i*19)%85)
                    r=5 if i%2 else 7
                    anim.ellipse((xx-r,yy-r,xx+r,yy+r),fill=(240,248,255,245))
            if code in (95,96,99):
                anim.line((cx+24,cy+54,cx-2,cy+99,cx+20,cy+99,cx-9,cy+145),fill=(255,226,92,255),width=8)
            # Seasonal motion is used only when no active precipitation is reported.
            month=now.month
            season='winter' if month in (12,1,2) else 'spring' if month in (3,4,5) else 'summer' if month in (6,7,8) else 'autumn'
            if code not in (51,53,55,56,57,61,63,65,66,67,80,81,82,95,96,99,71,73,75,77,85,86):
                colors={'winter':(205,230,255,200),'spring':(255,145,190,200),'summer':(255,205,90,185),'autumn':(224,135,65,210)}
                col=colors[season]
                for i in range(16):
                    px=28+int((i*47+phase*(10+i%4)*2)%(W-56))
                    py=1570+int((i*59+phase*(14+i%5)*2)%(H-1590))
                    r=5+(i%4)
                    if season=='autumn':
                        anim.polygon([(px,py-r),(px+r,py),(px,py+r),(px-r,py)],fill=col)
                    else:
                        anim.ellipse((px-r,py-r,px+r,py+r),fill=col)
        except Exception as e:
            log.debug('Weather animation render failed: %r', e)
    return img

# ── Load theme ─────────────────────────────────────────────────────────────────
_current_theme = None
_current_theme_path = ''

def load_theme(path):
    global _current_theme, _current_theme_path
    try:
        import zipfile, base64
        if path.endswith('.zip'):
            with zipfile.ZipFile(path) as z:
                tfile = next((n for n in z.namelist() if n.endswith('.ds916theme')), None)
                if not tfile: return False
                theme = json.loads(z.read(tfile).decode())
                # Load image layers from ZIP
                for el in theme.get('elements',[]):
                    if el.get('type')=='image' and el.get('filename'):
                        try:
                            data = z.read(el['filename'])
                            el['_pil_image'] = PILImage.open(io.BytesIO(data)).convert('RGBA')
                        except: pass
                # Load background from ZIP
                back = next((n for n in z.namelist() if n.lower().startswith('back.')), None)
                if back:
                    data = z.read(back)
                    theme['_background_image'] = PILImage.open(io.BytesIO(data)).convert('RGBA')
        elif path.endswith('.ds916theme'):
            with open(path, encoding='utf-8') as f: theme = json.load(f)
            # Background is embedded as a data URL: "data:image/png;base64,..."
            bg_data_url = theme.get('backgroundImage')
            if bg_data_url and isinstance(bg_data_url, str) and ',' in bg_data_url:
                header, b64 = bg_data_url.split(',', 1)
                if 'svg' in header.lower():
                    log.warning('Background image is SVG format, which Pillow cannot decode - '
                          'this theme was likely generated by an older version of the AI '
                          'Theme Generator. Re-generate and re-save the theme in the theme '
                          'builder to fix (newer versions export a PNG background instead).')
                else:
                    try:
                        img_bytes = base64.b64decode(b64)
                        theme['_background_image'] = PILImage.open(io.BytesIO(img_bytes)).convert('RGBA')
                        log.info(f'Background image loaded from embedded data ({len(img_bytes)//1024}KB)')
                    except Exception as e:
                        log.warning(f'Background image decode error: {e}')
            # Image layers are also embedded as data URLs
            for el in theme.get('elements', []):
                if el.get('type') == 'image' and el.get('data'):
                    try:
                        du = el['data']
                        if ',' in du:
                            _, b64 = du.split(',', 1)
                            img_bytes = base64.b64decode(b64)
                            el['_pil_image'] = PILImage.open(io.BytesIO(img_bytes)).convert('RGBA')
                    except Exception as e:
                        log.warning(f'Image layer decode error: {e}')
        else:
            return False

        # Standard sensor keys (CPU_USAGE, GPU_TEMP, etc.) need no mapping
        # step at all -- read_sharedmem() resolves them fresh by NAME on
        # every single read. Any CUSTOM_N entries in this theme's own
        # sensorMap are read directly from _current_theme by read_sensors()
        # when needed, with nothing copied into the global config.

        # Extract and register any custom fonts embedded in the theme
        load_custom_fonts_from_theme(theme)

        _current_theme = theme
        _current_theme_path = path
        cfg['theme_path'] = path
        save_cfg(cfg)
        log.info(f'Theme loaded: {theme.get("name","?")}')
        return True
    except Exception as e:
        log.error(f'Theme load error: {e}')
        return False

# ── Windows Now Playing (GSMTC) ───────────────────────────────────────────────
# Read Windows media sessions on a dedicated asyncio thread. Rendering and
# serial I/O never wait for media metadata calls.
_media_lock = threading.Lock()
_media_data = {
    "NOW_PLAYING_TITLE": "Нет активного трека",
    "NOW_PLAYING_ARTIST": "Запустите Яндекс Музыку или YouTube",
    "NOW_PLAYING_REMAINING": "—:—",
    "NOW_PLAYING_SOURCE": "ОЖИДАНИЕ",
    "NOW_PLAYING_ART_BYTES": None,
    "NOW_PLAYING_PLAYING": False,
}
_media_worker_started = False

def _format_media_time(seconds):
    try:
        seconds = max(0, int(seconds))
    except Exception:
        return "—:—"
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{sec:02d}" if hours else f"{minutes}:{sec:02d}"

def _set_media_data(**values):
    with _media_lock:
        _media_data.update(values)

def _short_media_text(value, limit):
    value = " ".join(str(value or "").split())
    return value if len(value) <= limit else value[:limit-1].rstrip() + "…"

async def _read_media_thumbnail(props):
    # Read GSMTC thumbnail stream into encoded image bytes, if available.
    try:
        thumb = getattr(props, 'thumbnail', None)
        if thumb is None:
            return None
        stream = await thumb.open_read_async()
        size = int(stream.size)
        if size <= 0 or size > 12 * 1024 * 1024:
            return None
        from winsdk.windows.storage.streams import DataReader
        reader = DataReader(stream.get_input_stream_at(0))
        await reader.load_async(size)
        data = bytearray(size)
        reader.read_bytes(data)
        reader.close()
        stream.close()
        return bytes(data)
    except Exception as e:
        log.debug('Media thumbnail unavailable: %r', e)
        return None

async def _media_poll_loop():
    from winsdk.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager,
        GlobalSystemMediaTransportControlsSessionPlaybackStatus,
    )
    import asyncio
    import time as _time

    manager = await GlobalSystemMediaTransportControlsSessionManager.request_async()
    log.info("Windows media sessions connected (GSMTC)")
    last_signature = None
    active_key = None
    duration_seconds = 0.0
    anchor_position = 0.0
    anchor_monotonic = _time.monotonic()
    last_reported_position = None
    last_playing = None
    while True:
        try:
            sessions = list(manager.get_sessions())
            chosen = None
            chosen_info = None
            for session in sessions:
                try:
                    info = session.get_playback_info()
                    if info.playback_status == GlobalSystemMediaTransportControlsSessionPlaybackStatus.PLAYING:
                        chosen, chosen_info = session, info
                        break
                except Exception:
                    continue
            if chosen is None:
                for session in sessions:
                    try:
                        chosen, chosen_info = session, session.get_playback_info()
                        break
                    except Exception:
                        continue

            if chosen is None:
                active_key = None
                duration_seconds = 0.0
                last_reported_position = None
                last_playing = None
                _set_media_data(
                    NOW_PLAYING_TITLE="Нет активного трека",
                    NOW_PLAYING_ARTIST="Запустите Яндекс Музыку или YouTube",
                    NOW_PLAYING_REMAINING="—:—", NOW_PLAYING_SOURCE="ОЖИДАНИЕ",
                    NOW_PLAYING_ART_BYTES=None, NOW_PLAYING_PLAYING=False)
            else:
                props = await chosen.try_get_media_properties_async()
                title = " ".join(str(getattr(props, "title", "") or "").split()) or "Название недоступно"
                artist = " ".join(str(getattr(props, "artist", "") or "").split()) or "Исполнитель не указан"
                art_bytes = await _read_media_thumbnail(props)
                try:
                    source_id = chosen.source_app_user_model_id or ""
                except Exception:
                    source_id = ""
                low = source_id.lower()
                if "yandex" in low:
                    source = "ЯНДЕКС МУЗЫКА"
                elif any(x in low for x in ("chrome", "msedge", "firefox", "brave", "opera", "vivaldi")):
                    source = "YOUTUBE / БРАУЗЕР"
                else:
                    source = (source_id.split("!")[-1] if source_id else "МЕДИА").upper()[:24]

                playing = bool(chosen_info and chosen_info.playback_status == GlobalSystemMediaTransportControlsSessionPlaybackStatus.PLAYING)
                status = "▶" if playing else "Ⅱ"
                now = _time.monotonic()
                try:
                    timeline = chosen.get_timeline_properties()
                    end_time = timeline.end_time
                    position = timeline.position
                    reported_duration = float(end_time.total_seconds())
                    reported_position = max(0.0, float(position.total_seconds()))
                    # GSMTC timelines from some players only update intermittently.
                    # Keep a monotonic local estimate between those updates.
                    track_key = (source_id, title, artist)
                    if track_key != active_key:
                        active_key = track_key
                        last_playing = playing
                        duration_seconds = reported_duration if reported_duration > 0 else 0.0
                        anchor_position = reported_position
                        anchor_monotonic = now
                        last_reported_position = reported_position
                        log.info("Media timeline: source=%s duration=%.1fs position=%.1fs", source, duration_seconds, reported_position)
                    else:
                        if last_playing is not None and playing != last_playing:
                            anchor_position = anchor_position + (now - anchor_monotonic if last_playing else 0.0)
                            anchor_monotonic = now
                        last_playing = playing
                        if reported_duration > 0 and abs(reported_duration - duration_seconds) > 2:
                            duration_seconds = reported_duration
                        estimated_position = anchor_position + (now - anchor_monotonic if playing else 0.0)
                        # Only re-anchor when Windows actually reports a position change.
                        # Some apps (notably browser media sessions) keep returning the same
                        # stale timeline position for several seconds. Comparing that stale
                        # value with our advancing estimate would periodically rewind the
                        # countdown to the same point.
                        if last_reported_position is None:
                            anchor_position = reported_position
                            anchor_monotonic = now
                        else:
                            reported_delta = reported_position - last_reported_position
                            elapsed_since_anchor = now - anchor_monotonic
                            expected_delta = elapsed_since_anchor if playing else 0.0
                            # A genuine seek is a sizable position jump that doesn't match
                            # normal playback progression. Ignore unchanged/stale samples.
                            if abs(reported_delta) > 2.5 and abs(reported_delta - expected_delta) > 2.0:
                                anchor_position = reported_position
                                anchor_monotonic = now
                            elif reported_delta > 0.25 and abs(reported_delta - expected_delta) <= 2.0:
                                # Windows has caught up with the locally estimated timeline.
                                # Re-anchor to its newer sample to avoid long-term drift.
                                anchor_position = reported_position
                                anchor_monotonic = now
                        last_reported_position = reported_position
                    estimated_position = anchor_position + (now - anchor_monotonic if playing else 0.0)
                    if duration_seconds > 0:
                        remaining_seconds = max(0.0, duration_seconds - min(duration_seconds, estimated_position))
                        remaining = "−" + _format_media_time(remaining_seconds)
                    else:
                        remaining = "—:—"
                except Exception as timeline_error:
                    remaining = "—:—"
                    log.debug("Media timeline unavailable for %s: %r", source, timeline_error)

                signature = (title, artist, remaining, source, status)
                if signature != last_signature:
                    log.debug("Now playing changed: %s — %s (%s; remaining %s)", title, artist, source, remaining)
                    last_signature = signature
                _set_media_data(NOW_PLAYING_TITLE=title, NOW_PLAYING_ARTIST=artist,
                    NOW_PLAYING_REMAINING=remaining, NOW_PLAYING_SOURCE=f"{status}  {source}",
                    NOW_PLAYING_ART_BYTES=art_bytes, NOW_PLAYING_PLAYING=playing)
        except Exception as e:
            log.debug("Windows media session poll failed: %r", e)
        await asyncio.sleep(1.0)

def _media_worker():
    try:
        import asyncio
        asyncio.run(_media_poll_loop())
    except ImportError as e:
        log.warning("Windows media metadata unavailable; install optional dependency: py -m pip install winsdk (%s)", e)
        _set_media_data(NOW_PLAYING_TITLE="Медиаданные не подключены",
            NOW_PLAYING_ARTIST="Установите пакет winsdk", NOW_PLAYING_REMAINING="—:—",
            NOW_PLAYING_SOURCE="НЕТ GSMTC", NOW_PLAYING_PLAYING=False, NOW_PLAYING_ART_BYTES=None)
    except Exception as e:
        log.warning("Windows media worker could not start: %r", e)
        _set_media_data(NOW_PLAYING_TITLE="Медиаданные недоступны",
            NOW_PLAYING_ARTIST="Проверьте установку winsdk", NOW_PLAYING_REMAINING="—:—",
            NOW_PLAYING_SOURCE="ОШИБКА GSMTC", NOW_PLAYING_PLAYING=False, NOW_PLAYING_ART_BYTES=None)

def _start_media_worker():
    global _media_worker_started
    if _media_worker_started:
        return
    _media_worker_started = True
    threading.Thread(target=_media_worker, name="DS916-MediaSessions", daemon=True).start()

def _get_media_data():
    # Return unmodified media metadata. Pixel scrolling is performed by the
    # renderer, based on the actual rendered width of each string and font.
    with _media_lock:
        return dict(_media_data)


# ── Serial / streaming ────────────────────────────────────────────────────────
_port       = None
_running    = False
_run_thread = None

def detect_ds916_port():
    """Scan serial ports for a device matching the DS916 VID/PID (33C3:F101).
    Returns the port name if found, otherwise None."""
    try:
        for port in serial.tools.list_ports.comports():
            # pyserial exposes VID/PID on the port info
            if port.vid == 0x33C3 and port.pid == 0xF101:
                log.info(f'DS916 auto-detected on {port.device} (VID=33C3 PID=F101)')
                return port.device
            # Also check the hardware ID string as fallback
            hwid = (port.hwid or '').upper()
            if 'VID_33C3' in hwid and 'PID_F101' in hwid:
                log.info(f'DS916 auto-detected on {port.device} via HWID match')
                return port.device
    except Exception as e:
        log.error(f'Port detection error: {e}')
    return None

def open_port():
    global _port
    try:
        if _port and _port.is_open: _port.close()
        # Auto-detect DS916 port; fall back to config value
        detected = detect_ds916_port()
        if detected and detected != cfg['com_port']:
            log.info(f'Updating COM port: {cfg["com_port"]} -> {detected}')
            cfg['com_port'] = detected
            save_cfg(cfg)
        port_to_use = cfg['com_port']
        _port = serial.Serial(port_to_use, baudrate=115200, timeout=2)
        log.info(f'Opened {port_to_use}')
        return True
    except Exception as e:
        log.error(f'Port error: {e}')
        return False

_display_mode = cfg.get('screen_mode', 'auto') if cfg.get('screen_mode', 'auto') in ('gaming', 'work', 'idle') else 'gaming'
_auto_screen_mode = cfg.get('screen_mode', 'auto') == 'auto'
_mode_low_since = None
_mode_log_last = 0.0

def _detect_display_mode(sensors):
    """Hysteretic three-state selector; manual tray selection overrides auto mode."""
    global _display_mode, _mode_low_since, _mode_log_last
    if not _auto_screen_mode:
        return _display_mode
    now = time.monotonic()
    try: fps = float(sensors.get('RTSS_FPS', 0) or 0)
    except Exception: fps = 0.0
    try: gpu = float(sensors.get('GPU_USAGE', 0) or 0)
    except Exception: gpu = 0.0
    try: cpu = float(sensors.get('CPU_USAGE', 0) or 0)
    except Exception: cpu = 0.0
    # RTSS FPS or configurable GPU load can trigger Gaming mode.
    gpu_threshold = max(10, min(90, int(cfg.get('gaming_gpu_threshold', 45))))
    if fps >= 45 or gpu >= gpu_threshold:
        target = 'gaming'
        _mode_low_since = None
    elif cpu < 8 and gpu < 5 and fps < 8:
        if _mode_low_since is None: _mode_low_since = now
        target = 'idle' if now - _mode_low_since >= 90 else ('gaming' if _display_mode == 'gaming' else 'work')
    else:
        _mode_low_since = None
        target = 'work'
    if target != _display_mode:
        old = _display_mode
        _display_mode = target
        if now - _mode_log_last > 1:
            log.info('Auto screen changed: %s -> %s (FPS=%.1f CPU=%.1f%% GPU=%.1f%%)', old, target, fps, cpu, gpu)
            _mode_log_last = now
    return _display_mode

def stream_loop():
    global _running
    frame_count = 0
    interval = 1.0 / max(1, cfg.get('fps',6))
    last_mode = None
    last_output_img = None
    transition_from_img = None
    transition_started = 0.0
    transition_duration = 0.32  # quick crossfade; temporarily raise frame rate while transitioning
    while _running:
        t0 = time.time()
        try:
            if _current_theme is None:
                time.sleep(0.5); continue
            check_hwinfo_restart_needed()  # internally gated to ~every 30 min, cheap no-op otherwise
            sensors = read_sensors()
            sensors.update(_get_media_data())
            active_mode = _detect_display_mode(sensors)
            sensors['_DISPLAY_MODE'] = active_mode
            img = render_frame(_current_theme, sensors)

            # Crossfade the previous visible frame into the newly selected screen.
            # The serial protocol and the HWiNFO/RTSS readers remain untouched.
            if last_mode is not None and active_mode != last_mode and last_output_img is not None:
                transition_from_img = last_output_img.copy()
                transition_started = time.monotonic()
                log.info('Screen transition started: %s -> %s', last_mode, active_mode)

            if transition_from_img is not None:
                progress = min(1.0, (time.monotonic() - transition_started) / transition_duration)
                if transition_from_img.size != img.size:
                    transition_from_img = transition_from_img.resize(img.size, Image.LANCZOS)
                img = Image.blend(transition_from_img, img, progress)
                if progress >= 1.0:
                    transition_from_img = None
                    log.debug('Screen transition completed: %s', active_mode)

            last_mode = active_mode
            last_output_img = img.copy()

            # Apply the selected output orientation after rendering the theme.
            # PIL rotates counter-clockwise; 90/270 also swap the frame dimensions.
            orientation = cfg.get('display_orientation')
            if not orientation:
                orientation = 'rotated_album' if cfg.get('rotate_display', True) else 'album'
            rotation_degrees = {
                # New intuitive names: album = horizontal, portrait = vertical.
                'portrait': 0,
                'rotated_portrait': 180,
                'album': 90,
                'rotated_album': 270,
                # Legacy values had misleading names: landscape actually meant portrait.
                'landscape': 0,
                'rotated_landscape': 180,
            }.get(orientation, 270)
            if rotation_degrees:
                img = img.rotate(rotation_degrees, expand=True)
            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=88, subsampling=0)
            jpeg = buf.getvalue()
            frame = make_frame(jpeg, first=(frame_count==0))
            if _port and _port.is_open:
                _port.write(frame)
                _port.flush()
            frame_count += 1
        except Exception as e:
            log.error(f'Stream error: {e}')
            time.sleep(1)
        elapsed = time.time()-t0
        # The normal display refresh can be as low as 6 FPS. That is too
        # coarse for a crossfade, so temporarily target 12 FPS only while
        # a transition is active; retain the configured rate otherwise.
        media_playing = bool(locals().get('sensors', {}).get('NOW_PLAYING_PLAYING', False))
        # Keep animation smooth during music playback; idle/static screens retain the configured FPS.
        target_interval = min(interval, 1.0 / 12.0) if media_playing else interval
        frame_interval = (1.0 / 15.0) if transition_from_img is not None else target_interval
        if elapsed < frame_interval:
            time.sleep(frame_interval-elapsed)

def start_display():
    global _running, _run_thread
    if _running: return
    if not open_port(): return
    _running = True
    _run_thread = threading.Thread(target=stream_loop, daemon=True)
    _run_thread.start()
    update_tray_icon()
    log.info('Display started')

def stop_display():
    global _running
    _running = False
    time.sleep(0.3)
    if _port and _port.is_open: _port.close()
    update_tray_icon()
    log.info('Display stopped')

# ── HWiNFO Auto-Restart (replaces the old Scheduled Task approach) ────────────
def get_hwinfo_start_time():
    """Return the datetime HWiNFO64.exe actually started, or None if it
    isn't currently running / detection fails. Used both to decide whether
    a restart is due, and purely informationally in Settings/Status."""
    try:
        import ctypes
        from ctypes import wintypes
        from datetime import timedelta
        psapi = ctypes.windll.psapi
        kernel32 = ctypes.windll.kernel32
        pids = (wintypes.DWORD * 1024)()
        cb_needed = wintypes.DWORD()
        psapi.EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(cb_needed))
        count = cb_needed.value // ctypes.sizeof(wintypes.DWORD)
        PROCESS_QUERY_INFORMATION = 0x0400
        for i in range(count):
            pid = pids[i]
            if not pid: continue
            hproc = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION, False, pid)
            if not hproc: continue
            try:
                name_buf = ctypes.create_unicode_buffer(260)
                size = wintypes.DWORD(260)
                if psapi.GetModuleBaseNameW(hproc, None, name_buf, size):
                    if name_buf.value.lower() == 'hwinfo64.exe':
                        creation = wintypes.FILETIME()
                        exit_t = wintypes.FILETIME()
                        kernel_t = wintypes.FILETIME()
                        user_t = wintypes.FILETIME()
                        if kernel32.GetProcessTimes(hproc, ctypes.byref(creation),
                                ctypes.byref(exit_t), ctypes.byref(kernel_t), ctypes.byref(user_t)):
                            ft = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
                            return datetime(1601,1,1) + timedelta(microseconds=ft/10)
            finally:
                kernel32.CloseHandle(hproc)
    except Exception as e:
        log.warning(f'Could not detect HWiNFO64 start time: {e}')
    return None


_last_hwinfo_restart_check = 0
HWINFO_RESTART_CHECK_INTERVAL = 30 * 60   # check every 30 minutes
HWINFO_RESTART_THRESHOLD     = 11.5 * 3600  # restart once HWiNFO64 has run this long (seconds)

def check_hwinfo_restart_needed():
    """Called periodically from the main loop (every ~30 min, gated by
    HWINFO_RESTART_CHECK_INTERVAL). If HWiNFO64 has been running for
    11.5+ hours and the auto-restart setting is enabled, stop and restart
    it -- a normal user-level action requiring no elevation/UAC prompt,
    since we're just killing and relaunching an ordinary application we
    already have permission to interact with (unlike registering a
    Windows Scheduled Task, which DOES require elevation).

    This replaces the old Scheduled-Task-based approach entirely. A fixed
    schedule had no way to know about real-world power cycles -- if the
    PC was shut down and restarted, the task's fixed timing could drift
    hours out of sync with HWiNFO64's actual uptime. Checking live, on a
    timer, against HWiNFO64's real current uptime has no such drift,
    because there's no schedule to drift from in the first place.
    """
    global _last_hwinfo_restart_check
    if not cfg.get('hwinfo_auto_restart', False):
        return
    now = time.time()
    if now - _last_hwinfo_restart_check < HWINFO_RESTART_CHECK_INTERVAL:
        return
    _last_hwinfo_restart_check = now

    start = get_hwinfo_start_time()
    if start is None:
        log.debug('HWiNFO restart check: HWiNFO64 not currently running, nothing to do')
        return

    uptime_seconds = (datetime.now() - start).total_seconds()
    log.debug(f'HWiNFO restart check: HWiNFO64 uptime is {uptime_seconds/3600:.2f}h')
    if uptime_seconds < HWINFO_RESTART_THRESHOLD:
        return

    path = cfg.get('hwinfo_path', '').strip()
    if not path or not os.path.exists(path):
        log.warning('HWiNFO64 is due for a restart (12h limit approaching) but no HWiNFO64.exe '
                    'path is configured in Settings -> HWiNFO -- cannot restart automatically. '
                    'Set the path in Settings to enable this.')
        return

    log.info(f'HWiNFO64 has been running {uptime_seconds/3600:.2f}h - restarting now to keep '
             f'shared memory active before the free-version 12h limit')
    try:
        import subprocess
        subprocess.run(['taskkill', '/IM', 'HWiNFO64.exe', '/F'],
                       capture_output=True, timeout=10)
        time.sleep(2)
        subprocess.Popen([path, '-sensors'])
        log.info('HWiNFO64 restarted successfully')
    except Exception as e:
        log.error(f'HWiNFO64 restart failed: {e}')

# ── Localization ──────────────────────────────────────────────────────────────
BUILTIN_TRANSLATIONS = {
    'DS916 Settings': 'Настройки DS916',
    'Weather city:': 'Город для погоды:',
    'Find city': 'Найти город',
    'City found:': 'Найден город:',
    'Could not find city. Check the spelling or add the country.': 'Город не найден. Проверьте название или добавьте страну.',
    'City search failed:': 'Ошибка поиска города:',
    '  General  ': '  Общие  ',
    'COM Port:': 'COM-порт:',
    'Auto-detect': 'Автоопределение',
    'DS916 not found — is it plugged in?': 'DS916 не найден — проверьте подключение.',
    'FPS:': 'FPS:',
    'Theme File:': 'Файл темы:',
    'Theme File': 'Файл темы',
    'Browse…': 'Обзор…',
    'Start display automatically with Windows': 'Запускать дисплей вместе с Windows',
    'Logging:': 'Уровень логирования:',
    'Logging': 'Уровень логирования:',
    'verbose = detailed per-frame/per-read diagnostics, for troubleshooting': 'verbose — подробная диагностика для поиска неисправностей',
    '📁 Open Log Folder': '📁 Открыть папку журнала',
    '  HWiNFO  ': '  HWiNFO  ',
    '  RTSS (FPS)  ': '  RTSS (FPS)  ',
    '  RTSS  ': '  RTSS  ',
    '  Screen  ': '  Экран  ',
    'Display behavior': 'Поведение дисплея',
    'Choose which layout is shown on the DS916 screen.': 'Выберите макет для экрана DS916.',
    'Screen switching': 'Переключение режимов',
    'Automatic switching (recommended)': 'Автоматическое переключение (рекомендуется)',
    'Gaming screen': 'Игровой экран',
    'Work screen': 'Рабочий экран',
    'Idle screen': 'Экран ожидания',
    'Automatic Gaming detection': 'Автоопределение игрового режима',
    'Switch to Gaming when GPU load reaches:': 'Переключать в игровой режим при загрузке GPU:',
    'RTSS FPS ≥ 45 also triggers Gaming. Idle requires CPU < 8%, GPU < 5% and FPS < 8 for 90 seconds.': 'Игровой режим также включается при RTSS FPS ≥ 45. Режим ожидания: CPU < 8%, GPU < 5% и FPS < 8 в течение 90 секунд.',
    'Album (horizontal)': 'Альбомная ориентация',
    'Rotated album (180°)': 'Альбомная, поворот 180°',
    'Portrait (vertical)': 'Книжная ориентация',
    'Rotated portrait (180°)': 'Книжная, поворот 180°',
    'Display orientation:': 'Ориентация дисплея:',
    'Enable decorative animation': 'Включить декоративную анимацию',
    'When disabled, the weather illustration and music equalizer stop animating.\nThis can reduce rendering load; sensor readings and text keep updating.': 'При отключении анимация погоды и эквалайзер музыки не двигаются.\nЭто снижает нагрузку на отрисовку; датчики и текст продолжают обновляться.',
    'Display actions': 'Управление дисплеем',
    '📂 Load Theme…': '📂 Загрузить тему…',
    '🎨 Theme Builder': '🎨 Редактор тем',
    '⏹ Stop Display': '⏹ Остановить дисплей',
    '▶ Start Display': '▶ Запустить дисплей',
    'Theme loaded successfully': 'Тема загружена',
    'Could not load this theme': 'Не удалось загрузить тему',
    'Display stopped': 'Дисплей остановлен',
    'Display started': 'Дисплей запущен',
    'Could not start display — check COM port': 'Не удалось запустить дисплей — проверьте COM-порт',
    'Cancel': 'Отмена',
    'Save': 'Сохранить',
    '✓ Settings saved successfully.': '✓ Настройки сохранены.',
    'Language:': 'Язык приложения:',
    'Import language dictionary…': 'Импортировать словарь…',
    'Language saved. Close and reopen Settings to apply it.': 'Язык сохранён. Закройте и снова откройте настройки, чтобы применить его.',
    'Choose a language dictionary': 'Выберите словарь языка',
    'Language dictionary imported. Select it in the Language list.': 'Словарь импортирован. Выберите язык в списке.',
    'Invalid dictionary': 'Некорректный словарь',
    'The file must be a JSON dictionary with language_name and translations fields.': 'JSON-файл должен содержать поля language_name и translations.',
    'English': 'English',
    'Russian': 'Русский',
    'DS916 Screen Manager': 'Управление экраном DS916',
    '▶ Start Display': '▶ Запустить дисплей',
    '⏹ Stop Display': '⏹ Остановить дисплей',
    '📂 Load Theme…': '📂 Загрузить тему…',
    '🎨 Open Theme Builder': '🎨 Редактор тем',
    '🔍 Discover Sensors': '🔍 Найти датчики',
    'ℹ Status…': 'ℹ Состояние…',
    '⚙ Settings…': '⚙ Настройки…',
    '🗑 Uninstall…': '🗑 Удалить приложение…',
    '❌ Exit': '❌ Выход',
    'When disabled, the weather illustration and music equalizer stop animating.\nThis can reduce rendering load; sensor readings and text keep updating.': 'При отключении останавливается анимация погоды и эквалайзера.\nЭто снижает нагрузку; датчики и текст продолжают обновляться.',
    'HWiNFO64 Shared Memory — 12-Hour Limit Workaround': 'HWiNFO64 Shared Memory — обход ограничения в 12 часов',
    'HWiNFO64 free edition disables shared memory after 12 hours.\nThis app can automatically detect HWiNFO64\'s real uptime\nevery 30 minutes and restart it once it has been running\nfor 11.5 hours, keeping shared memory active indefinitely.\n\nThis runs silently in the background — no window appears,\nand it requires no extra permissions, since restarting an\nordinary application you already have access to is not an\nelevated action (unlike registering a Windows Scheduled Task,\nwhich is why earlier versions needed a UAC prompt for this).': 'Бесплатная версия HWiNFO64 отключает Shared Memory через 12 часов.\nПриложение может каждые 30 минут проверять время работы HWiNFO64\nи перезапускать его после 11,5 часов работы, чтобы Shared Memory\nоставалась доступной.\n\nПроверка выполняется в фоне без отдельного окна и не требует\nдополнительных прав: перезапуск обычного приложения не требует\nповышения привилегий, в отличие от создания задания Windows,\nдля которого в ранних версиях требовалось подтверждение UAC.',
    'HWiNFO64.exe:': 'Путь к HWiNFO64.exe:',
    'Detect': 'Найти',
    'HWiNFO64 not found in common locations.\nBrowse to locate it manually.': 'HWiNFO64 не найден в стандартных папках.\nНажмите «Обзор», чтобы указать путь вручную.',
    'Locate HWiNFO64.exe': 'Укажите путь к HWiNFO64.exe',
    'Automatically restart HWiNFO64 before the 12-hour limit (off by default)': 'Автоматически перезапускать HWiNFO64 до истечения 12 часов (по умолчанию выключено)',
    "Leave this off if you have HWiNFO Pro (no 12-hour limit) or prefer to\nrestart HWiNFO64 yourself. When on, this restarts HWiNFO64 without\nasking each time — only enable it if you're comfortable with that.": 'Оставьте выключенным, если у вас HWiNFO Pro (без ограничения в 12 часов)\nили вы предпочитаете перезапускать HWiNFO64 самостоятельно. При включении\nприложение будет перезапускать HWiNFO64 без дополнительных вопросов.',
    '○ HWiNFO64 is not currently running': '○ HWiNFO64 сейчас не запущен',
    '↻ Refresh Status': '↻ Обновить состояние',
    'RivaTuner Statistics Server (RTSS) is an optional, separate source for\nreliable per-game FPS — independent of HWiNFO. If RTSS isn\'t installed\nor running, the FPS sensor simply stays unavailable; everything else\nkeeps working normally.': 'RivaTuner Statistics Server (RTSS) — дополнительный независимый\nот HWiNFO источник данных о FPS в игре. Если RTSS не установлен\nили не запущен, данные FPS будут недоступны, остальные функции\nпродолжат работать как обычно.',
    'Checking...': 'Проверка…',
    'Auto-detect active 3D app (recommended)': 'Автоматически определять активное 3D-приложение (рекомендуется)',
    'Pin a specific process:': 'Выбрать конкретный процесс:',
    '↻ Refresh List': '↻ Обновить список',
    '○ RTSS not running (optional — install from guru3d.com if you want FPS)': '○ RTSS не запущен (необязательно; установите с guru3d.com, если нужен FPS)',
    '✅ RTSS connected — no active 3D app detected right now': '✅ RTSS подключён — активное 3D-приложение сейчас не обнаружено',
    'HWiNFO64 not found in common locations.\nBrowse to locate it manually.': 'HWiNFO64 не найден в стандартных папках.\nНажмите «Обзор», чтобы указать путь вручную.',
    'HWiNFO64 running for {uptime:.1f}h — next auto-restart in ~{remaining:.1f}h': 'HWiNFO64 работает {uptime:.1f} ч — до автоперезапуска примерно {remaining:.1f} ч',
    'RTSS connected — {count} active 3D app(s) detected': 'RTSS подключён — обнаружено активных 3D-приложений: {count}',
    'DS916 Status': 'Состояние DS916',
    '⬡ DS916 Screen Manager': '⬡ Управление экраном DS916',
    '● Running': '● Работает',
    '○ Stopped': '○ Остановлен',
    'Display': 'Дисплей',
    'Status': 'Состояние',
    '● Streaming to screen': '● Передача данных на экран',
    'COM Port': 'COM-порт',
    'Target FPS': 'Целевая частота кадров',
    'Theme': 'Тема',
    'Resolution': 'Разрешение',
    'HWiNFO64 Sensor Source': 'Источник датчиков HWiNFO64',
    'Shared Memory  ✅': 'Общая память  ✅',
    'Unavailable — HWiNFO64 not running or Shared Memory Support not enabled': 'Недоступно — HWiNFO64 не запущен или Shared Memory Support не включён',
    'Source': 'Источник',
    'Auto-restart': 'Автоперезапуск',
    '✅ Auto-restart on — next in ~{next_restart:.1f}h (uptime {uptime:.1f}h)': '✅ Автоперезапуск включён — через ~{next_restart:.1f} ч (работает {uptime:.1f} ч)',
    '○ Auto-restart off (uptime {uptime:.1f}h)': '○ Автоперезапуск выключен (работает {uptime:.1f} ч)',
    '○ HWiNFO64 not running': '○ HWiNFO64 не запущен',
    'Live Sensor Snapshot': 'Текущие показания датчиков',
    'CPU Usage': 'Загрузка CPU',
    'CPU Temp': 'Температура CPU',
    'GPU Usage': 'Загрузка GPU',
    'GPU Temp': 'Температура GPU',
    'MB Temp': 'Температура материнской платы',
    'CPU Fan': 'Вентилятор CPU',
    '— (not mapped)': '— (не сопоставлено)',
    'Error': 'Ошибка',
    'System': 'Система',
    'Windows Startup': 'Запуск Windows',
    '✅ Enabled': '✅ Включён',
    '○ Disabled': '○ Выключен',
    '↻ Refresh': '↻ Обновить',
    'Close': 'Закрыть',
 }

def load_language_dictionaries():
    languages = {'English': {}}
    # Russian is built in; external JSON files can add/override any translations.
    languages['Русский'] = dict(BUILTIN_TRANSLATIONS)
    try:
        for filename in os.listdir(LANGUAGES_DIR):
            if not filename.lower().endswith('.json'):
                continue
            path = os.path.join(LANGUAGES_DIR, filename)
            try:
                with open(path, 'r', encoding='utf-8-sig') as f:
                    data = json.load(f)
                name = str(data.get('language_name', '')).strip()
                mapping = data.get('translations', {})
                if name and isinstance(mapping, dict):
                    # Keep built-in translations when an older user dictionary
                    # has the same language name; user entries override only matching keys.
                    merged = dict(languages.get(name, {}))
                    merged.update({str(k): str(v) for k, v in mapping.items()})
                    languages[name] = merged
            except Exception as e:
                log.warning('Could not load language dictionary %s: %s', path, e)
    except OSError:
        pass
    return languages

LANGUAGES = load_language_dictionaries()
def tr(text):
    lang = cfg.get('language', 'English') if 'cfg' in globals() else 'English'
    mapping = LANGUAGES.get(lang, {})
    return mapping.get(text) or text

# ── Settings Window ───────────────────────────────────────────────────────────
_settings_win = None

def open_settings():
    global _settings_win
    # If already open, just bring it to front
    if _settings_win and _settings_win.winfo_exists():
        _settings_win.lift()
        _settings_win.focus_force()
        return

    # Use Toplevel (child of hidden root) — NOT tk.Tk() which breaks on reopen
    win = tk.Toplevel(_tk_root)
    win.title(tr('DS916 Settings'))
    win.geometry('620x760')
    win.configure(bg='#18181c')
    win.resizable(True, True)
    win.minsize(500, 500)
    win.lift()
    win.focus_force()
    _settings_win = win

    style = ttk.Style(win)
    style.theme_use('clam')
    style.configure('TLabel',     background='#18181c', foreground='#e8e6df', font=('Segoe UI',10))
    style.configure('TEntry',     fieldbackground='#1f1f25', foreground='#e8e6df', font=('Segoe UI',10))
    style.configure('TButton',    background='#1f1f25', foreground='#e8e6df', font=('Segoe UI',10))
    style.configure('TCombobox',  fieldbackground='#1f1f25', foreground='#e8e6df')
    style.configure('TFrame',     background='#18181c')
    style.configure('TSpinbox',   fieldbackground='#1f1f25', foreground='#e8e6df')
    style.configure('TCheckbutton', background='#18181c', foreground='#e8e6df')
    style.configure('TNotebook',  background='#18181c')
    style.configure('TNotebook.Tab', background='#1f1f25', foreground='#888', padding=[8,4])
    style.map('TNotebook.Tab',
              background=[('selected','#252530')],
              foreground=[('selected','#00b4ff')])

    nb = ttk.Notebook(win)
    nb.pack(fill='both', expand=True, padx=10, pady=10)

    # ── Tab 1: General ────────────────────────────────────────────────────────
    t1 = ttk.Frame(nb); nb.add(t1, text=tr('  General  '))

    def lbl(parent, text, row, col=0):
        ttk.Label(parent, text=tr(text)).grid(row=row, column=col, sticky='w', padx=8, pady=4)

    # Language selection and importable JSON translation dictionaries.
    language_row = ttk.Frame(t1); language_row.grid(row=0, column=0, columnspan=3, sticky='ew', padx=8, pady=(8, 4))
    ttk.Label(language_row, text=tr('Language:')).pack(side='left', padx=(0, 8))
    language_var = tk.StringVar(value=cfg.get('language', 'English'))
    language_values = sorted(LANGUAGES.keys())
    if language_var.get() not in language_values:
        language_values.append(language_var.get())
    language_cb = ttk.Combobox(language_row, textvariable=language_var,
                               values=language_values, width=18, state='readonly')
    language_cb.pack(side='left')
    # Explicitly select the saved value so ttk displays it immediately on open.
    if language_var.get() in language_values:
        language_cb.current(language_values.index(language_var.get()))
    language_cb.set(language_var.get())
    def import_language_dictionary():
        path = filedialog.askopenfilename(parent=win, initialdir=LANGUAGES_DIR,
            title=tr('Choose a language dictionary'), filetypes=[('JSON', '*.json'), ('All files', '*.*')])
        if not path:
            return
        try:
            with open(path, 'r', encoding='utf-8-sig') as f:
                data = json.load(f)
            name, mapping = data.get('language_name'), data.get('translations')
            if not isinstance(name, str) or not name.strip() or not isinstance(mapping, dict):
                raise ValueError(tr('The file must be a JSON dictionary with language_name and translations fields.'))
            target = os.path.join(LANGUAGES_DIR, os.path.basename(path))
            if os.path.abspath(path) != os.path.abspath(target):
                import shutil
                shutil.copy2(path, target)
            LANGUAGES[name.strip()] = {str(k): str(v) for k, v in mapping.items()}
            language_cb.configure(values=sorted(set(LANGUAGES.keys()) | {language_var.get(), name.strip()}))
            language_var.set(name.strip())
            messagebox.showinfo(tr('DS916 Settings'), tr('Language dictionary imported. Select it in the Language list.'), parent=win)
        except Exception as e:
            messagebox.showerror(tr('Invalid dictionary'), str(e), parent=win)
    ttk.Button(language_row, text=tr('Import language dictionary…'), command=import_language_dictionary).pack(side='left', padx=8)

    # Weather city: geocode with Open-Meteo, then persist coordinates/time zone.
    weather_row = ttk.Frame(t1)
    weather_row.grid(row=1, column=0, columnspan=3, sticky='ew', padx=8, pady=(4, 4))
    ttk.Label(weather_row, text=tr('Weather city:')).pack(side='left', padx=(0, 8))
    weather_city_var = tk.StringVar(value=cfg.get('weather_city', 'Москва'))
    weather_city_entry = ttk.Entry(weather_row, textvariable=weather_city_var, width=22)
    weather_city_entry.pack(side='left')
    weather_status_lbl = ttk.Label(weather_row, text='', font=('Segoe UI', 8), foreground='#4fc87a')
    weather_status_lbl.pack(side='left', padx=8)

    def find_weather_city():
        query = weather_city_var.get().strip()
        if not query:
            weather_status_lbl.config(text=tr('Could not find city. Check the spelling or add the country.'), foreground='#e05a4b')
            return
        try:
            geo_url = 'https://geocoding-api.open-meteo.com/v1/search?name=' + urllib.parse.quote(query) + '&count=10&language=ru&format=json'
            req = urllib.request.Request(geo_url, headers={'User-Agent': 'DS916Tray/1.0', 'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=8) as response:
                geo = json.loads(response.read().decode('utf-8-sig'))
            results = geo.get('results') or []
            if not results:
                weather_status_lbl.config(text=tr('Could not find city. Check the spelling or add the country.'), foreground='#e05a4b')
                return
            # Prefer exact case-insensitive city-name match; otherwise use best geocoder result.
            chosen = next((r for r in results if str(r.get('name', '')).casefold() == query.casefold()), results[0])
            country = chosen.get('country', '')
            display_name = str(chosen.get('name') or query)
            if country:
                display_name += ', ' + str(country)
            weather_city_var.set(display_name)
            cfg['_weather_city_pending'] = {
                'name': display_name,
                'latitude': float(chosen['latitude']),
                'longitude': float(chosen['longitude']),
                'timezone': str(chosen.get('timezone') or 'auto')
            }
            weather_status_lbl.config(text=tr('City found:') + ' ' + display_name, foreground='#4fc87a')
        except Exception as e:
            weather_status_lbl.config(text=tr('City search failed:') + ' ' + str(e), foreground='#e05a4b')

    ttk.Button(weather_row, text=tr('Find city'), command=find_weather_city).pack(side='left', padx=6)
    lbl(t1, 'COM Port:', 2)
    com_var = tk.StringVar(value=cfg['com_port'])
    ports = [p.device for p in serial.tools.list_ports.comports()]
    # A saved port may be temporarily disconnected; include it so the dropdown
    # still shows the configured value instead of appearing blank.
    if com_var.get() and com_var.get() not in ports:
        ports.insert(0, com_var.get())
    com_cb = ttk.Combobox(t1, textvariable=com_var, values=ports, width=10, state='readonly')
    com_cb.grid(row=2, column=1, sticky='w', padx=8, pady=4)
    if com_var.get() in ports:
        com_cb.current(ports.index(com_var.get()))
    com_cb.set(com_var.get())
    def auto_detect_port():
        found = detect_ds916_port()
        if found:
            com_var.set(found)
            toast_lbl.config(text=f'✅ DS916 found on {found}', foreground='#4fc87a')
        else:
            toast_lbl.config(text=tr('DS916 not found — is it plugged in?'), foreground='#e05a4b')
    ttk.Button(t1, text=tr('Auto-detect'), command=auto_detect_port).grid(
        row=2, column=2, padx=4, pady=4)
    toast_lbl = ttk.Label(t1, text='', font=('Segoe UI', 8), foreground='#4fc87a')
    toast_lbl.grid(row=3, column=0, columnspan=3, sticky='w', padx=8)

    lbl(t1, 'FPS:', 4)
    fps_var = tk.IntVar(value=cfg.get('fps', 12))
    ttk.Spinbox(t1, from_=1, to=30, textvariable=fps_var, width=6).grid(
        row=4, column=1, sticky='w', padx=8, pady=4)

    lbl(t1, 'Theme File:', 5)
    theme_var = tk.StringVar(value=cfg.get('theme_path', ''))
    ttk.Entry(t1, textvariable=theme_var, width=28).grid(
        row=5, column=1, sticky='ew', padx=8, pady=4)
    def browse_theme():
        p = filedialog.askopenfilename(
            parent=win, filetypes=[('DS916 Theme', '*.ds916theme *.zip')])
        if p: theme_var.set(p)
    ttk.Button(t1, text=tr('Browse…'), command=browse_theme).grid(row=5, column=2, padx=4, pady=4)

    auto_var = tk.BooleanVar(value=cfg.get('autostart', True))
    ttk.Checkbutton(t1, text=tr('Start display automatically with Windows'),
                    variable=auto_var).grid(row=6, column=0, columnspan=3, sticky='w', padx=8, pady=6)

    lbl(t1, 'Logging:', 7)
    log_level_var = tk.StringVar(value=cfg.get('log_level', 'normal'))
    log_level_cb = ttk.Combobox(t1, textvariable=log_level_var,
                                values=['off', 'normal', 'verbose'], width=10, state='readonly')
    log_level_cb.grid(row=7, column=1, sticky='w', padx=8, pady=4)
    if log_level_var.get() in ['off', 'normal', 'verbose']:
        log_level_cb.current(['off', 'normal', 'verbose'].index(log_level_var.get()))
    log_level_cb.set(log_level_var.get())
    ttk.Label(t1, text=tr('verbose = detailed per-frame/per-read diagnostics, for troubleshooting'),
              font=('Segoe UI', 8), foreground='#555').grid(
        row=8, column=0, columnspan=3, sticky='w', padx=8)

    def open_log_folder():
        try:
            os.startfile(CONFIG_DIR)
        except Exception as e:
            messagebox.showerror('DS916', f'Could not open folder: {e}', parent=win)
    ttk.Button(t1, text=tr('📁 Open Log Folder'), command=open_log_folder).grid(
        row=9, column=0, columnspan=2, sticky='w', padx=8, pady=(6,4))

    # ── Tab 2: HWiNFO ────────────────────────────────────────────────────────
    t3 = ttk.Frame(nb); nb.add(t3, text=tr('  HWiNFO  '))

    ttk.Label(t3, text=tr('HWiNFO64 Shared Memory — 12-Hour Limit Workaround'),
              font=('Segoe UI', 10, 'bold'), foreground='#00b4ff').pack(
        anchor='w', padx=10, pady=(12,4))

    msg = (
        'HWiNFO64 free edition disables shared memory after 12 hours.\n'
        'This app can automatically detect HWiNFO64\'s real uptime\n'
        'every 30 minutes and restart it once it has been running\n'
        'for 11.5 hours, keeping shared memory active indefinitely.\n\n'
        'This runs silently in the background — no window appears,\n'
        'and it requires no extra permissions, since restarting an\n'
        'ordinary application you already have access to is not an\n'
        'elevated action (unlike registering a Windows Scheduled Task,\n'
        'which is why earlier versions needed a UAC prompt for this).'
    )
    ttk.Label(t3, text=tr(msg), font=('Segoe UI', 9), foreground='#aaa',
              justify='left', wraplength=460).pack(anchor='w', padx=10, pady=4)

    # Detect HWiNFO64 exe path
    hwinfo_path_var = tk.StringVar(value=cfg.get('hwinfo_path', ''))
    def detect_hwinfo():
        import glob
        candidates = [
            r'C:\Program Files\HWiNFO64\HWiNFO64.exe',
            r'C:\Program Files (x86)\HWiNFO64\HWiNFO64.exe',
        ] + glob.glob(r'C:\Users\*\AppData\Local\HWiNFO64\HWiNFO64.exe')
        for p in candidates:
            if os.path.exists(p):
                hwinfo_path_var.set(p)
                return
        messagebox.showinfo(tr('DS916'), tr('HWiNFO64 not found in common locations.\nBrowse to locate it manually.'), parent=win)

    def browse_hwinfo():
        p = filedialog.askopenfilename(
            parent=win, title=tr('Locate HWiNFO64.exe'),
            filetypes=[('HWiNFO64', 'HWiNFO64.exe'), ('Executable', '*.exe')])
        if p: hwinfo_path_var.set(p)

    hw_frame = ttk.Frame(t3); hw_frame.pack(fill='x', padx=10, pady=4)
    ttk.Label(hw_frame, text=tr('HWiNFO64.exe:')).grid(row=0, column=0, sticky='w', pady=2)
    ttk.Entry(hw_frame, textvariable=hwinfo_path_var, width=36).grid(row=0, column=1, padx=6, pady=2)
    ttk.Button(hw_frame, text=tr('Detect'), command=detect_hwinfo).grid(row=0, column=2, padx=2)
    ttk.Button(hw_frame, text=tr('Browse…'), command=browse_hwinfo).grid(row=0, column=3, padx=2)

    auto_restart_var = tk.BooleanVar(value=cfg.get('hwinfo_auto_restart', False))
    ttk.Checkbutton(t3, text=tr('Automatically restart HWiNFO64 before the 12-hour limit (off by default)'),
                    variable=auto_restart_var).pack(anchor='w', padx=10, pady=(10,4))
    ttk.Label(t3, text=tr("Leave this off if you have HWiNFO Pro (no 12-hour limit) or prefer to\n"
                       "restart HWiNFO64 yourself. When on, this restarts HWiNFO64 without\n"
                       "asking each time — only enable it if you're comfortable with that."),
              font=('Segoe UI', 8), foreground='#888', justify='left').pack(anchor='w', padx=10, pady=(0,4))

    hwinfo_status_lbl = ttk.Label(t3, text='', font=('Segoe UI', 9))
    hwinfo_status_lbl.pack(anchor='w', padx=10, pady=(2,8))
    def refresh_hwinfo_status():
        start = get_hwinfo_start_time()
        if start is None:
            hwinfo_status_lbl.config(text=tr('○ HWiNFO64 is not currently running'), foreground='#888')
            return
        uptime_h = (datetime.now() - start).total_seconds() / 3600
        next_restart_h = max(0, 11.5 - uptime_h)
        hwinfo_status_lbl.config(
            text=tr('HWiNFO64 running for {uptime:.1f}h — next auto-restart in ~{remaining:.1f}h').format(uptime=uptime_h, remaining=next_restart_h),
            foreground='#4fc87a')
    ttk.Button(t3, text=tr('↻ Refresh Status'), command=refresh_hwinfo_status).pack(anchor='w', padx=10)
    refresh_hwinfo_status()

    # ── Tab: RTSS (optional FPS source) ──────────────────────────────────────
    t4 = ttk.Frame(nb); nb.add(t4, text=tr('  RTSS (FPS)  '))
    ttk.Label(t4, text=tr('RivaTuner Statistics Server (RTSS) is an optional, separate source for\n'
                        'reliable per-game FPS — independent of HWiNFO. If RTSS isn\'t installed\n'
                        'or running, the FPS sensor simply stays unavailable; everything else\n'
                        'keeps working normally.'),
              font=('Segoe UI', 9), foreground='#888', justify='left').pack(anchor='w', padx=10, pady=(8,8))

    rtss_status_lbl = ttk.Label(t4, text=tr('Checking...'), font=('Segoe UI', 9))
    rtss_status_lbl.pack(anchor='w', padx=10, pady=(0,8))

    mode_frame = ttk.Frame(t4); mode_frame.pack(anchor='w', padx=10, pady=4, fill='x')
    rtss_mode_var = tk.StringVar(value='auto' if not cfg.get('rtss_process','').strip() else 'manual')
    def on_mode_change():
        is_manual = rtss_mode_var.get()=='manual'
        proc_cb.config(state='readonly' if is_manual else 'disabled')
    ttk.Radiobutton(mode_frame, text=tr('Auto-detect active 3D app (recommended)'), value='auto',
                    variable=rtss_mode_var, command=on_mode_change).pack(anchor='w')
    ttk.Radiobutton(mode_frame, text=tr('Pin a specific process:'), value='manual',
                    variable=rtss_mode_var, command=on_mode_change).pack(anchor='w', pady=(4,0))

    proc_row = ttk.Frame(t4); proc_row.pack(anchor='w', padx=28, pady=(2,8), fill='x')
    proc_var = tk.StringVar(value=cfg.get('rtss_process',''))
    proc_cb = ttk.Combobox(proc_row, textvariable=proc_var, width=30,
                           state='readonly' if rtss_mode_var.get()=='manual' else 'disabled')
    proc_cb.pack(side='left')
    if proc_var.get():
        proc_cb.configure(values=[proc_var.get()])
    # Set even when disabled/readonly; this is the persisted selection, not a scan result.
    proc_cb.set(proc_var.get())

    def refresh_rtss_apps():
        apps = list_rtss_apps()
        if apps:
            names = sorted(set(name for _,name,_ in apps))
            saved_process = proc_var.get().strip()
            if saved_process and saved_process not in names:
                names.insert(0, saved_process)
            proc_cb.config(values=names)
            rtss_status_lbl.config(
                text='✅ ' + tr('RTSS connected — {count} active 3D app(s) detected').format(count=len(apps)),
                foreground='#4fc87a')
        else:
            # Keep a configured process visible even if RTSS is not running or
            # the process is not currently detected.
            saved_process = proc_var.get().strip()
            proc_cb.config(values=[saved_process] if saved_process else [])
            proc_cb.set(saved_process)
            if _rtss_handle is None:
                rtss_status_lbl.config(
                    text=tr('○ RTSS not running (optional — install from guru3d.com if you want FPS)'),
                    foreground='#888')
            else:
                rtss_status_lbl.config(
                    text=tr('✅ RTSS connected — no active 3D app detected right now'),
                    foreground='#4fc87a')
    ttk.Button(proc_row, text=tr('↻ Refresh List'), command=refresh_rtss_apps).pack(side='left', padx=(6,0))
    refresh_rtss_apps()

    # ── Tab: Screen ───────────────────────────────────────────────────────────
    t5 = ttk.Frame(nb); nb.add(t5, text=tr('  Screen  '))
    ttk.Label(t5, text=tr('Display behavior'), font=('Segoe UI', 11, 'bold'),
              foreground='#00b4ff').pack(anchor='w', padx=12, pady=(12, 6))
    ttk.Label(t5, text=tr('Choose which layout is shown on the DS916 screen.'),
              foreground='#999', justify='left').pack(anchor='w', padx=12, pady=(0, 8))

    screen_mode_var = tk.StringVar(value=cfg.get('screen_mode', 'auto'))
    screen_modes_frame = ttk.LabelFrame(t5, text=tr('Screen switching'))
    screen_modes_frame.pack(fill='x', padx=12, pady=6)
    ttk.Radiobutton(screen_modes_frame, text=tr('Automatic switching (recommended)'),
                    value='auto', variable=screen_mode_var).pack(anchor='w', padx=10, pady=(6, 3))
    ttk.Radiobutton(screen_modes_frame, text=tr('Gaming screen'), value='gaming',
                    variable=screen_mode_var).pack(anchor='w', padx=10, pady=3)
    ttk.Radiobutton(screen_modes_frame, text=tr('Work screen'), value='work',
                    variable=screen_mode_var).pack(anchor='w', padx=10, pady=3)
    ttk.Radiobutton(screen_modes_frame, text=tr('Idle screen'), value='idle',
                    variable=screen_mode_var).pack(anchor='w', padx=10, pady=(3, 8))

    # Fine-tune the GPU-load threshold used to switch automatically to Gaming.
    gpu_threshold_frame = ttk.LabelFrame(t5, text=tr('Automatic Gaming detection'))
    gpu_threshold_frame.pack(fill='x', padx=12, pady=6)
    gpu_threshold_var = tk.IntVar(value=max(10, min(90, int(cfg.get('gaming_gpu_threshold', 45)))))
    gpu_threshold_header = ttk.Frame(gpu_threshold_frame)
    gpu_threshold_header.pack(fill='x', padx=10, pady=(7, 0))
    ttk.Label(gpu_threshold_header, text=tr('Switch to Gaming when GPU load reaches:')).pack(side='left')
    gpu_threshold_value_lbl = ttk.Label(gpu_threshold_header, text=f'{gpu_threshold_var.get()}%',
                                        font=('Segoe UI', 9, 'bold'))
    gpu_threshold_value_lbl.pack(side='right')
    def update_gpu_threshold_label(value):
        gpu_threshold_value_lbl.config(text=f'{int(float(value))}%')
    gpu_threshold_scale = ttk.Scale(
        gpu_threshold_frame, from_=10, to=90, orient='horizontal',
        command=update_gpu_threshold_label)
    gpu_threshold_scale.set(gpu_threshold_var.get())
    gpu_threshold_scale.pack(fill='x', padx=12, pady=(2, 0))
    ttk.Label(gpu_threshold_frame,
              text=tr('RTSS FPS ≥ 45 also triggers Gaming. Idle requires CPU < 8%, GPU < 5% and FPS < 8 for 90 seconds.'),
              foreground='#999', wraplength=430, justify='left').pack(anchor='w', padx=10, pady=(0, 8))

    orientation_labels = {
        'album': tr('Album (horizontal)'),
        'rotated_album': tr('Rotated album (180°)'),
        'portrait': tr('Portrait (vertical)'),
        'rotated_portrait': tr('Rotated portrait (180°)'),
    }
    orientation_value = cfg.get('display_orientation')
    if orientation_value in ('landscape', 'rotated_landscape'):
        orientation_value = 'portrait' if orientation_value == 'landscape' else 'rotated_portrait'
    if orientation_value not in orientation_labels:
        orientation_value = 'rotated_portrait' if cfg.get('rotate_display', True) else 'portrait'
    orientation_var = tk.StringVar(value=orientation_value)
    orientation_frame = ttk.Frame(t5)
    orientation_frame.pack(fill='x', padx=14, pady=(8, 4))
    ttk.Label(orientation_frame, text=tr('Display orientation:')).pack(side='left', padx=(0, 10))
    orientation_ui_var = tk.StringVar(value=orientation_labels[orientation_value])
    orientation_cb = ttk.Combobox(orientation_frame, state='readonly', width=24,
        values=[orientation_labels[k] for k in orientation_labels],
        textvariable=orientation_ui_var)
    # Keep the displayed label and the stored enum in sync without translating config values.
    def selected_orientation_key():
        label = orientation_cb.get()
        return next((k for k, v in orientation_labels.items() if v == label), orientation_value)
    orientation_cb.pack(side='left')
    orientation_cb.current(list(orientation_labels.keys()).index(orientation_value))
    orientation_cb.set(orientation_labels[orientation_value])

    animation_var = tk.BooleanVar(value=cfg.get('decorative_animation', True))
    ttk.Checkbutton(t5, text=tr('Enable decorative animation'), variable=animation_var).pack(
        anchor='w', padx=14, pady=4)
    ttk.Label(t5, text=tr('When disabled, the weather illustration and music equalizer stop animating.\n'
                          'This can reduce rendering load; sensor readings and text keep updating.'),
              foreground='#999', justify='left', wraplength=530).pack(
                  anchor='w', padx=32, pady=(0, 8))

    ttk.Separator(t5).pack(fill='x', padx=12, pady=8)
    ttk.Label(t5, text=tr('Display actions'), font=('Segoe UI', 11, 'bold'),
              foreground='#00b4ff').pack(anchor='w', padx=12, pady=(0, 6))
    action_row = ttk.Frame(t5); action_row.pack(fill='x', padx=12, pady=4)

    def screen_load_theme():
        path = filedialog.askopenfilename(initialdir=THEMES_DIR, parent=win, title='Load DS916 Theme',
            filetypes=[('DS916 Theme', '*.zip *.ds916theme'), ('All files', '*.*')])
        if path:
            if load_theme(path):
                theme_var.set(path)
                if not _running:
                    start_display()
                screen_action_status.config(text=tr('Theme loaded successfully'), foreground='#4fc87a')
            else:
                screen_action_status.config(text=tr('Could not load this theme'), foreground='#e05a4b')

    ttk.Button(action_row, text=tr('📂 Load Theme…'), command=screen_load_theme).pack(
        side='left', padx=(0, 6), pady=3)
    ttk.Button(action_row, text=tr('🎨 Theme Builder'), command=open_builder).pack(
        side='left', padx=6, pady=3)

    def screen_stop_display():
        stop_display()
        screen_action_status.config(text=tr('Display stopped'), foreground='#e0b45a')
    def screen_start_display():
        start_display()
        if _running:
            screen_action_status.config(text=tr('Display started'), foreground='#4fc87a')
        else:
            screen_action_status.config(text=tr('Could not start display — check COM port'), foreground='#e05a4b')
    display_action_row = ttk.Frame(t5)
    display_action_row.pack(fill='x', padx=12, pady=(0, 4))
    ttk.Button(display_action_row, text=tr('⏹ Stop Display'), command=screen_stop_display).pack(
        side='left', padx=(0, 6), pady=3)
    ttk.Button(display_action_row, text=tr('▶ Start Display'), command=screen_start_display).pack(
        side='left', padx=6, pady=3)
    screen_action_status = ttk.Label(t5, text='', font=('Segoe UI', 9))
    screen_action_status.pack(anchor='w', padx=14, pady=(0, 8))

    # ── Save / Cancel ─────────────────────────────────────────────────────────
    def find_default_theme_for_orientation(orientation, exclude_path=''):
        """Find the matching default theme in %APPDATA%\\DS916Tray\\Themes.
        Theme canvas dimensions remain 462x1920; output rotation is applied afterwards.
        """
        wanted = 'album' if orientation in ('album', 'rotated_album') else 'portrait'
        exact_name = f"Default_{wanted}.ds916theme"
        try:
            candidates = [os.path.join(THEMES_DIR, n) for n in os.listdir(THEMES_DIR)
                          if n.lower().endswith(('.ds916theme', '.zip'))]
        except Exception:
            candidates = []
        candidates = [p for p in candidates if os.path.abspath(p) != os.path.abspath(exclude_path or '__none__')]
        exact = [p for p in candidates if os.path.basename(p).lower() == exact_name.lower()]
        if exact:
            return exact[0]
        # Accept descriptive alternatives, but prefer a default with the requested orientation.
        preferred = [p for p in candidates
                     if 'default' in os.path.basename(p).lower()
                     and wanted in os.path.basename(p).lower()]
        if not preferred:
            preferred = [p for p in candidates
                         if wanted in os.path.basename(p).lower()
                         and not any(word in os.path.basename(p).lower()
                                     for word in ('rotated',))]
        return sorted(preferred, key=lambda p: p.lower())[0] if preferred else ''

    # Some Windows ttk themes do not paint readonly Combobox text until the
    # dropdown is opened. Re-apply saved values after all controls are mapped.
    def restore_combobox_display_values():
        for widget, value in ((language_cb, language_var.get()),
                              (com_cb, com_var.get()),
                              (log_level_cb, log_level_var.get()),
                              (proc_cb, proc_var.get()),
                              (orientation_cb, orientation_labels.get(orientation_value, ''))):
            try:
                widget.set(value)
            except tk.TclError:
                pass
    win.after_idle(restore_combobox_display_values)

    saved_status_lbl = ttk.Label(win, text='', foreground='#4fc87a', font=('Segoe UI', 9, 'bold'))

    def save_settings():
        old_cfg = dict(cfg)
        cfg['com_port']      = com_var.get()
        cfg['language']      = language_var.get()
        pending_weather = cfg.pop('_weather_city_pending', None)
        if pending_weather:
            cfg['weather_city'] = pending_weather['name']
            cfg['weather_latitude'] = pending_weather['latitude']
            cfg['weather_longitude'] = pending_weather['longitude']
            cfg['weather_timezone'] = pending_weather['timezone']
            # Force the next weather poll to use the new location immediately.
            global _weather_last_fetch
            _weather_last_fetch = 0.0
        cfg['fps']           = fps_var.get()
        cfg['screen_mode']   = screen_mode_var.get()
        cfg['gaming_gpu_threshold'] = int(round(float(gpu_threshold_scale.get())))
        new_orientation = selected_orientation_key()
        old_orientation = cfg.get('display_orientation') or ('rotated_portrait' if cfg.get('rotate_display', True) else 'portrait')
        if old_orientation == 'landscape':
            old_orientation = 'portrait'
        elif old_orientation == 'rotated_landscape':
            old_orientation = 'rotated_portrait' 
        cfg['display_orientation'] = new_orientation
        cfg['rotate_display'] = new_orientation in ('rotated_album', 'rotated_portrait')  # legacy compatibility
        cfg['decorative_animation'] = animation_var.get()
        cfg['theme_path']    = theme_var.get()
        cfg['autostart']     = auto_var.get()
        cfg['rtss_process']  = proc_var.get().strip() if rtss_mode_var.get()=='manual' else ''
        cfg['hwinfo_path']   = hwinfo_path_var.get().strip()
        cfg['hwinfo_auto_restart'] = auto_restart_var.get()
        cfg['log_level']     = log_level_var.get()

        # Load the matching default layout when switching between album and portrait families.
        # The renderer rotates the 462x1920 theme canvas after drawing it.
        orientation_note = ''
        old_family = 'album' if old_orientation in ('album', 'rotated_album') else 'portrait'
        new_family = 'album' if new_orientation in ('album', 'rotated_album') else 'portrait'
        if old_family != new_family:
            default_theme = find_default_theme_for_orientation(new_orientation, _current_theme_path)
            if default_theme:
                if load_theme(default_theme):
                    cfg['theme_path'] = default_theme
                    theme_var.set(default_theme)
                    orientation_note = f' Default {new_family} theme loaded.'
                    log.info('Orientation family changed to %s; loaded default theme: %s', new_family, default_theme)
            else:
                orientation_note = f' No Default_{new_family}.ds916theme found; current theme retained.'
                log.warning('No default %s theme found in %s', new_family, THEMES_DIR)

        # Log what actually changed, not just that Save was clicked -- useful
        # for understanding behavior changes later (e.g. "why did logging
        # stop" traces back to someone switching log_level to 'off' on a
        # specific date).
        for key in ('com_port','fps','screen_mode','gaming_gpu_threshold','rotate_display','display_orientation','decorative_animation',
                    'autostart','rtss_process','hwinfo_path','hwinfo_auto_restart','log_level',
                    'weather_city','weather_latitude','weather_longitude','weather_timezone'):
            if old_cfg.get(key) != cfg.get(key):
                log.info(f'Setting changed: {key} = {old_cfg.get(key)!r} -> {cfg.get(key)!r}')

        save_cfg(cfg)
        if _tray is not None:
            try:
                _tray.update_menu()
            except Exception:
                pass
        set_log_level(cfg['log_level'])
        set_autostart(cfg['autostart'])
        set_screen_mode(cfg['screen_mode'])
        if cfg['theme_path'] and cfg['theme_path'] != _current_theme_path:
            load_theme(cfg['theme_path'])
        saved_status_lbl.config(text=tr('✓ Settings saved successfully.') + orientation_note, foreground='#4fc87a')
        saved_status_lbl.pack(side='left', padx=8)

    btn_f = ttk.Frame(win); btn_f.pack(fill='x', padx=10, pady=8)
    ttk.Button(btn_f, text=tr('Cancel'), command=win.destroy).pack(side='right', padx=4)
    ttk.Button(btn_f, text=tr('Save'), command=save_settings).pack(side='right', padx=4)

# ── Windows autostart ─────────────────────────────────────────────────────────
def set_autostart(enable):
    key_path = r'Software\Microsoft\Windows\CurrentVersion\Run'
    exe = sys.executable if getattr(sys,'frozen',False) else os.path.abspath(__file__)
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE)
        if enable:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, f'"{exe}"')
        else:
            try: winreg.DeleteValue(key, APP_NAME)
            except: pass
        winreg.CloseKey(key)
    except Exception as e:
        log.error(f'Autostart error: {e}')

def is_autostart():
    key_path = r'Software\Microsoft\Windows\CurrentVersion\Run'
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path)
        winreg.QueryValueEx(key, APP_NAME)
        winreg.CloseKey(key)
        return True
    except: return False

# ── Tray icon ─────────────────────────────────────────────────────────────────
_tray = None

def make_tray_icon():
    """Create a simple DS916 icon programmatically."""
    size = 64
    img = PILImage.new('RGBA', (size,size), (0,0,0,0))
    d = ImageDraw.Draw(img)
    d.ellipse([4,4,60,60], fill=(0,20,30,255), outline=(0,180,255,255), width=3)
    d.rectangle([20,16,44,48], fill=(0,180,255,200))
    d.rectangle([22,18,42,46], fill=(0,10,20,255))
    for i in range(3):
        y0=22+i*8; d.rectangle([25,y0,39,y0+5],fill=(0,180,255,180))
    return img

def update_tray_icon():
    global _tray
    if not _tray: return
    status = '● Running' if _running else '○ Stopped'
    theme_name = _current_theme.get('name','No theme') if _current_theme else 'No theme loaded'
    _tray.title = f'DS916 — {status}\n{theme_name}'

# ── Main-thread dispatcher ────────────────────────────────────────────────────
# pystray callbacks run on a background thread. tkinter dialogs MUST run on the
# main thread. We use a queue + tk.after() poll to bridge the two safely.
import queue
_ui_queue = queue.Queue()
_tk_root  = None   # set in main before run_tray()

def _dispatch(fn, *args, **kwargs):
    """Schedule fn(*args,**kwargs) to run on the main tk thread."""
    _ui_queue.put((fn, args, kwargs))

def _poll_ui_queue():
    """Called every 100ms on the main tk thread to drain the queue."""
    try:
        while True:
            fn, args, kwargs = _ui_queue.get_nowait()
            fn(*args, **kwargs)
    except queue.Empty:
        pass
    finally:
        if _tk_root:
            _tk_root.after(100, _poll_ui_queue)

# ── Tray callbacks (run on pystray thread → dispatched to main thread) ────────
def load_theme_dialog(icon=None, item=None):
    _dispatch(_load_theme_dialog_main)

def _load_theme_dialog_main():
    from tkinter import filedialog
    path = filedialog.askopenfilename(
        parent=_tk_root,
        title='Load DS916 Theme',
        filetypes=[('DS916 Theme', '*.zip *.ds916theme'), ('All files', '*.*')]
    )
    if path:
        if load_theme(path):
            if not _running: start_display()
            update_tray_icon()
            log.info(f'Theme loaded and display started: {path}')

def toggle_display(icon=None, item=None):
    if _running: stop_display()
    else: start_display()
    update_tray_icon()

def open_builder(icon=None, item=None):
    import webbrowser
    # theme_builder.html now lives in CONFIG_DIR (AppData), installed there
    # by build.bat alongside the exe — not next to wherever the exe happens
    # to be run from. This keeps everything for this app in one place.
    html = os.path.join(CONFIG_DIR, 'theme_builder.html')
    if os.path.exists(html):
        webbrowser.open('file:///'+html.replace('\\','/'))
    else:
        _dispatch(lambda: messagebox.showwarning('DS916',
            'theme_builder.html not found in:\n'+CONFIG_DIR+
            '\n\nIf you built this app yourself, re-run build.bat to reinstall it there.'))

def open_settings_tray(icon=None, item=None):
    _dispatch(open_settings)

def quit_app(icon=None, item=None):
    stop_display()
    if _tray: _tray.stop()
    if _tk_root:
        try: _tk_root.quit()
        except: pass

def uninstall_app(icon=None, item=None):
    _dispatch(_uninstall_main)

def _uninstall_main():
    result = messagebox.askyesno(
        'DS916 Tray — Uninstall',
        'This will:\n\n'
        '  • Remove DS916Tray from Windows startup\n'
        '  • Remove the Desktop and Start Menu shortcuts\n'
        '  • Delete theme_builder.html, config, and discovered sensor data\n'
        '  • Close the tray app\n\n'
        'Your theme files (.ds916theme) will NOT be deleted.\n\n'
        'Note: DS916Tray.exe itself can\'t delete itself while running — '
        'it will be left behind in the (now otherwise empty) install folder. '
        'You can delete it manually once the app has closed.\n\n'
        'Continue?',
        parent=_tk_root
    )
    if not result:
        return

    # 1. Remove from startup
    set_autostart(False)

    # 2. Remove Desktop and Start Menu shortcuts created by build.bat
    try:
        import ctypes.wintypes
        CSIDL_DESKTOPDIRECTORY = 0x10
        CSIDL_PROGRAMS = 0x02
        buf = ctypes.create_unicode_buffer(260)
        shortcut_dirs = []
        for csidl in (CSIDL_DESKTOPDIRECTORY, CSIDL_PROGRAMS):
            ctypes.windll.shell32.SHGetFolderPathW(0, csidl, 0, 0, buf)
            shortcut_dirs.append(buf.value)
        for d in shortcut_dirs:
            lnk = os.path.join(d, 'DS916 Tray.lnk')
            if os.path.exists(lnk):
                os.unlink(lnk)
                log.info(f'Removed shortcut: {lnk}')
    except Exception as e:
        log.warning(f'Shortcut removal error: {e}')

    # 3. Delete everything in CONFIG_DIR (theme_builder.html, config, sensors)
    # EXCEPT the running exe itself (Windows won't let us delete it while
    # it's open — left behind harmlessly) and the Themes subfolder (the
    # dialog explicitly promises these won't be deleted).
    import shutil
    if os.path.exists(CONFIG_DIR):
        exe_name = 'DS916Tray.exe'
        themes_name = os.path.basename(THEMES_DIR)
        for entry in os.listdir(CONFIG_DIR):
            if entry == exe_name or entry == themes_name:
                continue
            full = os.path.join(CONFIG_DIR, entry)
            try:
                if os.path.isdir(full):
                    shutil.rmtree(full)
                else:
                    os.unlink(full)
                log.info(f'Removed: {full}')
            except Exception as e:
                log.warning(f'Removal error for {full}: {e}')

    # 4. Clean up temp font files
    for path in _custom_font_files.values():
        try:
            if os.path.exists(path):
                os.unlink(path)
        except: pass

    messagebox.showinfo(
        'DS916 Tray — Uninstalled',
        'DS916 Tray has been removed from startup, and shortcuts and '
        'data have been deleted.\n\n'
        'DS916Tray.exe itself is left in:\n'+CONFIG_DIR+
        '\n\nYou can delete that file manually now that the app is closing.',
        parent=_tk_root
    )

    # 5. Exit
    quit_app()

_status_win = None

def show_status(icon=None, item=None):
    _dispatch(_show_status_main)

def _show_status_main():
    global _status_win
    if _status_win and _status_win.winfo_exists():
        _status_win.lift(); _status_win.focus_force(); return

    win = tk.Toplevel(_tk_root)
    win.title(tr('DS916 Status'))
    win.configure(bg='#0f0f11')
    win.resizable(True, True)
    win.minsize(380, 360)
    win.withdraw()  # hide until sized correctly
    _status_win = win

    BG   = '#0f0f11'
    PAN  = '#18181c'
    ACC  = '#00b4ff'
    GRN  = '#4fc87a'
    RED  = '#e05a4b'
    MUT  = '#666'
    TXT  = '#e8e6df'

    # Header (fixed, outside scroll)
    hdr = tk.Frame(win, bg=ACC, padx=12, pady=8)
    hdr.pack(fill='x')
    tk.Label(hdr, text=tr('⬡ DS916 Screen Manager'), bg=ACC, fg='#000',
             font=('Segoe UI', 11, 'bold')).pack(side='left')
    status_text = tr('● Running') if _running else tr('○ Stopped')
    status_col  = '#003020' if _running else '#300000'
    tk.Label(hdr, text=status_text, bg=status_col, fg=GRN if _running else RED,
             font=('Segoe UI', 9, 'bold'), padx=8, pady=2).pack(side='right')

    # Scrollable body
    body_frame = tk.Frame(win, bg=BG)
    body_frame.pack(fill='both', expand=True)

    canvas = tk.Canvas(body_frame, bg=BG, highlightthickness=0)
    scrollbar = tk.Scrollbar(body_frame, orient='vertical', command=canvas.yview)
    canvas.configure(yscrollcommand=scrollbar.set)
    scrollbar.pack(side='right', fill='y')
    canvas.pack(side='left', fill='both', expand=True)

    inner = tk.Frame(canvas, bg=BG)
    inner_id = canvas.create_window((0,0), window=inner, anchor='nw')

    def on_configure(e):
        canvas.configure(scrollregion=canvas.bbox('all'))
    def on_canvas_resize(e):
        canvas.itemconfig(inner_id, width=e.width)
    inner.bind('<Configure>', on_configure)
    canvas.bind('<Configure>', on_canvas_resize)
    canvas.bind_all('<MouseWheel>', lambda e: canvas.yview_scroll(int(-1*(e.delta/120)), 'units'))

    def section(text):
        f = tk.Frame(inner, bg=PAN, padx=10, pady=6)
        f.pack(fill='x', padx=12, pady=(6,0))
        tk.Label(f, text=text, bg=PAN, fg=ACC,
                 font=('Segoe UI', 9, 'bold')).pack(anchor='w')
        return f

    def row(parent, label, value, value_color=TXT):
        f = tk.Frame(parent, bg=PAN)
        f.pack(fill='x', pady=1)
        tk.Label(f, text=label, bg=PAN, fg=MUT,
                 font=('Segoe UI', 9), width=18, anchor='w').pack(side='left')
        tk.Label(f, text=value, bg=PAN, fg=value_color,
                 font=('Segoe UI', 9, 'bold'), anchor='w', wraplength=260,
                 justify='left').pack(side='left', fill='x', expand=True)

    # Display
    s1 = section(tr('Display'))
    row(s1, tr('Status'), tr('● Streaming to screen') if _running else tr('○ Stopped'),
        GRN if _running else RED)
    row(s1, tr('COM Port'), cfg.get('com_port', 'COM3'))
    row(s1, tr('Target FPS'), str(cfg.get('fps', 12)))
    theme_name = _current_theme.get('name', '—') if _current_theme else '—'
    theme_res  = (f"{_current_theme.get('width',462)}×{_current_theme.get('height',1920)}"
                  if _current_theme else '—')
    row(s1, tr('Theme'), theme_name)
    row(s1, tr('Resolution'), theme_res)

    # HWiNFO
    s2 = section(tr('HWiNFO64 Sensor Source'))
    if _shm_handle is not None:
        src_label = tr('Shared Memory  ✅')
        src_col   = GRN
    else:
        src_label = tr('Unavailable — HWiNFO64 not running or Shared Memory Support not enabled')
        src_col   = '#d4b84a'
    row(s2, tr('Source'), src_label, src_col)

    hwinfo_start = get_hwinfo_start_time()
    if hwinfo_start:
        uptime_h = (datetime.now() - hwinfo_start).total_seconds() / 3600
        auto_on = cfg.get('hwinfo_auto_restart', False)
        if auto_on:
            next_restart_h = max(0, 11.5 - uptime_h)
            restart_label = tr('✅ Auto-restart on — next in ~{next_restart:.1f}h (uptime {uptime:.1f}h)').format(next_restart=next_restart_h, uptime=uptime_h)
            restart_col = GRN
        else:
            restart_label = tr('○ Auto-restart off (uptime {uptime:.1f}h)').format(uptime=uptime_h)
            restart_col = MUT
    else:
        restart_label = tr('○ HWiNFO64 not running')
        restart_col = MUT
    row(s2, tr('Auto-restart'), restart_label, restart_col)

    # Sensors
    s3 = section(tr('Live Sensor Snapshot'))
    try:
        sensors = read_sensors()
        pairs = [
            (tr('CPU Usage'),    sensors.get('CPU_USAGE'),  '%'),
            (tr('CPU Temp'),     sensors.get('CPU_TEMP'),   '°C'),
            (tr('GPU Usage'),    sensors.get('GPU_USAGE'),  '%'),
            (tr('GPU Temp'),     sensors.get('GPU_TEMP'),   '°C'),
            (tr('MB Temp'),      sensors.get('MB_TEMP'),    '°C'),
            (tr('CPU Fan'),      sensors.get('CPU_FAN'),    'RPM'),
        ]
        for lbl, val, unit in pairs:
            if val is not None:
                row(s3, lbl, f'{val:.1f} {unit}')
            else:
                row(s3, lbl, tr('— (not mapped)'), MUT)
    except Exception as e:
        row(s3, tr('Error'), str(e), RED)

    # System
    s4 = section(tr('System'))
    row(s4, tr('Windows Startup'), tr('✅ Enabled') if is_autostart() else tr('○ Disabled'),
        GRN if is_autostart() else MUT)

    # Spacer at bottom of scroll area
    tk.Frame(inner, bg=BG, height=8).pack()

    # Buttons outside scroll area (always visible at bottom)
    btn_frame = tk.Frame(win, bg=BG)
    btn_frame.pack(fill='x', padx=12, pady=8, side='bottom')
    tk.Button(btn_frame, text=tr('↻ Refresh'), bg=PAN, fg=ACC,
              font=('Segoe UI', 9), bd=0, padx=12, pady=4,
              command=lambda: [win.destroy(), _show_status_main()]).pack(side='left')
    tk.Button(btn_frame, text=tr('Close'), bg=PAN, fg=TXT,
              font=('Segoe UI', 9), bd=0, padx=12, pady=4,
              command=win.destroy).pack(side='right')

    # Size and show — build was hidden so no flash
    win.update_idletasks()
    screen_h = win.winfo_screenheight()
    screen_w = win.winfo_screenwidth()
    content_h = (inner.winfo_reqheight() + hdr.winfo_reqheight() +
                 btn_frame.winfo_reqheight() + 40)
    final_h = min(content_h, int(screen_h * 0.9))
    final_w = min(460, int(screen_w * 0.9))
    x = (screen_w - final_w) // 2
    y = (screen_h - final_h) // 2
    win.geometry(f'{final_w}x{final_h}+{x}+{y}')
    win.maxsize(int(screen_w * 0.95), int(screen_h * 0.95))
    win.deiconify()  # show now that size is correct
    win.lift()
    win.focus_force()

def discover_sensors_tray(icon=None, item=None):
    _dispatch(_discover_sensors_main)

def _discover_sensors_main():
    path = discover_sensors()
    if path:
        detect_gpu_vram_capacity()  # re-check in case the GPU changed since last discovery
        messagebox.showinfo('DS916 — Sensor Discovery',
            f'✅ {len(json.load(open(path))["sensors"])} sensors discovered and saved.\n\n'
            f'{path}\n\n'
            'Open the Theme Builder and click "Import Sensor List" to use them.',
            parent=_tk_root)
    else:
        messagebox.showwarning('DS916 — Sensor Discovery',
            'Could not discover sensors.\n'
            'Make sure HWiNFO64 is running with Shared Memory enabled.',
            parent=_tk_root)

def set_screen_mode(mode=None):
    """Tray callback: select auto mode or pin one of the three screens."""
    global _auto_screen_mode, _display_mode, _mode_low_since
    if mode == 'auto' or mode is None:
        _auto_screen_mode = True
        cfg['screen_mode'] = 'auto'
        save_cfg(cfg)
        _mode_low_since = None
        log.info('Screen mode control: automatic')
    elif mode in ('gaming', 'work', 'idle'):
        _auto_screen_mode = False
        _display_mode = mode
        cfg['screen_mode'] = mode
        save_cfg(cfg)
        _mode_low_since = None
        log.info('Screen mode control: manual -> %s', mode)
    if _tray is not None:
        try:
            _tray.update_menu()
        except Exception:
            pass


def _start_display_from_tray(icon=None, item=None):
    start_display()
    if _tray is not None:
        try:
            _tray.update_menu()
        except Exception:
            pass

def _stop_display_from_tray(icon=None, item=None):
    stop_display()
    if _tray is not None:
        try:
            _tray.update_menu()
        except Exception:
            pass

def build_menu():
    # Callable labels are resolved by pystray whenever the menu is shown,
    # so changing the language does not require recreating the tray icon.
    label = lambda source: (lambda item: tr(source))
    return pystray.Menu(
        Item(label('DS916 Screen Manager'), None, enabled=False),
        pystray.Menu.SEPARATOR,
        Item(label('▶ Start Display'), _start_display_from_tray, enabled=lambda item: not _running),
        Item(label('⏹ Stop Display'), _stop_display_from_tray, enabled=lambda item: _running),
        pystray.Menu.SEPARATOR,
        Item(label('📂 Load Theme…'), load_theme_dialog),
        Item(label('🎨 Open Theme Builder'), open_builder),
        pystray.Menu.SEPARATOR,
        Item(label('🔍 Discover Sensors'), discover_sensors_tray),
        Item(label('ℹ Status…'), show_status),
        Item(label('⚙ Settings…'), open_settings_tray),
        pystray.Menu.SEPARATOR,
        Item(label('🗑 Uninstall…'), uninstall_app),
        Item(label('❌ Exit'), quit_app),
    )

def run_tray():
    global _tray
    icon_img = make_tray_icon()
    _tray = pystray.Icon(APP_NAME, icon_img, 'DS916 Screen Manager', menu=build_menu())
    # Run pystray on its own thread so the main thread stays free for tk
    t = threading.Thread(target=_tray.run, daemon=True)
    t.start()

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    # Create a hidden tk root on the MAIN thread — must happen before anything
    # that needs dialogs.  All file dialogs and message boxes use this root.
    _tk_root = tk.Tk()
    _tk_root.withdraw()          # keep it invisible
    _tk_root.after(100, _poll_ui_queue)   # start queue polling
    _start_media_worker()  # Windows Now Playing metadata (Yandex Music / browser)

    # Try shared memory at startup. If it's not available yet (HWiNFO64 not
    # running, or Shared Memory Support not enabled), don't treat this as
    # fatal — read_sensors() retries on every sensor read, so it'll connect
    # automatically as soon as HWiNFO64 becomes available.
    if try_open_sharedmem():
        log.info('HWiNFO source: Shared Memory connected')
        # Auto-discover and save sensors on every startup
        discover_sensors()
        # Detect GPU VRAM capacity once at startup (static hardware info,
        # never needs re-checking during the session) so VRAM_USAGE can be
        # computed as a percentage even on cards that don't expose one directly
        detect_gpu_vram_capacity()
    else:
        log.info('HWiNFO source: unavailable for now - will keep retrying on each sensor read')
        log.info('  (start HWiNFO64 with Settings -> General -> Shared Memory Support enabled)')

    # Set autostart if configured
    if cfg.get('autostart', True):
        set_autostart(True)

    # Auto-load last theme
    if cfg.get('theme_path') and os.path.exists(cfg['theme_path']):
        if load_theme(cfg['theme_path']):
            start_display()

    # Start tray icon on background thread
    run_tray()

    # Main thread runs tk event loop (handles dialogs safely)
    try:
        _tk_root.mainloop()
    except KeyboardInterrupt:
        quit_app()
