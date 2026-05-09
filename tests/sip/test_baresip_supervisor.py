"""Tests for glados.sip.baresip_supervisor.

We don't run the real baresip binary in unit tests — instead we use a
small Python "fake baresip" script that opens a TCP listener on the
configured ctrl_tcp port and behaves the way our supervisor expects.
That isolates the supervisor's logic (subprocess management, ready
detection, exit handling, restart) from the SIP stack itself.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import textwrap
import time

import pytest

from glados.sip.baresip_supervisor import BaresipExited, BaresipSupervisor
from glados.sip.config import SipConfig


# ---------------------------------------------------------------------------
# Fakes — write a small Python script we'll spawn instead of real baresip
# ---------------------------------------------------------------------------

_FAKE_BARESIP_TEMPLATE = textwrap.dedent("""\
    import socket
    import sys
    import time

    port = {port}
    duration = {duration}
    exit_code = {exit_code}

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port))
    s.listen(5)
    print(f"fake-baresip listening on 127.0.0.1:{{port}}", flush=True)

    end = time.time() + duration if duration > 0 else None
    while True:
        if end is not None and time.time() >= end:
            break
        s.settimeout(0.1)
        try:
            conn, _ = s.accept()
            conn.close()
        except socket.timeout:
            pass
    sys.exit(exit_code)
""")


def _write_fake(tmp_path: pathlib.Path, port: int, duration: float = 999.0,
                exit_code: int = 0) -> pathlib.Path:
    """Materialise a fake-baresip script at tmp_path/fake.py."""
    p = tmp_path / "fake_baresip.py"
    p.write_text(_FAKE_BARESIP_TEMPLATE.format(
        port=port, duration=duration, exit_code=exit_code,
    ))
    return p


def _minimal_cfg() -> SipConfig:
    return SipConfig(
        enabled=True,
        server={"host": "192.168.1.1", "username": "glados", "password": "x"},
    )


# Helper: pick an unused port
def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_waits_for_ctrl_tcp_then_succeeds(tmp_path: pathlib.Path) -> None:
    port = _free_port()
    fake = _write_fake(tmp_path, port=port, duration=5.0)
    sup = BaresipSupervisor(
        _minimal_cfg(),
        config_dir=tmp_path / "baresip",
        ctrl_tcp_port=port,
        baresip_path=sys.executable,
        ready_timeout=10.0,
    )
    # Override the spawn cmd by monkey-patching: we need
    # `baresip_path` to be `python`, and the args to be the fake script.
    # Simplest: subclass and override _spawn.
    real_spawn = sup._spawn

    async def _spawn_fake() -> None:
        sup._proc = await asyncio.create_subprocess_exec(
            sys.executable, str(fake),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        asyncio.create_task(sup._drain_stream(sup._proc.stdout, "stdout"))
        asyncio.create_task(sup._drain_stream(sup._proc.stderr, "stderr"))
        sup._monitor_task = asyncio.create_task(sup._monitor_exit())

    sup._spawn = _spawn_fake  # type: ignore[method-assign]
    try:
        await sup.start()
        assert sup.is_running
        assert sup.pid is not None
    finally:
        await sup.stop()


@pytest.mark.asyncio
async def test_ready_timeout_raises_if_port_never_opens(tmp_path: pathlib.Path) -> None:
    """If the fake never opens its TCP port, supervisor should give up."""
    # Use a "fake" that doesn't bind — just sleeps.
    silent = tmp_path / "silent.py"
    silent.write_text("import time; time.sleep(60)\n")

    sup = BaresipSupervisor(
        _minimal_cfg(),
        config_dir=tmp_path / "baresip",
        ctrl_tcp_port=_free_port(),
        baresip_path=sys.executable,
        ready_timeout=2.0,
    )

    async def _spawn_silent() -> None:
        sup._proc = await asyncio.create_subprocess_exec(
            sys.executable, str(silent),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        asyncio.create_task(sup._drain_stream(sup._proc.stdout, "stdout"))
        asyncio.create_task(sup._drain_stream(sup._proc.stderr, "stderr"))
        sup._monitor_task = asyncio.create_task(sup._monitor_exit())

    sup._spawn = _spawn_silent  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError, match="did not accept"):
            await sup.start()
    finally:
        await sup.stop()


@pytest.mark.asyncio
async def test_exit_during_startup_raises(tmp_path: pathlib.Path) -> None:
    """If the subprocess exits before opening the TCP port, raise."""
    crasher = tmp_path / "crasher.py"
    crasher.write_text("import sys; sys.exit(2)\n")

    sup = BaresipSupervisor(
        _minimal_cfg(),
        config_dir=tmp_path / "baresip",
        ctrl_tcp_port=_free_port(),
        baresip_path=sys.executable,
        ready_timeout=3.0,
        max_restart_attempts=0,  # Don't auto-restart for this test
    )

    async def _spawn_crasher() -> None:
        sup._proc = await asyncio.create_subprocess_exec(
            sys.executable, str(crasher),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        asyncio.create_task(sup._drain_stream(sup._proc.stdout, "stdout"))
        asyncio.create_task(sup._drain_stream(sup._proc.stderr, "stderr"))
        sup._monitor_task = asyncio.create_task(sup._monitor_exit())

    sup._spawn = _spawn_crasher  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError, match="exited during startup"):
            await sup.start()
    finally:
        await sup.stop()


@pytest.mark.asyncio
async def test_on_exit_callback_fires_on_clean_stop(tmp_path: pathlib.Path) -> None:
    port = _free_port()
    fake = _write_fake(tmp_path, port=port, duration=999.0)

    captured: list[BaresipExited] = []

    async def on_exit(event: BaresipExited) -> None:
        captured.append(event)

    sup = BaresipSupervisor(
        _minimal_cfg(),
        on_exit=on_exit,
        config_dir=tmp_path / "baresip",
        ctrl_tcp_port=port,
        baresip_path=sys.executable,
        ready_timeout=10.0,
    )

    async def _spawn_fake() -> None:
        sup._proc = await asyncio.create_subprocess_exec(
            sys.executable, str(fake),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        asyncio.create_task(sup._drain_stream(sup._proc.stdout, "stdout"))
        asyncio.create_task(sup._drain_stream(sup._proc.stderr, "stderr"))
        sup._monitor_task = asyncio.create_task(sup._monitor_exit())

    sup._spawn = _spawn_fake  # type: ignore[method-assign]
    await sup.start()
    await sup.stop()
    # Give the monitor task time to fire the callback.
    await asyncio.sleep(0.3)

    assert len(captured) == 1
    assert captured[0].expected is True
    # Return code semantics differ across platforms (POSIX returns negative
    # on signal, Windows can return 1 from terminated Python). The
    # important invariant is that the callback fired with expected=True.
    assert captured[0].return_code is not None


@pytest.mark.asyncio
async def test_stop_is_idempotent(tmp_path: pathlib.Path) -> None:
    port = _free_port()
    fake = _write_fake(tmp_path, port=port, duration=999.0)
    sup = BaresipSupervisor(
        _minimal_cfg(),
        config_dir=tmp_path / "baresip",
        ctrl_tcp_port=port,
        baresip_path=sys.executable,
    )

    async def _spawn_fake() -> None:
        sup._proc = await asyncio.create_subprocess_exec(
            sys.executable, str(fake),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        asyncio.create_task(sup._drain_stream(sup._proc.stdout, "stdout"))
        asyncio.create_task(sup._drain_stream(sup._proc.stderr, "stderr"))
        sup._monitor_task = asyncio.create_task(sup._monitor_exit())

    sup._spawn = _spawn_fake  # type: ignore[method-assign]
    await sup.start()
    await sup.stop()
    # Second stop should be a no-op
    await sup.stop()
    assert not sup.is_running
