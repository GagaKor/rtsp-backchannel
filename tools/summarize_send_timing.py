#!/usr/bin/env python3
"""Validate RTP send timing JSONL and write pacing metrics and checks."""

from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import stat
import sys
import tempfile

if __package__ in {None, ""}:
    _REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[1]
    _PYTHON_ROOT = _REPOSITORY_ROOT / "python"
    _python_root_string = str(_PYTHON_ROOT)
    if _python_root_string not in sys.path:
        sys.path.insert(0, _python_root_string)

from backchannel_rtp import (
    TIMING_LOG_MAX_BYTES,
    TIMING_LOG_MAX_LINE_BYTES,
    TIMING_LOG_MAX_ROWS,
    paths_refer_to_same_file,
)


MAX_TIMING_ROWS = TIMING_LOG_MAX_ROWS
MAX_TIMING_LINE_BYTES = TIMING_LOG_MAX_LINE_BYTES
MAX_TIMING_BYTES = TIMING_LOG_MAX_BYTES
MAX_GST_SUMMARY_BYTES = 16 * 1024 * 1024
MAX_GST_SUMMARY_LINE_BYTES = 64 * 1024
TASK5_SAMPLE_RATE = 8000
TASK5_PACKET_SAMPLES = 160
TASK5_PACKET_DURATION_NS = 20_000_000
TASK5_SEVERE_LATENESS_NS = 20_000_000
TASK5_POST_SEVERE_MINIMUM_NS = 15_000_000


def _open_bounded_regular_file(path, maximum, label):
    path = pathlib.Path(path)
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"{label} is not a regular file: {path}")
        if metadata.st_size > maximum:
            raise ValueError(
                f"{label} size {metadata.st_size} exceeds {maximum} bytes"
            )
        source = os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise
    return source


def _bounded_lines(source, *, maximum, line_maximum, label):
    total_bytes = 0
    line_number = 0
    while True:
        line = source.readline(line_maximum + 1)
        if not line:
            return
        line_number += 1
        if len(line) > line_maximum:
            raise ValueError(
                f"{label} line {line_number} exceeds {line_maximum} bytes"
            )
        total_bytes += len(line)
        if total_bytes > maximum:
            raise ValueError(
                f"{label} cumulative size {total_bytes} exceeds {maximum} bytes"
            )
        yield line_number, line


def load_timing_jsonl(path):
    rows = []
    with _open_bounded_regular_file(
        path, MAX_TIMING_BYTES, "timing JSONL"
    ) as source:
        for line_number, line in _bounded_lines(
            source,
            maximum=MAX_TIMING_BYTES,
            line_maximum=MAX_TIMING_LINE_BYTES,
            label="timing JSONL",
        ):
            if not line.strip():
                continue
            if len(rows) >= MAX_TIMING_ROWS:
                raise ValueError(
                    f"timing JSONL row count exceeds {MAX_TIMING_ROWS}"
                )
            try:
                decoded = line.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ValueError(
                    f"timing JSONL line {line_number} is not valid UTF-8"
                ) from error
            try:
                row = json.loads(decoded)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"malformed JSONL at line {line_number}: {error.msg}"
                ) from error
            if not isinstance(row, dict):
                raise ValueError(
                    f"timing JSONL line {line_number} is not a JSON object"
                )
            rows.append(row)
    if not rows:
        raise ValueError("timing JSONL must contain at least one row")
    return rows


def load_gst_summary(path):
    encoded = bytearray()
    with _open_bounded_regular_file(
        path, MAX_GST_SUMMARY_BYTES, "GST summary"
    ) as source:
        for _, line in _bounded_lines(
            source,
            maximum=MAX_GST_SUMMARY_BYTES,
            line_maximum=MAX_GST_SUMMARY_LINE_BYTES,
            label="GST summary",
        ):
            encoded.extend(line)
    try:
        value = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid GST summary JSON: {error}") from error
    if not isinstance(value, dict):
        raise ValueError("GST summary must be a JSON object")
    return value


def _integer_field(row, field, index, *, minimum=None, maximum=None):
    if field not in row:
        raise ValueError(f"timing row {index} is missing {field}")
    value = row[field]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"timing row {index} {field} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"timing row {index} {field} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"timing row {index} {field} must be at most {maximum}")
    return value


def _validate_rows(rows):
    previous_actual_ns = None
    for index, row in enumerate(rows):
        packet_index = _integer_field(row, "packet_index", index, minimum=0)
        if packet_index != index:
            raise ValueError(
                f"timing row {index} packet_index is {packet_index}; expected {index}"
            )
        _integer_field(row, "rtp_timestamp", index, minimum=0, maximum=0xFFFFFFFF)
        _integer_field(row, "samples", index, minimum=1, maximum=0xFFFFFFFF)
        target_ns = _integer_field(row, "target_monotonic_ns", index)
        actual_ns = _integer_field(row, "actual_monotonic_ns", index)
        lateness_ns = _integer_field(row, "lateness_ns", index)
        if lateness_ns != actual_ns - target_ns:
            raise ValueError(
                f"timing row {index} lateness_ns does not match actual-target"
            )
        interval_ns = row.get("interval_ns")
        if interval_ns is not None:
            if isinstance(interval_ns, bool) or not isinstance(interval_ns, int):
                raise ValueError(
                    f"timing row {index} interval_ns must be an integer or null"
                )
            if interval_ns < 0:
                raise ValueError(
                    f"timing row {index} interval_ns must be nonnegative"
                )
        if previous_actual_ns is not None and actual_ns < previous_actual_ns:
            raise ValueError(
                f"timing row {index} actual_monotonic_ns must be nondecreasing"
            )
        expected_interval = (
            None if previous_actual_ns is None else actual_ns - previous_actual_ns
        )
        if interval_ns != expected_interval:
            raise ValueError(
                f"timing row {index} interval_ns is {interval_ns!r}; "
                f"expected {expected_interval!r}"
            )
        if not isinstance(row.get("rebased"), bool):
            raise ValueError(f"timing row {index} rebased must be a bool")
        for optional in (
            "sample_rate",
            "packet_duration_ns",
            "configured_jitter_bound_ns",
            "injected_after_packet",
            "injected_oversleep_ns",
        ):
            if optional in row:
                _integer_field(row, optional, index, minimum=0)
        previous_actual_ns = actual_ns


def _percentile(sorted_values, percentile):
    if not sorted_values:
        return None
    rank = max(1, math.ceil(percentile * len(sorted_values)))
    return sorted_values[rank - 1]


def _stats(values, *, include_min=True):
    if not values:
        return None
    ordered = sorted(values)
    result = {}
    if include_min:
        result["min"] = ordered[0]
    result.update({
        "p50": _percentile(ordered, 0.50),
        "p95": _percentile(ordered, 0.95),
        "p99": _percentile(ordered, 0.99),
        "max": ordered[-1],
    })
    return result


def _gst_inter_arrival_p99(gst_summary):
    inter_arrival = gst_summary.get("inter_arrival_ns")
    p99 = inter_arrival.get("p99") if isinstance(inter_arrival, dict) else None
    if (
        isinstance(p99, bool)
        or not isinstance(p99, (int, float))
        or not math.isfinite(p99)
        or p99 < 0
    ):
        raise ValueError(
            "GST summary inter_arrival_ns.p99 must be a finite nonnegative number"
        )
    return p99


def summarize_timing(rows, *, gst_summary=None):
    _validate_rows(rows)
    unexpected_sample_rates = [
        {
            "packet_index": row["packet_index"],
            "expected": TASK5_SAMPLE_RATE,
            "actual": row.get("sample_rate"),
        }
        for row in rows
        if row.get("sample_rate") != TASK5_SAMPLE_RATE
    ]
    unexpected_samples = [
        {
            "packet_index": row["packet_index"],
            "expected": TASK5_PACKET_SAMPLES,
            "actual": row["samples"],
        }
        for row in rows
        if row["samples"] != TASK5_PACKET_SAMPLES
    ]
    unexpected_packet_durations = [
        {
            "packet_index": row["packet_index"],
            "expected": TASK5_PACKET_DURATION_NS,
            "actual": row.get("packet_duration_ns"),
        }
        for row in rows
        if row.get("packet_duration_ns") != TASK5_PACKET_DURATION_NS
    ]
    unexpected_target_deadlines = []
    for index in range(1, len(rows)):
        previous = rows[index - 1]
        deadline_base_ns = (
            previous["actual_monotonic_ns"]
            if previous["rebased"]
            else previous["target_monotonic_ns"]
        )
        expected_target_ns = deadline_base_ns + TASK5_PACKET_DURATION_NS
        actual_target_ns = rows[index]["target_monotonic_ns"]
        if actual_target_ns != expected_target_ns:
            unexpected_target_deadlines.append({
                "packet_index": index,
                "expected": expected_target_ns,
                "actual": actual_target_ns,
                "previous_packet_rebased": previous["rebased"],
            })
    incoherent_rebase_flags = []
    for index, row in enumerate(rows):
        expected_rebased = (
            index > 0 and row["lateness_ns"] >= TASK5_SEVERE_LATENESS_NS
        )
        if row["rebased"] != expected_rebased:
            incoherent_rebase_flags.append({
                "packet_index": index,
                "expected": expected_rebased,
                "actual": row["rebased"],
                "lateness_ns": row["lateness_ns"],
            })
    delta_histogram = {}
    unexpected_deltas = []
    for index in range(1, len(rows)):
        delta = (
            rows[index]["rtp_timestamp"] - rows[index - 1]["rtp_timestamp"]
        ) & 0xFFFFFFFF
        key = str(delta)
        delta_histogram[key] = delta_histogram.get(key, 0) + 1
        if delta != TASK5_PACKET_SAMPLES:
            unexpected_deltas.append({
                "packet_index": index,
                "expected": TASK5_PACKET_SAMPLES,
                "actual": delta,
            })

    intervals = [row["interval_ns"] for row in rows[1:]]
    deadline_errors = [
        abs(row["actual_monotonic_ns"] - row["target_monotonic_ns"])
        for row in rows
    ]
    rebase_indexes = [
        row["packet_index"] for row in rows if row["rebased"]
    ]
    injected_indexes = []
    for index, row in enumerate(rows):
        if row.get("injected_oversleep_ns", 0) > 0:
            injected_indexes.append(index)
    severe_indexes = {
        index
        for index, row in enumerate(rows)
        if row["lateness_ns"] >= TASK5_SEVERE_LATENESS_NS
    }

    post_oversleep_intervals = [
        rows[index + 1]["interval_ns"]
        for index in sorted(severe_indexes)
        if index + 1 < len(rows)
    ]
    post_interval_checks = [
        rows[index + 1]["interval_ns"] >= TASK5_POST_SEVERE_MINIMUM_NS
        for index in sorted(severe_indexes)
        if index + 1 < len(rows)
    ]

    configured_jitter_bound_ns = max(
        (row.get("configured_jitter_bound_ns", 0) for row in rows),
        default=0,
    )
    gst_reference = None
    deadline_error_limit_ns = 2_000_000
    if gst_summary is not None:
        gst_p99_ns = _gst_inter_arrival_p99(gst_summary)
        gst_acceptance_bound_ns = gst_p99_ns + 1_000_000
        deadline_error_limit_ns = max(2_000_000, gst_acceptance_bound_ns)
        gst_reference = {
            "packet_count": gst_summary.get("packet_count"),
            "inter_arrival_ns": gst_summary.get("inter_arrival_ns"),
            "acceptance_metric": "inter_arrival_ns.p99 + 1000000ns",
            "acceptance_bound_ns": gst_acceptance_bound_ns,
            "comparison": "semantic_caveat",
            "note": (
                "Task 5 applies the GST inter-arrival p99 as the acceptance "
                "bound even though inter-arrival and deadline-error measure "
                "different timing properties."
            ),
        }
    absolute_error_stats = _stats(deadline_errors, include_min=False)
    checks = {
        "all_sample_rates_match_expected": not unexpected_sample_rates,
        "all_packet_samples_match_expected": not unexpected_samples,
        "all_packet_durations_match_expected": not unexpected_packet_durations,
        "no_unexpected_target_deadlines": not unexpected_target_deadlines,
        "no_incoherent_rebase_flags": not incoherent_rebase_flags,
        "no_unexpected_timestamp_deltas": not unexpected_deltas,
        "no_post_severe_interval_below_75_percent": all(post_interval_checks),
        "p99_deadline_error_within_bound": (
            absolute_error_stats["p99"] <= deadline_error_limit_ns
        ),
    }
    summary = {
        "packet_count": len(rows),
        "expected_sample_rate": TASK5_SAMPLE_RATE,
        "expected_samples": TASK5_PACKET_SAMPLES,
        "expected_packet_duration_ns": TASK5_PACKET_DURATION_NS,
        "severe_lateness_threshold_ns": TASK5_SEVERE_LATENESS_NS,
        "post_severe_minimum_interval_ns": TASK5_POST_SEVERE_MINIMUM_NS,
        "unexpected_sample_rates": unexpected_sample_rates,
        "unexpected_packet_samples": unexpected_samples,
        "unexpected_packet_durations": unexpected_packet_durations,
        "unexpected_target_deadlines": unexpected_target_deadlines,
        "incoherent_rebase_flags": incoherent_rebase_flags,
        "rtp_timestamp_delta_histogram": delta_histogram,
        "unexpected_timestamp_deltas": unexpected_deltas,
        "interval_ns": _stats(intervals),
        "absolute_deadline_error_ns": absolute_error_stats,
        "rebase_count": len(rebase_indexes),
        "rebase_indexes": rebase_indexes,
        "injected_packet_indexes": injected_indexes,
        "injected_interval_ns": _stats(
            [rows[index]["interval_ns"] for index in injected_indexes]
        ),
        "post_oversleep_interval_ns": _stats(post_oversleep_intervals),
        "configured_jitter_bound_ns": configured_jitter_bound_ns,
        "deadline_error_limit_ns": deadline_error_limit_ns,
        "checks": checks,
        "pass": all(checks.values()),
    }
    if gst_reference is not None:
        summary["gst_reference"] = gst_reference
    return summary


def _remove_output(path):
    try:
        pathlib.Path(path).unlink()
    except FileNotFoundError:
        pass


def _atomic_write_json(path, value):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def build_argument_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--gst-summary", type=pathlib.Path)
    return parser


def main(argv=None):
    arguments = build_argument_parser().parse_args(argv)
    try:
        for input_option, input_path in (
            ("--input", arguments.input),
            ("--gst-summary", arguments.gst_summary),
        ):
            if (
                input_path is not None
                and paths_refer_to_same_file(arguments.output, input_path)
            ):
                raise ValueError(
                    f"--output must not refer to the same file as {input_option}"
                )
        _remove_output(arguments.output)
        rows = load_timing_jsonl(arguments.input)
        gst_summary = (
            load_gst_summary(arguments.gst_summary)
            if arguments.gst_summary is not None
            else None
        )
        summary = summarize_timing(
            rows,
            gst_summary=gst_summary,
        )
        _atomic_write_json(arguments.output, summary)
    except (OSError, TypeError, ValueError) as error:
        print(f"summarize_send_timing: {error}", file=sys.stderr)
        return 1
    return 0 if summary["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
