"""Deterministic audio conversion helpers for ONVIF backchannel playback."""

from __future__ import annotations

import math
import os
import pathlib
import shutil
import stat
import struct
import subprocess
import threading
import time


MAX_SOURCE_FILE_BYTES = 128 * 1024 * 1024
MAX_DECODED_S16_BYTES = 128 * 1024 * 1024
FFMPEG_TIMEOUT_SECONDS = 120
MAX_FFMPEG_DIAGNOSTIC_BYTES = 64 * 1024
PROCESS_IO_CHUNK_BYTES = 16 * 1024
PROCESS_TERMINATE_GRACE_SECONDS = 0.5
Q11_UNITY = 1 << 11
# GstVolume's regular volume property is bounded to 10.0.
MAX_Q11_GAIN = 10 * Q11_UNITY


def _require_int(name: str, value: int, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _require_byte_limit(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must be 0 or greater")
    return value


def _validate_s16le(data: bytes) -> bytes:
    if not isinstance(data, bytes):
        raise TypeError("S16LE audio must be bytes")
    if len(data) % 2:
        raise ValueError("S16LE audio must contain an even number of bytes")
    if len(data) > MAX_DECODED_S16_BYTES:
        raise ValueError(
            f"S16LE audio exceeds {MAX_DECODED_S16_BYTES} byte limit"
        )
    return data


def _validate_sample_rate(sample_rate: int) -> int:
    return _require_int("sample rate", sample_rate, 1, 384000)


def _validate_volume(volume: float) -> float:
    if isinstance(volume, bool) or not isinstance(volume, (int, float)):
        raise TypeError("volume must be a number")
    volume = float(volume)
    if not math.isfinite(volume) or not 0.0 <= volume <= 1.0:
        raise ValueError("volume must be finite and between 0.0 and 1.0")
    return volume


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def linear_to_alaw(sample: int) -> int:
    """Encode one signed S16 sample using GStreamer's A-law mapping."""
    sample = _require_int("sample", sample, -32768, 32767)
    sign = 0x80 if sample < 0 else 0
    if sign:
        sample = -sample
    sample = min(sample, 32635)
    if sample >= 256:
        exponent = 7
        mask = 0x4000
        while not sample & mask and exponent > 0:
            exponent -= 1
            mask >>= 1
        mantissa = (sample >> (exponent + 3)) & 0x0F
        compressed = (exponent << 4) | mantissa
    else:
        compressed = sample >> 4
    return ((sign | compressed) ^ 0xD5) & 0xFF


def encode_alaw_s16le(s16le: bytes) -> bytes:
    s16le = _validate_s16le(s16le)
    return bytes(
        linear_to_alaw(sample[0]) for sample in struct.iter_unpack("<h", s16le)
    )


def decode_alaw_byte(encoded: int) -> int:
    encoded = _require_int("A-law byte", encoded, 0, 255) ^ 0x55
    magnitude = (encoded & 0x0F) << 4
    segment = (encoded & 0x70) >> 4
    if segment == 0:
        magnitude += 8
    elif segment == 1:
        magnitude += 0x108
    else:
        magnitude = (magnitude + 0x108) << (segment - 1)
    return magnitude if encoded & 0x80 else -magnitude


def volume_to_q11(volume: float) -> int:
    """Convert volume to GStreamer's truncating Q11 integer gain."""
    return int(_validate_volume(volume) * Q11_UNITY)


def apply_q11_volume_sample(sample: int, gain_q11: int) -> int:
    """Apply a bounded GStreamer-style Q11 gain and clip to signed S16."""
    sample = _require_int("sample", sample, -32768, 32767)
    gain_q11 = _require_int("Q11 gain", gain_q11, 0, MAX_Q11_GAIN)
    # Python's signed right shift is arithmetic, matching the integer volume path.
    scaled = (sample * gain_q11) >> 11
    return max(-32768, min(32767, scaled))


def apply_q11_gain_s16le(s16le: bytes, gain_q11: int) -> bytes:
    """Apply one bounded Q11 gain to every S16LE sample with clipping."""
    s16le = _validate_s16le(s16le)
    gain_q11 = _require_int("Q11 gain", gain_q11, 0, MAX_Q11_GAIN)
    output = bytearray(len(s16le))
    for offset, (sample,) in enumerate(struct.iter_unpack("<h", s16le)):
        struct.pack_into(
            "<h", output, offset * 2, apply_q11_volume_sample(sample, gain_q11)
        )
    return bytes(output)


def apply_q11_volume_s16le(s16le: bytes, volume: float) -> bytes:
    return apply_q11_gain_s16le(s16le, volume_to_q11(volume))


def encode_pcma_gst_compatible(s16le: bytes, volume: float) -> bytes:
    return encode_alaw_s16le(apply_q11_volume_s16le(s16le, volume))


def build_decode_argv(path: os.PathLike[str] | str, sample_rate: int) -> list[str]:
    sample_rate = _validate_sample_rate(sample_rate)
    resample = (
        f"aresample={sample_rate}:resampler=swr:filter_size=32:phase_shift=10:"
        "linear_interp=1:exact_rational=1:cutoff=0.97:dither_method=none:"
        "osf=s16:ochl=mono"
    )
    return [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        os.fspath(path),
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-af",
        resample,
        "-c:a",
        "pcm_s16le",
        "-f",
        "s16le",
        "-fs",
        str(MAX_DECODED_S16_BYTES + 1),
        "pipe:1",
    ]


def build_ffmpeg_pcma_argv(
    sample_rate: int, volume: float, s16_bytes: int
) -> list[str]:
    sample_rate = _validate_sample_rate(sample_rate)
    volume = _validate_volume(volume)
    _require_int("S16 byte count", s16_bytes, 0, MAX_DECODED_S16_BYTES)
    if s16_bytes % 2:
        raise ValueError("S16 byte count must be even")
    expected_bytes = s16_bytes // 2
    return [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "s16le",
        "-ar",
        str(sample_rate),
        "-ac",
        "1",
        "-i",
        "pipe:0",
        "-af",
        f"volume={volume!r}",
        "-c:a",
        "pcm_alaw",
        "-f",
        "alaw",
        "-fs",
        str(expected_bytes + 1),
        "pipe:1",
    ]


def _terminate_and_reap(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        process.wait()
        return
    process.terminate()
    try:
        process.wait(timeout=PROCESS_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _run_ffmpeg(
    argv: list[str],
    *,
    input_data: bytes | None,
    max_output_bytes: int,
    context: str,
    max_input_bytes: int = MAX_DECODED_S16_BYTES,
    max_stderr_bytes: int = MAX_FFMPEG_DIAGNOSTIC_BYTES,
) -> bytes:
    """Run a media child with concurrent, byte-bounded pipe handling."""
    max_input_bytes = _require_byte_limit("input byte limit", max_input_bytes)
    max_output_bytes = _require_byte_limit("output byte limit", max_output_bytes)
    max_stderr_bytes = _require_byte_limit(
        "stderr byte limit", max_stderr_bytes
    )
    if input_data is not None:
        if not isinstance(input_data, bytes):
            raise TypeError("subprocess input must be bytes")
        if len(input_data) > max_input_bytes:
            raise ValueError(
                f"subprocess input exceeds {max_input_bytes} byte limit"
            )

    try:
        process = subprocess.Popen(
            argv,
            stdin=(subprocess.PIPE if input_data is not None else subprocess.DEVNULL),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
    except FileNotFoundError as error:
        raise RuntimeError("ffmpeg executable was not found") from error

    stdout = bytearray()
    stderr = bytearray()
    overflow_event = threading.Event()
    overflow_lock = threading.Lock()
    overflow: list[tuple[str, int]] = []
    thread_errors: list[BaseException] = []

    def mark_overflow(stream_name: str, limit: int) -> None:
        with overflow_lock:
            if not overflow:
                overflow.append((stream_name, limit))
        overflow_event.set()

    def drain_pipe(stream, retained: bytearray, limit: int, name: str) -> None:
        total = 0
        try:
            while True:
                chunk = stream.read(PROCESS_IO_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                remaining = limit - len(retained)
                if remaining > 0:
                    retained.extend(chunk[:remaining])
                if total > limit:
                    mark_overflow(name, limit)
        except BaseException as error:
            thread_errors.append(error)
        finally:
            stream.close()

    def write_input() -> None:
        try:
            view = memoryview(input_data)
            offset = 0
            while offset < len(view):
                written = process.stdin.write(
                    view[offset : offset + PROCESS_IO_CHUNK_BYTES]
                )
                if not written:
                    raise BrokenPipeError("subprocess stdin closed during write")
                offset += written
        except BrokenPipeError:
            pass
        except BaseException as error:
            thread_errors.append(error)
        finally:
            process.stdin.close()

    readers = [
        threading.Thread(
            target=drain_pipe,
            args=(process.stdout, stdout, max_output_bytes, "stdout"),
            daemon=True,
        ),
        threading.Thread(
            target=drain_pipe,
            args=(process.stderr, stderr, max_stderr_bytes, "stderr"),
            daemon=True,
        ),
    ]
    writer = None
    timed_out = False
    try:
        for reader in readers:
            reader.start()
        if input_data is not None:
            writer = threading.Thread(target=write_input, daemon=True)
            writer.start()

        deadline = time.monotonic() + FFMPEG_TIMEOUT_SECONDS
        while process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            if overflow_event.wait(min(0.01, remaining)):
                break

        if process.poll() is None:
            _terminate_and_reap(process)
        else:
            process.wait()
    finally:
        if process.poll() is None:
            _terminate_and_reap(process)
        if writer is not None:
            writer.join()
        for reader in readers:
            reader.join()

    detail = bytes(stderr).decode("utf-8", "replace").strip()
    if overflow:
        stream_name, limit = overflow[0]
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(
            f"ffmpeg {context} {stream_name} exceeds {limit} byte limit{suffix}"
        )
    if timed_out:
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(
            f"ffmpeg {context} timed out after "
            f"{FFMPEG_TIMEOUT_SECONDS} seconds{suffix}"
        )
    if thread_errors:
        raise RuntimeError(
            f"ffmpeg {context} pipe handling failed: {thread_errors[0]}"
        ) from thread_errors[0]
    if process.returncode != 0:
        raise RuntimeError(
            f"ffmpeg {context} failed with exit code {process.returncode}: "
            f"{detail or 'no error details'}"
        )
    return bytes(stdout)


def _validate_source(path: os.PathLike[str] | str) -> pathlib.Path:
    path = pathlib.Path(path)
    try:
        metadata = path.stat()
    except FileNotFoundError as error:
        raise ValueError(f"source file does not exist: {path}") from error
    except OSError as error:
        raise ValueError(f"cannot inspect source file {path}: {error}") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"source path is not a regular file: {path}")
    if metadata.st_size > MAX_SOURCE_FILE_BYTES:
        raise ValueError(
            f"source file {path} exceeds {MAX_SOURCE_FILE_BYTES} byte limit"
        )
    return path


def decode_source(path: os.PathLike[str] | str, sample_rate: int = 8000) -> bytes:
    path = _validate_source(path)
    decoded = _run_ffmpeg(
        build_decode_argv(path, sample_rate),
        input_data=None,
        max_output_bytes=MAX_DECODED_S16_BYTES,
        context=f"decode of {path}",
    )
    _validate_s16le(decoded)
    if not decoded:
        raise RuntimeError(f"ffmpeg decode of {path} produced no audio samples")
    return decoded


def encode_pcma_ffmpeg(
    s16le: bytes, volume: float, sample_rate: int = 8000
) -> bytes:
    s16le = _validate_s16le(s16le)
    _validate_volume(volume)
    if not s16le:
        raise ValueError("S16LE audio must not be empty")
    expected_bytes = len(s16le) // 2
    encoded = _run_ffmpeg(
        build_ffmpeg_pcma_argv(sample_rate, volume, len(s16le)),
        input_data=s16le,
        max_output_bytes=expected_bytes,
        context="PCMA encode",
    )
    if len(encoded) != expected_bytes:
        raise RuntimeError(
            f"ffmpeg PCMA encode produced {len(encoded)} bytes for "
            f"{expected_bytes} samples"
        )
    return encoded


def decoded_error_metrics(
    first: bytes, second: bytes
) -> dict[str, float | int | None]:
    if not isinstance(first, bytes) or not isinstance(second, bytes):
        raise TypeError("PCMA inputs must be bytes")
    if len(first) != len(second):
        raise ValueError("PCMA inputs must have equal lengths")
    if not first:
        raise ValueError("PCMA inputs must not be empty")
    squared_error = 0
    signal_square_sum = 0
    differing = 0
    for left_byte, right_byte in zip(first, second):
        left = decode_alaw_byte(left_byte)
        right = decode_alaw_byte(right_byte)
        squared_error += (left - right) ** 2
        signal_square_sum += right * right
        differing += left_byte != right_byte
    signal_power = signal_square_sum / len(second)
    error_rms = math.sqrt(squared_error / len(second))
    signal_rms = math.sqrt(signal_power)
    error_dbfs = 20 * math.log10(error_rms / 32768) if error_rms else None
    if error_rms == 0:
        snr_db = None
    elif signal_rms == 0:
        snr_db = None
    else:
        snr_db = 20 * math.log10(signal_rms / error_rms)
    return {
        "differing_bytes": differing,
        "differing_percent": differing * 100.0 / len(first),
        "decoded_error_rms": error_rms,
        "decoded_error_dbfs": error_dbfs,
        "signal_rms": signal_rms,
        "snr_db": snr_db,
    }
