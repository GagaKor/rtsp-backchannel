#!/usr/bin/env python3
"""Parse and summarize RTP captures without requiring GStreamer."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import pathlib
import struct
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from typing import Iterator


MAX_RTP_PACKET_SIZE = 65535


@dataclass(frozen=True)
class RtpPacketMeta:
    version: int
    padding_size: int
    has_extension: bool
    csrc_count: int
    marker: bool
    payload_type: int
    sequence: int
    timestamp: int
    ssrc: int
    header_size: int
    extension_profile: int | None
    extension_size: int
    payload_size: int
    payload_sha256: str


def parse_rtp_packet(packet: bytes) -> RtpPacketMeta:
    """Return RTP header and payload metadata for one complete packet."""
    if len(packet) < 12:
        raise ValueError(
            f"packet is shorter than 12-byte RTP header: found {len(packet)} bytes"
        )

    first, second, sequence, timestamp, ssrc = struct.unpack_from("!BBHII", packet)
    version = first >> 6
    if version != 2:
        raise ValueError(f"unsupported RTP version {version}; expected version 2")

    csrc_count = first & 0x0F
    has_extension = bool(first & 0x10)
    has_padding = bool(first & 0x20)
    offset = 12 + csrc_count * 4
    if len(packet) < offset:
        raise ValueError(
            f"truncated RTP CSRC list: expected header size {offset}, "
            f"found {len(packet)} bytes"
        )

    extension_profile = None
    extension_size = 0
    if has_extension:
        if len(packet) < offset + 4:
            raise ValueError("truncated RTP extension header")
        extension_profile, extension_words = struct.unpack_from("!HH", packet, offset)
        extension_size = extension_words * 4
        offset += 4
        if len(packet) < offset + extension_size:
            raise ValueError(
                f"truncated RTP extension data: expected {extension_size} bytes"
            )
        offset += extension_size

    body_size = len(packet) - offset
    padding_size = 0
    if has_padding:
        if body_size == 0:
            raise ValueError("RTP padding flag is set but the RTP body is empty")
        padding_size = packet[-1]
        if padding_size == 0:
            raise ValueError("invalid RTP padding length 0")
        if padding_size > body_size:
            raise ValueError(
                f"RTP padding length {padding_size} exceeds RTP body size {body_size}"
            )

    payload_end = len(packet) - padding_size
    payload = packet[offset:payload_end]
    return RtpPacketMeta(
        version=version,
        padding_size=padding_size,
        has_extension=has_extension,
        csrc_count=csrc_count,
        marker=bool(second & 0x80),
        payload_type=second & 0x7F,
        sequence=sequence,
        timestamp=timestamp,
        ssrc=ssrc,
        header_size=offset,
        extension_profile=extension_profile,
        extension_size=extension_size,
        payload_size=len(payload),
        payload_sha256=hashlib.sha256(payload).hexdigest(),
    )


def read_length_prefixed_packets(path: pathlib.Path) -> Iterator[bytes]:
    """Yield packets framed by an unsigned four-byte big-endian length."""
    with pathlib.Path(path).open("rb") as capture:
        packet_index = 0
        while True:
            prefix = capture.read(4)
            if not prefix:
                return
            if len(prefix) != 4:
                raise ValueError(
                    f"truncated 4-byte packet length prefix at packet {packet_index}: "
                    f"found {len(prefix)} bytes"
                )
            packet_size = struct.unpack("!I", prefix)[0]
            if packet_size == 0:
                raise ValueError(
                    f"invalid RTP packet length 0 at packet {packet_index}"
                )
            if packet_size > MAX_RTP_PACKET_SIZE:
                raise ValueError(
                    f"packet {packet_index} length {packet_size} exceeds maximum "
                    f"RTP packet size {MAX_RTP_PACKET_SIZE}"
                )
            packet = capture.read(packet_size)
            if len(packet) != packet_size:
                raise ValueError(
                    f"truncated packet {packet_index}: expected {packet_size} bytes, "
                    f"found {len(packet)}"
                )
            yield packet
            packet_index += 1


def load_manifest(path: pathlib.Path) -> list[dict]:
    """Load a JSON Lines packet manifest with actionable parse errors."""
    rows = []
    with pathlib.Path(path).open("r", encoding="utf-8") as manifest:
        for line_number, line in enumerate(manifest, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"malformed JSONL at line {line_number}: {error.msg}"
                ) from error
            if not isinstance(row, dict):
                raise ValueError(f"manifest line {line_number} is not a JSON object")
            rows.append(row)
    return rows


def _percentile(values: list[int], percentile: float) -> int | float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    value = ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
    return int(value) if value.is_integer() else value


def _histogram(values: list[int]) -> dict[str, int]:
    return {str(value): count for value, count in sorted(Counter(values).items())}


def summarize_manifest(rows: list[dict]) -> dict:
    """Build deterministic timing and RTP continuity metrics from manifest rows."""
    relative_times = [int(row["relative_monotonic_ns"]) for row in rows]
    inter_arrivals = [
        current - previous
        for previous, current in zip(relative_times, relative_times[1:])
    ]
    timestamp_deltas = [
        (int(current["timestamp"]) - int(previous["timestamp"])) & 0xFFFFFFFF
        for previous, current in zip(rows, rows[1:])
        if int(current["ssrc"]) == int(previous["ssrc"])
    ]

    sender_counts = Counter(
        (int(row["payload_type"]), int(row["ssrc"])) for row in rows
    )
    sender_tuples = [
        {"payload_type": payload_type, "ssrc": ssrc, "packet_count": count}
        for (payload_type, ssrc), count in sorted(sender_counts.items())
    ]

    ssrc_changes = []
    discontinuities = []
    for previous, current in zip(rows, rows[1:]):
        packet_index = int(current["packet_index"])
        previous_ssrc = int(previous["ssrc"])
        current_ssrc = int(current["ssrc"])
        if current_ssrc != previous_ssrc:
            ssrc_changes.append(
                {
                    "packet_index": packet_index,
                    "from_ssrc": previous_ssrc,
                    "to_ssrc": current_ssrc,
                }
            )
            continue

        expected = (int(previous["sequence"]) + 1) & 0xFFFF
        actual = int(current["sequence"])
        if actual != expected:
            forward_delta = (actual - expected) & 0xFFFF
            discontinuities.append(
                {
                    "packet_index": packet_index,
                    "expected_sequence": expected,
                    "actual_sequence": actual,
                    "missing_packets": forward_delta if forward_delta < 0x8000 else 0,
                }
            )

    duration_ns = max(relative_times) - min(relative_times) if rows else 0
    return {
        "packet_count": len(rows),
        "duration_ns": duration_ns,
        "duration_seconds": duration_ns / 1_000_000_000,
        "payload_size_histogram": _histogram(
            [int(row["payload_size"]) for row in rows]
        ),
        "timestamp_delta_histogram": _histogram(timestamp_deltas),
        "inter_arrival_ns": {
            "p50": _percentile(inter_arrivals, 0.50),
            "p95": _percentile(inter_arrivals, 0.95),
            "p99": _percentile(inter_arrivals, 0.99),
            "max": max(inter_arrivals) if inter_arrivals else None,
        },
        "marker_count": sum(bool(row["marker"]) for row in rows),
        "sender_tuples": sender_tuples,
        "ssrc_changes": ssrc_changes,
        "discontinuities": discontinuities,
    }


def _atomic_write_text(path: pathlib.Path, content: str) -> None:
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    summarize = subparsers.add_parser("summarize", help="summarize a manifest JSONL")
    summarize.add_argument("manifest", type=pathlib.Path)
    summarize.add_argument("--output", type=pathlib.Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _build_argument_parser().parse_args(argv)
    if arguments.command == "summarize":
        rendered = json.dumps(
            summarize_manifest(load_manifest(arguments.manifest)),
            indent=2,
            sort_keys=True,
        ) + "\n"
        if arguments.output is None:
            sys.stdout.write(rendered)
        else:
            _atomic_write_text(arguments.output, rendered)
        return 0
    raise AssertionError(f"unhandled command: {arguments.command}")


if __name__ == "__main__":
    raise SystemExit(main())
