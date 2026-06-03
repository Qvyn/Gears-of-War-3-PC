# EzBrowser — Live Matchmaking (Windows-friendly)
# - Keeps your current layout and features
# - Adds a "Live Matchmaking" button wired to your matchmaker running on http://216.201.73.145:5050
# - Uses simple presence heartbeat, enqueue/poll/accept, then launches the correct game to the given endpoint
# - Non-blocking-ish polling (yields to UI loop)

from __future__ import annotations

import sys, os, json, uuid, threading, time, subprocess, platform, re, ctypes, atexit
import asyncio

# Optional Windows registry access (Steam detection)
try:
    import winreg  # type: ignore
except Exception:
    winreg = None

from dataclasses import dataclass, asdict
from typing import Dict, List, Optional
from pathlib import Path

import requests

# ---------- Discord Rich Presence (optional) ----------
# Uses pypresence (pip install pypresence). Safe no-op if not installed or Discord isn't running.
try:
    from pypresence import Presence as DiscordPresence  # type: ignore
except Exception:
    DiscordPresence = None
from PyQt6.QtWidgets import (
QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QListWidget, QAbstractItemView, QListWidgetItem, QPushButton, QMessageBox, QLineEdit,
    QLabel, QInputDialog, QMenuBar, QMenu, QFrame,  QSplitter,
    QSplashScreen, QFileDialog, QDialog, QDialogButtonBox, QComboBox,
    QGraphicsDropShadowEffect, QSizePolicy, QFormLayout

)
from PyQt6.QtGui import QIcon, QPixmap, QColor, QPainter, QFont, QLinearGradient, QAction
from PyQt6.QtCore import QTimer, Qt, pyqtSignal, QEventLoop, QPropertyAnimation, QSize


class MatchSearchDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Live Matchmaking")
        self.setModal(True)
        self.setMinimumWidth(520)
        self._cancelled = False
        self._awaiting_accept = False
        layout = QVBoxLayout(self)
        self.title = QLabel("Searching for a match…")
        layout.addWidget(self.title)
        self.sub = QLabel("You’ll see players as they connect. This may take a moment.")
        self.sub.setWordWrap(True)
        layout.addWidget(self.sub)
        self.log = QListWidget()
        self.log.setMinimumHeight(220)
        layout.addWidget(self.log)
        self.btns = QDialogButtonBox()
        self.btn_cancel = self.btns.addButton("Cancel search", QDialogButtonBox.ButtonRole.RejectRole)
        self.btn_cancel.clicked.connect(self._on_cancel)
        layout.addWidget(self.btns)

    def _on_cancel(self):
        self._cancelled = True
        self.close()

    def log_line(self, text: str):
        self.log.addItem(text)
        self.log.scrollToBottom()
        QApplication.processEvents()

    def set_awaiting_accept(self, endpoint: str):
        if self._awaiting_accept:
            return
        self._awaiting_accept = True
        self.title.setText("Match found!")
        self.sub.setText(f"Opponent ready at {endpoint}. Join?")
        self.btns.clear()
        self.btn_accept = self.btns.addButton("Join", QDialogButtonBox.ButtonRole.AcceptRole)
        self.btn_decline = self.btns.addButton("Decline", QDialogButtonBox.ButtonRole.RejectRole)
        self.btn_accept.clicked.connect(lambda: self.done(QDialog.DialogCode.Accepted))
        self.btn_decline.clicked.connect(lambda: self.done(QDialog.DialogCode.Rejected))

    @property
    def cancelled(self) -> bool:
        return self._cancelled

# Optional audio (safe-guarded)
import pygame
try:
    pygame.mixer.pre_init(frequency=44100, size=-16, channels=2, buffer=512)
    pygame.mixer.init()
except Exception:
    pass

# ---------- Constants ----------

# ---------- Branding Toggle ----------
SHOW_INUI = False  # set True to show 'INUI' branding in the UI
APP_BRAND = "EzBrowser"



# Hide the Steam integration debug line in the sidebar (above the heartbeat light)
SHOW_STEAM_DEBUG_IN_UI = False
def app_base_dir() -> str:
    """Return the folder next to the running script/EXE (persistent, not _MEIPASS)."""
    try:
        return os.path.dirname(os.path.abspath(sys.argv[0]))
    except Exception:
        return os.getcwd()

def config_dir() -> str:
    """Persistent config folder next to the EXE/script."""
    base = app_base_dir()
    d = os.path.join(base, "config")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass

    # One-time migration: if older builds stored files next to the EXE/script,
    # move them into config/ so users don't lose settings.
    try:
        for fn in ("settings.json", "hosts.json", "host_config.json", "game_paths.json", "client_id.txt"):
            src = os.path.join(base, fn)
            dst = os.path.join(d, fn)
            if os.path.exists(src) and not os.path.exists(dst):
                try:
                    os.replace(src, dst)
                except Exception:
                    pass
    except Exception:
        pass

    return d

def config_path(name: str) -> str:
    return os.path.join(config_dir(), name)

# ---------- Gears 3 System Link INI bridge (Render -> GearGame.ini) ----------
def _derive_game_root_from_exe(exe_path: str) -> Optional[str]:
    r"""Given ...\Binaries\Win64\<exe>, return the game root folder."""
    try:
        p = Path(exe_path).resolve()
        # Expect: <Root>/Binaries/Win64/<Exe>
        if p.parent.name.lower() in ("win64", "win32") and p.parent.parent.name.lower() == "binaries":
            return str(p.parent.parent.parent)
        # Fallback: if user points at <Root>/Binaries/<Exe> or similar, walk up until we see GearGame/Config
        cur = p.parent
        for _ in range(6):
            if (cur / "GearGame" / "Config").exists():
                return str(cur)
            cur = cur.parent
    except Exception:
        pass
    return None

def _render_servers_to_custom_lines(servers: list) -> list:
    """Convert backend JSON rows into CustomBackendServerLines entries: Name|IP|Port|Map (Map optional)."""
    lines = []
    if not isinstance(servers, list):
        return lines
    for s in servers:
        if not isinstance(s, dict):
            continue
        name = str(s.get("name", "")).strip()
        ip = str(s.get("public_ip", "")).strip()
        port = str(s.get("port", "")).strip()
        map_name = str(s.get("map", "")).strip()
        if not name or not ip or not port:
            continue
        # Map is optional; keep it if present
        if map_name:
            lines.append(f'{name}|{ip}|{port}|{map_name}')
        else:
            lines.append(f'{name}|{ip}|{port}')
    return lines

def _upsert_systemlink_section(ini_text: str, server_lines: list) -> str:
    section_header = "[GearGame.UIScene_SystemLink]"
    new_lines = [section_header, "bUseCustomLobbyBackend=true"]
    for line in server_lines:
        # Unreal INI array syntax: repeated keys
        new_lines.append(f'CustomBackendServerLines="{line}"')
    new_block = "\n".join(new_lines).rstrip() + "\n"

    # Remove existing section (if any)
    pattern = re.compile(r"(?ms)^\[GearGame\.UIScene_SystemLink\]\s.*?(?=^\[|\Z)")
    ini_text = ini_text or ""
    if pattern.search(ini_text):
        ini_text = pattern.sub(new_block + "\n", ini_text).rstrip() + "\n"
        return ini_text
    # Append at end
    if ini_text and not ini_text.endswith("\n"):
        ini_text += "\n"
    if ini_text and not ini_text.endswith("\n\n"):
        ini_text += "\n"
    return ini_text + new_block

def _write_systemlink_ini_files(exe_path: str, server_lines: list) -> None:
    """Write servers into GearGame.ini for both install-root and user 'My Games' config locations."""
    if not exe_path or not server_lines:
        return

    # 1) Install-root GearGame.ini
    root = _derive_game_root_from_exe(exe_path)
    if root:
        ini_path = Path(root) / "GearGame" / "Config" / "GearGame.ini"
        try:
            ini_path.parent.mkdir(parents=True, exist_ok=True)
            cur = ini_path.read_text(encoding="utf-8", errors="ignore") if ini_path.exists() else ""
            updated = _upsert_systemlink_section(cur, server_lines)
            ini_path.write_text(updated, encoding="utf-8")
        except Exception:
            pass

    # 2) User config under Documents\My Games\*\GearGame\Config\GearGame.ini
    try:
        docs = Path.home() / "Documents" / "My Games"
        if docs.exists():
            for sub in docs.iterdir():
                if not sub.is_dir():
                    continue
                # Heuristic: only touch folders that look like a Gears/Jacinto build
                name = sub.name.lower()
                if ("gears" not in name) and ("jacinto" not in name) and ("v-day" not in name) and ("vday" not in name):
                    continue
                user_ini = sub / "GearGame" / "Config" / "GearGame.ini"
                if user_ini.parent.exists() or user_ini.exists():
                    try:
                        user_ini.parent.mkdir(parents=True, exist_ok=True)
                        cur = user_ini.read_text(encoding="utf-8", errors="ignore") if user_ini.exists() else ""
                        updated = _upsert_systemlink_section(cur, server_lines)
                        user_ini.write_text(updated, encoding="utf-8")
                    except Exception:
                        pass
    except Exception:
        pass

BACKEND_URL = "https://ezbrowser.onrender.com"  # Render master server (list/heartbeat)
MATCH_URL   = "https://ezbrowser.onrender.com"  # (unused) kept for compatibility
ENABLE_MATCHMAKER = False  # We are NOT running the matchmaker on Render
HEARTBEAT_INTERVAL = 5
HOSTS_DB = config_path("hosts.json")
LEGACY_HOST_CFG = config_path("host_config.json")
GAME_PATHS_DB = config_path("game_paths.json")
CLIENT_ID_FILE = config_path("client_id.txt")
# ---------- Local Mode (disable backend) ----------
SETTINGS_FILE = config_path("settings.json")
DEFAULT_LOCAL_MODE = False  # backend is online (Render)

def _settings_path() -> str:
    return SETTINGS_FILE
def load_settings() -> dict:
    try:
        p = _settings_path()
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f) or {}
    except Exception:
        pass
    return {}

def save_settings(d: dict) -> None:
    try:
        p = _settings_path()
        with open(p, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
    except Exception:
        pass

OMEN_WAV = "return-of-the-omen-fixed.wav"
MAD_WORLD_WAV = "mad-world-fixed.wav"
COG_TAG_WAV = "gears-of-war-cog-tag-fixed.wav"
SPLASH_PNG = "splash.png"
APP_ICON = "Jacinto.ico"

# ---------- Theme ----------
ACCENT = "#ff2a2a"
ACCENT_HOVER = "#ff3b3b"
ACCENT_FOCUS = "#ff5555"
BG = "#0f0f10"
BG_ELEV_1 = "#141416"
BG_ELEV_2 = "#1a1b1d"
STROKE = "#2a2b2e"
TEXT = "#e6e6e6"
TEXT_MUTED = "#a9abb2"


def apply_shadow(w: QWidget, radius: int = 24, dx: int = 0, dy: int = 12, a: int = 160):
    eff = QGraphicsDropShadowEffect(w)
    eff.setBlurRadius(radius)
    eff.setOffset(dx, dy)
    eff.setColor(QColor(0, 0, 0, a))
    w.setGraphicsEffect(eff)
    return eff


# ---------- Utils ----------

def resource_path(relative_path: str) -> str:
    base_path = getattr(sys, "_MEIPASS", None)
    if base_path:
        return os.path.join(base_path, relative_path)
    if os.path.exists(relative_path):
        return os.path.abspath(relative_path)
    here = os.path.dirname(os.path.abspath(sys.argv[0]))
    return os.path.join(here, relative_path)


def load_sound(name: str) -> Optional["pygame.mixer.Sound"]:
    try:
        path = resource_path(name)
        if os.path.exists(path):
            return pygame.mixer.Sound(path)
    except Exception:
        return None
    return None


def choose_exe_path(title: str) -> Optional[str]:
    path, _ = QFileDialog.getOpenFileName(None, title, "", "Executable (*.exe);;All Files (*)")
    if not path:
        return None
    if not os.path.exists(path):
        QMessageBox.critical(None, "Error", "Selected path does not exist.")
        return None
    return path


def get_client_id() -> str:
    try:
        if os.path.exists(CLIENT_ID_FILE):
            return open(CLIENT_ID_FILE, "r").read().strip()
        cid = str(uuid.uuid4())
        open(CLIENT_ID_FILE, "w").write(cid)
        return cid
    except Exception:
        # last-resort ephemeral id
        return str(uuid.uuid4())


# ---------- Steam Integration (best-effort, no game rebuild required) ----------
class SteamManager:
    """Best-effort Steam integration.

    Features:
    - Detect Steam install / running state (Windows-oriented).
    - Resolve SteamID64 (stable client_id for matchmaking).
    - Launch via Steam URL protocol: steam://run/<appid>//<args>
    - Best-effort Rich Presence:
        * Prefer direct ctypes bindings to steam_api64.dll (works even when wrappers are flaky)
        * Fallback to python wrappers if installed
    """

    STEAMID64_BASE = 76561197960265728  # SteamID64 = base + account_id (32-bit)

    @staticmethod
    def _reg_get_value(root, path, name):
        if not winreg:
            return None
        try:
            k = winreg.OpenKey(root, path)
            val, _typ = winreg.QueryValueEx(k, name)
            try:
                winreg.CloseKey(k)
            except Exception:
                pass
            return val
        except Exception:
            return None

    @staticmethod
    def steam_install_path() -> Optional[str]:
        if platform.system().lower() == "windows" and winreg:
            p = SteamManager._reg_get_value(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath")
            if isinstance(p, str) and p.strip():
                return p
            p = SteamManager._reg_get_value(winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Valve\Steam", "InstallPath")
            if isinstance(p, str) and p.strip():
                return p
            p = SteamManager._reg_get_value(winreg.HKEY_LOCAL_MACHINE, r"Software\Valve\Steam", "InstallPath")
            if isinstance(p, str) and p.strip():
                return p

        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        for guess in (os.path.join(pf86, "Steam"), os.path.join(pf, "Steam")):
            if guess and os.path.exists(guess):
                return guess
        return None

    @staticmethod
    def is_steam_installed() -> bool:
        p = SteamManager.steam_install_path()
        return bool(p and os.path.exists(os.path.join(p, "steam.exe")))

    @staticmethod
    def is_steam_running() -> bool:
        try:
            if platform.system().lower() == "windows":
                out = subprocess.check_output(["tasklist"], text=True, errors="ignore")
                return "steam.exe" in out.lower()
        except Exception:
            pass
        return False

    @staticmethod
    def get_steam_account_id_from_registry() -> Optional[int]:
        if platform.system().lower() != "windows" or not winreg:
            return None
        v = SteamManager._reg_get_value(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam\ActiveProcess", "ActiveUser")
        try:
            if v is None:
                return None
            account_id = int(v)
            if account_id <= 0:
                return None
            return account_id
        except Exception:
            return None

    @staticmethod
    def _read_loginusers_vdf() -> Optional[str]:
        p = SteamManager.steam_install_path()
        if not p:
            return None
        vdf = os.path.join(p, "config", "loginusers.vdf")
        if os.path.exists(vdf):
            try:
                return open(vdf, "r", encoding="utf-8", errors="ignore").read()
            except Exception:
                return None
        return None

    @staticmethod
    def get_steamid64() -> Optional[str]:
        aid = SteamManager.get_steam_account_id_from_registry()
        if aid is not None:
            return str(SteamManager.STEAMID64_BASE + int(aid))

        raw = SteamManager._read_loginusers_vdf()
        if not raw:
            return None

        ids = re.findall(r'"\s*(\d{16,18})\s*"\s*\{', raw)
        if not ids:
            return None

        for sid in ids:
            m = re.search(rf'"{re.escape(sid)}"\s*\{{(.*?)\n\s*\}}', raw, flags=re.S)
            block = m.group(1) if m else ""
            if re.search(r'"MostRecent"\s*"1"', block):
                return sid

        return ids[0]

    @staticmethod
    def _url_encode_args(args: str) -> str:
        return args.replace("%", "%25").replace(" ", "%20")

    @staticmethod
    def build_run_url(appid: str, args: str = "") -> str:
        appid = str(appid).strip()
        if args:
            return f"steam://run/{appid}//{SteamManager._url_encode_args(args)}"
        return f"steam://run/{appid}"

    @staticmethod
    def open_url(url: str) -> bool:
        try:
            if platform.system().lower() == "windows":
                os.startfile(url)  # type: ignore[attr-defined]
                return True
            subprocess.Popen(["xdg-open", url])
            return True
        except Exception:
            return False

    def __init__(self, appid: Optional[str] = None):
        self.appid = str(appid).strip() if appid else ""
        self._ready = False
        self.steamapi_init_ok: Optional[bool] = None
        self.friends_iface_found: Optional[bool] = None
        self.last_rich_presence: Dict[str, Optional[bool]] = {}

        # Best-effort wrapper handles (optional)
        self._api = None
        self._friends = None

        # Native steam_api64.dll (preferred)
        self._steam_dll = None

        # ctypes Rich Presence bindings (preferred)
        self._friends_ptr = None
        self._fn_set_rp = None
        self._fn_clear_rp = None
        self._fn_run_callbacks = None

        self._init_native()
        self._bind_ctypes_rich_presence()
        self._init_wrapper_best_effort()

    def _init_native(self) -> None:
        try:
            if self.appid:
                os.environ.setdefault("SteamAppId", self.appid)
                os.environ.setdefault("SteamGameId", self.appid)

            candidates = [
                os.path.join(os.getcwd(), "steam_api64.dll"),
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "steam_api64.dll"),
            ]
            dll_path = next((p for p in candidates if os.path.exists(p)), None)
            if not dll_path:
                return

            self._steam_dll = ctypes.CDLL(dll_path)
            try:
                self._steam_dll.SteamAPI_Init.restype = ctypes.c_bool
            except Exception:
                pass

            ok = bool(self._steam_dll.SteamAPI_Init())

            self.steamapi_init_ok = ok

            self._ready = ok
            if self._ready:
                try:
                    atexit.register(self.clear_presence)
                except Exception:
                    pass
        except Exception:
            self._ready = False
            self.steamapi_init_ok = False
            self._steam_dll = None

    def _bind_ctypes_rich_presence(self) -> None:
        """Bind flat ISteamFriends Rich Presence exports if available.

        This does NOT require python wrappers and tends to work even when wrapper init is flaky.
        """
        if not self._ready or self._steam_dll is None:
            return

        # Optional callbacks pump
        try:
            self._fn_run_callbacks = self._steam_dll.SteamAPI_RunCallbacks
            self._fn_run_callbacks.restype = None
        except Exception:
            self._fn_run_callbacks = None

        # Try to get an ISteamFriends pointer via SteamAPI_SteamFriends_vXXX exports
        ptr = None
        for ver in range(50, 0, -1):
            name = f"SteamAPI_SteamFriends_v{ver:03d}"
            try:
                fn = getattr(self._steam_dll, name)
            except Exception:
                continue
            try:
                fn.restype = ctypes.c_void_p
                p = fn()
                if p:
                    ptr = p
                    break
            except Exception:
                continue

        if not ptr:
            self.friends_iface_found = False
            return

        self.friends_iface_found = True
        self._friends_ptr = ctypes.c_void_p(ptr)

        try:
            self._fn_set_rp = self._steam_dll.SteamAPI_ISteamFriends_SetRichPresence
            self._fn_set_rp.restype = ctypes.c_bool
            self._fn_set_rp.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
        except Exception:
            self._fn_set_rp = None

        try:
            self._fn_clear_rp = self._steam_dll.SteamAPI_ISteamFriends_ClearRichPresence
            self._fn_clear_rp.restype = None
            self._fn_clear_rp.argtypes = [ctypes.c_void_p]
        except Exception:
            self._fn_clear_rp = None

    def _init_wrapper_best_effort(self) -> None:
        """Optional wrapper init (steamworks / steamworks.py), for environments where it's present."""
        try:
            import steamworks as _sw  # type: ignore

            # Newer wrapper may expose STEAMWORKS; older exposes Steam
            try:
                from steamworks import STEAMWORKS  # type: ignore
                self._api = STEAMWORKS()
            except Exception:
                try:
                    from steamworks import Steam  # type: ignore
                    self._api = Steam()
                except Exception:
                    self._api = None

            # Friends interface variations
            if hasattr(_sw, "SteamFriends"):
                try:
                    self._friends = _sw.SteamFriends()
                except Exception:
                    try:
                        self._friends = _sw.SteamFriends(self._api)
                    except Exception:
                        self._friends = None

            # Some wrappers expose friends off api directly
            if not self._friends and self._api:
                if hasattr(self._api, "Friends"):
                    self._friends = getattr(self._api, "Friends")
                elif hasattr(self._api, "friends"):
                    self._friends = getattr(self._api, "friends")

            if self._friends is not None:
                self.friends_iface_found = True

            # If native init failed, try wrapper init as last resort
            if not self._ready and self._api:
                try:
                    if hasattr(self._api, "initialize"):
                        self._ready = bool(self._api.initialize())
                    elif hasattr(self._api, "init"):
                        self._ready = bool(self._api.init())
                    elif hasattr(self._api, "Init"):
                        self._ready = bool(self._api.Init())
                    else:
                        self._ready = True
                except Exception:
                    self._ready = False
        except Exception:
            pass

    def is_ready(self) -> bool:
        return bool(self._ready)

    def set_presence(self, key: str, value: str) -> Optional[bool]:
        """Set a Steam Rich Presence key/value (best-effort).

        Returns:
            True/False when the call path provides a meaningful result, otherwise None.
        """
        if not self._ready:
            self.last_rich_presence[str(key)] = None
            return None

        k = str(key).encode("utf-8", errors="ignore")
        v = str(value).encode("utf-8", errors="ignore")
        result: Optional[bool] = None

        # Prefer ctypes binding (returns a real bool)
        try:
            if self._fn_set_rp is not None and self._friends_ptr is not None:
                result = bool(self._fn_set_rp(self._friends_ptr, k, v))
                if self._fn_run_callbacks is not None:
                    try:
                        self._fn_run_callbacks()
                    except Exception:
                        pass
                self.last_rich_presence[str(key)] = result
                return result
        except Exception:
            result = False

        # Fallback wrappers (most wrappers do not return a bool)
        try:
            if self._friends is not None and hasattr(self._friends, "SetRichPresence"):
                self._friends.SetRichPresence(k.decode("utf-8"), v.decode("utf-8"))
                result = True

            elif self._api is not None and hasattr(self._api, "Friends") and hasattr(self._api.Friends, "SetRichPresence"):
                self._api.Friends.SetRichPresence(k.decode("utf-8"), v.decode("utf-8"))
                result = True

            elif self._api is not None and hasattr(self._api, "friends") and hasattr(self._api.friends, "SetRichPresence"):
                self._api.friends.SetRichPresence(k.decode("utf-8"), v.decode("utf-8"))
                result = True

            else:
                try:
                    from steamworks import SteamFriends  # type: ignore

                    fr = SteamFriends(self._api)
                    if hasattr(fr, "SetRichPresence"):
                        fr.SetRichPresence(k.decode("utf-8"), v.decode("utf-8"))
                        result = True
                except Exception:
                    result = result if result is not None else None
        except Exception:
            result = False

        self.last_rich_presence[str(key)] = result
        return result

    def clear_presence(self) -> None:
        """Clear Steam Rich Presence values (best-effort)."""
        if not self._ready:
            return

        # Prefer ctypes binding
        try:
            if self._fn_clear_rp is not None and self._friends_ptr is not None:
                self._fn_clear_rp(self._friends_ptr)
                if self._fn_run_callbacks is not None:
                    try:
                        self._fn_run_callbacks()
                    except Exception:
                        pass
                return
        except Exception:
            pass

        # Wrapper fallbacks
        try:
            if self._friends is not None and hasattr(self._friends, "ClearRichPresence"):
                try:
                    self._friends.ClearRichPresence()
                    return
                except Exception:
                    pass

            if self._api is not None and hasattr(self._api, "Friends") and hasattr(self._api.Friends, "ClearRichPresence"):
                self._api.Friends.ClearRichPresence()
                return

            if self._api is not None and hasattr(self._api, "friends") and hasattr(self._api.friends, "ClearRichPresence"):
                self._api.friends.ClearRichPresence()
                return

            try:
                from steamworks import SteamFriends  # type: ignore
                fr = SteamFriends(self._api)
                if hasattr(fr, "ClearRichPresence"):
                    fr.ClearRichPresence()
                    return
            except Exception:
                pass

            if self._api is not None and hasattr(self._api, "ClearRichPresence"):
                self._api.ClearRichPresence()
        except Exception:
            pass


class DiscordManager:
    # Discord Rich Presence for EzBrowser (launcher-side only).
    # Requires a Discord Application Client ID.
    # Safe no-op if pypresence isn't installed or Discord isn't running.

    def __init__(self, client_id: str):
        self.client_id = str(client_id or "").strip()
        self._rpc = None
        self._ready = False
        if not self.client_id or not DiscordPresence:
            return
        try:
            self._rpc = DiscordPresence(self.client_id)
            self._rpc.connect()
            self._ready = True
        except Exception:
            self._rpc = None
            self._ready = False

    def is_ready(self) -> bool:
        return bool(self._ready and self._rpc)

    def set_activity(self, *, details: str = "", state: str = "", large_text: str = "EzBrowser", small_text: str = "") -> None:
        try:
            if not self.is_ready():
                return
            payload = {}
            if details:
                payload["details"] = str(details)[:128]
            if state:
                payload["state"] = str(state)[:128]
            payload["large_text"] = str(large_text)[:128]
            if small_text:
                payload["small_text"] = str(small_text)[:128]
            # pypresence supports "large_image"/"small_image" if configured in the Discord app; we keep text-only by default.
            self._rpc.update(**payload)
        except Exception:
            pass

    def clear(self) -> None:
        try:
            if self.is_ready():
                self._rpc.clear()
        except Exception:
            pass

    def close(self) -> None:
        try:
            if self._rpc:
                self._rpc.close()
        except Exception:
            pass

# ---------- Models ----------
@dataclass
class Host:
    id: str
    name: str
    public_ip: str
    local_ip: str
    port: int
    map: str
    password: str
    exe_path: str

    @staticmethod
    def from_prompt() -> Optional["Host"]:
        prompts = [
            ("Server Name", "Enter your server name:"),
            ("Public IP", "Enter your public IP:"),
            ("Local IP", "Enter your local IP:"),
            ("Port", "Enter your server port:"),
            ("Map", "Enter map name:"),
        ]
        vals: List[str] = []
        for title, msg in prompts:
            text, ok = QInputDialog.getText(None, title, msg)
            if not ok or not text.strip():
                return None
            vals.append(text.strip())
        pw, ok = QInputDialog.getText(None, "Password", "Set a host password:", echo=QLineEdit.EchoMode.Password)
        if not ok:
            return None
        try:
            port = int(vals[3])
        except ValueError:
            QMessageBox.critical(None, "Error", "Port must be an integer.")
            return None
        exe = choose_exe_path("Select game EXE for this server")
        if not exe:
            return None
        return Host(
            id=str(uuid.uuid4()),
            name=vals[0], public_ip=vals[1], local_ip=vals[2], port=port, map=vals[4], password=pw or "", exe_path=exe,
        )

# ---------- Storage ----------

def load_hosts() -> List[Host]:
    if os.path.exists(HOSTS_DB):
        try:
            data = json.load(open(HOSTS_DB, "r"))
            items: List[Host] = []
            for h in data:
                if "exe_path" not in h:
                    h["exe_path"] = ""
                items.append(Host(**h))
            return items
        except Exception:
            pass
    if os.path.exists(LEGACY_HOST_CFG):
        try:
            legacy = json.load(open(LEGACY_HOST_CFG, "r"))
            host = Host(
                id=str(uuid.uuid4()),
                name=legacy["name"], public_ip=legacy["public_ip"], local_ip=legacy["local_ip"],
                port=int(legacy["port"]), map=legacy["map"], password=legacy.get("password", ""), exe_path="",
            )
            save_hosts([host])
            try:
                os.remove(LEGACY_HOST_CFG)
            except OSError:
                pass
            return [host]
        except Exception:
            pass
    return []


def save_hosts(items: List[Host]) -> None:
    json.dump([asdict(h) for h in items], open(HOSTS_DB, "w"), indent=2)

# ---------- Game Paths ----------
class GamePathManager:
    VDAY = "vday"
    JACINTO = "jacinto"

    @staticmethod
    def load() -> Dict[str, str]:
        if os.path.exists(GAME_PATHS_DB):
            try:
                data = json.load(open(GAME_PATHS_DB, "r"))
                if isinstance(data, dict):
                    return {k: str(v) for k, v in data.items()}
            except Exception:
                pass
        return {GamePathManager.VDAY: "", GamePathManager.JACINTO: ""}

    @staticmethod
    def save(paths: Dict[str, str]) -> None:
        json.dump(paths, open(GAME_PATHS_DB, "w"), indent=2)

    @staticmethod
    def ensure_path(game_key: str, parent: Optional[QWidget] = None) -> Optional[str]:
        paths = GamePathManager.load()
        cur = paths.get(game_key, "")
        if cur and os.path.exists(cur):
            return cur
        title = "Select EXE for Gears of War V-Day" if game_key == GamePathManager.VDAY else "Select EXE for Gears of War Jacinto 2.0"
        exe = choose_exe_path(title)
        if not exe:
            return None
        paths[game_key] = exe
        GamePathManager.save(paths)
        return exe

# ---------- Matchmake Dialog ----------
class MatchmakeDialog(QDialog):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Pick Game for Live Matchmaking")
        self.setModal(True)
        self.setStyleSheet(f"QDialog {{ background-color: {BG_ELEV_2}; color: {TEXT}; }}")

        v = QVBoxLayout(self)
        row = QHBoxLayout()
        lab = QLabel("Game:"); lab.setStyleSheet(f"color:{TEXT}; background:transparent;")
        row.addWidget(lab)
        self.combo = QComboBox(self)
        self.combo.addItem("Gears of War V-Day", GamePathManager.VDAY)
        self.combo.addItem("Gears of War Jacinto 2.0", GamePathManager.JACINTO)
        self.combo.setStyleSheet(
            f"QComboBox {{ background:{BG_ELEV_1}; border:1px solid {STROKE}; border-radius:8px; padding:6px; color:{TEXT}; }}"
            f"QComboBox:hover {{ border-color:{ACCENT}; }}"
            f"QComboBox QAbstractItemView {{ background:{BG_ELEV_2}; color:{TEXT}; border:1px solid {STROKE}; }}"
        )
        row.addWidget(self.combo)
        v.addLayout(row)

        self.path_label = QLabel(""); self.path_label.setWordWrap(True); self.path_label.setStyleSheet(f"color:{TEXT_MUTED}; background:transparent;")
        v.addWidget(self.path_label)

        browse_row = QHBoxLayout()
        self.browse_btn = QPushButton("Set/Change Game EXE…")
        self.browse_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.browse_btn.setStyleSheet(
            f"QPushButton {{ background:{BG_ELEV_1}; border:1px solid {STROKE}; border-radius:10px; padding:8px 12px; color:{TEXT}; }}"
            f"QPushButton:hover {{ border-color:{ACCENT}; background:{BG_ELEV_2}; }}"
            f"QPushButton:pressed {{ background:{BG_ELEV_2}; border-color:{ACCENT_FOCUS}; }}"
        )
        self.browse_btn.clicked.connect(self.browse)
        browse_row.addWidget(self.browse_btn)
        v.addLayout(browse_row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.setStyleSheet(
            f"QDialogButtonBox QPushButton {{ background:{ACCENT}; border:0; border-radius:10px; padding:8px 16px; color:white; }}"
            f"QDialogButtonBox QPushButton:hover {{ background:{ACCENT_HOVER}; }}"
            f"QDialogButtonBox QPushButton:pressed {{ background:{ACCENT_FOCUS}; }}"
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        v.addWidget(buttons)

        self.combo.currentIndexChanged.connect(self.refresh_path_label)
        self.refresh_path_label()

    def selected_key(self) -> str:
        return str(self.combo.currentData())

    def refresh_path_label(self):
        key = self.selected_key()
        cur = GamePathManager.load().get(key, "")
        shown = cur if cur else "No EXE set yet. Click the button below to choose."
        self.path_label.setText(f"Current EXE: {shown}")

    def browse(self):
        key = self.selected_key()
        exe = choose_exe_path("Select Game EXE")
        if exe:
            paths = GamePathManager.load()
            paths[key] = exe
            GamePathManager.save(paths)
            self.refresh_path_label()


# ---------- Server Editor Dialog ----------
class ServerEditorDialog(QDialog):
    """Single-screen server editor.

    Replaces the old multi-popup flow so adding/editing a server is faster and less error-prone.
    """
    def __init__(self, parent: Optional[QWidget] = None, host: Optional[Host] = None):
        super().__init__(parent)
        self.setWindowTitle("Server Setup" if host is None else "Edit Server")
        self.setModal(True)
        self.setMinimumWidth(620)
        self._host_id = host.id if host else str(uuid.uuid4())
        self.setStyleSheet(
            f"QDialog {{ background:{BG}; color:{TEXT}; }}"
            f"QLabel {{ background:transparent; color:{TEXT_MUTED}; }}"
            f"QLineEdit {{ background:{BG_ELEV_1}; border:1px solid {STROKE}; border-radius:10px; padding:8px 10px; color:{TEXT}; }}"
            f"QLineEdit:focus {{ border-color:{ACCENT}; }}"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(12)

        head = QLabel("Everything needed to host/join this server is on one screen.")
        head.setWordWrap(True)
        head.setStyleSheet(f"color:{TEXT}; font-size:15px; font-weight:600; background:transparent;")
        root.addWidget(head)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(10)

        self.name = QLineEdit(host.name if host else "")
        self.public_ip = QLineEdit(host.public_ip if host else "")
        self.local_ip = QLineEdit(host.local_ip if host else "")
        self.port = QLineEdit(str(host.port) if host else "1000")
        self.map = QLineEdit(host.map if host else "MP_BloodDriveG3")
        self.password = QLineEdit(host.password if host else "")
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.exe_path = QLineEdit(host.exe_path if host else "")
        self.exe_path.setReadOnly(True)

        form.addRow("Server name", self.name)
        form.addRow("Public IP", self.public_ip)
        form.addRow("Local IP", self.local_ip)
        form.addRow("Port", self.port)
        form.addRow("Map", self.map)
        form.addRow("Host password", self.password)

        exe_row = QHBoxLayout()
        exe_row.setSpacing(8)
        exe_row.addWidget(self.exe_path, 1)
        browse = QPushButton("Browse…")
        browse.setCursor(Qt.CursorShape.PointingHandCursor)
        browse.clicked.connect(self._browse_exe)
        exe_row.addWidget(browse)
        form.addRow("Game EXE", exe_row)

        root.addLayout(form)

        help_text = QLabel("Tip: public IP is what guests use. local IP is what the host uses when auto-joining after starting the server.")
        help_text.setWordWrap(True)
        help_text.setStyleSheet(f"color:{TEXT_MUTED}; background:transparent; font-size:12px;")
        root.addWidget(help_text)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _browse_exe(self):
        exe = choose_exe_path("Select game EXE for this server")
        if exe:
            self.exe_path.setText(exe)

    def accept(self):
        required = [
            (self.name, "Server name"),
            (self.public_ip, "Public IP"),
            (self.local_ip, "Local IP"),
            (self.port, "Port"),
            (self.map, "Map"),
        ]
        for field, label in required:
            if not field.text().strip():
                QMessageBox.warning(self, "Missing Field", f"{label} is required.")
                field.setFocus()
                return
        try:
            int(self.port.text().strip())
        except ValueError:
            QMessageBox.warning(self, "Invalid Port", "Port must be a number.")
            self.port.setFocus()
            return
        super().accept()

    def to_host(self) -> Host:
        return Host(
            id=self._host_id,
            name=self.name.text().strip(),
            public_ip=self.public_ip.text().strip(),
            local_ip=self.local_ip.text().strip(),
            port=int(self.port.text().strip()),
            map=self.map.text().strip(),
            password=self.password.text(),
            exe_path=self.exe_path.text().strip(),
        )


# ---------- Server Presets ----------
# Jacinto 2.0 / Gears 3-style game aliases. Keep KOTH's exact launch style intact,
# but let the selected alias replace the ?game= value for direct match launches.
SERVER_GAME_PRESETS = [
    ("lobby", "Lobby", "GearGameContent.GearPreGameLobbyGameDedicated", "Pregame lobby / playlist lobby"),

    # Known Jacinto/Gears short aliases already used by this build/community.
    ("tdm", "TDM", "tdm", "Team Deathmatch"),
    ("koth", "KOTH", "koth", "King of the Hill"),
    ("ffa", "FFA", "ffa", "Free For All"),
    ("exe", "Execution", "exe", "Execution / single-life rounds"),
    ("ktl", "KTL", "ktl", "KTL"),
    ("ctl", "CTL", "ctl", "Capture the Leader"),

    # Extra Gears 3 modes. These are included as launch-test presets because
    # Jacinto's exact aliases may differ from the retail display names.
    ("warzone", "Warzone", "warzone", "Warzone / single-life elimination"),
    ("wingman", "Wingman", "wingman", "Wingman / 2-player teams"),
    ("execution", "Execution (full alias)", "execution", "Alternate Execution alias for testing"),
]
SERVER_GAME_PRESET_KEYS = {key for key, _label, _code, _desc in SERVER_GAME_PRESETS}
SERVER_GAME_PRESET_BY_KEY = {key: {"label": label, "code": code, "description": desc} for key, label, code, desc in SERVER_GAME_PRESETS}


def server_preset_label(key: str) -> str:
    info = SERVER_GAME_PRESET_BY_KEY.get(str(key or "").lower())
    return info["label"] if info else str(key or "KOTH").upper()


def server_preset_game_code(key: str) -> str:
    info = SERVER_GAME_PRESET_BY_KEY.get(str(key or "").lower())
    return info["code"] if info else "koth"

# ---------- RowCard ----------
class RowCard(QWidget):
    def __init__(self, title: str, subtitle: str = "", tag: str = "", status_color: str = "#7ed957", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        base = QFrame(self); base.setObjectName("rowcard")
        base.setStyleSheet(f"QFrame#rowcard {{ background:{BG_ELEV_2}; border:1px solid {STROKE}; border-radius:14px; }}")
        apply_shadow(base, 18, 0, 8, 110)
        root = QHBoxLayout(self); root.setContentsMargins(0,0,0,0); root.addWidget(base)
        h = QHBoxLayout(base); h.setContentsMargins(12,10,12,10); h.setSpacing(10)

        dot = QLabel(); dot.setFixedSize(10,10)
        dot.setStyleSheet(f"background:{status_color}; border-radius:5px; border:1px solid {STROKE};")
        h.addWidget(dot, 0, Qt.AlignmentFlag.AlignVCenter)

        text_box = QVBoxLayout(); text_box.setSpacing(2)
        title_lbl = QLabel(title); title_lbl.setStyleSheet(f"background:transparent; color:{TEXT}; font-weight:600; font-size:14px;")
        sub_lbl = QLabel(subtitle); sub_lbl.setStyleSheet(f"background:transparent; color:{TEXT_MUTED}; font-size:12px;")
        text_box.addWidget(title_lbl)
        text_box.addWidget(sub_lbl)
        h.addLayout(text_box, 1)

        if tag:
            tag_lbl = QLabel(tag)
            tag_lbl.setStyleSheet(f"QLabel {{ background:{BG_ELEV_1}; color:{TEXT}; border:1px solid {STROKE}; border-radius:10px; padding:4px 8px; font-size:12px; }}")
            h.addWidget(tag_lbl, 0, Qt.AlignmentFlag.AlignVCenter)

        chev = QLabel("›")
        chev.setStyleSheet(f"color:{TEXT_MUTED}; font-size:18px; background:transparent;")
        h.addWidget(chev, 0, Qt.AlignmentFlag.AlignVCenter)

# ---------- UI Shell ----------
class JacintoLobbyBrowser(QWidget):
    hb_color_signal = pyqtSignal(str)
    backend_offline_signal = pyqtSignal(str)
    available_refresh_signal = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_BRAND} — Live MM" if not SHOW_INUI else f"{APP_BRAND} — Live MM")
        icon_path = resource_path(APP_ICON)
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
        self.setGeometry(200, 100, 1140, 720)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet(self._theme())

        s = load_settings()
        self.settings = dict(s) if isinstance(s, dict) else {}
        self.local_mode = bool(self.settings.get('local_mode', DEFAULT_LOCAL_MODE))
        # Server command preset (for dedicated server start).
        # Lobby keeps the pregame lobby startup. Match presets replace the ?game= alias.
        _scp = str(self.settings.get('server_cmd_preset', 'lobby') or 'lobby').strip().lower()
        self.server_cmd_preset = _scp if _scp in SERVER_GAME_PRESET_KEYS else 'lobby'


        # Steam settings (best-effort)
        self.steam_enabled = bool(self.settings.get("steam_enabled", True))
        self.steam_launch = bool(self.settings.get("steam_launch", True))
        self.steam_appid = "480"  # fixed Spacewar AppID
        self.steam_joins_only = bool(self.settings.get("steam_joins_only", True))
        self.steam_rich_presence = bool(self.settings.get("steam_rich_presence", True))

        # Discord settings (best-effort Rich Presence)
        self.discord_enabled = bool(self.settings.get("discord_enabled", True))
        self.discord_rich_presence = bool(self.settings.get("discord_rich_presence", True))
        self.discord_client_id = "1460086147534553143"  # fixed Discord Client ID  # Discord App Client ID
        self._discord_api = DiscordManager(self.discord_client_id) if (self.discord_enabled and self.discord_rich_presence and self.discord_client_id) else None
        self._current_host_for_presence: Optional[Host] = None


        self._steam_api = SteamManager(self.steam_appid) if (self.steam_enabled and self.steam_rich_presence and self.steam_appid) else None


        self.is_muted = False
        self.click = load_sound(COG_TAG_WAV)
        self.bg = load_sound(OMEN_WAV)
        self.bg_ch = pygame.mixer.Channel(1) if pygame.mixer.get_init() else None
        self.fx_ch = pygame.mixer.Channel(2) if pygame.mixer.get_init() else None

        self.available_timer = QTimer(self)
        self.available_timer.timeout.connect(self._refresh_available)
        if not self.local_mode:
            self.available_timer.start(5000)
        self._backend_failures = 0
        self._backend_hard_disabled = False
        self.backend_offline_signal.connect(self._on_backend_offline)
        self.available_refresh_signal.connect(self._refresh_available)
        self.hb_threads: Dict[str, threading.Event] = {}
        self.hb_color_signal.connect(self._set_hb_color)

        # Live MM presence
        steam_id = SteamManager.get_steamid64() if self.steam_enabled else None
        self.client_id = steam_id or get_client_id()
        self._client_id_source = "steam" if steam_id else "uuid" 
        self.presence_stop = threading.Event()
        self.presence_thread = None
        if (not self.local_mode) and ENABLE_MATCHMAKER:
            self.presence_thread = threading.Thread(target=self._presence_loop, args=(self.presence_stop,), daemon=True)
            self.presence_thread.start()

        self._setup_ui()
        if not self.local_mode:
            self._refresh_available()
        self._refresh_mine()

        # Auto-start Discord announcer (embedded)
        try:
            self._discord_watcher = EmbeddedDiscordWatcher(self)
            self._discord_watcher.start()
        except Exception:
            self._discord_watcher = None

    def set_local_mode(self, enabled: bool) -> None:
        self.local_mode = bool(enabled)
        s = load_settings()
        s["local_mode"] = bool(enabled)
        save_settings(s)

        # Stop backend-driven work when local mode is enabled
        if self.local_mode:
            try:
                self.available_timer.stop()
            except Exception:
                pass
            try:
                self.presence_stop.set()
            except Exception:
                pass
            # stop any running heartbeat threads
            try:
                self.manual_stop_heartbeat()
            except Exception:
                pass
            # clear available list to avoid stale data
            try:
                self.available_list.clear()
            except Exception:
                pass
        else:
            # Re-enable backend work
            try:
                if not self.available_timer.isActive():
                    self.available_timer.start(5000)
            except Exception:
                pass
            try:
                # restart presence loop thread (only if matchmaker enabled)
                if ENABLE_MATCHMAKER:
                    self.presence_stop = threading.Event()
                    self.presence_thread = threading.Thread(target=self._presence_loop, args=(self.presence_stop,), daemon=True)
                    self.presence_thread.start()
            except Exception:
                pass
            try:
                self._refresh_available()
            except Exception:
                pass

        # Update button states (backend-only actions)
        try:
            self.btn_match.setEnabled(not self.local_mode)
        except Exception:
            pass
        try:
            self.btn_live.setEnabled(not self.local_mode)
        except Exception:
            pass

    def _on_backend_offline(self, reason: str = "") -> None:
        """Circuit-breaker: stop backend retries after repeated failures."""
        if bool(getattr(self, "_backend_hard_disabled", False)):
            return
        self._backend_hard_disabled = True
        try:
            if hasattr(self, "_backend_local_action") and self._backend_local_action is not None:
                self._backend_local_action.setChecked(True)
        except Exception:
            pass
        try:
            self.set_local_mode(True)
        except Exception:
            pass

    def _toggle_local_mode(self, checked: bool = None) -> None:
        """UI handler for Backend → Local Mode toggle."""
        try:
            if checked is None:
                checked = bool(self._backend_local_action.isChecked())
        except Exception:
            checked = bool(checked)

        # Apply mode + persist
        self.set_local_mode(bool(checked))

        # Update UI controls that rely on backend, if they exist
        try:
            if hasattr(self, "btn_match") and self.btn_match is not None:
                self.btn_match.setEnabled(not self.local_mode)
        except Exception:
            pass
        try:
            if hasattr(self, "btn_live") and self.btn_live is not None:
                self.btn_live.setEnabled(not self.local_mode)
        except Exception:
            pass
        try:
            # If there is a status label for backend, update it
            if hasattr(self, "lbl_backend_mode") and self.lbl_backend_mode is not None:
                self.lbl_backend_mode.setText("LOCAL MODE" if self.local_mode else "BACKEND MODE")
        except Exception:
            pass

    def _theme(self) -> str:
        return (
            f"QWidget {{ background-color: {BG}; color: {TEXT}; font-family: 'Segoe UI Variable', 'Segoe UI', Arial; font-size: 14px; }}"
            f"QMenuBar {{ background: {BG_ELEV_2}; border-bottom: 1px solid {STROKE}; color:{TEXT}; }}"
            f"QMenuBar::item {{ padding:6px 10px; background: transparent; }}"
            f"QMenuBar::item:selected {{ background: {BG_ELEV_1}; border-bottom:2px solid {ACCENT}; }}"
            f"QMenu {{ background: {BG_ELEV_1}; border:1px solid {STROKE}; color:{TEXT}; }}"
            f"QMenu::item:selected {{ background: {BG_ELEV_2}; }}"
            f"QPushButton {{ background:{BG_ELEV_1}; border:1px solid {STROKE}; border-radius:12px; padding:10px 14px; color:{TEXT}; }}"
            f"QPushButton:hover {{ border-color:{ACCENT}; }}"
            f"QPushButton:pressed {{ background:{BG_ELEV_2}; border-color:{ACCENT_FOCUS}; }}"
            f"QLineEdit {{ background:{BG_ELEV_1}; border:1px solid {STROKE}; border-radius:14px; padding:10px 12px; color:{TEXT}; }}"
            f"QLineEdit:focus {{ border-color:{ACCENT}; }}"
            f"QListWidget {{ background: transparent; border: none; }}"
            f"QScrollBar:vertical {{ width:10px; background: transparent; margin:4px; }}"
            f"QScrollBar::handle:vertical {{ background:{STROKE}; border-radius:5px; min-height:40px; }}"
            f"QScrollBar::handle:vertical:hover {{ background:#3a3b3e; }}"
            f"QScrollBar:horizontal {{ height:10px; background: transparent; margin:4px; }}"
            f"QScrollBar::handle:horizontal {{ background:{STROKE}; border-radius:5px; min-width:40px; }}"
        )


    def _make_section_label(self, text: str, subtext: str = "") -> QWidget:
        box = QVBoxLayout()
        wrap = QWidget(self)
        wrap.setLayout(box)
        wrap.setStyleSheet("background:transparent;")
        title = QLabel(text)
        title.setStyleSheet(f"color:{TEXT}; font-weight:800; font-size:17px; background:transparent;")
        box.addWidget(title)
        if subtext:
            sub = QLabel(subtext)
            sub.setWordWrap(True)
            sub.setStyleSheet(f"color:{TEXT_MUTED}; font-size:12px; background:transparent;")
            box.addWidget(sub)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(2)
        return wrap

    def _wire_selection_helpers(self) -> None:
        self.search.textChanged.connect(self._apply_search_filter)
        self.available_list.itemSelectionChanged.connect(self._update_action_state)
        self.my_list.itemSelectionChanged.connect(self._update_action_state)
        self.available_list.itemDoubleClicked.connect(lambda _item: self.launch_selected())
        self.my_list.itemDoubleClicked.connect(lambda _item: self.launch_selected())
        self._update_action_state()

    def _update_action_state(self) -> None:
        has_available = self.available_list.currentRow() >= 0
        has_mine = self.my_list.currentRow() >= 0
        has_any = has_available or has_mine
        try:
            self.btn_launch.setEnabled(has_any)
            self.btn_edit.setEnabled(has_mine)
            self.btn_start_server.setEnabled(has_mine)
            self.btn_hb_start.setEnabled(has_mine and not self.local_mode)
            self.btn_hb_stop.setEnabled(bool(self.hb_threads))
            self.btn_delete.setEnabled(has_mine)
        except Exception:
            pass
        try:
            mode = "LOCAL" if self.local_mode else "ONLINE"
            hb = "ON" if any(not ev.is_set() for ev in self.hb_threads.values()) else "OFF"
            cmd = server_preset_label(str(getattr(self, "server_cmd_preset", "lobby") or "lobby"))
            self.lbl_status_strip.setText(f"Mode: {mode}   •   Heartbeat: {hb}   •   Server preset: {cmd}")
        except Exception:
            pass

    def _apply_search_filter(self, text: str = "") -> None:
        needle = (text or "").strip().lower()
        for lst in (getattr(self, "available_list", None), getattr(self, "my_list", None)):
            if lst is None:
                continue
            for row in range(lst.count()):
                item = lst.item(row)
                hay = str(item.data(Qt.ItemDataRole.UserRole) or "").lower()
                widget = lst.itemWidget(item)
                # RowCard stores visible text in child labels, so include those too.
                try:
                    hay += " " + " ".join(lbl.text().lower() for lbl in widget.findChildren(QLabel))
                except Exception:
                    pass
                item.setHidden(bool(needle and needle not in hay))

    def _remove_selected_server(self) -> None:
        idx = self.my_list.currentRow()
        if idx < 0:
            QMessageBox.information(self, "Select", "Select a server in 'My Servers' first.")
            return
        hosts = load_hosts()
        if idx >= len(hosts):
            return
        host = hosts[idx]
        res = QMessageBox.question(
            self,
            "Remove Server",
            f"Remove '{host.name}' from My Servers?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if res != QMessageBox.StandardButton.Yes:
            return
        del hosts[idx]
        save_hosts(hosts)
        self._refresh_mine()
        self._update_action_state()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)
        self.setLayout(root)

        # Menu bar: keep advanced/rare actions out of the main workflow.
        bar = QMenuBar(self)
        hb = QMenu("Heartbeat", self)
        hb.addAction("Start selected server", self.manual_start_heartbeat)
        hb.addAction("Stop all heartbeats", self.manual_stop_heartbeat)
        bar.addMenu(hb)

        backend = QMenu("Backend", self)
        self._backend_local_action = QAction("Local Mode (disable backend)", self)
        self._backend_local_action.setCheckable(True)
        try:
            self._backend_local_action.setChecked(bool(self.settings.get("local_mode", DEFAULT_LOCAL_MODE)))
        except Exception:
            self._backend_local_action.setChecked(bool(DEFAULT_LOCAL_MODE))
        self._backend_local_action.triggered.connect(self._toggle_local_mode)
        backend.addAction(self._backend_local_action)
        bar.addMenu(backend)

        server_cmd = QMenu("Server Preset", self)
        self._server_cmd_actions = {}
        for preset_key, preset_label, _game_code, preset_desc in SERVER_GAME_PRESETS:
            action_text = preset_label if preset_key == "lobby" else f"{preset_label} — {preset_desc}"
            action = QAction(action_text, self)
            action.setCheckable(True)
            action.setChecked(getattr(self, "server_cmd_preset", "lobby") == preset_key)
            action.triggered.connect(lambda _checked=False, key=preset_key: self._set_server_cmd_preset(key))
            server_cmd.addAction(action)
            self._server_cmd_actions[preset_key] = action
        bar.addMenu(server_cmd)

        steam = QMenu("Steam", self)
        self._steam_enabled_action = QAction("Enable Steam integration", self)
        self._steam_enabled_action.setCheckable(True)
        self._steam_enabled_action.setChecked(bool(self.steam_enabled))
        self._steam_enabled_action.triggered.connect(self._toggle_steam_enabled)
        steam.addAction(self._steam_enabled_action)
        self._steam_launch_action = QAction("Launch joins via Steam", self)
        self._steam_launch_action.setCheckable(True)
        self._steam_launch_action.setChecked(bool(self.steam_launch))
        self._steam_launch_action.triggered.connect(self._toggle_steam_launch)
        steam.addAction(self._steam_launch_action)
        self._steam_rp_action = QAction("Rich Presence", self)
        self._steam_rp_action.setCheckable(True)
        self._steam_rp_action.setChecked(bool(self.steam_rich_presence))
        self._steam_rp_action.triggered.connect(self._toggle_steam_rich_presence)
        steam.addAction(self._steam_rp_action)
        steam.addSeparator()
        steam.addAction("Steam status…", self._steam_status_dialog)
        bar.addMenu(steam)

        discord = QMenu("Discord", self)
        self._discord_enabled_action = QAction("Enable Discord integration", self)
        self._discord_enabled_action.setCheckable(True)
        self._discord_enabled_action.setChecked(bool(getattr(self, "discord_enabled", True)))
        self._discord_enabled_action.triggered.connect(self._toggle_discord_enabled)
        discord.addAction(self._discord_enabled_action)
        self._discord_rp_action = QAction("Rich Presence", self)
        self._discord_rp_action.setCheckable(True)
        self._discord_rp_action.setChecked(bool(getattr(self, "discord_rich_presence", True)))
        self._discord_rp_action.triggered.connect(self._toggle_discord_rich_presence)
        discord.addAction(self._discord_rp_action)
        discord.addSeparator()
        discord.addAction("Discord status…", self._discord_status_dialog)
        bar.addMenu(discord)

        audio = QMenu("Audio", self)
        mute_action = QAction("Toggle mute", self)
        mute_action.triggered.connect(self.toggle_mute)
        audio.addAction(mute_action)
        bar.addMenu(audio)
        root.setMenuBar(bar)

        header = QFrame(self)
        header.setObjectName("header")
        header.setStyleSheet(f"QFrame#header {{ background:{BG_ELEV_2}; border:1px solid {STROKE}; border-radius:18px; }}")
        apply_shadow(header, 20, 0, 8, 100)
        hv = QVBoxLayout(header)
        hv.setContentsMargins(18, 14, 18, 14)
        hv.setSpacing(10)

        top = QHBoxLayout()
        brand = QLabel("EzBrowser")
        brand.setStyleSheet(f"color:{TEXT}; font-size:24px; font-weight:900; background:transparent;")
        top.addWidget(brand)
        self.lbl_status_strip = QLabel("")
        self.lbl_status_strip.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.lbl_status_strip.setStyleSheet(f"color:{TEXT_MUTED}; background:transparent; font-size:12px;")
        top.addWidget(self.lbl_status_strip, 1)
        hb_lab = QLabel("HB")
        hb_lab.setStyleSheet(f"color:{TEXT_MUTED}; background:transparent; font-size:12px;")
        top.addWidget(hb_lab)
        self.hb_status = QLabel()
        self.hb_status.setFixedSize(16, 16)
        self._set_hb_color("red")
        top.addWidget(self.hb_status)
        hv.addLayout(top)

        tools = QHBoxLayout()
        tools.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search by server, IP, map…")
        self.search.setClearButtonEnabled(True)
        tools.addWidget(self.search, 1)
        self.btn_live = QPushButton("Live Matchmaking")
        self.btn_match = QPushButton("Quick Match")
        self.btn_add = QPushButton("+ Add Server")
        for b in (self.btn_live, self.btn_match, self.btn_add):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setMinimumHeight(40)
            tools.addWidget(b)
        hv.addLayout(tools)
        root.addWidget(header)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        left_card = QFrame(self)
        left_card.setObjectName("pane")
        left_card.setStyleSheet(f"QFrame#pane {{ background:{BG_ELEV_2}; border:1px solid {STROKE}; border-radius:16px; }}")
        apply_shadow(left_card, 18, 0, 8, 95)
        lv = QVBoxLayout(left_card)
        lv.setContentsMargins(14, 14, 14, 14)
        lv.setSpacing(10)
        lv.addWidget(self._make_section_label("Available Servers", "Public servers from the backend. Double-click to join."))
        self.available_list = QListWidget()
        self.available_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.available_list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.available_list.setIconSize(QSize(1, 1))
        lv.addWidget(self.available_list, 1)

        right_card = QFrame(self)
        right_card.setObjectName("pane")
        right_card.setStyleSheet(f"QFrame#pane {{ background:{BG_ELEV_2}; border:1px solid {STROKE}; border-radius:16px; }}")
        apply_shadow(right_card, 18, 0, 8, 95)
        rv = QVBoxLayout(right_card)
        rv.setContentsMargins(14, 14, 14, 14)
        rv.setSpacing(10)
        rv.addWidget(self._make_section_label("My Servers", "Saved host profiles. Select one to launch, edit, heartbeat, or remove."))
        self.my_list = QListWidget()
        self.my_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.my_list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.my_list.setIconSize(QSize(1, 1))
        rv.addWidget(self.my_list, 1)

        splitter.addWidget(left_card)
        splitter.addWidget(right_card)
        splitter.setSizes([600, 600])
        root.addWidget(splitter, 1)

        actions = QFrame(self)
        actions.setObjectName("actions")
        actions.setStyleSheet(f"QFrame#actions {{ background:{BG_ELEV_2}; border:1px solid {STROKE}; border-radius:16px; }}")
        av = QHBoxLayout(actions)
        av.setContentsMargins(12, 10, 12, 10)
        av.setSpacing(8)
        self.btn_launch = QPushButton("Join / Launch Selected")
        self.btn_start_server = QPushButton("Start Server")
        self.btn_hb_start = QPushButton("Start Heartbeat")
        self.btn_hb_stop = QPushButton("Stop Heartbeat")
        self.btn_edit = QPushButton("Edit")
        self.btn_delete = QPushButton("Remove")
        for b in (self.btn_launch, self.btn_start_server, self.btn_hb_start, self.btn_hb_stop, self.btn_edit, self.btn_delete):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setMinimumHeight(38)
            av.addWidget(b)
        root.addWidget(actions)

        self.lbl_steam_debug = None
        if SHOW_STEAM_DEBUG_IN_UI:
            self.lbl_steam_debug = QLabel()
            self.lbl_steam_debug.setWordWrap(True)
            self.lbl_steam_debug.setStyleSheet(f"color:{TEXT_MUTED}; background:transparent; font-size:11px;")
            root.addWidget(self.lbl_steam_debug)
            self._update_steam_debug_ui()

        self.btn_live.clicked.connect(self.live_matchmaking)
        self.btn_match.clicked.connect(self.matchmake)
        self.btn_add.clicked.connect(self.add_server)
        self.btn_launch.clicked.connect(self.launch_selected)
        self.btn_start_server.clicked.connect(self.start_server_only)
        self.btn_hb_start.clicked.connect(self.manual_start_heartbeat)
        self.btn_hb_stop.clicked.connect(self.manual_stop_heartbeat)
        self.btn_edit.clicked.connect(self.edit_or_remove)
        self.btn_delete.clicked.connect(self._remove_selected_server)

        self.btn_match.setEnabled(not self.local_mode)
        self.btn_live.setEnabled(not self.local_mode)
        self.btn_hb_start.setEnabled(not self.local_mode)
        self._wire_selection_helpers()

    # --- Behavior helpers ---
    def _click(self):
        if self.is_muted or not self.fx_ch or not self.click:
            return
        if self.bg_ch and self.bg_ch.get_busy():
            self.bg_ch.pause()
        self.fx_ch.play(self.click)
        t0 = time.time()
        while self.fx_ch.get_busy() and time.time() - t0 < 1.0:
            pygame.time.wait(10)
        if self.bg_ch:
            self.bg_ch.unpause()

    def toggle_mute(self):
        self.is_muted = not self.is_muted
        try:
            pygame.mixer.pause() if self.is_muted else pygame.mixer.unpause()
        except Exception:
            pass


    # ---------- Steam helpers ----------
    def _persist_settings(self) -> None:
        try:
            self.settings["local_mode"] = bool(getattr(self, "local_mode", False))
            self.settings["server_cmd_preset"] = str(getattr(self, "server_cmd_preset", "lobby") or "lobby")
            self.settings["steam_enabled"] = bool(getattr(self, "steam_enabled", False))
            self.settings["steam_launch"] = bool(getattr(self, "steam_launch", False))
            self.settings["steam_appid"] = str(getattr(self, "steam_appid", "") or "").strip()
            self.settings["steam_joins_only"] = bool(getattr(self, "steam_joins_only", True))
            self.settings["steam_rich_presence"] = bool(getattr(self, "steam_rich_presence", True))

            self.settings["discord_enabled"] = bool(getattr(self, "discord_enabled", True))
            self.settings["discord_rich_presence"] = bool(getattr(self, "discord_rich_presence", True))
            self.settings["discord_client_id"] = str(getattr(self, "discord_client_id", "") or "").strip()
            save_settings(self.settings)
        except Exception:
            pass


    # ---------- Server Command Preset ----------
    def _set_server_cmd_preset(self, preset: str) -> None:
        preset = str(preset or "").strip().lower()
        if preset not in SERVER_GAME_PRESET_KEYS:
            preset = "lobby"
        self.server_cmd_preset = preset
        self._persist_settings()

        # Keep menu checkmarks exclusive without needing QActionGroup.
        try:
            actions = getattr(self, "_server_cmd_actions", {}) or {}
            for key, action in actions.items():
                try:
                    action.setChecked(key == self.server_cmd_preset)
                except Exception:
                    pass
        except Exception:
            pass

        try:
            self._update_action_state()
        except Exception:
            pass

    def _server_command_argv(self, cfg: "Host") -> list:
        """Return argv (excluding exe) for dedicated server startup based on selected preset."""
        preset = str(getattr(self, "server_cmd_preset", "lobby") or "lobby").strip().lower()
        if preset == "lobby":
            # User-provided pregame lobby dedicated startup. Do not change this behavior.
            return [
                "server",
                "gearstart?game=GearGameContent.GearPreGameLobbyGameDedicated?MaxPlayers=10?bots=10",
                f"-port={int(cfg.port)}",
                "-log",
            ]

        # Direct match startup. The selected preset only changes the ?game= alias.
        game_code = server_preset_game_code(preset)
        return [
            "server",
            f"{cfg.map}.gear?game={game_code}?MaxPlayers=10?bots=6?",
            f"-port={int(cfg.port)}",
            "-useallavailablecores",
            "-log",
        ]

    def _recompute_client_id(self) -> None:
        try:
            steam_id = SteamManager.get_steamid64() if self.steam_enabled else None
            self.client_id = steam_id or get_client_id()
            self._client_id_source = "steam" if steam_id else "uuid"
        except Exception:
            self.client_id = get_client_id()
            self._client_id_source = "uuid"

    def _steam_presence(self, status: str = "", connect: str = "", extra: str = "") -> None:
        try:
            if not self.steam_enabled or not self.steam_rich_presence:
                return

            if not self._steam_api and self.steam_appid:
                self._steam_api = SteamManager(self.steam_appid)

            if not self._steam_api:
                return

            # Even if init failed, still reflect status in UI
            if not self._steam_api.is_ready():
                return

            if status:
                self._steam_api.set_presence("status", status)
            if connect:
                self._steam_api.set_presence("connect", connect)
            if extra:
                self._steam_api.set_presence("extra", extra)
        except Exception:
            pass
        finally:
            try:
                self._update_steam_debug_ui()
            except Exception:
                pass

    def _update_steam_debug_ui(self) -> None:
        if not hasattr(self, "lbl_steam_debug") or self.lbl_steam_debug is None:
            return

        def fmt(v: Optional[bool]) -> str:
            if v is None:
                return "?"
            return "True" if v else "False"

        if not bool(getattr(self, "steam_enabled", True)):
            self.lbl_steam_debug.setText("Steam: disabled")
            return

        api = getattr(self, "_steam_api", None)
        init_ok = getattr(api, "steamapi_init_ok", None) if api is not None else None
        friends_ok = getattr(api, "friends_iface_found", None) if api is not None else None
        rp_status = None
        try:
            if api is not None and hasattr(api, "last_rich_presence"):
                rp_status = api.last_rich_presence.get("status")
        except Exception:
            rp_status = None

        self.lbl_steam_debug.setText(
            f"SteamAPI_Init: {fmt(init_ok)} | Friends: {fmt(friends_ok)} | SetRichPresence(status): {fmt(rp_status)}"
        )

    def _toggle_steam_enabled(self, checked: bool = None) -> None:
        try:
            if checked is None:
                checked = bool(self._steam_enabled_action.isChecked())
        except Exception:
            checked = bool(checked)
        self.steam_enabled = bool(checked)
        self._recompute_client_id()
        self._persist_settings()
        QMessageBox.information(self, "Steam", f"Steam integration is now {'ON' if self.steam_enabled else 'OFF'}.\nClient ID source: {getattr(self, '_client_id_source', 'uuid')}")

    def _toggle_steam_launch(self, checked: bool = None) -> None:
        try:
            if checked is None:
                checked = bool(self._steam_launch_action.isChecked())
        except Exception:
            checked = bool(checked)
        self.steam_launch = bool(checked)
        self._persist_settings()

    def _toggle_steam_rich_presence(self, checked: bool = None) -> None:
        try:
            if checked is None:
                checked = bool(self._steam_rp_action.isChecked())
        except Exception:
            checked = bool(checked)
        self.steam_rich_presence = bool(checked)
        if not self.steam_rich_presence and self._steam_api:
            try:
                self._steam_api.clear_presence()
            except Exception:
                pass
        self._persist_settings()

    def _set_steam_appid(self):
        return

    def _steam_status_dialog(self) -> None:
        installed = SteamManager.is_steam_installed()
        running = SteamManager.is_steam_running()
        sid = SteamManager.get_steamid64()
        msg = "\\n".join([
            f"Steam installed: {'Yes' if installed else 'No'}",
            f"Steam running: {'Yes' if running else 'No'}",
            f"Configured AppID: {self.steam_appid or '(none)'}",
            f"Detected SteamID64: {sid or '(not detected)'}",
            f"EzBrowser client_id: {getattr(self, 'client_id', '')} ({getattr(self, '_client_id_source', 'uuid')})",
        ])
        QMessageBox.information(self, "Steam Status", msg)


    # ---------- Discord helpers ----------
    def _discord_presence(self, *, details: str = "", state: str = "", small: str = "") -> None:
        try:
            if not getattr(self, "discord_enabled", False) or not getattr(self, "discord_rich_presence", False):
                return
            if not getattr(self, "_discord_api", None) and getattr(self, "discord_client_id", ""):
                self._discord_api = DiscordManager(self.discord_client_id)
            if not self._discord_api or not self._discord_api.is_ready():
                return
            self._discord_api.set_activity(details=details, state=state, large_text="EzBrowser", small_text=small)
        except Exception:
            pass

    def _discord_clear(self) -> None:
        try:
            if getattr(self, "_discord_api", None):
                self._discord_api.clear()
        except Exception:
            pass

    def _toggle_discord_enabled(self, checked: bool = None) -> None:
        try:
            if checked is None:
                checked = bool(self._discord_enabled_action.isChecked())
        except Exception:
            checked = bool(checked)
        self.discord_enabled = bool(checked)
        if not self.discord_enabled:
            self._discord_clear()
        self._persist_settings()
        QMessageBox.information(self, "Discord", f"Discord integration is now {'ON' if self.discord_enabled else 'OFF'}.")

    def _toggle_discord_rich_presence(self, checked: bool = None) -> None:
        try:
            if checked is None:
                checked = bool(self._discord_rp_action.isChecked())
        except Exception:
            checked = bool(checked)
        self.discord_rich_presence = bool(checked)
        if not self.discord_rich_presence:
            self._discord_clear()
        self._persist_settings()

    def _set_discord_client_id(self):
        return

    def _discord_status_dialog(self) -> None:
        ready = bool(getattr(self, "_discord_api", None) and self._discord_api.is_ready())
        msg = "\n".join([
            f"Discord enabled: {'Yes' if getattr(self, 'discord_enabled', False) else 'No'}",
            f"Rich Presence: {'Yes' if getattr(self, 'discord_rich_presence', False) else 'No'}",
            f"Client ID set: {'Yes' if bool(getattr(self, 'discord_client_id', '').strip()) else 'No'}",
            f"Connected: {'Yes' if ready else 'No'}",
        ])
        QMessageBox.information(self, "Discord Status", msg)

    def _apply_hosting_presence(self, host: Optional[Host]) -> None:
        # Called when hosting state changes (start/stop heartbeat)
        try:
            self._current_host_for_presence = host
        except Exception:
            pass
        if not host:
            # Clear both presences
            try:
                if getattr(self, "_steam_api", None):
                    self._steam_api.clear_presence()
            except Exception:
                pass
            self._discord_clear()
            return

        # Steam: show "Hosting" to friends (no direct connect string to force joining through EzBrowser)
        try:
            self._steam_presence(status=f"Hosting Jacinto 2.0", connect="", extra=f"{host.map} — Join via EzBrowser")
        except Exception:
            pass

        # Discord: show hosting info
        try:
            endpoint = f"{host.public_ip}:{host.port}"
            self._discord_presence(details="Hosting Jacinto 2.0 (via EzBrowser)", state=f"{host.map} | {endpoint}", small="Hosting")
        except Exception:
            pass

    def _launch_join(self, exe: Optional[str], endpoint: str) -> bool:
        endpoint = str(endpoint).strip()
        if not endpoint:
            return False

        if self.steam_enabled and self.steam_launch and self.steam_appid:
            url = SteamManager.build_run_url(self.steam_appid, endpoint)
            if SteamManager.open_url(url):
                self._steam_presence(status="In Game", connect=endpoint, extra="Joined via EzBrowser")
                return True

        if exe:
            try:
                subprocess.Popen([exe, endpoint])
                return True
            except Exception:
                return False
        return False

    def _set_hb_color(self, color: str):
        self.hb_status.setStyleSheet(f"background-color: {color}; border-radius: 8px; border:1px solid {STROKE};")

    # Populate lists
    def _row_for_available(self, name: str, endpoint: str) -> QWidget:
        return RowCard(title=name, subtitle=endpoint, tag="JOIN", status_color="#7ed957")

    def _row_for_mine(self, name: str, endpoint: str, map_name: str) -> QWidget:
        return RowCard(title=name, subtitle=endpoint, tag=map_name.upper(), status_color="#ffaa00")

    def _upsert_available_host(self, host: Host) -> None:
        """Show a just-started heartbeat server immediately in Available Servers.

        The backend refresh still remains the source of truth. This is only instant
        local feedback so the host does not have to restart EzBrowser to see that
        their server is being advertised.
        """
        try:
            endpoint = f"{host.public_ip}:{int(host.port)}"
            for i in range(self.available_list.count()):
                item = self.available_list.item(i)
                if item and str(item.data(Qt.ItemDataRole.UserRole)) == endpoint:
                    self.available_list.setItemWidget(item, self._row_for_available(host.name, endpoint))
                    item.setHidden(False)
                    self.available_list.setCurrentRow(i)
                    self._apply_search_filter(self.search.text() if hasattr(self, "search") else "")
                    self._update_action_state()
                    return

            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, endpoint)
            item.setSizeHint(QSize(0, 56))
            self.available_list.addItem(item)
            self.available_list.setItemWidget(item, self._row_for_available(host.name, endpoint))
            self.available_list.setCurrentItem(item)
            self._apply_search_filter(self.search.text() if hasattr(self, "search") else "")
            self._update_action_state()
        except Exception:
            pass

    def _refresh_available(self):
        if getattr(self, 'local_mode', False) or bool(getattr(self, "_backend_hard_disabled", False)):
            try:
                self.available_list.clear()
            except Exception:
                pass
            return

        try:
            res = requests.get(f"{BACKEND_URL}/servers", timeout=3)
            if not (200 <= res.status_code < 300):
                raise RuntimeError(f"HTTP {res.status_code}")

            data = res.json()
            if not isinstance(data, list):
                data = []

            self._backend_failures = 0

            # Bridge: write current backend servers into Gears 3 System Link config so the in-game browser can see them.
            try:
                server_lines = _render_servers_to_custom_lines(data)
                # Prefer Jacinto path if configured; otherwise try V-Day.
                paths = GamePathManager.load()
                exe = paths.get(GamePathManager.JACINTO) or paths.get(GamePathManager.VDAY) or ""
                _write_systemlink_ini_files(exe, server_lines)
            except Exception:
                pass

            self.available_list.clear()
            for s in data:
                name = s.get('name', 'Server')
                endpoint = f"{s.get('public_ip','')}:{s.get('port','')}"
                item = QListWidgetItem()
                item.setData(Qt.ItemDataRole.UserRole, endpoint)
                item.setSizeHint(QSize(0, 56))
                self.available_list.addItem(item)
                self.available_list.setItemWidget(item, self._row_for_available(name, endpoint))
            self._apply_search_filter(self.search.text() if hasattr(self, "search") else "")
            self._update_action_state()
        except Exception:
            self._backend_failures = int(getattr(self, "_backend_failures", 0)) + 1
            if self._backend_failures >= 3:
                try:
                    self.backend_offline_signal.emit("servers")
                except Exception:
                    pass

    def _refresh_mine(self):
        self.my_list.clear()
        for h in load_hosts():
            name = h.name
            endpoint = f"{h.public_ip}:{h.port}"
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, endpoint)
            item.setSizeHint(QSize(0, 56))
            self.my_list.addItem(item)
            self.my_list.setItemWidget(item, self._row_for_mine(name, endpoint, h.map))
        self._apply_search_filter(self.search.text() if hasattr(self, "search") else "")
        self._update_action_state()

    # CRUD
    def add_server(self):
        dlg = ServerEditorDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        new_host = dlg.to_host()
        hosts = load_hosts()
        if any(h.public_ip == new_host.public_ip and h.port == new_host.port for h in hosts):
            QMessageBox.warning(self, "Duplicate", "A server with the same public IP and port already exists.")
            return
        hosts.append(new_host)
        save_hosts(hosts)
        self._refresh_mine()
        self._update_action_state()

    def edit_or_remove(self):
        idx = self.my_list.currentRow()
        if idx < 0:
            QMessageBox.information(self, "Select", "Select a server in 'My Servers' first.")
            return
        hosts = load_hosts()
        if idx >= len(hosts):
            return
        host = hosts[idx]
        dlg = ServerEditorDialog(self, host)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        edited = dlg.to_host()
        edited.id = host.id
        hosts[idx] = edited
        save_hosts(hosts)
        self._refresh_mine()
        self._update_action_state()

    # Launch / Join
    def _ensure_host_exe(self, host: Host) -> Optional[str]:
        if host.exe_path and os.path.exists(host.exe_path):
            return host.exe_path
        exe = choose_exe_path("Set game EXE for this server")
        if not exe:
            return None
        hosts = load_hosts()
        for i, h in enumerate(hosts):
            if h.id == host.id:
                h.exe_path = exe
                hosts[i] = h
                break
        save_hosts(hosts)
        return exe


    def start_server_only(self):
        """Start the selected 'My Server' as a dedicated server only (no auto-join)."""
        idx = self.my_list.currentRow()
        if idx < 0:
            QMessageBox.information(self, "Select", "Select a server in 'My Servers' first.")
            return

        hosts = load_hosts()
        cfg = hosts[idx]
        exe = self._ensure_host_exe(cfg)
        if not exe:
            QMessageBox.critical(self, "Error", "Executable is required for this server.")
            return

        pw, ok = QInputDialog.getText(self, "Host Password", "Enter host password to start the server:", echo=QLineEdit.EchoMode.Password)
        if not ok:
            return
        if (pw or "") != (cfg.password or ""):
            QMessageBox.warning(self, "Denied", "Incorrect host password. Server not started.")
            return

        try:
            subprocess.Popen([exe] + self._server_command_argv(cfg))
            QMessageBox.information(self, "Server Started", f"Started server on {cfg.public_ip}:{cfg.port}")
        except Exception as e:
            QMessageBox.critical(self, "Launch Error", str(e))

    def launch_selected(self):
        idx = self.my_list.currentRow()
        if idx >= 0:
            hosts = load_hosts()
            cfg = hosts[idx]
            exe = self._ensure_host_exe(cfg)
            if not exe:
                QMessageBox.critical(self, "Error", "Executable is required for this server.")
                return
            pw, ok = QInputDialog.getText(self, "Join Password", "Enter host password (blank guest):", echo=QLineEdit.EchoMode.Password)
            if ok and pw == cfg.password:
                try:
                    subprocess.Popen([exe] + self._server_command_argv(cfg))  # noqa: E501
                    time.sleep(10)
                    subprocess.Popen([exe, f"{cfg.local_ip}:{cfg.port}"])
                except Exception as e:
                    QMessageBox.critical(self, "Launch Error", str(e))
            else:
                try:
                    self._launch_join(exe, f"{cfg.public_ip}:{cfg.port}")
                except Exception as e:
                    QMessageBox.critical(self, "Launch Error", str(e))
            return

        aidx = self.available_list.currentRow()
        if aidx < 0:
            QMessageBox.information(self, "Select", "Select a server to launch/join.")
            return
        item = self.available_list.currentItem()
        endpoint = item.data(Qt.ItemDataRole.UserRole)
        if not endpoint:
            QMessageBox.critical(self, "Parse Error", "Could not determine selected server endpoint.")
            return
        exe = GamePathManager.ensure_path(GamePathManager.JACINTO, self)
        if not exe:
            return
        ok = self._launch_join(exe, endpoint)
        if not ok:
            QMessageBox.critical(self, "Launch Error", "Failed to launch via Steam or the selected EXE.")

    # Legacy simple matchmake (list-driven)
    def matchmake(self):
        if getattr(self, 'local_mode', False):
            QMessageBox.information(self, "Local Mode", "Backend/matchmaker is disabled in Local Mode.")
            return
        dlg = MatchmakeDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        game_key = dlg.selected_key()
        exe = GamePathManager.ensure_path(game_key, self)
        if not exe:
            QMessageBox.information(self, "Matchmake", "No executable selected. Cancelled.")
            return
        try:
            res = requests.get(f"{BACKEND_URL}/servers", timeout=4)
            servers = res.json() if res.status_code == 200 else []
        except Exception as e:
            QMessageBox.critical(self, "Matchmake Error", f"Failed to fetch servers: {e}")
            return
        if not servers:
            QMessageBox.information(self, "Matchmake", "No open servers found.")
            return

        def infer_game(name: str) -> Optional[str]:
            low = name.lower()
            if "jacinto" in low:
                return GamePathManager.JACINTO
            if "v-day" in low or "vday" in low or "v day" in low:
                return GamePathManager.VDAY
            return None

        preferred: List[dict] = [s for s in servers if infer_game(s.get("name", "")) == game_key]
        picked = (preferred[0] if preferred else servers[0])
        endpoint = f"{picked['public_ip']}:{picked['port']}"
        ok = self._launch_join(exe, endpoint)
        if not ok:
            QMessageBox.critical(self, "Matchmake Error", "Failed to launch via Steam or the selected EXE.")
            return
        QMessageBox.information(self, "Matchmake", f"Joining {picked.get('name','server')} @ {endpoint}")

    # NEW: Live Matchmaking (matchmaker API)
    def live_matchmaking(self):
        if getattr(self, 'local_mode', False):
            QMessageBox.information(self, "Local Mode", "Backend/matchmaker is disabled in Local Mode.")
            return
        # pick game
        dlg_pick = MatchmakeDialog(self)
        if dlg_pick.exec() != QDialog.DialogCode.Accepted:
            return
        game_key = dlg_pick.selected_key()

        # ensure game path first (so we don't prompt after match)
        exe = GamePathManager.ensure_path(game_key, self)
        if not exe:
            QMessageBox.information(self, "Live Matchmaking", "No executable selected. Cancelled.")
            return

        ui = MatchSearchDialog(self)
        ui.log_line("Queueing with matchmaker…")

        try:
            r = requests.post(f"{MATCH_URL}/queue/enqueue",
                              json={"client_id": self.client_id, "prefs": {"game": game_key}},
                              timeout=5)
            if r.status_code >= 300:
                raise RuntimeError(r.text)
            ui.log_line("Enqueued. Searching for peers…")
        except Exception as e:
            QMessageBox.critical(self, "Live Matchmaking", f"Queue failed: {e}")
            return

        ui.show()
        QApplication.processEvents()

        started = time.time()
        dots = 0

        while ui.isVisible() and not ui.cancelled:
            QApplication.processEvents()
            time.sleep(0.25)

            dots = (dots + 1) % 4
            ui.title.setText("Searching for a match" + ("." * dots))

            try:
                s = requests.get(f"{MATCH_URL}/queue/poll",
                                 params={"client_id": self.client_id},
                                 timeout=10).json()
            except Exception:
                ui.log_line("…poll timeout; still searching")
                continue

            st = s.get("status")

            if st in (None, "searching"):
                qsz = s.get("queue_size")
                if qsz is not None:
                    ui.log_line(f"Searching… players in queue: {qsz}")
                if time.time() - started > 120:
                    ui.log_line("Still searching after 2 minutes. You can keep waiting or cancel.")
                continue

            if st == "awaiting_accept":
                t = s.get("ticket", {})
                endpoint = t.get("endpoint", "unknown endpoint")
                ui.log_line(f"Match found at {endpoint}. Waiting for your response.")
                ui.set_awaiting_accept(endpoint)

                choice = ui.exec()
                ok = (choice == QDialog.DialogCode.Accepted)

                try:
                    requests.post(f"{MATCH_URL}/match/accept",
                                  json={"client_id": self.client_id,
                                        "ticket_id": t.get("ticket_id"),
                                        "accept": ok},
                                  timeout=5)
                except Exception:
                    pass

                if not ok:
                    ui.log_line("Declined. Resuming search…")
                    ui._awaiting_accept = False
                    ui.btns.clear()
                    ui.btn_cancel = ui.btns.addButton("Cancel search", QDialogButtonBox.ButtonRole.RejectRole)
                    ui.btn_cancel.clicked.connect(ui._on_cancel)
                    ui.title.setText("Searching for a match…")
                    ui.sub.setText("You’ll see players as they connect. This may take a moment.")
                    continue

                ui.log_line("Accepted. Preparing lobby…")
                continue

            if st == "ready":
                t = s.get("ticket", {})
                endpoint = s.get("endpoint")
                if endpoint:
                    ui.log_line(f"Launching game at {endpoint}…")
                    ok = self._launch_join(exe, endpoint)
                    if not ok:
                        QMessageBox.critical(self, "Launch Error", "Failed to launch via Steam or the selected EXE.")
                ui.close()
                return

            if st == "error":
                ui.log_line("Error from matchmaker: " + s.get("message", "Unknown error"))
                QMessageBox.critical(self, "Live Matchmaking", s.get("message", "Error"))
                ui.close()
                return

        if getattr(ui, "_cancelled", False):
            try:
                requests.post(f"{MATCH_URL}/queue/cancel",
                              json={"client_id": self.client_id},
                              timeout=4)
            except Exception:
                pass

    # Heartbeat (manual)
    def manual_start_heartbeat(self):
        if getattr(self, 'local_mode', False):
            QMessageBox.information(self, "Local Mode", "Heartbeat backend is disabled in Local Mode.")
            return
        idx = self.my_list.currentRow()
        if idx < 0:
            QMessageBox.information(self, "Select", "Select a server in 'My Servers' to heartbeat.")
            return
        hosts = load_hosts()
        host = hosts[idx]
        if host.id in self.hb_threads and not self.hb_threads[host.id].is_set():
            QMessageBox.information(self, "Already Running", "Heartbeat already running for this server.")
            return
        stop_event = threading.Event()
        self.hb_threads[host.id] = stop_event
        self.hb_color_signal.emit("#ffaa00")
        self._apply_hosting_presence(host)
        self._upsert_available_host(host)
        t = threading.Thread(target=self._hb_loop, args=(host, stop_event), daemon=True)
        t.start()
        try:
            QTimer.singleShot(750, self._refresh_available)
        except Exception:
            pass
        self._update_action_state()

        # Discord announce (UP) immediately when starting heartbeat
        try:
            if getattr(self, '_discord_watcher', None):
                self._discord_watcher.announce_up(host)
        except Exception:
            pass

    def manual_stop_heartbeat(self):
        any_running = False
        for ev in list(self.hb_threads.values()):
            if not ev.is_set():
                ev.set(); any_running = True

        # Discord announce (DOWN) for any servers we were heartbeating
        try:
            if getattr(self, '_discord_watcher', None) and self.hb_threads:
                hosts = load_hosts()
                by_id = {h.id: h for h in hosts}
                for hid in list(self.hb_threads.keys()):
                    h = by_id.get(hid)
                    if h:
                        self._discord_watcher.announce_down(h)
        except Exception:
            pass
        self.hb_threads.clear()
        try:
            self._apply_hosting_presence(None)
        except Exception:
            pass
        if any_running:
            self.hb_color_signal.emit("red")
            try:
                QTimer.singleShot(500, self._refresh_available)
            except Exception:
                pass
            QMessageBox.information(self, "Stopped", "All heartbeats stopped.")
        else:
            QMessageBox.information(self, "No Active Heartbeat", "No heartbeat is currently running.")

    def _hb_loop(self, host: Host, stop_event: threading.Event):
        failures = 0
        while not stop_event.is_set():
            try:
                payload = {"name": host.name, "public_ip": host.public_ip, "port": int(host.port), "map": host.map}
                r = requests.post(f"{BACKEND_URL}/add_server", json=payload, timeout=3)
                if 200 <= r.status_code < 300:
                    failures = 0
                    self.hb_color_signal.emit("lime")
                    try:
                        self.available_refresh_signal.emit()
                    except Exception:
                        pass
                else:
                    failures += 1
            except Exception:
                failures += 1

            if failures >= 2:
                self.hb_color_signal.emit("red")

            if failures >= 3:
                try:
                    self.backend_offline_signal.emit("heartbeat")
                except Exception:
                    pass
                stop_event.set()
                break

            stop_event.wait(HEARTBEAT_INTERVAL)

    # Matchmaker presence
    def _presence_loop(self, stop_event: threading.Event):
        payload = {"client_id": self.client_id}
        failures = 0
        while not stop_event.is_set():
            try:
                r = requests.post(f"{MATCH_URL}/presence/heartbeat", json=payload, timeout=3)
                if 200 <= getattr(r, "status_code", 0) < 300:
                    failures = 0
                else:
                    failures += 1
            except Exception:
                failures += 1

            if failures >= 3:
                try:
                    self.backend_offline_signal.emit("matchmaker")
                except Exception:
                    pass
                stop_event.set()
                break

            stop_event.wait(5)

    def _set_hb_color(self, color: str):
        self.hb_status.setStyleSheet(f"background-color: {color}; border-radius: 8px; border:1px solid {STROKE};")

    def paintEvent(self, event):
        pass

    def closeEvent(self, event):
        for ev in self.hb_threads.values():
            ev.set()
        if hasattr(self, "presence_stop"):
            self.presence_stop.set()
        try:
            pygame.mixer.stop()
        except Exception:
            pass
        try:
            self._apply_hosting_presence(None)
        except Exception:
            pass
        try:
            if getattr(self, "_discord_api", None):
                self._discord_api.close()
        except Exception:
            pass
        try:
            if getattr(self, '_discord_watcher', None):
                self._discord_watcher.stop()
        except Exception:
            pass
        event.accept()

# ---------------- Embedded Discord Watcher (auto-runs) ----------------
# Merged from discord_server_watcher_linked.py so you don't run a separate bot.
# - Starts automatically when EzBrowser opens (if token is configured)
# - Announces when your heartbeat starts/stops
# - Still supports polling the backend /servers for UP/DOWN changes
# - Stops cleanly when EzBrowser exits

try:
    import discord
    from discord import app_commands
    from discord.ext import tasks
    import aiohttp
    import ssl
    import certifi
except Exception:
    discord = None
    aiohttp = None

def _watcher_cfg_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    path = os.path.join(base, "JacintoWatcher")
    os.makedirs(path, exist_ok=True)
    return path

_WATCHER_CFG_PATH = os.path.join(_watcher_cfg_dir(), "config.json")
_WATCHER_STATE_FILE = os.path.join(_watcher_cfg_dir(), "watcher_state.json")

def _watcher_load_cfg() -> dict:
    if os.path.exists(_WATCHER_CFG_PATH):
        try:
            return json.load(open(_WATCHER_CFG_PATH, "r", encoding="utf-8"))
        except Exception:
            pass
    return {
        "token": "",
        "channel_id": 0,
        "backend_url": BACKEND_URL,   # reuse EzBrowser backend url
        "poll_seconds": 15,
        "watch_names": [],
        "push_secret": "",
        # embedded: disable push listener by default (set to a port >0 if you want it)
        "push_port": 0,
    }

def _watcher_save_cfg(cfg: dict) -> None:
    try:
        json.dump(cfg, open(_WATCHER_CFG_PATH, "w", encoding="utf-8"), indent=2)
    except Exception:
        pass

def _watcher_load_state() -> set:
    if not os.path.exists(_WATCHER_STATE_FILE):
        return set()
    try:
        return {tuple(x) for x in json.load(open(_WATCHER_STATE_FILE, "r", encoding="utf-8"))}
    except Exception:
        return set()

def _watcher_save_state(keys: set) -> None:
    try:
        json.dump([list(k) for k in sorted(keys)], open(_WATCHER_STATE_FILE, "w", encoding="utf-8"), indent=2)
    except Exception:
        pass

def _watcher_key(s: dict):
    return (s.get("name", ""), (s.get("public_ip") or s.get("ip") or ""), int(s.get("port", 0)))

async def _watcher_fetch_servers(session, backend_url: str):
    async with session.get(f"{backend_url}/servers", timeout=5) as resp:
        resp.raise_for_status()
        return await resp.json()

class _WatcherBot(discord.Client):
    def __init__(self, *, intents, cfg: dict):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.cfg = cfg
        self._channel = None
        self._last_seen = _watcher_load_state()
        self._session = None
        self.poll_seconds = int(cfg.get("poll_seconds", 15))
        self.watch_names = set(cfg.get("watch_names", []))

    async def setup_hook(self) -> None:
        # Basic commands (same as standalone)
        @self.tree.command(name="usehere", description="Post UP/DOWN messages in this channel")
        async def usehere_cmd(interaction: discord.Interaction):
            ch = interaction.channel
            if not isinstance(ch, discord.TextChannel):
                await interaction.response.send_message("This isn't a text channel.", ephemeral=True)
                return
            self._channel = ch
            self.cfg["channel_id"] = ch.id
            _watcher_save_cfg(self.cfg)
            await interaction.response.send_message(f"Okay! I’ll post here: #{ch.name}", ephemeral=True)

        @self.tree.command(name="status", description="Show current Jacinto servers")
        async def status_cmd(interaction: discord.Interaction):
            await interaction.response.defer(thinking=True, ephemeral=True)
            try:
                servers = await _watcher_fetch_servers(self._session, self.cfg["backend_url"])
                if self.watch_names:
                    servers = [s for s in servers if s.get("name") in self.watch_names]
                msg = self._format_servers(servers) or "No servers found."
                await interaction.followup.send(msg, ephemeral=True)
            except Exception as e:
                await interaction.followup.send(f"Error: {e}", ephemeral=True)

        @self.tree.command(name="watch", description="Manage name filters (add/remove/list)")
        @app_commands.describe(action="add/remove/list", name="Server name (optional for list)")
        async def watch_cmd(interaction: discord.Interaction, action: str, name: str = ""):
            action = (action or "").lower().strip()
            if action == "list":
                msg = ", ".join(sorted(self.watch_names)) or "<none>"
                await interaction.response.send_message(f"Filters: {msg}", ephemeral=True)
                return
            name = (name or "").strip()
            if not name:
                await interaction.response.send_message("Provide a server name.", ephemeral=True)
                return
            if action == "add":
                self.watch_names.add(name)
                self.cfg["watch_names"] = sorted(self.watch_names)
                _watcher_save_cfg(self.cfg)
                await interaction.response.send_message(f"Added filter: {name}", ephemeral=True)
            elif action == "remove":
                self.watch_names.discard(name)
                self.cfg["watch_names"] = sorted(self.watch_names)
                _watcher_save_cfg(self.cfg)
                await interaction.response.send_message(f"Removed filter: {name}", ephemeral=True)
            else:
                await interaction.response.send_message("Use add/remove/list", ephemeral=True)

        await self.tree.sync()

        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10),
            connector=aiohttp.TCPConnector(ssl=ssl_ctx),
        )

        # polling loop
        self.poller.change_interval(seconds=self.poll_seconds)
        self.poller.start()

    async def on_ready(self):
        try:
            ch_id = int(self.cfg.get("channel_id", 0) or 0)
        except Exception:
            ch_id = 0
        if ch_id:
            ch = self.get_channel(ch_id)
            if isinstance(ch, discord.TextChannel):
                self._channel = ch

    async def send_up(self, host_name: str, public_ip: str, port: int, map_name: str = ""):
        if self._channel:
            await self._channel.send(f"🟢 **UP**: `{host_name}` at `{public_ip}:{port}` is now available. (map: {map_name or '?'})")

    async def send_down(self, host_name: str, public_ip: str, port: int):
        if self._channel:
            await self._channel.send(f"🔴 **DOWN**: `{host_name}` at `{public_ip}:{port}` is no longer available.")

    @tasks.loop(seconds=15)
    async def poller(self):
        if not self._session:
            return
        try:
            servers = await _watcher_fetch_servers(self._session, self.cfg["backend_url"])
        except Exception:
            return
        if self.watch_names:
            servers = [s for s in servers if s.get("name") in self.watch_names]

        current = {_watcher_key(s) for s in servers}
        ups = current - self._last_seen
        downs = self._last_seen - current

        if ups or downs:
            _watcher_save_state(current)
            self._last_seen = current

        if self._channel:
            for name, ip, port in sorted(ups):
                await self._channel.send(f"🟢 **UP**: `{name}` at `{ip}:{port}` is now available.")
            for name, ip, port in sorted(downs):
                await self._channel.send(f"🔴 **DOWN**: `{name}` at `{ip}:{port}` is no longer available.")

    @poller.before_loop
    async def before_poller(self):
        await self.wait_until_ready()

    def _format_servers(self, servers):
        if not servers:
            return ""
        lines = ["**Current Servers**"]
        for s in servers:
            lines.append(f"• {s.get('name','?')} — `{s.get('public_ip','?')}:{s.get('port','?')}` (map: {s.get('map','?')})")
        return "\n".join(lines)

    async def close(self):
        try:
            if self._session:
                await self._session.close()
        finally:
            await super().close()

class EmbeddedDiscordWatcher:
    def __init__(self, parent_widget=None):
        self.parent_widget = parent_widget
        self.cfg = None
        self.bot = None
        self.thread = None
        self.loop = None
        self._ready = threading.Event()

    def start(self):
        if discord is None or aiohttp is None:
            return

        self.cfg = _watcher_load_cfg()

        # Ask for token once (GUI prompt), then persist.
        token = (self.cfg.get("token") or "").strip()
        if not token:
            try:
                token, ok = QInputDialog.getText(self.parent_widget, "Discord Bot Token",
                                                "Enter your Discord bot token (saved for next runs):",
                                                echo=QLineEdit.EchoMode.Password)
            except Exception:
                token, ok = ("", False)
            token = (token or "").strip()
            if not ok or not token:
                return
            self.cfg["token"] = token
            _watcher_save_cfg(self.cfg)

        # Ensure backend_url tracks EzBrowser
        self.cfg["backend_url"] = BACKEND_URL
        _watcher_save_cfg(self.cfg)

        intents = discord.Intents.none()
        self.bot = _WatcherBot(intents=intents, cfg=self.cfg)

        def _runner():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)

            async def _start():
                await self.bot.start(token)

            try:
                self.loop.create_task(_start())
                self._ready.set()
                self.loop.run_forever()
            finally:
                try:
                    if not self.loop.is_closed():
                        self.loop.run_until_complete(self.bot.close())
                except Exception:
                    pass
                try:
                    self.loop.close()
                except Exception:
                    pass

        self.thread = threading.Thread(target=_runner, daemon=True)
        self.thread.start()

    def stop(self):
        try:
            if self.loop and self.bot:
                fut = asyncio.run_coroutine_threadsafe(self.bot.close(), self.loop)
                try:
                    fut.result(timeout=5)
                except Exception:
                    pass
                self.loop.call_soon_threadsafe(self.loop.stop)
        except Exception:
            pass

    def announce_up(self, host):
        if not (self.loop and self.bot):
            return
        try:
            name = getattr(host, "name", "Server")
            ip = getattr(host, "public_ip", "") or getattr(host, "ip", "")
            port = int(getattr(host, "port", 0) or 0)
            mapn = getattr(host, "map", "") or ""
            asyncio.run_coroutine_threadsafe(self.bot.send_up(name, ip, port, mapn), self.loop)
        except Exception:
            pass

    def announce_down(self, host):
        if not (self.loop and self.bot):
            return
        try:
            name = getattr(host, "name", "Server")
            ip = getattr(host, "public_ip", "") or getattr(host, "ip", "")
            port = int(getattr(host, "port", 0) or 0)
            asyncio.run_coroutine_threadsafe(self.bot.send_down(name, ip, port), self.loop)
        except Exception:
            pass



# ---------- Main ----------
if __name__ == "__main__":
    app = QApplication(sys.argv)

    def make_xboxlike_splash_with_png() -> QPixmap:
        screen = app.primaryScreen(); sw = screen.size().width() if screen else 1280
        w = int(min(1200, sw * 0.75)); h = int(w * 0.62)
        pm = QPixmap(w, h); pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        grad = QLinearGradient(0, 0, 0, h)
        grad.setColorAt(0.0, QColor(10, 58, 64))
        grad.setColorAt(0.35, QColor(14, 32, 40))
        grad.setColorAt(1.0, QColor(10, 10, 12))
        p.fillRect(0, 0, w, h, grad)
        p.fillRect(0, 0, w, h, QColor(0, 0, 0, 35))
        logo_path = resource_path(SPLASH_PNG)
        if os.path.exists(logo_path):
            logo = QPixmap(logo_path)
            target_h = int(h * 0.26)
            logo_scaled = logo.scaledToHeight(target_h, Qt.TransformationMode.SmoothTransformation)
            lx = (w - logo_scaled.width()) // 2
            ly = (h - logo_scaled.height()) // 2
            p.drawPixmap(lx, ly, logo_scaled)
        else:
            size = 100
            p.setBrush(QColor(255, 255, 255)); p.setPen(Qt.NoPen)
            p.drawEllipse((w - size) // 2, (h - size) // 2, size, size)
        p.setPen(QColor(230, 230, 230, 220)); f = QFont(); f.setPointSize(10); p.setFont(f)
        caption = "Launching EzBrowser"
        metrics = p.fontMetrics()
        try:
            tw = metrics.horizontalAdvance(caption)
        except Exception:
            tw = metrics.width(caption)
        p.drawText((w - tw)//2, h - 34, caption); p.end(); return pm

    splash_pm = make_xboxlike_splash_with_png()
    splash = QSplashScreen(splash_pm)
    splash.setWindowFlags(Qt.WindowType.SplashScreen | Qt.WindowType.FramelessWindowHint)
    splash.show()
    anim = QPropertyAnimation(splash, b"windowOpacity"); anim.setDuration(900); anim.setStartValue(0.2); anim.setEndValue(1.0); anim.start()
    loop = QEventLoop(); QTimer.singleShot(1400, loop.quit); loop.exec()

    win = JacintoLobbyBrowser(); win.show()

    if pygame.mixer.get_init():
        bg = load_sound(OMEN_WAV)
        if bg:
            try:
                ch = pygame.mixer.Channel(1); ch.play(bg, loops=-1)
            except Exception:
                pass

    splash.finish(win)
    sys.exit(app.exec())
import json
