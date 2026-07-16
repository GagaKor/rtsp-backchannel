#!/usr/bin/env python3
"""Run the RTP pacer against a discard sink and atomically log send timing."""

from __future__ import annotations

import argparse
import math
import pathlib
import sys
import time
from decimal import Decimal, ROUND_CEILING

if __package__ in {None, ""}:
    _REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[1]
    _PYTHON_ROOT = _REPOSITORY_ROOT / "python"
    for _import_path in (_REPOSITORY_ROOT, _PYTHON_ROOT):
        _import_path_string = str(_import_path)
        if _import_path_string not in sys.path:
            sys.path.insert(0, _import_path_string)

from backchannel_rtp import (
    RtpPacer,
    RtpPacketizer,
    TIMING_LOG_MAX_BYTES,
    TIMING_LOG_MAX_LINE_BYTES,
    TIMING_LOG_MAX_ROWS,
    atomic_write_jsonl,
    remove_output,
)


MAX_DURATION_SECONDS = 6 * 60 * 60
MAX_PACKET_COUNT = TIMING_LOG_MAX_ROWS
MAX_TIMING_LINE_BYTES = TIMING_LOG_MAX_LINE_BYTES
MAX_TIMING_OUTPUT_BYTES = TIMING_LOG_MAX_BYTES
MAX_INJECT_MS = 60_000


def _validate_positive_integer(name, value, maximum=None):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be at most {maximum}")
    return value


def _validate_run(
    duration,
    sample_rate,
    packet_samples,
    mode,
    inject_after_packet,
    inject_ms,
):
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or not 0 < duration <= MAX_DURATION_SECONDS
    ):
        raise ValueError(
            f"duration must be greater than 0 and at most {MAX_DURATION_SECONDS} seconds"
        )
    sample_rate = _validate_positive_integer("sample_rate", sample_rate)
    packet_samples = _validate_positive_integer(
        "packet_samples", packet_samples, 0xFFFFFFFF
    )
    if mode not in RtpPacer.MODES:
        raise ValueError(f"pacer must be one of {', '.join(RtpPacer.MODES)}")

    packet_count = int(
        (
            Decimal(str(duration))
            * Decimal(sample_rate)
            / Decimal(packet_samples)
        ).to_integral_value(rounding=ROUND_CEILING)
    )
    if packet_count > MAX_PACKET_COUNT:
        raise ValueError(
            f"packet count {packet_count} exceeds {MAX_PACKET_COUNT}"
        )
    represented_samples = packet_count * packet_samples
    if represented_samples > MAX_DURATION_SECONDS * sample_rate:
        raise ValueError(
            "represented media duration exceeds "
            f"{MAX_DURATION_SECONDS} seconds"
        )

    if (
        isinstance(inject_ms, bool)
        or not isinstance(inject_ms, (int, float))
        or not math.isfinite(inject_ms)
        or inject_ms < 0
        or inject_ms > MAX_INJECT_MS
    ):
        raise ValueError(f"inject_ms must be between 0 and {MAX_INJECT_MS}")
    if inject_after_packet is None:
        if inject_ms != 0:
            raise ValueError("--inject-after-packet is required when inject_ms is nonzero")
    else:
        if isinstance(inject_after_packet, bool) or not isinstance(
            inject_after_packet, int
        ):
            raise ValueError("inject-after packet must be an integer")
        if inject_ms <= 0:
            raise ValueError("inject_ms must be positive when injection is enabled")
        if not 0 <= inject_after_packet < packet_count - 1:
            raise ValueError(
                "inject-after packet must identify a packet before the final packet"
            )
    return packet_count


def run_pacer(
    *,
    duration,
    sample_rate,
    packet_samples,
    mode,
    inject_after_packet,
    inject_ms,
    output,
    monotonic_ns=time.monotonic_ns,
    sleeper=time.sleep,
):
    output = pathlib.Path(output)
    remove_output(output)
    packet_count = _validate_run(
        duration,
        sample_rate,
        packet_samples,
        mode,
        inject_after_packet,
        inject_ms,
    )

    injection_armed = False
    injection_consumed = False

    def sleep_with_injection(seconds):
        nonlocal injection_consumed
        if injection_armed and not injection_consumed:
            injection_consumed = True
            sleeper(seconds + inject_ms / 1000)
        else:
            sleeper(seconds)

    pacer = RtpPacer(
        sample_rate,
        mode=mode,
        monotonic_ns=monotonic_ns,
        sleeper=sleep_with_injection,
    )
    packetizer = RtpPacketizer(96, ssrc=1, sequence=0, timestamp=0)
    rows = []
    packet_duration_ns = packet_samples * 1_000_000_000 // sample_rate

    for packet_index in range(packet_count):
        consumed_before_wait = injection_consumed
        timing = pacer.wait(packet_samples)
        rtp_timestamp = packetizer.timestamp
        packetizer.build(b"", packet_samples)
        row = {
            "packet_index": packet_index,
            "rtp_timestamp": rtp_timestamp,
            "samples": packet_samples,
            "sample_rate": sample_rate,
            "pacer": mode,
            "packet_duration_ns": packet_duration_ns,
            "configured_jitter_bound_ns": 0,
            "target_monotonic_ns": timing.target_monotonic_ns,
            "actual_monotonic_ns": timing.actual_monotonic_ns,
            "lateness_ns": timing.lateness_ns,
            "interval_ns": timing.interval_ns,
            "rebased": timing.rebased,
        }
        if injection_consumed and not consumed_before_wait:
            row["injected_after_packet"] = inject_after_packet
            row["injected_oversleep_ns"] = round(inject_ms * 1_000_000)
        rows.append(row)
        if packet_index == inject_after_packet:
            injection_armed = True

    pacer.finish()
    atomic_write_jsonl(
        output,
        rows,
        max_rows=MAX_PACKET_COUNT,
        max_line_bytes=MAX_TIMING_LINE_BYTES,
        max_bytes=MAX_TIMING_OUTPUT_BYTES,
    )
    return rows


def build_argument_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument("--sample-rate", type=int, default=8000)
    parser.add_argument("--packet-samples", type=int, default=160)
    parser.add_argument("--pacer", choices=RtpPacer.MODES, default="legacy")
    parser.add_argument("--inject-after-packet", type=int)
    parser.add_argument("--inject-ms", type=float, default=0)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    return parser


def main(argv=None):
    arguments = build_argument_parser().parse_args(argv)
    try:
        run_pacer(
            duration=arguments.duration,
            sample_rate=arguments.sample_rate,
            packet_samples=arguments.packet_samples,
            mode=arguments.pacer,
            inject_after_packet=arguments.inject_after_packet,
            inject_ms=arguments.inject_ms,
            output=arguments.output,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        print(f"run_rtp_pacer: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
