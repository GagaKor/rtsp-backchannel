"""Pure RTP packet construction for the Python backchannel sender."""

import json
import os
import pathlib
import struct
import tempfile
import time
from dataclasses import dataclass


def _uint(name, value, maximum):
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if not 0 <= value <= maximum:
        raise ValueError(f"{name} must be between 0 and {maximum}")
    return value


@dataclass(frozen=True)
class PacingTiming:
    target_monotonic_ns: int
    actual_monotonic_ns: int
    lateness_ns: int
    interval_ns: int | None
    rebased: bool


class RtpPacer:
    """Pace packet sends against RTP sample time without changing RTP state."""

    MODES = ("legacy", "rebase")

    def __init__(
        self,
        sample_rate,
        *,
        mode="legacy",
        monotonic_ns=time.monotonic_ns,
        sleeper=time.sleep,
    ):
        if isinstance(sample_rate, bool) or not isinstance(sample_rate, int):
            raise TypeError("sample_rate must be an integer")
        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        if mode not in self.MODES:
            raise ValueError(f"mode must be one of {', '.join(self.MODES)}")
        if not callable(monotonic_ns):
            raise TypeError("monotonic_ns must be callable")
        if not callable(sleeper):
            raise TypeError("sleeper must be callable")
        self.sample_rate = sample_rate
        self.mode = mode
        self._monotonic_ns = monotonic_ns
        self._sleeper = sleeper
        self._last_clock_ns = None
        self._anchor_ns = None
        self._segment_samples = 0
        self._total_samples = 0
        self._next_target_ns = None
        self._previous_duration_ns = None
        self._last_actual_ns = None
        self._current_target_ns = None
        self._current_stream_samples = 0
        self.rebase_count = 0

    def _read_clock(self):
        now_ns = self._monotonic_ns()
        if isinstance(now_ns, bool) or not isinstance(now_ns, int):
            raise TypeError("monotonic_ns must return an integer")
        if self._last_clock_ns is not None and now_ns < self._last_clock_ns:
            raise RuntimeError(
                "monotonic clock moved backward: "
                f"{now_ns} < {self._last_clock_ns}"
            )
        self._last_clock_ns = now_ns
        return now_ns

    @staticmethod
    def _validate_samples(samples):
        if isinstance(samples, bool) or not isinstance(samples, int):
            raise TypeError("samples must be an integer")
        if not 1 <= samples <= 0xFFFFFFFF:
            raise ValueError("samples must be between 1 and 4294967295")
        return samples

    def _target_after_segment_samples(self):
        return self._anchor_ns + (
            self._segment_samples * 1_000_000_000 // self.sample_rate
        )

    def _sleep_until(self, target_ns):
        now_ns = self._read_clock()
        while now_ns < target_ns:
            self._sleeper((target_ns - now_ns) / 1_000_000_000)
            now_ns = self._read_clock()
        return now_ns

    def wait(self, samples):
        """Wait until the packet's send deadline and register its media duration."""
        samples = self._validate_samples(samples)
        if self._next_target_ns is None:
            actual_ns = self._read_clock()
            self._anchor_ns = actual_ns
            target_ns = actual_ns
        else:
            target_ns = self._next_target_ns
            actual_ns = self._sleep_until(target_ns)

        lateness_ns = actual_ns - target_ns
        rebased = False
        if (
            self.mode == "rebase"
            and self._previous_duration_ns is not None
            and lateness_ns >= self._previous_duration_ns
        ):
            target_ns = actual_ns
            lateness_ns = 0
            rebased = True
            self.rebase_count += 1
            self._anchor_ns = actual_ns
            self._segment_samples = 0

        interval_ns = (
            None
            if self._last_actual_ns is None
            else actual_ns - self._last_actual_ns
        )
        self._current_target_ns = target_ns
        self._current_stream_samples = self._total_samples
        self._total_samples += samples
        self._segment_samples += samples
        self._next_target_ns = self._target_after_segment_samples()
        self._previous_duration_ns = self._next_target_ns - target_ns
        self._last_actual_ns = actual_ns
        return PacingTiming(
            target_monotonic_ns=target_ns,
            actual_monotonic_ns=actual_ns,
            lateness_ns=lateness_ns,
            interval_ns=interval_ns,
            rebased=rebased,
        )

    def finish(self):
        """Wait through the exact media duration represented by the final packet."""
        if self._next_target_ns is None:
            return self._read_clock()
        return self._sleep_until(self._next_target_ns)

    def stream_samples_at(self, monotonic_ns=None):
        """Map a monotonic session instant onto the rebased RTP sample timeline."""
        if self._current_target_ns is None:
            return 0
        if monotonic_ns is None:
            monotonic_ns = self._read_clock()
        elif isinstance(monotonic_ns, bool) or not isinstance(monotonic_ns, int):
            raise TypeError("monotonic_ns must be an integer")
        elapsed_ns = max(0, monotonic_ns - self._current_target_ns)
        elapsed_samples = elapsed_ns * self.sample_rate // 1_000_000_000
        return min(
            self._total_samples,
            self._current_stream_samples + elapsed_samples,
        )


def remove_output(path):
    if path is None:
        return
    try:
        pathlib.Path(path).unlink()
    except FileNotFoundError:
        pass


def atomic_write_jsonl(path, rows):
    """Publish complete timing rows with a same-directory atomic replace."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            for row in rows:
                output.write(json.dumps(row, sort_keys=True) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


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
