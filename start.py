"""Start the FTRE gateway, desktop app, and documentation site on Windows."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DESKTOP_ROOT = Path(os.environ.get("FTRE_DESKTOP_ROOT", str(ROOT.parent / "ftre-desktop")))
DOCS_ROOT = Path(os.environ.get("FTRE_DOCS_ROOT", str(ROOT.parent / "ftre-docs")))

# 端口的单一事实源是 ~/.ftre/config.json 的 servers 段；读不到时回退到下面的默认值。
CONFIG_PATH = Path(os.environ.get("USERPROFILE", Path.home())) / ".ftre" / "config.json"
_PORT_FALLBACK = {"gateway": 48650, "frontend": 48651, "docs": 48652}


def _resolve_port(name: str) -> int:
    try:
        servers = json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("servers", {})
        port = servers.get(name, {}).get("port")
        if isinstance(port, int):
            return port
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return _PORT_FALLBACK[name]


GATEWAY_PORT = _resolve_port("gateway")
DESKTOP_PORT = _resolve_port("frontend")
DOCS_PORT = _resolve_port("docs")

STARTUP_TIMEOUT_SECONDS = 60


def port_is_listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def wait_for_port(port: int, process: subprocess.Popen, label: str) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if port_is_listening(port):
            print(f"[ftre] {label} ready on port {port}")
            return
        exit_code = process.poll()
        if exit_code is not None:
            raise RuntimeError(f"{label} exited during startup (code {exit_code})")
        time.sleep(0.5)
    raise TimeoutError(
        f"{label} did not listen on port {port} within {STARTUP_TIMEOUT_SECONDS}s"
    )


def resolve_pnpm() -> str:
    executable = shutil.which("pnpm") or shutil.which("pnpm.cmd")
    if executable:
        return executable

    candidates = [
        Path(os.environ.get("APPDATA", "")) / "npm" / "pnpm.cmd",
        Path(os.environ.get("LOCALAPPDATA", "")) / "pnpm" / "pnpm.cmd",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise FileNotFoundError("pnpm was not found in PATH, APPDATA, or LOCALAPPDATA")


def start_gateway() -> subprocess.Popen | None:
    if port_is_listening(GATEWAY_PORT):
        print(f"[ftre] Gateway already listening on port {GATEWAY_PORT}; reusing it")
        return None

    env = os.environ.copy()
    source_root = str(ROOT / "src")
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (source_root, env.get("PYTHONPATH", "")) if part
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "ftre.main", "gateway"],
        cwd=ROOT,
        env=env,
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )
    wait_for_port(GATEWAY_PORT, process, "Gateway")
    return process


def start_pnpm_service(
    label: str,
    root: Path,
    port: int,
    pnpm: str,
) -> subprocess.Popen | None:
    if port_is_listening(port):
        print(f"[ftre] {label} already listening on port {port}; reusing it")
        return None
    if not root.is_dir():
        raise FileNotFoundError(f"{label} directory does not exist: {root}")

    env = os.environ.copy()
    pnpm_directory = str(Path(pnpm).parent)
    env["PATH"] = os.pathsep.join(
        part for part in (pnpm_directory, env.get("PATH", "")) if part
    )
    command = subprocess.list2cmdline([pnpm, "dev"])
    process = subprocess.Popen(
        ["cmd.exe", "/d", "/c", command],
        cwd=root,
        env=env,
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )
    wait_for_port(port, process, label)
    return process


def stop_process_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    subprocess.run(
        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def main() -> int:
    if os.name != "nt":
        print("[ftre] start.py currently supports Windows only", file=sys.stderr)
        return 1

    started: list[subprocess.Popen] = []
    try:
        gateway = start_gateway()
        if gateway:
            started.append(gateway)

        pnpm = resolve_pnpm()
        desktop = start_pnpm_service("Desktop", DESKTOP_ROOT, DESKTOP_PORT, pnpm)
        if desktop:
            started.append(desktop)
        docs = start_pnpm_service("Docs", DOCS_ROOT, DOCS_PORT, pnpm)
        if docs:
            started.append(docs)

        print("[ftre] All services are ready")
        print(f"  Gateway: ws://127.0.0.1:{GATEWAY_PORT}/")
        print(f"  Desktop dev server: http://127.0.0.1:{DESKTOP_PORT}/")
        print(f"  Docs: http://127.0.0.1:{DOCS_PORT}/")
        input("Press Enter to stop services started by this command...\n")
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"[ftre] ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        for process in reversed(started):
            stop_process_tree(process)


if __name__ == "__main__":
    raise SystemExit(main())
