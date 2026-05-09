"""Manage the baresip subprocess lifecycle.

Responsibilities:
- Generate baresip config files from ``SipConfig``
- Spawn ``baresip -f <configdir>`` as an asyncio subprocess
- Detect ready state (loopback ctrl_tcp port accepts a connection)
- Forward stdout/stderr to the existing logger
- Detect subprocess exit and emit a ``BaresipExited`` callback
- Auto-restart with exponential backoff when no call is active
- Graceful shutdown (SIGTERM, then SIGKILL after timeout)

Not responsible for:
- Sending commands or parsing events (that's ``ctrl_client.py``, Task 4)
- Audio bridging (that's ``audio_bridge.py``, Task 5)
- Call state (that's ``call_session.py``, Task 11)

The supervisor is the single owner of the baresip subprocess lifetime.
Other modules ask it whether baresip is up and subscribe to its
exit/restart events; they do not spawn or kill baresip themselves.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import shutil
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from loguru import logger

from glados.sip._baresip_config import write_baresip_files
from glados.sip.config import SipConfig


@dataclass
class BaresipExited:
    """Event payload when the baresip subprocess exits."""
    return_code: int
    expected: bool  # True iff we asked it to stop; False ⇒ crash/spontaneous exit


# Type alias for exit subscribers. Async to match asyncio idioms.
ExitCallback = Callable[[BaresipExited], Awaitable[None]]


class BaresipSupervisor:
    """Owns the baresip subprocess.

    Usage:
        sup = BaresipSupervisor(cfg, on_exit=my_callback)
        await sup.start()         # spawns baresip, waits for ready
        ...                       # ctrl_client + audio_bridge do their work
        await sup.stop()          # clean shutdown
    """

    def __init__(
        self,
        cfg: SipConfig,
        *,
        on_exit: ExitCallback | None = None,
        config_dir: pathlib.Path | str = "/tmp/baresip",
        ctrl_tcp_port: int = 4444,
        rx_fifo: str = "/tmp/sip-rx.fifo",
        tx_fifo: str = "/tmp/sip-tx.fifo",
        baresip_path: str | None = None,
        ready_timeout: float = 15.0,
        sigterm_grace: float = 5.0,
        max_restart_attempts: int = 5,
    ) -> None:
        self._cfg = cfg
        self._on_exit = on_exit
        self._config_dir = pathlib.Path(config_dir)
        self._ctrl_tcp_port = ctrl_tcp_port
        self._rx_fifo = rx_fifo
        self._tx_fifo = tx_fifo
        self._baresip_path = baresip_path or shutil.which("baresip") or "baresip"
        self._ready_timeout = ready_timeout
        self._sigterm_grace = sigterm_grace
        self._max_restart_attempts = max_restart_attempts

        self._proc: asyncio.subprocess.Process | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._stop_requested = False
        self._restart_attempts = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Generate configs, spawn baresip, wait for ctrl_tcp ready.

        Raises ``RuntimeError`` if baresip fails to start or the ctrl
        port doesn't accept within ``ready_timeout`` seconds.
        """
        self._stop_requested = False
        write_baresip_files(
            self._cfg,
            self._config_dir,
            ctrl_tcp_port=self._ctrl_tcp_port,
            rx_fifo=self._rx_fifo,
            tx_fifo=self._tx_fifo,
        )
        await self._spawn()
        await self._await_ready()
        logger.bind(group="sip").success(
            f"baresip ready (pid={self._proc.pid}, ctrl_tcp=127.0.0.1:{self._ctrl_tcp_port})"
        )

    async def stop(self) -> None:
        """Gracefully terminate baresip. SIGTERM, then SIGKILL after grace."""
        self._stop_requested = True
        proc = self._proc
        if proc is None:
            return
        if proc.returncode is not None:
            return  # Already exited

        logger.bind(group="sip").info("baresip: sending SIGTERM")
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=self._sigterm_grace)
        except asyncio.TimeoutError:
            logger.bind(group="sip").warning(
                f"baresip: SIGTERM grace ({self._sigterm_grace}s) exceeded; sending SIGKILL"
            )
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()

        if self._monitor_task is not None:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except (asyncio.CancelledError, Exception):
                pass

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    # ------------------------------------------------------------------
    # Internal — spawn + monitor
    # ------------------------------------------------------------------

    async def _spawn(self) -> None:
        """Start the baresip subprocess. Forwards stdout/stderr to loguru."""
        cmd = [self._baresip_path, "-f", str(self._config_dir)]
        logger.bind(group="sip").info(f"baresip: spawning {' '.join(cmd)}")
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # Start tasks to drain stdout/stderr into the logger.
        asyncio.create_task(self._drain_stream(self._proc.stdout, "stdout"))
        asyncio.create_task(self._drain_stream(self._proc.stderr, "stderr"))
        # Start the exit-monitor task.
        self._monitor_task = asyncio.create_task(self._monitor_exit())

    async def _drain_stream(self, stream: asyncio.StreamReader | None, label: str) -> None:
        """Forward each line from baresip's stream to loguru."""
        if stream is None:
            return
        log = logger.bind(group="sip")
        while True:
            line = await stream.readline()
            if not line:
                break
            log.debug(f"baresip[{label}]: {line.decode('utf-8', errors='replace').rstrip()}")

    async def _await_ready(self) -> None:
        """Poll the ctrl_tcp port until it accepts a connection."""
        deadline = asyncio.get_event_loop().time() + self._ready_timeout
        delay = 0.1
        while asyncio.get_event_loop().time() < deadline:
            if self._proc is None or self._proc.returncode is not None:
                raise RuntimeError(
                    f"baresip exited during startup with code {self._proc.returncode if self._proc else '?'}"
                )
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", self._ctrl_tcp_port),
                    timeout=1.0,
                )
            except (OSError, asyncio.TimeoutError):
                await asyncio.sleep(delay)
                delay = min(delay * 1.5, 1.0)
                continue
            else:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
                return
        raise RuntimeError(
            f"baresip ctrl_tcp port {self._ctrl_tcp_port} did not accept "
            f"a connection within {self._ready_timeout}s"
        )

    async def _monitor_exit(self) -> None:
        """Await subprocess exit; fire on_exit callback. Restart on crash."""
        if self._proc is None:
            return
        return_code = await self._proc.wait()
        expected = self._stop_requested
        log = logger.bind(group="sip")
        if expected:
            log.info(f"baresip exited cleanly (rc={return_code})")
        else:
            log.error(f"baresip exited unexpectedly (rc={return_code})")

        if self._on_exit is not None:
            try:
                await self._on_exit(BaresipExited(return_code=return_code, expected=expected))
            except Exception as e:
                log.exception(f"on_exit callback raised: {e}")

        # Auto-restart on unexpected exit (best-effort; if the host has
        # no baresip binary or the config is malformed, we don't want
        # an infinite restart storm).
        if not expected and self._restart_attempts < self._max_restart_attempts:
            self._restart_attempts += 1
            backoff = min(2.0 ** self._restart_attempts, 30.0)
            log.warning(
                f"baresip: auto-restart attempt {self._restart_attempts}/{self._max_restart_attempts} "
                f"in {backoff:.1f}s"
            )
            await asyncio.sleep(backoff)
            try:
                await self._spawn()
                await self._await_ready()
                self._restart_attempts = 0  # reset on successful restart
                log.success(f"baresip: auto-restart succeeded (pid={self._proc.pid})")
            except Exception as e:
                log.error(f"baresip: auto-restart failed: {e}")
        elif not expected:
            log.error(
                f"baresip: max restart attempts ({self._max_restart_attempts}) exhausted; giving up"
            )
