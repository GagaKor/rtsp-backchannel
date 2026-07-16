"""Pure RTP packet construction for the Python backchannel sender."""

import hashlib
import json
import os
import pathlib
import stat
import struct
import tempfile
import time
from dataclasses import dataclass


TIMING_LOG_MAX_ROWS = 10_000
TIMING_LOG_MAX_LINE_BYTES = 1024
TIMING_LOG_MAX_BYTES = TIMING_LOG_MAX_ROWS * TIMING_LOG_MAX_LINE_BYTES
RTP_FIXED_HEADER_BYTES = 12
MAX_RTP_PACKET_SIZE = 65_535
PACKET_PATTERN_MAX_ROWS = 10_000
PACKET_PATTERN_MAX_LINE_BYTES = 1024
PACKET_PATTERN_MAX_BYTES = (
    PACKET_PATTERN_MAX_ROWS * PACKET_PATTERN_MAX_LINE_BYTES
)


def _uint(name, value, maximum):
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if not 0 <= value <= maximum:
        raise ValueError(f"{name} must be between 0 and {maximum}")
    return value


@dataclass(frozen=True)
class RtpBoundary:
    payload: bytes | memoryview
    payload_offset: int
    samples: int
    timestamp_offset: int
    target_time_ns: int
    duration_ns: int

    @property
    def payload_size(self):
        return len(self.payload)

    @property
    def timestamp_advance(self):
        return self.samples


@dataclass(frozen=True)
class RtpBoundaryPlan:
    sample_rate: int
    bytes_per_sample: int
    total_samples: int
    finish_time_ns: int
    packet_count: int
    _payload: bytes
    _payload_sizes: tuple[int, ...] | None
    _fixed_payload_size: int | None

    @staticmethod
    def _coerce_payload(payload):
        if isinstance(payload, bytes):
            return payload
        try:
            return memoryview(payload).tobytes()
        except TypeError as error:
            raise TypeError("payload must be bytes-like") from error

    @staticmethod
    def _validate_format(sample_rate, bytes_per_sample):
        if isinstance(sample_rate, bool) or not isinstance(sample_rate, int):
            raise TypeError("sample_rate must be an integer")
        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        if (
            isinstance(bytes_per_sample, bool)
            or not isinstance(bytes_per_sample, int)
        ):
            raise TypeError("bytes_per_sample must be an integer")
        if bytes_per_sample <= 0:
            raise ValueError("bytes_per_sample must be positive")

    def _iter_payload_sizes(self):
        if self._payload_sizes is not None:
            yield from self._payload_sizes
            return
        for offset in range(0, len(self._payload), self._fixed_payload_size):
            yield min(self._fixed_payload_size, len(self._payload) - offset)

    @property
    def packets(self):
        payload_view = memoryview(self._payload)
        payload_offset = 0
        sample_offset = 0
        for payload_size in self._iter_payload_sizes():
            next_payload_offset = payload_offset + payload_size
            samples = payload_size // self.bytes_per_sample
            yield RtpBoundary(
                payload=payload_view[payload_offset:next_payload_offset],
                payload_offset=payload_offset,
                samples=samples,
                timestamp_offset=sample_offset,
                target_time_ns=(
                    sample_offset * 1_000_000_000 // self.sample_rate
                ),
                duration_ns=samples * 1_000_000_000 // self.sample_rate,
            )
            payload_offset = next_payload_offset
            sample_offset += samples

    @classmethod
    def from_payload_sizes(
        cls,
        payload,
        payload_sizes,
        *,
        sample_rate,
        bytes_per_sample,
    ):
        payload = cls._coerce_payload(payload)
        cls._validate_format(sample_rate, bytes_per_sample)

        validated_payload_sizes = []
        payload_offset = 0
        sample_offset = 0
        for packet_index, payload_size in enumerate(payload_sizes):
            if isinstance(payload_size, bool) or not isinstance(payload_size, int):
                raise TypeError(
                    f"payload size at index {packet_index} must be an integer"
                )
            if payload_size <= 0:
                raise ValueError(
                    f"payload size at index {packet_index} must be positive"
                )
            if payload_size % bytes_per_sample:
                raise ValueError(
                    f"payload size {payload_size} at index {packet_index} "
                    f"is not divisible by {bytes_per_sample} bytes per sample"
                )
            next_payload_offset = payload_offset + payload_size
            if next_payload_offset > len(payload):
                raise ValueError(
                    "boundary plan has too many payload bytes: "
                    f"needs {next_payload_offset}, payload has {len(payload)}"
                )
            samples = payload_size // bytes_per_sample
            if samples > 0xFFFFFFFF:
                raise ValueError(
                    f"sample count at index {packet_index} exceeds uint32"
                )
            validated_payload_sizes.append(payload_size)
            payload_offset = next_payload_offset
            sample_offset += samples

        if payload_offset < len(payload):
            raise ValueError(
                "boundary plan has too few payload bytes: "
                f"covers {payload_offset}, payload has {len(payload)}"
            )
        return cls(
            sample_rate=sample_rate,
            bytes_per_sample=bytes_per_sample,
            total_samples=sample_offset,
            finish_time_ns=sample_offset * 1_000_000_000 // sample_rate,
            packet_count=len(validated_payload_sizes),
            _payload=payload,
            _payload_sizes=tuple(validated_payload_sizes),
            _fixed_payload_size=None,
        )

    @classmethod
    def fixed(
        cls,
        payload,
        samples_per_packet,
        *,
        sample_rate,
        bytes_per_sample,
    ):
        payload = cls._coerce_payload(payload)
        cls._validate_format(sample_rate, bytes_per_sample)
        if (
            isinstance(samples_per_packet, bool)
            or not isinstance(samples_per_packet, int)
        ):
            raise TypeError("samples_per_packet must be an integer")
        if not 1 <= samples_per_packet <= 0xFFFFFFFF:
            raise ValueError(
                "samples_per_packet must be between 1 and 4294967295"
            )
        if len(payload) % bytes_per_sample:
            raise ValueError(
                f"payload length is not divisible by {bytes_per_sample} "
                "bytes per sample"
            )
        packet_payload_size = samples_per_packet * bytes_per_sample
        total_samples = len(payload) // bytes_per_sample
        packet_count = (
            (len(payload) + packet_payload_size - 1) // packet_payload_size
            if payload
            else 0
        )
        return cls(
            sample_rate=sample_rate,
            bytes_per_sample=bytes_per_sample,
            total_samples=total_samples,
            finish_time_ns=total_samples * 1_000_000_000 // sample_rate,
            packet_count=packet_count,
            _payload=payload,
            _payload_sizes=None,
            _fixed_payload_size=packet_payload_size,
        )


def fixed_packet_size_candidate(payload_sizes):
    """Return a fixed size only when the capture is fixed packets plus a tail."""
    payload_sizes = tuple(payload_sizes)
    for packet_index, payload_size in enumerate(payload_sizes):
        if isinstance(payload_size, bool) or not isinstance(payload_size, int):
            raise TypeError(
                f"payload size at index {packet_index} must be an integer"
            )
        if payload_size <= 0:
            raise ValueError(
                f"payload size at index {packet_index} must be positive"
            )
    if len(payload_sizes) < 2:
        return None
    candidate = payload_sizes[0]
    if all(size == candidate for size in payload_sizes[:-1]):
        if payload_sizes[-1] <= candidate:
            return candidate
    return None


@dataclass(frozen=True)
class NormalizedRtpStream:
    payload_lengths: tuple[int, ...]
    sequence_offsets: tuple[int, ...]
    timestamp_offsets: tuple[int, ...]
    marker_positions: tuple[int, ...]
    packet_count: int
    duration_samples: int
    duration_ns: int
    constant_ssrc: bool
    payload_sha256: str


def _parse_rtp_for_normalization(packet, packet_index):
    try:
        packet = memoryview(packet).tobytes()
    except TypeError as error:
        raise TypeError(f"RTP packet {packet_index} must be bytes-like") from error
    if len(packet) < 12:
        raise ValueError(f"RTP packet {packet_index} is shorter than 12 bytes")
    first, second, sequence, timestamp, ssrc = struct.unpack_from(
        "!BBHII", packet
    )
    if first >> 6 != 2:
        raise ValueError(f"RTP packet {packet_index} is not version 2")
    payload_offset = 12 + (first & 0x0F) * 4
    if payload_offset > len(packet):
        raise ValueError(f"RTP packet {packet_index} has a truncated CSRC list")
    if first & 0x10:
        if payload_offset + 4 > len(packet):
            raise ValueError(
                f"RTP packet {packet_index} has a truncated extension header"
            )
        extension_words = struct.unpack_from("!H", packet, payload_offset + 2)[0]
        payload_offset += 4 + extension_words * 4
        if payload_offset > len(packet):
            raise ValueError(
                f"RTP packet {packet_index} has truncated extension data"
            )
    payload_end = len(packet)
    if first & 0x20:
        padding_size = packet[-1]
        if padding_size == 0 or padding_size > payload_end - payload_offset:
            raise ValueError(f"RTP packet {packet_index} has invalid padding")
        payload_end -= padding_size
    payload = packet[payload_offset:payload_end]
    if not payload:
        raise ValueError(f"RTP packet {packet_index} has an empty PCMA payload")
    return sequence, timestamp, ssrc, bool(second & 0x80), payload


def normalize_pcma_rtp_packets(packets, sample_rate=8000):
    if isinstance(sample_rate, bool) or not isinstance(sample_rate, int):
        raise TypeError("sample_rate must be an integer")
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")

    payload_lengths = []
    sequence_offsets = []
    timestamp_offsets = []
    marker_positions = []
    ssrcs = []
    payload_digest = hashlib.sha256()
    first_sequence = None
    first_timestamp = None
    for packet_index, packet in enumerate(packets):
        sequence, timestamp, ssrc, marker, payload = _parse_rtp_for_normalization(
            packet, packet_index
        )
        if first_sequence is None:
            first_sequence = sequence
            first_timestamp = timestamp
        payload_lengths.append(len(payload))
        sequence_offsets.append((sequence - first_sequence) & 0xFFFF)
        timestamp_offsets.append((timestamp - first_timestamp) & 0xFFFFFFFF)
        if marker:
            marker_positions.append(packet_index)
        ssrcs.append(ssrc)
        payload_digest.update(payload)

    duration_samples = (
        timestamp_offsets[-1] + payload_lengths[-1]
        if payload_lengths
        else 0
    )
    return NormalizedRtpStream(
        payload_lengths=tuple(payload_lengths),
        sequence_offsets=tuple(sequence_offsets),
        timestamp_offsets=tuple(timestamp_offsets),
        marker_positions=tuple(marker_positions),
        packet_count=len(payload_lengths),
        duration_samples=duration_samples,
        duration_ns=duration_samples * 1_000_000_000 // sample_rate,
        constant_ssrc=len(set(ssrcs)) <= 1,
        payload_sha256=payload_digest.hexdigest(),
    )


def normalized_rtp_differences(expected, actual):
    if not isinstance(expected, NormalizedRtpStream):
        raise TypeError("expected must be a NormalizedRtpStream")
    if not isinstance(actual, NormalizedRtpStream):
        raise TypeError("actual must be a NormalizedRtpStream")
    field_names = (
        "payload_lengths",
        "sequence_offsets",
        "timestamp_offsets",
        "marker_positions",
        "packet_count",
        "duration_samples",
        "duration_ns",
        "constant_ssrc",
        "payload_sha256",
    )
    return tuple(
        field_name
        for field_name in field_names
        if getattr(expected, field_name) != getattr(actual, field_name)
    )


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
        had_scheduled_target = self._next_target_ns is not None
        if not had_scheduled_target:
            actual_ns = self._read_clock()
            self._anchor_ns = actual_ns
            target_ns = actual_ns
        else:
            target_ns = self._next_target_ns
            actual_ns = self._sleep_until(target_ns)

        lateness_ns = actual_ns - target_ns
        next_segment_samples = self._segment_samples + samples
        scheduled_next_target_ns = self._anchor_ns + (
            next_segment_samples * 1_000_000_000 // self.sample_rate
        )
        duration_to_next_target_ns = scheduled_next_target_ns - target_ns
        rebased = False
        if (
            self.mode == "rebase"
            and had_scheduled_target
            and lateness_ns >= duration_to_next_target_ns
        ):
            rebased = True
            self.rebase_count += 1
            self._anchor_ns = actual_ns
            self._segment_samples = 0

        interval_ns = (
            None
            if self._last_actual_ns is None
            else actual_ns - self._last_actual_ns
        )
        self._current_target_ns = actual_ns if rebased else target_ns
        self._current_stream_samples = self._total_samples
        self._total_samples += samples
        self._segment_samples += samples
        self._next_target_ns = self._target_after_segment_samples()
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


def paths_refer_to_same_file(first, second):
    """Return whether two path spellings resolve to the same filesystem object."""
    first = pathlib.Path(first).expanduser()
    second = pathlib.Path(second).expanduser()
    try:
        if first.resolve(strict=False) == second.resolve(strict=False):
            return True
        return first.samefile(second)
    except FileNotFoundError:
        return False
    except (OSError, RuntimeError) as error:
        raise ValueError(
            f"cannot safely compare paths {first!s} and {second!s}: {error}"
        ) from error


def load_packet_pattern(
    path,
    *,
    max_rows=PACKET_PATTERN_MAX_ROWS,
    max_line_bytes=PACKET_PATTERN_MAX_LINE_BYTES,
    max_bytes=PACKET_PATTERN_MAX_BYTES,
    max_rtp_packet_size=MAX_RTP_PACKET_SIZE,
):
    """Load an ordered payload-size manifest with bounded binary reads."""
    for name, value in (
        ("max_rows", max_rows),
        ("max_line_bytes", max_line_bytes),
        ("max_bytes", max_bytes),
        ("max_rtp_packet_size", max_rtp_packet_size),
    ):
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{name} must be an integer")
        if value <= 0:
            raise ValueError(f"{name} must be positive")
    if max_rtp_packet_size <= RTP_FIXED_HEADER_BYTES:
        raise ValueError(
            f"max_rtp_packet_size must exceed {RTP_FIXED_HEADER_BYTES}"
        )

    path = pathlib.Path(path)
    try:
        metadata = path.stat()
    except FileNotFoundError as error:
        raise ValueError(f"packet pattern does not exist: {path}") from error
    except OSError as error:
        raise ValueError(f"cannot inspect packet pattern {path}: {error}") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"packet pattern is not a regular file: {path}")
    if metadata.st_size > max_bytes:
        raise ValueError(
            f"packet pattern {path} exceeds {max_bytes} byte limit"
        )

    payload_sizes = []
    bytes_read = 0
    try:
        with path.open("rb") as source:
            while True:
                line = source.readline(max_line_bytes + 1)
                if not line:
                    break
                line_number = len(payload_sizes) + 1
                bytes_read += len(line)
                if len(line) > max_line_bytes:
                    raise ValueError(
                        f"packet pattern line {line_number} exceeds "
                        f"{max_line_bytes} bytes"
                    )
                if bytes_read > max_bytes:
                    raise ValueError(
                        f"packet pattern {path} exceeds {max_bytes} byte limit"
                    )
                if line_number > max_rows:
                    raise ValueError(
                        f"packet pattern row count exceeds {max_rows}"
                    )
                try:
                    text = line.decode("utf-8")
                except UnicodeDecodeError as error:
                    raise ValueError(
                        f"packet pattern line {line_number} must be UTF-8"
                    ) from error
                try:
                    row = json.loads(text)
                except json.JSONDecodeError as error:
                    raise ValueError(
                        f"malformed JSON on line {line_number}: {error.msg}"
                    ) from error
                if not isinstance(row, dict):
                    raise ValueError(
                        f"packet pattern line {line_number} must be a JSON object"
                    )
                packet_index = row.get("packet_index")
                if isinstance(packet_index, bool) or not isinstance(
                    packet_index, int
                ):
                    raise ValueError(
                        f"packet_index on line {line_number} must be an integer"
                    )
                expected_index = len(payload_sizes)
                if packet_index != expected_index:
                    raise ValueError(
                        f"packet_index {packet_index} on line {line_number}; "
                        f"expected {expected_index}"
                    )
                payload_size = row.get("payload_size")
                if isinstance(payload_size, bool) or not isinstance(
                    payload_size, int
                ):
                    raise ValueError(
                        f"payload_size on line {line_number} must be an integer"
                    )
                if payload_size <= 0:
                    raise ValueError(
                        f"payload_size on line {line_number} must be positive"
                    )
                rtp_packet_size = RTP_FIXED_HEADER_BYTES + payload_size
                if rtp_packet_size > max_rtp_packet_size:
                    raise ValueError(
                        f"RTP packet size {rtp_packet_size} on line "
                        f"{line_number} exceeds {max_rtp_packet_size}"
                    )
                payload_sizes.append(payload_size)
    except OSError as error:
        raise ValueError(f"cannot read packet pattern {path}: {error}") from error
    if not payload_sizes:
        raise ValueError(f"packet pattern {path} must not be empty")
    return tuple(payload_sizes)


def atomic_write_jsonl(
    path,
    rows,
    *,
    max_rows=None,
    max_line_bytes=None,
    max_bytes=None,
):
    """Publish complete timing rows with a same-directory atomic replace."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            bytes_written = 0
            for index, row in enumerate(rows):
                if max_rows is not None and index >= max_rows:
                    raise ValueError(
                        f"JSONL row count exceeds {max_rows}"
                    )
                rendered = json.dumps(row, sort_keys=True) + "\n"
                rendered_bytes = len(rendered.encode("utf-8"))
                if (
                    max_line_bytes is not None
                    and rendered_bytes > max_line_bytes
                ):
                    raise ValueError(
                        f"JSONL line {index + 1} exceeds "
                        f"{max_line_bytes} bytes"
                    )
                bytes_written += rendered_bytes
                if max_bytes is not None and bytes_written > max_bytes:
                    raise ValueError(
                        f"JSONL output exceeds {max_bytes} bytes"
                    )
                output.write(rendered)
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
