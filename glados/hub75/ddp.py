"""
DDP (Distributed Display Protocol) packet builder and UDP sender.

Builds 10-byte DDP headers and sends raw RGB pixel data over UDP to
a WLED device.  Frames are split into multiple MTU-safe packets
(≤1440 bytes of pixel data each) so ESP32 devices can receive them
without UDP reassembly issues.

No external dependencies.  Pure stdlib (socket, struct).
"""

from __future__ import annotations

import socket
import struct
import time

from loguru import logger

# DDP header constants
_DDP_VER1 = 0x40       # Version 1
_DDP_PUSH = 0x01       # Push flag — render after this packet
_DDP_TYPE = 0x00       # Data type: raw RGB
_DDP_ID = 0x01         # Default WLED device ID

# Max pixel data per DDP packet.  480 pixels × 3 bytes = 1440 bytes,
# which with the 10-byte header = 1450 bytes — safely under the
# 1500-byte Ethernet MTU.  ESP32 lwIP drops oversized reassembled
# UDP datagrams, so every packet must fit in a single frame.
_MAX_CHUNK_BYTES = 480 * 3  # 1440

# Default inter-packet delay (seconds).  USB-powered ESP32-S3 can get
# overwhelmed when 9 packets arrive in rapid succession.  1.5ms between
# chunks gives the lwIP stack time to process each one.
# Override via DdpSender constructor or hub75.yaml ddp_inter_packet_delay_ms.
_DEFAULT_INTER_PACKET_DELAY = 0.0015  # 1.5 ms


def build_ddp_packet(
    rgb_data: bytes,
    offset: int = 0,
    push: bool = True,
    seq: int = 0,
) -> bytes:
    """Build a single DDP packet with header + pixel data.

    Args:
        rgb_data: Raw RGB bytes for this chunk.
        offset: Byte offset into the full frame (not pixel offset).
        push: If ``True``, set the PUSH flag (tells WLED to render).
        seq: Sequence number (0 = disable sequence checking).

    Returns:
        Complete DDP packet: 10-byte header + rgb_data.
    """
    flags = _DDP_VER1 | (_DDP_PUSH if push else 0)
    header = struct.pack(
        ">BBBBIH",
        flags,
        seq & 0xFF,
        _DDP_TYPE,
        _DDP_ID,
        offset,
        len(rgb_data),
    )
    return header + rgb_data


class DdpSender:
    """Fire-and-forget UDP sender for DDP frames.

    Automatically splits large frames into multiple MTU-safe packets
    so that ESP32 devices (especially on WLED-MM) can receive the
    full pixel payload without UDP fragmentation issues.
    """

    def __init__(
        self,
        ip: str,
        port: int = 4048,
        inter_packet_delay: float = _DEFAULT_INTER_PACKET_DELAY,
    ) -> None:
        self._ip = ip
        self._port = port
        self._inter_packet_delay = inter_packet_delay
        self._sock: socket.socket | None = None
        self._consecutive_errors = 0
        self._seq = 0

    def _ensure_socket(self) -> socket.socket:
        """Lazy-create or reconnect the UDP socket."""
        if self._sock is None:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        return self._sock

    def send_frame(self, rgb_bytes: bytes) -> None:
        """Send a complete frame, split into MTU-safe DDP packets.

        For a 64×64 panel (12,288 bytes), this sends 9 packets of
        ≤1450 bytes each.  Only the final packet carries the PUSH
        flag, telling WLED to render the assembled frame.

        Fire-and-forget: logs warnings on failure, never raises.
        After 10 consecutive failures, logs at ERROR and reconnects.
        """
        try:
            sock = self._ensure_socket()
            total = len(rgb_bytes)
            offset = 0

            # Use the same sequence number for all packets in one frame.
            # Seq 0 = "no checking" in DDP, so cycle 1–15.
            self._seq = (self._seq % 15) + 1
            frame_seq = self._seq

            while offset < total:
                chunk_end = min(offset + _MAX_CHUNK_BYTES, total)
                is_last = chunk_end >= total
                chunk = rgb_bytes[offset:chunk_end]

                packet = build_ddp_packet(
                    chunk,
                    offset=offset,
                    push=is_last,
                    seq=frame_seq,
                )
                sock.sendto(packet, (self._ip, self._port))
                offset = chunk_end

                # Pause between packets so the ESP32 can keep up.
                # Skip delay after the last packet (no point waiting).
                if not is_last and self._inter_packet_delay > 0:
                    time.sleep(self._inter_packet_delay)

            self._consecutive_errors = 0
        except OSError as exc:
            self._consecutive_errors += 1
            if self._consecutive_errors >= 10:
                logger.error(
                    "DDP: {} consecutive send failures to {}:{} — reconnecting socket: {}",
                    self._consecutive_errors, self._ip, self._port, exc,
                )
                self.close()  # Force socket recreation on next send
            else:
                logger.warning(
                    "DDP: send failed to {}:{}: {}",
                    self._ip, self._port, exc,
                )

    def close(self) -> None:
        """Close the UDP socket."""
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
