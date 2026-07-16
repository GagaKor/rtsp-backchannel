#!/usr/bin/env python3
"""Replay a captured RTP reference unchanged over an ONVIF RTSP backchannel."""

from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import struct
import sys
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from typing import Callable, Sequence

if __package__ in {None, ""}:
    _REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[1]
    for _import_path in (_REPOSITORY_ROOT, _REPOSITORY_ROOT / "python"):
        _import_path_string = str(_import_path)
        if _import_path_string not in sys.path:
            sys.path.insert(0, _import_path_string)

from onvif_play import open_backchannel_transport
from tools.rtp_reference import (
    MAX_RTP_PACKET_SIZE,
    RtpPacketMeta,
    parse_rtp_packet,
)


MAX_PACKET_CAPTURE_BYTES = 128 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_LINE_BYTES = 16 * 1024
MAX_REFERENCE_PACKET_COUNT = 1_000_000
MAX_SETTLE_SECONDS = 60.0
MAX_INTER_PACKET_GAP_NS = 10 * 1_000_000_000
MAX_TOTAL_REFERENCE_DURATION_NS = 6 * 60 * 60 * 1_000_000_000
MAX_CREDENTIAL_DECODE_ROUNDS = 3


@dataclass(frozen=True)
class ReferencePacket:
    packet: bytes
    manifest: dict
    meta: RtpPacketMeta


def _manifest_field(row: dict, field: str, packet_index: int):
    try:
        return row[field]
    except KeyError as error:
        raise ValueError(
            f"manifest packet {packet_index} is missing {field}"
        ) from error


def _validate_file_size(path: pathlib.Path, maximum: int, label: str) -> None:
    size = path.stat().st_size
    if size > maximum:
        raise ValueError(f"{label} size {size} exceeds {maximum} bytes")


def _iter_bounded_packets(path: pathlib.Path):
    with path.open("rb") as capture:
        packet_index = 0
        while True:
            prefix = capture.read(4)
            if not prefix:
                return
            if len(prefix) != 4:
                raise ValueError(
                    "truncated 4-byte packet length prefix at packet "
                    f"{packet_index}: found {len(prefix)} bytes"
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


def _iter_bounded_manifest(path: pathlib.Path):
    with path.open("rb") as manifest:
        line_number = 0
        while True:
            line = manifest.readline(MAX_MANIFEST_LINE_BYTES + 1)
            if not line:
                return
            line_number += 1
            if len(line) > MAX_MANIFEST_LINE_BYTES:
                raise ValueError(
                    f"manifest line {line_number} exceeds "
                    f"{MAX_MANIFEST_LINE_BYTES} bytes"
                )
            if not line.strip():
                continue
            try:
                decoded = line.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ValueError(
                    f"manifest line {line_number} is not valid UTF-8"
                ) from error
            try:
                row = json.loads(decoded)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"malformed JSONL at line {line_number}: {error.msg}"
                ) from error
            if not isinstance(row, dict):
                raise ValueError(
                    f"manifest line {line_number} is not a JSON object"
                )
            yield row


def load_and_validate_reference(
    packets_path: pathlib.Path, manifest_path: pathlib.Path
) -> list[ReferencePacket]:
    """Read the complete capture and reject any packet/manifest mismatch."""
    packets_path = pathlib.Path(packets_path)
    manifest_path = pathlib.Path(manifest_path)
    _validate_file_size(
        packets_path, MAX_PACKET_CAPTURE_BYTES, "packet capture"
    )
    _validate_file_size(manifest_path, MAX_MANIFEST_BYTES, "manifest")

    validated = []
    previous_relative_ns = None
    first_relative_ns = None
    metadata_fields = (
        "payload_type",
        "marker",
        "sequence",
        "timestamp",
        "ssrc",
        "payload_size",
        "payload_sha256",
    )
    packet_iterator = iter(_iter_bounded_packets(packets_path))
    manifest_iterator = iter(_iter_bounded_manifest(manifest_path))
    missing = object()
    packet_index = 0
    while True:
        packet = next(packet_iterator, missing)
        row = next(manifest_iterator, missing)
        if packet is missing and row is missing:
            break
        if packet is missing or row is missing:
            packet_count = packet_index + (packet is not missing)
            manifest_count = packet_index + (row is not missing)
            raise ValueError(
                "reference packet and manifest counts differ: "
                f"at least {packet_count} packets and "
                f"{manifest_count} manifest rows"
            )
        if packet_index >= MAX_REFERENCE_PACKET_COUNT:
            raise ValueError(
                "reference packet count exceeds "
                f"{MAX_REFERENCE_PACKET_COUNT}"
            )
        recorded_index = _manifest_field(row, "packet_index", packet_index)
        if recorded_index != packet_index:
            raise ValueError(
                f"manifest packet_index at row {packet_index} is "
                f"{recorded_index!r}; expected {packet_index}"
            )

        relative_ns = _manifest_field(
            row, "relative_monotonic_ns", packet_index
        )
        if not isinstance(relative_ns, int) or isinstance(relative_ns, bool):
            raise ValueError(
                f"manifest packet {packet_index} relative_monotonic_ns must be an integer"
            )
        if relative_ns < 0:
            raise ValueError(
                f"manifest packet {packet_index} relative_monotonic_ns must be nonnegative"
            )
        if previous_relative_ns is not None:
            gap_ns = relative_ns - previous_relative_ns
            if gap_ns < 0:
                raise ValueError(
                    "manifest relative_monotonic_ns must be nondecreasing: "
                    f"packet {packet_index} is {relative_ns} after "
                    f"{previous_relative_ns}"
                )
            if gap_ns > MAX_INTER_PACKET_GAP_NS:
                raise ValueError(
                    f"manifest packet {packet_index} inter-packet gap {gap_ns} ns "
                    f"exceeds {MAX_INTER_PACKET_GAP_NS} ns"
                )
        else:
            first_relative_ns = relative_ns
        duration_ns = relative_ns - first_relative_ns
        if duration_ns > MAX_TOTAL_REFERENCE_DURATION_NS:
            raise ValueError(
                f"reference duration {duration_ns} ns exceeds "
                f"{MAX_TOTAL_REFERENCE_DURATION_NS} ns"
            )
        previous_relative_ns = relative_ns

        packet_size = _manifest_field(row, "packet_size", packet_index)
        if packet_size != len(packet):
            raise ValueError(
                f"manifest packet {packet_index} packet_size is {packet_size!r}; "
                f"exact packet has {len(packet)} bytes"
            )

        meta = parse_rtp_packet(packet)
        for field in metadata_fields:
            recorded = _manifest_field(row, field, packet_index)
            parsed = getattr(meta, field)
            if recorded != parsed:
                raise ValueError(
                    f"manifest packet {packet_index} {field} is {recorded!r}; "
                    f"parsed RTP value is {parsed!r}"
                )
        validated.append(ReferencePacket(packet=packet, manifest=row, meta=meta))
        packet_index += 1
    if not validated:
        raise ValueError("reference packet and manifest counts must be nonzero")
    return validated


def _remove_send_log(path: pathlib.Path) -> None:
    try:
        pathlib.Path(path).unlink()
    except FileNotFoundError:
        pass


def _validate_settle_seconds(value) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
        or value > MAX_SETTLE_SECONDS
    ):
        raise ValueError(
            f"settle seconds must be between 0 and {MAX_SETTLE_SECONDS:g}"
        )
    return float(value)


def _atomic_write_jsonl(path: pathlib.Path, rows: Sequence[dict]) -> None:
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


def replay_reference(
    reference: Sequence[ReferencePacket],
    transport,
    send_log: pathlib.Path,
    *,
    settle_seconds: float = 4.0,
    monotonic_ns: Callable[[], int] = time.monotonic_ns,
    sleeper: Callable[[float], None] = time.sleep,
) -> list[dict]:
    """Replay validated packets at normalized captured pre-emit deadlines."""
    send_log = pathlib.Path(send_log)
    _remove_send_log(send_log)
    if not reference:
        raise ValueError("reference packet count must be nonzero")
    settle_seconds = _validate_settle_seconds(settle_seconds)

    sleeper(settle_seconds)
    replay_start_ns = monotonic_ns()
    first_captured_ns = reference[0].manifest["relative_monotonic_ns"]
    rows = []

    for item in reference:
        captured_relative_ns = item.manifest["relative_monotonic_ns"]
        target_ns = replay_start_ns + captured_relative_ns - first_captured_ns
        remaining_ns = target_ns - monotonic_ns()
        if remaining_ns > 0:
            sleeper(remaining_ns / 1_000_000_000)
        actual_ns = monotonic_ns()
        transport.send_rtp(item.packet)
        rows.append({
            "packet_index": item.manifest["packet_index"],
            "captured_relative_ns": captured_relative_ns,
            "target_monotonic_ns": target_ns,
            "actual_monotonic_ns": actual_ns,
            "lateness_ns": actual_ns - target_ns,
            "packet_size": len(item.packet),
            "seq": item.meta.sequence,
            "timestamp": item.meta.timestamp,
            "ssrc": item.meta.ssrc,
        })

    _atomic_write_jsonl(send_log, rows)
    return rows


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--pass", dest="password", required=True)
    parser.add_argument("--packets", required=True, type=pathlib.Path)
    parser.add_argument("--manifest", required=True, type=pathlib.Path)
    parser.add_argument("--send-log", required=True, type=pathlib.Path)
    parser.add_argument("--settle-seconds", type=float, default=4.0)
    parser.add_argument(
        "--transport",
        choices=("tcp",),
        default="tcp",
        help="RTP transport (only tcp is supported for exact replay)",
    )
    return parser


def _url_decoded_forms(value: str) -> tuple[str, ...]:
    forms = [value]
    for _ in range(MAX_CREDENTIAL_DECODE_ROUNDS):
        decoded = urllib.parse.unquote_plus(forms[-1])
        if decoded == forms[-1]:
            break
        forms.append(decoded)
    return tuple(forms)


def _redacted_error(error: BaseException, credentials: Sequence[str]) -> str:
    message = _url_decoded_forms(str(error))[-1]
    candidates = {
        candidate
        for credential in credentials
        if credential
        for candidate in _url_decoded_forms(credential)
        if candidate
    }
    for candidate in sorted(candidates, key=len, reverse=True):
        message = message.replace(candidate, "<redacted>")
    return message


def main(argv: list[str] | None = None) -> int:
    arguments = build_argument_parser().parse_args(argv)
    try:
        settle_seconds = _validate_settle_seconds(arguments.settle_seconds)
        reference = load_and_validate_reference(
            arguments.packets, arguments.manifest
        )
        _remove_send_log(arguments.send_log)
        with open_backchannel_transport(
            arguments.host,
            arguments.user,
            arguments.password,
            transport=arguments.transport,
        ) as transport:
            replay_reference(
                reference,
                transport,
                arguments.send_log,
                settle_seconds=settle_seconds,
            )
        return 0
    except KeyboardInterrupt:
        _remove_send_log(arguments.send_log)
        sys.stderr.write("replay interrupted\n")
        return 130
    except Exception as error:
        _remove_send_log(arguments.send_log)
        message = _redacted_error(
            error, (arguments.user, arguments.password)
        )
        sys.stderr.write(f"replay failed: {message}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
