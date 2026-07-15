"""Pure RTP packet construction for the Python backchannel sender."""

import os
import struct


def _uint(name, value, maximum):
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if not 0 <= value <= maximum:
        raise ValueError(f"{name} must be between 0 and {maximum}")
    return value


class RtpPacketizer:
    """Own RTP sender state and build fixed-header RTP v2 packets."""

    def __init__(self, payload_type, ssrc=None, sequence=None, timestamp=None):
        self._payload_type = _uint("payload_type", payload_type, 0x7F)
        if ssrc is None:
            ssrc = int.from_bytes(os.urandom(4), "big")
        if sequence is None:
            sequence = int.from_bytes(os.urandom(2), "big")
        if timestamp is None:
            timestamp = int.from_bytes(os.urandom(4), "big")
        self._ssrc = _uint("ssrc", ssrc, 0xFFFFFFFF)
        self._sequence = _uint("sequence", sequence, 0xFFFF)
        self._timestamp = _uint("timestamp", timestamp, 0xFFFFFFFF)
        self._initial_state = (self._ssrc, self._sequence, self._timestamp)

    @property
    def payload_type(self):
        return self._payload_type

    @property
    def ssrc(self):
        return self._ssrc

    @property
    def sequence(self):
        return self._sequence

    @property
    def timestamp(self):
        return self._timestamp

    @property
    def initial_state(self):
        return self._initial_state

    def build(self, payload: bytes, samples: int, marker: bool = False) -> bytes:
        try:
            payload_bytes = memoryview(payload).tobytes()
        except TypeError as error:
            raise TypeError("payload must be bytes-like") from error
        if isinstance(samples, bool) or not isinstance(samples, int):
            raise TypeError("samples must be an integer")
        if not 1 <= samples <= 0xFFFFFFFF:
            raise ValueError("samples must be between 1 and 4294967295")
        if not isinstance(marker, bool):
            raise TypeError("marker must be a bool")

        header = struct.pack(
            "!BBHII",
            0x80,
            self._payload_type | (0x80 if marker else 0),
            self._sequence,
            self._timestamp,
            self._ssrc,
        )
        self._sequence = (self._sequence + 1) & 0xFFFF
        self._timestamp = (self._timestamp + samples) & 0xFFFFFFFF
        return header + payload_bytes
