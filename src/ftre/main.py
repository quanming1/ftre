"""Thin CLI entry point.

All Gateway construction and lifecycle ownership lives in
``ftre.app.gateway.bootstrap``.  This module only formats logs, parses CLI
options and delegates to that Composition Root.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import ClassVar

import typer

from ftre.app.gateway.process import GatewayRuntime
from ftre.services.config.loader import load_gateway_address


class ColorFormatter(logging.Formatter):
    """Readable ANSI formatter for interactive Gateway logs."""

    RESET = "\033[0m"
    DIM = "\033[2m"
    SEP = "\033[90m"
    MESSAGE = "\033[97m"
    LEVEL_COLORS: ClassVar[dict[str, str]] = {
        "DEBUG": "\033[94m",
        "INFO": "\033[92m",
        "WARNING": "\033[93m",
        "ERROR": "\033[91m",
        "CRITICAL": "\033[95m",
    }
    NAMESPACE_COLORS: ClassVar[dict[str, str]] = {
        "ftre.services": "\033[38;5;214m",
        "ftre.features": "\033[38;5;208m",
        "ftre_agent_core": "\033[38;5;75m",
        "__main__": "\033[38;5;203m",
    }
    DEFAULT_NAME = "\033[96m"
    TRACEBACK = "\033[91m"

    def format(self, record: logging.LogRecord) -> str:
        message = f"{self.MESSAGE}{record.getMessage()}{self.RESET}"
        if record.exc_info:
            message += f"\n{self.TRACEBACK}{self.formatException(record.exc_info)}{self.RESET}"
        timestamp = self.formatTime(record, self.datefmt)
        level_color = self.LEVEL_COLORS.get(record.levelname, "")
        level = f"{level_color}{record.levelname:<8}{self.RESET}"
        name_color = self._name_color(record.name)
        name = f"{name_color}{record.name}{self.RESET}"
        sep = f"{self.SEP}-{self.RESET}"
        return f"{self.DIM}{timestamp}{self.RESET} {sep} {level} {sep} {name} {sep} {message}"

    def _name_color(self, name: str) -> str:
        for namespace, color in self.NAMESPACE_COLORS.items():
            if name == namespace or name.startswith(f"{namespace}."):
                return color
        return self.DEFAULT_NAME


def configure_logging() -> None:
    handler = logging.StreamHandler()
    if sys.stderr.isatty():
        handler.setFormatter(ColorFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)-8s - %(name)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logging.root.addHandler(handler)
    logging.root.setLevel(logging.INFO)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


configure_logging()

app = typer.Typer(name="ftre", no_args_is_help=True, help="ftre - AI 编程助手")
gateway_app = typer.Typer(invoke_without_command=True, no_args_is_help=False, help="启动和管理 ftre 网关服务。")
app.add_typer(gateway_app, name="gateway")


async def run_gateway(*, port: int | None = None, host: str | None = None) -> None:
    from ftre.app.gateway.bootstrap import run_gateway_runtime

    await run_gateway_runtime(port=port, host=host)


@gateway_app.callback(invoke_without_command=True)
def gateway(
    ctx: typer.Context,
    port: int | None = typer.Option(None, "--port", "-p", help="网关端口"),
    host: str | None = typer.Option(None, "--host", "-H", help="绑定地址"),
    background: bool = typer.Option(False, "--background", "-d", help="后台运行"),
    foreground: bool = typer.Option(False, "--foreground", help="前台运行"),
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    if background and foreground:
        raise typer.BadParameter("--foreground 和 --background 不能同时使用")
    if background:
        runtime = GatewayRuntime()
        config_host, config_port = load_gateway_address()
        ok, message, status = runtime.start(port=port or config_port, host=host or config_host or "127.0.0.1")
        if not ok:
            print(f"✗ Gateway not started: {message}")
            raise typer.Exit(1)
        print("✓ ftre gateway started in background")
        print(f"  PID: {status.pid}\n  Port: {status.port}\n  Logs: {status.log_path}")
        return
    asyncio.run(run_gateway(port=port, host=host))


@gateway_app.command("status")
def gateway_status() -> None:
    runtime = GatewayRuntime()
    status = runtime.status()
    print(f"Running: {'yes' if status.running else 'no'}")
    print(f"Reason: {status.reason}\nPID: {status.pid}\nPort: {status.port}")
    print(f"State: {status.state_path}\nLogs: {status.log_path}")


@gateway_app.command("stop")
def gateway_stop(timeout: int = typer.Option(20, "--timeout", help="停止超时（秒）")) -> None:
    ok, message, status = GatewayRuntime().stop(timeout_s=timeout)
    if ok:
        print("✓ Gateway stopped.")
    else:
        print(f"✗ Gateway not stopped: {message}")
        if status.pid is not None:
            print(f"  PID: {status.pid}")


@gateway_app.command("restart")
def gateway_restart(
    port: int | None = typer.Option(None, "--port", "-p"),
    host: str | None = typer.Option(None, "--host", "-H"),
    timeout: int = typer.Option(20, "--timeout"),
) -> None:
    ok, message, status = GatewayRuntime().restart(port=port, host=host, timeout_s=timeout)
    if not ok:
        print(f"✗ Gateway not restarted: {message}")
        raise typer.Exit(1)
    print(f"✓ Gateway restarted.\n  PID: {status.pid}\n  Port: {status.port}\n  Logs: {status.log_path}")


@gateway_app.command("logs")
def gateway_logs(
    tail: int = typer.Option(200, "--tail"),
    follow: bool = typer.Option(True, "--follow/--no-follow"),
) -> None:
    runtime = GatewayRuntime()
    if follow:
        raise typer.Exit(runtime.follow_logs(tail=tail))
    for line in runtime.read_log_tail(tail=tail):
        print(line)


if __name__ == "__main__":
    app()
