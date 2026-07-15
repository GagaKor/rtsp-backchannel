#!/usr/bin/env python3
"""Validate RTP send timing JSONL and write pacing metrics and checks."""

from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import sys
import tempfile


MAX_TIMING_BYTES = 128 * 1024 * 1024
MAX_TIMING_LINE_BYTES = 16 * 1024
MAX_TIMING_ROWS = 1_000_000
MAX_GST_SUMMARY_BYTES = 16 * 1024 * 1024


def _bounded_size(path, maximum, label):
    path = pathlib.Path(path)
    size = path.stat().st_size
    if size > maximum:
        raise ValueError(f"{label} size {size} exceeds {maximum} bytes")
    return path


def load_timing_jsonl(path):
    path = _bounded_size(path, MAX_TIMING_BYTES, "timing JSONL")
    rows = []
    with path.open("rb") as source:
        line_number = 0
        while True:
            line = source.readline(MAX_TIMING_LINE_BYTES + 1)
            if not line:
                break
            line_number += 1
            if len(line) > MAX_TIMING_LINE_BYTES:
                raise ValueError(
                    f"timing JSONL line {line_number} exceeds "
                    f"{MAX_TIMING_LINE_BYTES} bytes"
                )
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
    path = _bounded_size(path, MAX_GST_SUMMARY_BYTES, "GST summary")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
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


def _packet_duration_ns(row):
    duration_ns = row.get("packet_duration_ns")
    if isinstance(duration_ns, int) and not isinstance(duration_ns, bool):
        return duration_ns
    sample_rate = row.get("sample_rate")
    if isinstance(sample_rate, int) and sample_rate > 0:
        return row["samples"] * 1_000_000_000 // sample_rate
    return None


def summarize_timing(rows, *, gst_summary=None):
    _validate_rows(rows)
    delta_histogram = {}
    unexpected_deltas = []
    for index in range(1, len(rows)):
        delta = (
            rows[index]["rtp_timestamp"] - rows[index - 1]["rtp_timestamp"]
        ) & 0xFFFFFFFF
        key = str(delta)
        delta_histogram[key] = delta_histogram.get(key, 0) + 1
        expected = rows[index - 1]["samples"]
        if delta != expected:
            unexpected_deltas.append({
                "packet_index": index,
                "expected": expected,
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
    severe_indexes = set(rebase_indexes)
    injected_indexes = []
    for index, row in enumerate(rows):
        if row.get("injected_oversleep_ns", 0) > 0:
            injected_indexes.append(index)
            severe_indexes.add(index)
        elif index > 0:
            prior_duration_ns = _packet_duration_ns(rows[index - 1])
            if prior_duration_ns is not None and row["lateness_ns"] >= prior_duration_ns:
                severe_indexes.add(index)

    post_oversleep_intervals = [
        rows[index + 1]["interval_ns"]
        for index in sorted(severe_indexes)
        if index + 1 < len(rows)
    ]
    post_interval_checks = []
    for index in sorted(severe_indexes):
        if index + 1 >= len(rows):
            continue
        duration_ns = _packet_duration_ns(rows[index])
        if duration_ns is not None:
            post_interval_checks.append(
                rows[index + 1]["interval_ns"] >= duration_ns * 3 // 4
            )

    configured_jitter_bound_ns = max(
        (row.get("configured_jitter_bound_ns", 0) for row in rows),
        default=0,
    )
    gst_reference = None
    if gst_summary is not None:
        gst_reference = {
            "packet_count": gst_summary.get("packet_count"),
            "inter_arrival_ns": gst_summary.get("inter_arrival_ns"),
            "comparison": "not_comparable",
            "note": (
                "GST inter-arrival statistics are not deadline-error statistics; "
                "unlike metrics were not compared."
            ),
        }
        reference_bound = gst_summary.get("deadline_error_jitter_bound_ns")
        if (
            isinstance(reference_bound, int)
            and not isinstance(reference_bound, bool)
            and reference_bound >= 0
        ):
            configured_jitter_bound_ns = max(
                configured_jitter_bound_ns, reference_bound
            )

    deadline_error_limit_ns = max(
        2_000_000, configured_jitter_bound_ns + 1_000_000
    )
    absolute_error_stats = _stats(deadline_errors, include_min=False)
    checks = {
        "no_unexpected_timestamp_deltas": not unexpected_deltas,
        "no_post_severe_interval_below_75_percent": all(post_interval_checks),
        "p99_deadline_error_within_bound": (
            absolute_error_stats["p99"] <= deadline_error_limit_ns
        ),
    }
    summary = {
        "packet_count": len(rows),
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
    _remove_output(arguments.output)
    try:
        rows = load_timing_jsonl(arguments.input)
        gst_summary = (
            load_gst_summary(arguments.gst_summary)
            if arguments.gst_summary is not None
            else None
        )
        summary = summarize_timing(rows, gst_summary=gst_summary)
        _atomic_write_json(arguments.output, summary)
    except (OSError, TypeError, ValueError) as error:
        print(f"summarize_send_timing: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
