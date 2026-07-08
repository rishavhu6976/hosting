"""
docker_sandbox.py
──────────────────
Per-user Docker sandboxing for the Telegram script-hosting bot.

Model: one persistent container per user ("scripthost_<user_id>"), created on
first use and reused for every later run. The user's existing host folder
(get_user_folder(user_id)) is bind-mounted read-write at /workspace inside
the container — nothing outside that folder is reachable from inside the
container, and no other user's container can see it either.

Requirements this implements:
  - Persistent container per user (not one-shot per script run).
  - Outbound network allowed (bots need to call the Telegram API etc.).
  - Base image ships both Python 3 and Node.js so it's a drop-in replacement
    for the existing run_script / run_js_script paths.

Isolation applied to every container:
  - --cap-drop ALL --security-opt no-new-privileges
  - --pids-limit / --memory / --memory-swap / --cpus  (kernel-enforced
    cgroup limits, not just RLIMIT inside a shared host process tree)
  - --network bridge, no published ports (outbound only, nothing inbound)
  - Only that one user's folder is mounted — no other user's data and no
    host system paths are visible inside the container.
"""

import os
import subprocess
import logging
import threading

logger = logging.getLogger(__name__)

DOCKER_IMAGE          = os.environ.get("SCRIPTHOST_DOCKER_IMAGE", "nikolaik/python-nodejs:python3.11-nodejs20-slim")
CONTAINER_PREFIX      = "scripthost_"
CONTAINER_MEMORY      = os.environ.get("SCRIPTHOST_CONTAINER_MEMORY", "512m")
CONTAINER_CPUS        = os.environ.get("SCRIPTHOST_CONTAINER_CPUS", "1.0")
CONTAINER_PIDS_LIMIT  = os.environ.get("SCRIPTHOST_CONTAINER_PIDS", "400")
WORKSPACE_PATH        = "/workspace"

_lock = threading.Lock()          # serialize container create/start races
_checked_docker = False
_docker_ok = False


def container_name(user_id) -> str:
    return f"{CONTAINER_PREFIX}{user_id}"


def _run(cmd, timeout=30, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)


def docker_available() -> bool:
    """Cached check that the `docker` CLI works and the daemon is reachable."""
    global _checked_docker, _docker_ok
    if _checked_docker:
        return _docker_ok
    _checked_docker = True
    try:
        r = _run(["docker", "info"], timeout=10)
        _docker_ok = (r.returncode == 0)
        if not _docker_ok:
            logger.error(f"docker not usable: {r.stderr.strip()[:300]}")
    except FileNotFoundError:
        logger.error("docker CLI not found on PATH.")
        _docker_ok = False
    except Exception as e:
        logger.error(f"docker_available check failed: {e}")
        _docker_ok = False
    return _docker_ok


def ensure_image() -> bool:
    """Pull the base image once if it isn't present locally."""
    try:
        r = _run(["docker", "image", "inspect", DOCKER_IMAGE], timeout=10)
        if r.returncode == 0:
            return True
        logger.info(f"Pulling sandbox image {DOCKER_IMAGE} (first run only)...")
        r = _run(["docker", "pull", DOCKER_IMAGE], timeout=600)
        if r.returncode != 0:
            logger.error(f"docker pull failed: {r.stderr.strip()[:300]}")
            return False
        return True
    except Exception as e:
        logger.error(f"ensure_image error: {e}")
        return False


def _container_state(name: str):
    """Returns 'running' | 'exited' (or other status) | None (doesn't exist)."""
    r = _run(["docker", "inspect", "-f", "{{.State.Status}}", name], timeout=10)
    if r.returncode != 0:
        return None
    return r.stdout.strip()


def ensure_user_container(user_id, user_folder: str) -> bool:
    """
    Ensures a persistent, resource-limited container for this user exists
    and is running, bind-mounting user_folder -> /workspace. Idempotent —
    safe to call before every script run.
    """
    if not docker_available():
        return False
    if not ensure_image():
        return False

    name = container_name(user_id)
    with _lock:
        state = _container_state(name)
        if state == "running":
            return True
        if state is not None:
            r = _run(["docker", "start", name], timeout=20)
            if r.returncode == 0:
                return True
            logger.warning(f"docker start failed for {name}, recreating: {r.stderr.strip()[:200]}")
            _run(["docker", "rm", "-f", name], timeout=20)

        user_folder = os.path.abspath(user_folder)
        os.makedirs(user_folder, exist_ok=True)

        # Match the container's user to whatever uid:gid actually owns the
        # bind-mounted host folder (normally the OS user host.py runs as).
        # The base image's default user (uid 1000, non-root) otherwise can't
        # write into /workspace if that doesn't line up, e.g. "Permission
        # denied: '/workspace/.venv'".
        try:
            st = os.stat(user_folder)
            run_as = f"{st.st_uid}:{st.st_gid}"
        except Exception:
            run_as = f"{os.getuid()}:{os.getgid()}"

        cmd = [
            "docker", "run", "-d",
            "--name", name,
            "--restart", "unless-stopped",
            "--user", run_as,
            "-e", f"HOME={WORKSPACE_PATH}",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", CONTAINER_PIDS_LIMIT,
            "--memory", CONTAINER_MEMORY,
            "--memory-swap", CONTAINER_MEMORY,   # no swap growth beyond the mem cap
            "--cpus", CONTAINER_CPUS,
            "--network", "bridge",               # outbound only, nothing published
            "-v", f"{user_folder}:{WORKSPACE_PATH}:rw",
            "-w", WORKSPACE_PATH,
            "--label", "scripthost=1",
            "--label", f"scripthost_user={user_id}",
            DOCKER_IMAGE,
            "tail", "-f", "/dev/null",
        ]
        r = _run(cmd, timeout=60)
        if r.returncode != 0:
            logger.error(f"docker run failed for {name}: {r.stderr.strip()[:400]}")
            return False
        return True


def to_container_path(user_folder: str, host_path: str) -> str:
    """Translate an absolute host path under user_folder to its /workspace equivalent."""
    user_folder = os.path.abspath(user_folder)
    host_path = os.path.abspath(host_path)
    rel = os.path.relpath(host_path, user_folder)
    if rel == os.pardir or rel.startswith(f"{os.pardir}{os.sep}"):
        raise ValueError(f"path {host_path} is outside sandbox {user_folder}")
    return WORKSPACE_PATH if rel == "." else os.path.join(WORKSPACE_PATH, rel)


def exec_argv(user_id, cmd_list, tty=False, cwd=None):
    """
    Build the `docker exec` argv that runs cmd_list inside the user's
    container. Any host/script paths in cmd_list must already be translated
    to /workspace paths (via to_container_path) by the caller.

    PATH is set so the venv's bin dir comes first — scripts that
    self-install packages with a bare `pip install X` (rather than calling
    sys.executable -m pip) still land in the venv's site-packages instead of
    falling back to a `--user` install the venv interpreter never sees.
    """
    name = container_name(user_id)
    venv_bin = f"{WORKSPACE_PATH}/.venv/bin"
    container_path = f"{venv_bin}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    base = ["docker", "exec", "-i"]
    if tty:
        base.append("-t")
    base += [
        "-e", f"PATH={container_path}",
        "-e", f"VIRTUAL_ENV={WORKSPACE_PATH}/.venv",
        "-w", cwd or WORKSPACE_PATH, name,
    ]
    return base + cmd_list


def run_sync(user_id, user_folder, cmd_list, timeout=None, **kw):
    """Blocking helper mirroring subprocess.run, executed inside the container."""
    if not ensure_user_container(user_id, user_folder):
        raise RuntimeError("sandbox container unavailable")
    argv = exec_argv(user_id, cmd_list, tty=False)
    return subprocess.run(argv, timeout=timeout, **kw)


def stop_user_container(user_id):
    _run(["docker", "stop", "-t", "5", container_name(user_id)], timeout=15)


def remove_user_container(user_id):
    _run(["docker", "rm", "-f", container_name(user_id)], timeout=20)


def list_sandbox_containers():
    r = _run(["docker", "ps", "-a", "--filter", "label=scripthost=1",
              "--format", "{{.Names}}\t{{.Status}}"], timeout=15)
    if r.returncode != 0:
        return []
    return [line for line in r.stdout.splitlines() if line.strip()]
