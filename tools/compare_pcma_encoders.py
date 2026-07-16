#!/usr/bin/env python3
"""Compare FFmpeg and GStreamer-compatible PCMA encoding from one decode."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import stat
import sys
import tempfile
from contextlib import contextmanager


ROOT = pathlib.Path(__file__).resolve().parents[1]
PYTHON_DIR = ROOT / "python"
if str(PYTHON_DIR) not in sys.path:
    sys.path.insert(0, str(PYTHON_DIR))

import backchannel_audio


SOURCE_READ_CHUNK_BYTES = 1024 * 1024


def _digest(data: bytes) -> dict[str, int | str]:
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def _read_source_snapshot(path: pathlib.Path) -> bytes:
    path = pathlib.Path(path)
    try:
        source = path.open("rb")
    except FileNotFoundError as error:
        raise ValueError(f"source file does not exist: {path}") from error
    except OSError as error:
        raise ValueError(f"cannot open source file {path}: {error}") from error
    with source as opened_source:
        try:
            metadata = os.fstat(opened_source.fileno())
        except OSError as error:
            raise ValueError(f"cannot inspect source file {path}: {error}") from error
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"source path is not a regular file: {path}")
        limit = backchannel_audio.MAX_SOURCE_FILE_BYTES
        if metadata.st_size > limit:
            raise ValueError(f"source file {path} exceeds {limit} byte limit")

        snapshot = bytearray()
        while True:
            remaining = limit - len(snapshot)
            chunk = opened_source.read(
                min(SOURCE_READ_CHUNK_BYTES, remaining + 1)
            )
            if not chunk:
                break
            if len(chunk) > remaining:
                raise ValueError(
                    f"source file {path} exceeds {limit} byte limit"
                )
            snapshot.extend(chunk)
    return bytes(snapshot)


@contextmanager
def _temporary_source_snapshot(snapshot: bytes, suffix: str):
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="pcma-compare-source-", suffix=suffix
    )
    temporary_path = pathlib.Path(temporary_name)
    temporary = None
    try:
        temporary = os.fdopen(descriptor, "wb")
        with temporary:
            temporary.write(snapshot)
            temporary.flush()
            os.fsync(temporary.fileno())
        yield temporary_path
    finally:
        if temporary is None:
            os.close(descriptor)
        elif not temporary.closed:
            temporary.close()
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def compare(path: pathlib.Path, volume: float, sample_rate: int) -> dict:
    path = pathlib.Path(path)
    snapshot = _read_source_snapshot(path)
    source = _digest(snapshot)
    with _temporary_source_snapshot(snapshot, path.suffix) as snapshot_path:
        s16 = backchannel_audio.decode_source(snapshot_path, sample_rate)
    ffmpeg_pcma = backchannel_audio.encode_pcma_ffmpeg(s16, volume, sample_rate)
    gst_pcma = backchannel_audio.encode_pcma_gst_compatible(s16, volume)
    return {
        "file": str(path),
        "sample_rate": sample_rate,
        "volume": volume,
        "source": source,
        "s16": _digest(s16),
        "ffmpeg_pcma": _digest(ffmpeg_pcma),
        "gst_compatible_pcma": _digest(gst_pcma),
        **backchannel_audio.decoded_error_metrics(ffmpeg_pcma, gst_pcma),
    }


def _atomic_write_json(path: pathlib.Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
            json.dump(report, temporary, allow_nan=False, indent=2, sort_keys=True)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=pathlib.Path, required=True)
    parser.add_argument("--volume", type=float, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--sample-rate", type=int, default=8000)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_argument_parser().parse_args(argv)
    try:
        report = compare(arguments.file, arguments.volume, arguments.sample_rate)
        _atomic_write_json(arguments.output, report)
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
