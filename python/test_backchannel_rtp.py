import errno
import hashlib
import importlib
import json
import os
import pathlib
import re
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from io import StringIO
from unittest import mock

from tools.capture_gst_backchannel import (
    BackchannelPushError,
    CaptureArtifacts,
    build_session_metadata,
    build_argument_parser,
    configure_legacy_push,
    ensure_push_succeeded,
    load_gstreamer_library,
    redact_uri,
    resolve_endpoint,
    sha256_file,
)
from tools.rtp_reference import (
    MAX_RTP_PACKET_SIZE,
    extract_payloads,
    load_manifest,
    parse_rtp_packet,
    read_length_prefixed_packets,
    summarize_manifest,
)


ROOT = pathlib.Path(__file__).resolve().parents[1]
RTP_REFERENCE = ROOT / "tools" / "rtp_reference.py"
CAPTURE_TOOL = ROOT / "tools" / "capture_gst_backchannel.py"
RUN_PACER_TOOL = ROOT / "tools" / "run_rtp_pacer.py"
SUMMARIZE_TIMING_TOOL = ROOT / "tools" / "summarize_send_timing.py"


def make_rtp_packet(
    payload=b"payload",
    *,
    payload_type=8,
    marker=False,
    sequence=0x1234,
    timestamp=0x10203040,
    ssrc=0x50607080,
    csrcs=(),
    extension=None,
    padding=0,
):
    first = 0x80 | len(csrcs)
    if extension is not None:
        first |= 0x10
    if padding:
        first |= 0x20
    second = payload_type | (0x80 if marker else 0)
    packet = bytearray(struct.pack("!BBHII", first, second, sequence, timestamp, ssrc))
    for csrc in csrcs:
        packet.extend(struct.pack("!I", csrc))
    if extension is not None:
        profile, data = extension
        if len(data) % 4:
            raise ValueError("test extension data must be 32-bit aligned")
        packet.extend(struct.pack("!HH", profile, len(data) // 4))
        packet.extend(data)
    packet.extend(payload)
    if padding:
        packet.extend(bytes(padding - 1))
        packet.append(padding)
    return bytes(packet)


class RtpPacketizerTests(unittest.TestCase):
    def setUp(self):
        try:
            self.module = importlib.import_module("backchannel_rtp")
        except ModuleNotFoundError as error:
            self.fail(f"backchannel_rtp module is missing: {error}")
        self.packetizer_type = self.module.RtpPacketizer

    def test_defaults_consume_independent_secure_random_values(self):
        random_values = [
            b"\x01\x02\x03\x04",
            b"\x05\x06",
            b"\x07\x08\x09\x0a",
        ]

        with mock.patch.object(
            self.module.os, "urandom", side_effect=random_values
        ) as urandom:
            packetizer = self.packetizer_type(payload_type=8)

        self.assertEqual(packetizer.initial_state, (0x01020304, 0x0506, 0x0708090A))
        self.assertEqual(packetizer.ssrc, 0x01020304)
        self.assertEqual(packetizer.sequence, 0x0506)
        self.assertEqual(packetizer.timestamp, 0x0708090A)
        self.assertEqual([call.args for call in urandom.call_args_list], [(4,), (2,), (4,)])

    def test_build_emits_rtp_v2_header_payload_marker_and_payload_type(self):
        packetizer = self.packetizer_type(
            payload_type=97,
            ssrc=0x50607080,
            sequence=0x1234,
            timestamp=0x10203040,
        )

        packet = packetizer.build(memoryview(b"audio"), samples=160, marker=True)
        meta = parse_rtp_packet(packet)

        self.assertEqual(packet[0], 0x80)
        self.assertEqual(meta.payload_type, 97)
        self.assertTrue(meta.marker)
        self.assertEqual(meta.sequence, 0x1234)
        self.assertEqual(meta.timestamp, 0x10203040)
        self.assertEqual(meta.ssrc, 0x50607080)
        self.assertEqual(packet[12:], b"audio")
        self.assertEqual(packetizer.sequence, 0x1235)
        self.assertEqual(packetizer.timestamp, 0x102030E0)
        self.assertEqual(packetizer.initial_state, (0x50607080, 0x1234, 0x10203040))

    def test_build_wraps_sequence_and_timestamp_while_ssrc_stays_constant(self):
        packetizer = self.packetizer_type(
            payload_type=8,
            ssrc=0xAABBCCDD,
            sequence=0xFFFF,
            timestamp=0xFFFFFFFE,
        )

        first = parse_rtp_packet(packetizer.build(bytearray(b"a"), samples=2))
        second = parse_rtp_packet(packetizer.build(b"b", samples=1))

        self.assertEqual((first.sequence, first.timestamp, first.ssrc),
                         (0xFFFF, 0xFFFFFFFE, 0xAABBCCDD))
        self.assertEqual((second.sequence, second.timestamp, second.ssrc),
                         (0, 0, 0xAABBCCDD))
        self.assertEqual(packetizer.sequence, 1)
        self.assertEqual(packetizer.timestamp, 1)
        self.assertEqual(packetizer.ssrc, 0xAABBCCDD)

    def test_initial_state_is_read_only(self):
        packetizer = self.packetizer_type(8, ssrc=1, sequence=2, timestamp=3)

        with self.assertRaises(AttributeError):
            packetizer.initial_state = (4, 5, 6)

    def test_rejects_invalid_constructor_values(self):
        cases = {
            "negative payload type": {"payload_type": -1},
            "large payload type": {"payload_type": 128},
            "noninteger payload type": {"payload_type": 8.5},
            "negative ssrc": {"payload_type": 8, "ssrc": -1},
            "large ssrc": {"payload_type": 8, "ssrc": 1 << 32},
            "negative sequence": {"payload_type": 8, "sequence": -1},
            "large sequence": {"payload_type": 8, "sequence": 1 << 16},
            "negative timestamp": {"payload_type": 8, "timestamp": -1},
            "large timestamp": {"payload_type": 8, "timestamp": 1 << 32},
        }

        for name, kwargs in cases.items():
            with self.subTest(name=name), self.assertRaises((TypeError, ValueError)):
                self.packetizer_type(**kwargs)

    def test_rejects_invalid_payload_and_sample_counts_without_advancing(self):
        packetizer = self.packetizer_type(8, ssrc=1, sequence=2, timestamp=3)

        for payload in (None, "audio", 7):
            with self.subTest(payload=payload), self.assertRaises(TypeError):
                packetizer.build(payload, samples=1)
        for samples in (0, -1, 1 << 32, 1.5, True):
            with self.subTest(samples=samples), self.assertRaises(
                (TypeError, ValueError)
            ):
                packetizer.build(b"audio", samples=samples)

        self.assertEqual((packetizer.sequence, packetizer.timestamp), (2, 3))

    def test_rejects_nonboolean_markers_without_advancing(self):
        packetizer = self.packetizer_type(8, ssrc=1, sequence=2, timestamp=3)

        for marker in (1, None, "true"):
            with self.subTest(marker=marker), self.assertRaises(TypeError):
                packetizer.build(b"audio", samples=160, marker=marker)

        self.assertEqual((packetizer.sequence, packetizer.timestamp), (2, 3))
        self.assertEqual(packetizer.ssrc, 1)


class FakePacerClock:
    def __init__(
        self,
        now_ns=1_000_000_000,
        *,
        overshoot_ns=0,
        inject_on_sleep=None,
        injected_overshoot_ns=0,
    ):
        self.start_ns = now_ns
        self.now_ns = now_ns
        self.overshoot_ns = overshoot_ns
        self.inject_on_sleep = inject_on_sleep
        self.injected_overshoot_ns = injected_overshoot_ns
        self.sleeps = []

    def monotonic_ns(self):
        return self.now_ns

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        extra_ns = self.overshoot_ns
        if len(self.sleeps) == self.inject_on_sleep:
            extra_ns += self.injected_overshoot_ns
        self.now_ns += round(seconds * 1_000_000_000) + extra_ns


class RtpBoundaryPlanTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module("backchannel_rtp")

    def test_irregular_pcma_plan_couples_boundaries_timestamps_and_pacing(self):
        pattern = [192, 192, 160, 192, 64]
        payload = bytes((index * 37) % 256 for index in range(800))
        plan = self.module.RtpBoundaryPlan.from_payload_sizes(
            payload,
            pattern,
            sample_rate=8000,
            bytes_per_sample=1,
        )

        self.assertEqual(
            [packet.payload_size for packet in plan.packets], pattern
        )
        self.assertEqual([packet.samples for packet in plan.packets], pattern)
        self.assertEqual(
            [packet.timestamp_advance for packet in plan.packets], pattern
        )
        self.assertEqual(
            [packet.timestamp_offset for packet in plan.packets],
            [0, 192, 384, 544, 736],
        )
        self.assertEqual(
            [packet.target_time_ns for packet in plan.packets],
            [0, 24_000_000, 48_000_000, 68_000_000, 92_000_000],
        )
        self.assertEqual(
            [packet.duration_ns for packet in plan.packets],
            [24_000_000, 24_000_000, 20_000_000, 24_000_000, 8_000_000],
        )
        self.assertEqual(plan.total_samples, 800)
        self.assertEqual(plan.finish_time_ns, 100_000_000)
        self.assertEqual(
            b"".join(packet.payload for packet in plan.packets), payload
        )

        clock = FakePacerClock()
        pacer = self.module.RtpPacer(
            8000,
            mode="rebase",
            monotonic_ns=clock.monotonic_ns,
            sleeper=clock.sleep,
        )
        packetizer = self.module.RtpPacketizer(
            8, ssrc=1, sequence=2, timestamp=1000
        )
        timings = []
        rtp_packets = []
        for boundary in plan.packets:
            timings.append(pacer.wait(boundary.samples))
            rtp_packets.append(
                packetizer.build(
                    boundary.payload,
                    boundary.timestamp_advance,
                )
            )

        self.assertEqual(
            [timing.target_monotonic_ns - clock.start_ns for timing in timings],
            [0, 24_000_000, 48_000_000, 68_000_000, 92_000_000],
        )
        self.assertEqual(pacer.finish() - clock.start_ns, 100_000_000)
        self.assertEqual(
            [parse_rtp_packet(packet).timestamp for packet in rtp_packets],
            [1000, 1192, 1384, 1544, 1736],
        )
        self.assertEqual(b"".join(packet[12:] for packet in rtp_packets), payload)
        self.assertEqual(packetizer.timestamp, 1800)

    def test_plan_rejects_boundaries_that_do_not_consume_payload_exactly(self):
        for pattern, message in (
            ([3, 1], "too few"),
            ([3, 3], "too many"),
        ):
            with self.subTest(pattern=pattern), self.assertRaisesRegex(
                ValueError, message
            ):
                self.module.RtpBoundaryPlan.from_payload_sizes(
                    b"12345",
                    pattern,
                    sample_rate=8000,
                    bytes_per_sample=1,
                )

    def test_fixed_plan_materializes_reusable_boundaries_lazily(self):
        payload = b"x" * 1_000
        with mock.patch.object(
            self.module,
            "RtpBoundary",
            wraps=self.module.RtpBoundary,
        ) as boundary_type:
            plan = self.module.RtpBoundaryPlan.fixed(
                payload,
                160,
                sample_rate=8000,
                bytes_per_sample=1,
            )
            boundary_type.assert_not_called()
            first_pass = [packet.payload_size for packet in plan.packets]
            second_pass = [packet.payload_size for packet in plan.packets]

        self.assertEqual(plan.packet_count, 7)
        self.assertEqual(first_pass, [160] * 6 + [40])
        self.assertEqual(second_pass, first_pass)

    def test_boundary_durations_use_adjacent_cumulative_targets_at_48khz(self):
        plan = self.module.RtpBoundaryPlan.fixed(
            b"abc",
            1,
            sample_rate=48_000,
            bytes_per_sample=1,
        )
        packets = list(plan.packets)
        targets = [packet.target_time_ns for packet in packets]
        next_targets = targets[1:] + [plan.finish_time_ns]

        self.assertEqual(targets, [0, 20_833, 41_666])
        self.assertEqual(plan.finish_time_ns, 62_500)
        self.assertEqual(
            [packet.duration_ns for packet in packets],
            [20_833, 20_833, 20_834],
        )
        self.assertEqual(
            [packet.duration_ns for packet in packets],
            [
                next_target - target
                for target, next_target in zip(
                    targets, next_targets, strict=True
                )
            ],
        )
        self.assertEqual(
            sum(packet.duration_ns for packet in packets),
            plan.finish_time_ns,
        )

    def test_fixed_candidate_requires_every_nonfinal_packet_to_be_uniform(self):
        captured = [289] + [320] * 249 + [31]
        cases = (
            ([320, 320, 31], 320),
            ([320, 320, 320], 320),
            ([192, 160, 192, 64], None),
            (captured, None),
            ([31], None),
        )

        for pattern, expected in cases:
            with self.subTest(pattern=(pattern[:3], len(pattern))):
                self.assertEqual(
                    self.module.fixed_packet_size_candidate(pattern), expected
                )
        self.assertEqual(sum(captured), 80_000)


class NormalizedRtpStreamTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module("backchannel_rtp")

    @staticmethod
    def make_stream(
        payload,
        sizes,
        *,
        sequence=100,
        timestamp=1000,
        ssrc=10,
        markers=(0,),
        payload_types=None,
        sequence_offsets=None,
        timestamp_offsets=None,
        ssrcs=None,
    ):
        packets = []
        payload_offset = 0
        timestamp_offset = 0
        for index, size in enumerate(sizes):
            chunk = payload[payload_offset : payload_offset + size]
            packet_sequence_offset = (
                index if sequence_offsets is None else sequence_offsets[index]
            )
            packet_timestamp_offset = (
                timestamp_offset
                if timestamp_offsets is None
                else timestamp_offsets[index]
            )
            payload_type = 8 if payload_types is None else payload_types[index]
            packet_ssrc = ssrc if ssrcs is None else ssrcs[index]
            packets.append(
                make_rtp_packet(
                    chunk,
                    payload_type=payload_type,
                    sequence=(sequence + packet_sequence_offset) & 0xFFFF,
                    timestamp=(timestamp + packet_timestamp_offset) & 0xFFFFFFFF,
                    ssrc=packet_ssrc,
                    marker=index in markers,
                )
            )
            payload_offset += size
            timestamp_offset += size
        if payload_offset != len(payload):
            raise AssertionError("test sizes must consume the payload")
        return packets

    def test_random_rtp_bases_normalize_to_complete_pcma_stream_identity(self):
        sizes = [192, 192, 160, 192, 64]
        payload = bytes((index * 19) % 256 for index in range(800))
        first = self.module.normalize_pcma_rtp_packets(
            self.make_stream(
                payload,
                sizes,
                sequence=0xFFF0,
                timestamp=0xFFFFFF00,
                ssrc=0x01020304,
            )
        )
        second = self.module.normalize_pcma_rtp_packets(
            self.make_stream(
                payload,
                sizes,
                sequence=77,
                timestamp=123_456,
                ssrc=0xAABBCCDD,
            )
        )

        self.assertEqual(first, second)
        self.assertEqual(first.payload_lengths, tuple(sizes))
        self.assertEqual(first.payload_types, (8, 8, 8, 8, 8))
        self.assertEqual(first.sequence_offsets, (0, 1, 2, 3, 4))
        self.assertEqual(first.timestamp_offsets, (0, 192, 384, 544, 736))
        self.assertEqual(first.ssrc_segments, (0, 0, 0, 0, 0))
        self.assertEqual(first.marker_positions, (0,))
        self.assertEqual(first.packet_count, 5)
        self.assertEqual(first.duration_samples, 800)
        self.assertEqual(first.duration_ns, 100_000_000)
        self.assertTrue(first.constant_ssrc)
        self.assertEqual(first.payload_sha256, hashlib.sha256(payload).hexdigest())
        self.assertEqual(
            self.module.normalized_rtp_differences(first, second), ()
        )

    def test_equal_payload_histograms_with_different_order_do_not_compare_equal(self):
        payload = bytes((index * 11) % 256 for index in range(608))
        first = self.module.normalize_pcma_rtp_packets(
            self.make_stream(payload, [192, 160, 192, 64])
        )
        reordered = self.module.normalize_pcma_rtp_packets(
            self.make_stream(payload, [192, 192, 160, 64])
        )

        self.assertEqual(sorted(first.payload_lengths), sorted(reordered.payload_lengths))
        self.assertEqual(first.payload_sha256, reordered.payload_sha256)
        self.assertNotEqual(first, reordered)
        self.assertEqual(
            self.module.normalized_rtp_differences(first, reordered),
            ("payload_lengths", "timestamp_offsets"),
        )

    def test_payload_type_difference_is_not_normalized_away(self):
        payload = b"abcdef"
        expected = self.module.normalize_pcma_rtp_packets(
            self.make_stream(payload, [2, 2, 2], payload_types=[8, 8, 8])
        )
        wrong_payload_type = self.module.normalize_pcma_rtp_packets(
            self.make_stream(payload, [2, 2, 2], payload_types=[0, 0, 0])
        )

        self.assertEqual(
            self.module.normalized_rtp_differences(
                expected, wrong_payload_type
            ),
            ("payload_types",),
        )

    def test_distinct_ssrc_change_positions_are_not_normalized_away(self):
        payload = b"abcdefgh"
        expected = self.module.normalize_pcma_rtp_packets(
            self.make_stream(
                payload,
                [2, 2, 2, 2],
                ssrcs=[10, 10, 20, 20],
            )
        )
        same_segments = self.module.normalize_pcma_rtp_packets(
            self.make_stream(
                payload,
                [2, 2, 2, 2],
                ssrcs=[1_000, 1_000, 2_000, 2_000],
            )
        )
        wrong_segments = self.module.normalize_pcma_rtp_packets(
            self.make_stream(
                payload,
                [2, 2, 2, 2],
                ssrcs=[100, 200, 200, 200],
            )
        )

        self.assertFalse(expected.constant_ssrc)
        self.assertEqual(expected, same_segments)
        self.assertFalse(wrong_segments.constant_ssrc)
        self.assertEqual(expected.ssrc_segments, (0, 0, 1, 1))
        self.assertEqual(wrong_segments.ssrc_segments, (0, 1, 1, 1))
        self.assertEqual(
            self.module.normalized_rtp_differences(expected, wrong_segments),
            ("ssrc_segments",),
        )

    def test_comparison_reports_every_required_nonbase_mismatch(self):
        payload = b"abcdef"
        sizes = [2, 2, 2]
        expected = self.module.normalize_pcma_rtp_packets(
            self.make_stream(payload, sizes)
        )
        cases = {
            "sequence_offsets": self.make_stream(
                payload, sizes, sequence_offsets=[0, 1, 3]
            ),
            "timestamp_offsets": self.make_stream(
                payload, sizes, timestamp_offsets=[0, 2, 5]
            ),
            "marker_positions": self.make_stream(
                payload, sizes, markers=(0, 1)
            ),
            "packet_count": self.make_stream(payload, [2, 4]),
            "duration_samples": self.make_stream(
                payload, sizes, timestamp_offsets=[0, 2, 5]
            ),
            "constant_ssrc": self.make_stream(
                payload, sizes, ssrcs=[10, 11, 10]
            ),
            "payload_sha256": self.make_stream(b"abcdeg", sizes),
        }

        for field, packets in cases.items():
            with self.subTest(field=field):
                actual = self.module.normalize_pcma_rtp_packets(packets)
                self.assertIn(
                    field,
                    self.module.normalized_rtp_differences(expected, actual),
                )


class PacketPatternManifestTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module("backchannel_rtp")

    @staticmethod
    def write_manifest(path, rows):
        path.write_text(
            "".join(json.dumps(row) + "\n" for row in rows),
            encoding="utf-8",
        )

    def test_loads_complete_ordered_payload_size_sequence(self):
        rows = [
            {"packet_index": index, "payload_size": size, "ignored": "metadata"}
            for index, size in enumerate([192, 192, 160, 192, 64])
        ]
        with tempfile.TemporaryDirectory() as directory:
            manifest = pathlib.Path(directory) / "manifest.jsonl"
            self.write_manifest(manifest, rows)

            self.assertEqual(
                self.module.load_packet_pattern(manifest),
                (192, 192, 160, 192, 64),
            )

    def test_rejects_empty_malformed_and_non_object_manifests(self):
        cases = (
            (b"", "must not be empty"),
            (b"{not json}\n", "malformed JSON on line 1"),
            (b"[]\n", "line 1 must be a JSON object"),
            (b"\xff\n", "UTF-8"),
        )
        with tempfile.TemporaryDirectory() as directory:
            manifest = pathlib.Path(directory) / "manifest.jsonl"
            for contents, message in cases:
                with self.subTest(contents=contents):
                    manifest.write_bytes(contents)
                    with self.assertRaisesRegex(ValueError, message):
                        self.module.load_packet_pattern(manifest)

    def test_requires_zero_based_contiguous_integral_packet_indices(self):
        cases = (
            ([{"packet_index": 1, "payload_size": 1}], "expected 0"),
            (
                [
                    {"packet_index": 0, "payload_size": 1},
                    {"packet_index": 2, "payload_size": 1},
                ],
                "expected 1",
            ),
            ([{"packet_index": True, "payload_size": 1}], "integer"),
            ([{"packet_index": 0.0, "payload_size": 1}], "integer"),
        )
        with tempfile.TemporaryDirectory() as directory:
            manifest = pathlib.Path(directory) / "manifest.jsonl"
            for rows, message in cases:
                with self.subTest(rows=rows):
                    self.write_manifest(manifest, rows)
                    with self.assertRaisesRegex(ValueError, message):
                        self.module.load_packet_pattern(manifest)

    def test_requires_positive_integral_payload_sizes(self):
        for payload_size in (0, -1, 1.5, True, "160", None):
            with self.subTest(payload_size=payload_size), tempfile.TemporaryDirectory() as directory:
                manifest = pathlib.Path(directory) / "manifest.jsonl"
                self.write_manifest(
                    manifest,
                    [{"packet_index": 0, "payload_size": payload_size}],
                )
                with self.assertRaisesRegex(ValueError, "payload_size"):
                    self.module.load_packet_pattern(manifest)

    def test_rejects_nonregular_and_descriptor_oversized_files_before_reading(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = pathlib.Path(directory)
            with self.assertRaisesRegex(ValueError, "regular file"):
                self.module.load_packet_pattern(directory)

            manifest = directory / "manifest.jsonl"
            manifest.write_bytes(b"1234")
            with mock.patch.object(
                self.module.os,
                "fdopen",
                side_effect=AssertionError("oversized manifest was read"),
            ), self.assertRaisesRegex(ValueError, "exceeds 3 byte limit"):
                self.module.load_packet_pattern(manifest, max_bytes=3)

    def test_fifo_replacement_race_fails_without_blocking_or_leaking_fd(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("FIFO replacement requires mkfifo")

        with tempfile.TemporaryDirectory() as directory:
            manifest = pathlib.Path(directory) / "manifest.jsonl"
            self.write_manifest(
                manifest,
                [{"packet_index": 0, "payload_size": 1}],
            )
            original_path_stat = pathlib.Path.stat
            original_os_open = os.open
            replaced = False
            replacement_lock = threading.Lock()
            outcomes = []

            def replace_with_fifo():
                nonlocal replaced
                with replacement_lock:
                    if replaced:
                        return
                    manifest.unlink()
                    os.mkfifo(manifest)
                    replaced = True

            def racing_stat(path, *args, **kwargs):
                metadata = original_path_stat(path, *args, **kwargs)
                if pathlib.Path(path) == manifest:
                    replace_with_fifo()
                return metadata

            def racing_open(path, flags, *args, **kwargs):
                if pathlib.Path(path) == manifest:
                    replace_with_fifo()
                return original_os_open(path, flags, *args, **kwargs)

            def run_loader():
                try:
                    outcomes.append(self.module.load_packet_pattern(manifest))
                except BaseException as error:
                    outcomes.append(error)

            with mock.patch.object(
                pathlib.Path, "stat", new=racing_stat
            ), mock.patch.object(
                self.module.os, "open", side_effect=racing_open
            ):
                loader = threading.Thread(target=run_loader, daemon=True)
                loader.start()
                loader.join(1)
                blocked = loader.is_alive()
                if blocked:
                    writer = original_os_open(
                        manifest, os.O_WRONLY | os.O_NONBLOCK
                    )
                    try:
                        os.write(
                            writer,
                            b'{"packet_index": 0, "payload_size": 1}\n',
                        )
                    finally:
                        os.close(writer)
                    loader.join(1)

            self.assertFalse(blocked, "packet pattern open blocked on a FIFO")
            self.assertFalse(loader.is_alive(), "blocked loader did not exit")
            self.assertEqual(len(outcomes), 1)
            self.assertIsInstance(outcomes[0], ValueError)
            self.assertIn("regular file", str(outcomes[0]))
            with self.assertRaises(OSError) as raised:
                original_os_open(manifest, os.O_WRONLY | os.O_NONBLOCK)
            self.assertEqual(raised.exception.errno, errno.ENXIO)

    def test_enforces_line_row_cumulative_and_rtp_packet_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = pathlib.Path(directory) / "manifest.jsonl"
            self.write_manifest(
                manifest,
                [
                    {"packet_index": 0, "payload_size": 1},
                    {"packet_index": 1, "payload_size": 1},
                ],
            )
            with self.assertRaisesRegex(ValueError, "line 1 exceeds"):
                self.module.load_packet_pattern(manifest, max_line_bytes=16)
            with self.assertRaisesRegex(ValueError, "row count exceeds 1"):
                self.module.load_packet_pattern(manifest, max_rows=1)
            with mock.patch.object(
                self.module.os,
                "fstat",
                return_value=type(
                    "Metadata",
                    (),
                    {"st_mode": stat.S_IFREG, "st_size": 0},
                )(),
            ), self.assertRaisesRegex(ValueError, "exceeds 40 byte limit"):
                self.module.load_packet_pattern(manifest, max_bytes=40)

            self.write_manifest(
                manifest,
                [{"packet_index": 0, "payload_size": 21}],
            )
            with self.assertRaisesRegex(ValueError, "RTP packet size 33"):
                self.module.load_packet_pattern(
                    manifest,
                    max_rtp_packet_size=32,
                )


class RtpPacerTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module("backchannel_rtp")
        self.pacer_type = self.module.RtpPacer

    def make_pacer(self, clock, mode="rebase", sample_rate=8000):
        return self.pacer_type(
            sample_rate,
            mode=mode,
            monotonic_ns=clock.monotonic_ns,
            sleeper=clock.sleep,
        )

    def test_first_packet_is_immediate_and_fixed_packets_use_absolute_deadlines(self):
        clock = FakePacerClock()
        pacer = self.make_pacer(clock)

        timings = [pacer.wait(160) for _ in range(4)]

        self.assertEqual(
            [timing.target_monotonic_ns - clock.start_ns for timing in timings],
            [0, 20_000_000, 40_000_000, 60_000_000],
        )
        self.assertEqual(
            [timing.actual_monotonic_ns - clock.start_ns for timing in timings],
            [0, 20_000_000, 40_000_000, 60_000_000],
        )
        self.assertEqual(
            [timing.interval_ns for timing in timings],
            [None, 20_000_000, 20_000_000, 20_000_000],
        )
        self.assertEqual(clock.sleeps, [0.02, 0.02, 0.02])

    def test_repeated_small_oversleeps_do_not_accumulate_drift(self):
        clock = FakePacerClock(overshoot_ns=1_000_000)
        pacer = self.make_pacer(clock)

        timings = [pacer.wait(160) for _ in range(5)]

        self.assertEqual(
            [timing.target_monotonic_ns - clock.start_ns for timing in timings],
            [0, 20_000_000, 40_000_000, 60_000_000, 80_000_000],
        )
        self.assertEqual(
            [timing.actual_monotonic_ns - timing.target_monotonic_ns for timing in timings],
            [0, 1_000_000, 1_000_000, 1_000_000, 1_000_000],
        )
        self.assertAlmostEqual(clock.sleeps[0], 0.02)
        for sleep in clock.sleeps[1:]:
            self.assertAlmostEqual(sleep, 0.019)

    def test_legacy_catches_up_immediately_after_packet_1500_oversleep(self):
        clock = FakePacerClock(
            inject_on_sleep=1500, injected_overshoot_ns=45_000_000
        )
        pacer = self.make_pacer(clock, mode="legacy")

        timings = [pacer.wait(160) for _ in range(1503)]

        self.assertEqual(timings[1500].lateness_ns, 45_000_000)
        self.assertFalse(timings[1500].rebased)
        self.assertEqual(timings[1501].interval_ns, 0)
        self.assertEqual(timings[1502].interval_ns, 0)

    def test_rebase_prevents_immediate_catch_up_after_packet_1500_oversleep(self):
        clock = FakePacerClock(
            inject_on_sleep=1500, injected_overshoot_ns=45_000_000
        )
        pacer = self.make_pacer(clock, mode="rebase")

        timings = [pacer.wait(160) for _ in range(1503)]

        self.assertTrue(timings[1500].rebased)
        self.assertEqual(
            timings[1500].target_monotonic_ns - clock.start_ns,
            30_000_000_000,
        )
        self.assertEqual(timings[1500].lateness_ns, 45_000_000)
        self.assertEqual(
            timings[1500].actual_monotonic_ns
            - timings[1500].target_monotonic_ns,
            45_000_000,
        )
        self.assertEqual(timings[1501].interval_ns, 20_000_000)
        self.assertEqual(timings[1502].interval_ns, 20_000_000)

    def test_variable_packet_rebase_uses_duration_to_next_deadline(self):
        clock = FakePacerClock(
            inject_on_sleep=1, injected_overshoot_ns=10_000_000
        )
        pacer = self.make_pacer(clock, mode="rebase")
        packetizer = self.module.RtpPacketizer(
            8, ssrc=1, sequence=2, timestamp=0
        )

        samples = (160, 40, 160)
        timings = []
        timestamps = []
        for packet_samples in samples:
            timings.append(pacer.wait(packet_samples))
            timestamps.append(
                parse_rtp_packet(
                    packetizer.build(b"x", packet_samples)
                ).timestamp
            )

        self.assertEqual(
            [timing.target_monotonic_ns - clock.start_ns for timing in timings],
            [0, 20_000_000, 35_000_000],
        )
        self.assertEqual(
            [timing.lateness_ns for timing in timings],
            [0, 10_000_000, 0],
        )
        self.assertEqual(
            [timing.interval_ns for timing in timings],
            [None, 30_000_000, 5_000_000],
        )
        self.assertEqual(
            [timing.rebased for timing in timings],
            [False, True, False],
        )
        self.assertEqual(timestamps, [0, 160, 200])
        self.assertEqual(packetizer.timestamp, 360)

    def test_variable_tail_finish_waits_exact_final_media_duration(self):
        for mode in ("legacy", "rebase"):
            with self.subTest(mode=mode):
                clock = FakePacerClock()
                pacer = self.make_pacer(clock, mode=mode)

                first = pacer.wait(160)
                tail = pacer.wait(40)
                finished_ns = pacer.finish()

                self.assertEqual(first.actual_monotonic_ns, clock.start_ns)
                self.assertEqual(tail.actual_monotonic_ns - clock.start_ns, 20_000_000)
                self.assertEqual(finished_ns - clock.start_ns, 25_000_000)
                self.assertAlmostEqual(clock.sleeps[-1], 0.005)

    def test_wall_clock_rebase_never_changes_rtp_timestamp_progression_or_wrap(self):
        clock = FakePacerClock(
            inject_on_sleep=1, injected_overshoot_ns=45_000_000
        )
        pacer = self.make_pacer(clock, mode="rebase")
        packetizer = self.module.RtpPacketizer(
            8, ssrc=1, sequence=2, timestamp=0xFFFFFF60
        )

        timestamps = []
        for _ in range(4):
            pacer.wait(160)
            timestamps.append(parse_rtp_packet(packetizer.build(b"x", 160)).timestamp)

        self.assertEqual(timestamps, [0xFFFFFF60, 0, 160, 320])
        self.assertEqual(packetizer.timestamp, 480)

    def test_rejects_invalid_rates_samples_modes_and_dependencies(self):
        clock = FakePacerClock()
        for rate in (0, -1, 8000.0, True):
            with self.subTest(rate=rate), self.assertRaises((TypeError, ValueError)):
                self.pacer_type(rate)
        for mode in ("adaptive", "", None):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                self.pacer_type(8000, mode=mode)
        with self.assertRaises(TypeError):
            self.pacer_type(8000, monotonic_ns=1)
        with self.assertRaises(TypeError):
            self.pacer_type(8000, sleeper=1)

        pacer = self.make_pacer(clock)
        for samples in (0, -1, 1.5, True, 1 << 32):
            with self.subTest(samples=samples), self.assertRaises((TypeError, ValueError)):
                pacer.wait(samples)

    def test_clock_moving_backward_fails_explicitly(self):
        readings = iter((100, 99))
        pacer = self.pacer_type(
            8000,
            monotonic_ns=lambda: next(readings),
            sleeper=lambda _seconds: None,
        )

        pacer.wait(160)
        with self.assertRaisesRegex(RuntimeError, "monotonic clock moved backward"):
            pacer.wait(160)


class RunRtpPacerToolTests(unittest.TestCase):
    def setUp(self):
        self.runner = importlib.import_module("tools.run_rtp_pacer")

    def test_run_writes_fixed_sample_timestamps_and_injection_marker(self):
        clock = FakePacerClock()
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "timing.jsonl"
            rows = self.runner.run_pacer(
                duration=0.08,
                sample_rate=8000,
                packet_samples=160,
                mode="rebase",
                inject_after_packet=0,
                inject_ms=45,
                output=output,
                monotonic_ns=clock.monotonic_ns,
                sleeper=clock.sleep,
            )
            published = [json.loads(line) for line in output.read_text().splitlines()]

        self.assertEqual(published, rows)
        self.assertEqual([row["packet_index"] for row in rows], [0, 1, 2, 3])
        self.assertEqual([row["rtp_timestamp"] for row in rows], [0, 160, 320, 480])
        self.assertEqual([row["samples"] for row in rows], [160] * 4)
        self.assertEqual(rows[1]["injected_after_packet"], 0)
        self.assertEqual(rows[1]["injected_oversleep_ns"], 45_000_000)
        self.assertTrue(rows[1]["rebased"])
        self.assertEqual(rows[2]["interval_ns"], 20_000_000)
        self.assertEqual(clock.now_ns - clock.start_ns, 125_000_000)

    def test_legacy_runner_records_catch_up_after_injection(self):
        clock = FakePacerClock()
        with tempfile.TemporaryDirectory() as directory:
            rows = self.runner.run_pacer(
                duration=0.08,
                sample_rate=8000,
                packet_samples=160,
                mode="legacy",
                inject_after_packet=0,
                inject_ms=45,
                output=pathlib.Path(directory) / "timing.jsonl",
                monotonic_ns=clock.monotonic_ns,
                sleeper=clock.sleep,
            )

        self.assertEqual(rows[1]["lateness_ns"], 45_000_000)
        self.assertEqual(rows[2]["interval_ns"], 0)

    def test_run_rejects_bounds_and_injection_pairs_before_publishing(self):
        cases = (
            ({"duration": 0}, "duration"),
            ({"duration": 21601}, "duration"),
            ({"sample_rate": 0}, "sample_rate"),
            ({"packet_samples": 0}, "packet_samples"),
            ({"duration": 1, "sample_rate": 1_000_001, "packet_samples": 1}, "packet count"),
            ({"inject_after_packet": 0, "inject_ms": 0}, "inject_ms"),
            ({"inject_after_packet": None, "inject_ms": 1}, "inject-after"),
            ({"inject_after_packet": -1, "inject_ms": 1}, "inject-after"),
            ({"inject_after_packet": 4, "inject_ms": 1}, "inject-after"),
            ({"inject_after_packet": 0, "inject_ms": 60_001}, "inject_ms"),
        )
        defaults = {
            "duration": 0.08,
            "sample_rate": 8000,
            "packet_samples": 160,
            "mode": "legacy",
            "inject_after_packet": None,
            "inject_ms": 0,
        }
        for overrides, message in cases:
            with self.subTest(overrides=overrides), tempfile.TemporaryDirectory() as directory:
                output = pathlib.Path(directory) / "timing.jsonl"
                output.write_text("stale\n")
                arguments = {**defaults, **overrides, "output": output}
                with self.assertRaisesRegex(ValueError, message):
                    self.runner.run_pacer(**arguments)
                self.assertFalse(output.exists())

    def test_run_rejects_ceil_padded_media_duration_above_six_hours(self):
        clock = FakePacerClock()
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "timing.jsonl"
            output.write_text("stale\n")

            with self.assertRaisesRegex(
                ValueError, "represented media duration exceeds 21600 seconds"
            ):
                self.runner.run_pacer(
                    duration=1,
                    sample_rate=1,
                    packet_samples=0xFFFFFFFF,
                    mode="rebase",
                    inject_after_packet=None,
                    inject_ms=0,
                    output=output,
                    monotonic_ns=clock.monotonic_ns,
                    sleeper=clock.sleep,
                )

            self.assertEqual(clock.sleeps, [])
            self.assertFalse(output.exists())

    def test_run_allows_ceil_padded_media_duration_at_six_hour_boundary(self):
        clock = FakePacerClock()
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "timing.jsonl"
            rows = self.runner.run_pacer(
                duration=10_800.1,
                sample_rate=1,
                packet_samples=10_800,
                mode="rebase",
                inject_after_packet=None,
                inject_ms=0,
                output=output,
                monotonic_ns=clock.monotonic_ns,
                sleeper=clock.sleep,
            )

            self.assertEqual(len(rows), 2)
            self.assertEqual(
                clock.now_ns - clock.start_ns,
                self.runner.MAX_DURATION_SECONDS * 1_000_000_000,
            )
            self.assertTrue(output.exists())

    def test_run_failure_removes_stale_output_and_temporary_file(self):
        def failing_sleep(_seconds):
            raise RuntimeError("sleep failed")

        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "timing.jsonl"
            output.write_text("stale\n")
            with self.assertRaisesRegex(RuntimeError, "sleep failed"):
                self.runner.run_pacer(
                    duration=0.04,
                    sample_rate=8000,
                    packet_samples=160,
                    mode="legacy",
                    inject_after_packet=None,
                    inject_ms=0,
                    output=output,
                    sleeper=failing_sleep,
                )

            self.assertFalse(output.exists())
            self.assertFalse(list(output.parent.glob(f".{output.name}.*.tmp")))

    def test_runner_limits_match_memory_bounded_summarizer_capacity(self):
        summarizer = importlib.import_module("tools.summarize_send_timing")

        self.assertLessEqual(self.runner.MAX_PACKET_COUNT, 10_000)
        self.assertEqual(
            self.runner.MAX_PACKET_COUNT, summarizer.MAX_TIMING_ROWS
        )
        self.assertEqual(
            getattr(self.runner, "MAX_TIMING_LINE_BYTES", None),
            summarizer.MAX_TIMING_LINE_BYTES,
        )
        self.assertEqual(
            getattr(self.runner, "MAX_TIMING_OUTPUT_BYTES", None),
            summarizer.MAX_TIMING_BYTES,
        )
        self.assertEqual(
            self.runner.MAX_TIMING_OUTPUT_BYTES,
            self.runner.MAX_PACKET_COUNT * self.runner.MAX_TIMING_LINE_BYTES,
        )

    def test_runner_enforces_serialized_row_bound_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            self.runner, "MAX_TIMING_LINE_BYTES", 64, create=True
        ):
            output = pathlib.Path(directory) / "timing.jsonl"
            with self.assertRaisesRegex(ValueError, "JSONL line 1 exceeds 64 bytes"):
                self.runner.run_pacer(
                    duration=0.02,
                    sample_rate=8000,
                    packet_samples=160,
                    mode="rebase",
                    inject_after_packet=None,
                    inject_ms=0,
                    output=output,
                )

            self.assertFalse(output.exists())
            self.assertFalse(list(output.parent.glob(f".{output.name}.*.tmp")))

    def test_direct_help_runs_without_pythonpath(self):
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        completed = subprocess.run(
            [sys.executable, str(RUN_PACER_TOOL), "--help"],
            cwd=ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--inject-after-packet", completed.stdout)
        self.assertIn("--pacer", completed.stdout)


class SummarizeSendTimingToolTests(unittest.TestCase):
    def setUp(self):
        self.summarizer = importlib.import_module("tools.summarize_send_timing")
        self.runner = importlib.import_module("tools.run_rtp_pacer")

    def make_rows(self, directory, mode):
        clock = FakePacerClock()
        return self.runner.run_pacer(
            duration=0.08,
            sample_rate=8000,
            packet_samples=160,
            mode=mode,
            inject_after_packet=0,
            inject_ms=45,
            output=pathlib.Path(directory) / f"{mode}.jsonl",
            monotonic_ns=clock.monotonic_ns,
            sleeper=clock.sleep,
        )

    @staticmethod
    def decreasing_timing_rows(interval_ns):
        return [
            {
                "packet_index": 0,
                "rtp_timestamp": 0,
                "samples": 160,
                "target_monotonic_ns": 100,
                "actual_monotonic_ns": 100,
                "lateness_ns": 0,
                "interval_ns": None,
                "rebased": False,
            },
            {
                "packet_index": 1,
                "rtp_timestamp": 160,
                "samples": 160,
                "target_monotonic_ns": 99,
                "actual_monotonic_ns": 99,
                "lateness_ns": 0,
                "interval_ns": interval_ns,
                "rebased": False,
            },
        ]

    @staticmethod
    def task5_row(index, target_ns, actual_ns, interval_ns, *, rebased=False):
        return {
            "packet_index": index,
            "rtp_timestamp": index * 160,
            "samples": 160,
            "sample_rate": 8000,
            "packet_duration_ns": 20_000_000,
            "target_monotonic_ns": target_ns,
            "actual_monotonic_ns": actual_ns,
            "lateness_ns": actual_ns - target_ns,
            "interval_ns": interval_ns,
            "rebased": rebased,
        }

    def test_rejects_decreasing_actual_send_timestamps(self):
        rows = self.decreasing_timing_rows(interval_ns=0)

        with self.assertRaisesRegex(
            ValueError, "actual_monotonic_ns must be nondecreasing"
        ):
            self.summarizer.summarize_timing(rows)

    def test_rejects_negative_interval_even_when_timestamp_difference_matches(self):
        rows = self.decreasing_timing_rows(interval_ns=-1)

        with self.assertRaisesRegex(ValueError, "interval_ns must be nonnegative"):
            self.summarizer.summarize_timing(rows)

    def test_rejects_forged_ten_millisecond_target_progression(self):
        rows = [
            self.task5_row(
                index,
                index * 10_000_000,
                index * 10_000_000,
                None if index == 0 else 10_000_000,
            )
            for index in range(4)
        ]

        summary = self.summarizer.summarize_timing(rows)

        self.assertEqual(
            summary["unexpected_target_deadlines"],
            [
                {
                    "packet_index": index,
                    "expected": index * 10_000_000 + 10_000_000,
                    "actual": index * 10_000_000,
                    "previous_packet_rebased": False,
                }
                for index in range(1, 4)
            ],
        )
        self.assertFalse(
            summary["checks"]["no_unexpected_target_deadlines"]
        )
        self.assertFalse(summary["pass"])

    def test_rejects_rebase_flag_without_severe_observed_lateness(self):
        rows = [
            self.task5_row(0, 0, 0, None),
            self.task5_row(
                1,
                20_000_000,
                30_000_000,
                30_000_000,
                rebased=True,
            ),
            self.task5_row(2, 50_000_000, 50_000_000, 20_000_000),
        ]

        summary = self.summarizer.summarize_timing(
            rows,
            gst_summary={"inter_arrival_ns": {"p99": 10_000_000}},
        )

        self.assertEqual(
            summary["incoherent_rebase_flags"],
            [
                {
                    "packet_index": 1,
                    "expected": False,
                    "actual": True,
                    "lateness_ns": 10_000_000,
                }
            ],
        )
        self.assertTrue(
            summary["checks"]["no_unexpected_target_deadlines"]
        )
        self.assertFalse(
            summary["checks"]["no_incoherent_rebase_flags"]
        )
        self.assertFalse(summary["pass"])

    def test_rejects_missing_rebase_flag_after_severe_observed_lateness(self):
        rows = [
            self.task5_row(0, 0, 0, None),
            self.task5_row(1, 20_000_000, 40_000_000, 40_000_000),
            self.task5_row(2, 40_000_000, 55_000_000, 15_000_000),
        ]

        summary = self.summarizer.summarize_timing(
            rows,
            gst_summary={"inter_arrival_ns": {"p99": 20_000_000}},
        )

        self.assertEqual(
            summary["incoherent_rebase_flags"],
            [
                {
                    "packet_index": 1,
                    "expected": True,
                    "actual": False,
                    "lateness_ns": 20_000_000,
                }
            ],
        )
        self.assertTrue(
            summary["checks"]["no_unexpected_target_deadlines"]
        )
        self.assertFalse(
            summary["checks"]["no_incoherent_rebase_flags"]
        )
        self.assertFalse(summary["pass"])

    def test_rejects_rebase_flag_on_anchor_row(self):
        rows = [
            self.task5_row(
                0,
                0,
                20_000_000,
                None,
                rebased=True,
            ),
            self.task5_row(1, 40_000_000, 40_000_000, 20_000_000),
        ]

        summary = self.summarizer.summarize_timing(
            rows,
            gst_summary={"inter_arrival_ns": {"p99": 20_000_000}},
        )

        self.assertEqual(
            summary["incoherent_rebase_flags"],
            [
                {
                    "packet_index": 0,
                    "expected": False,
                    "actual": True,
                    "lateness_ns": 20_000_000,
                }
            ],
        )
        self.assertTrue(
            summary["checks"]["no_unexpected_target_deadlines"]
        )
        self.assertFalse(
            summary["checks"]["no_incoherent_rebase_flags"]
        )
        self.assertFalse(summary["pass"])

    def test_summary_reports_required_metrics_and_observed_rebase_lateness(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = self.make_rows(directory, "rebase")
            summary = self.summarizer.summarize_timing(rows)

        self.assertEqual(summary["packet_count"], 4)
        self.assertEqual(summary["rtp_timestamp_delta_histogram"], {"160": 3})
        self.assertEqual(
            summary["interval_ns"],
            {"min": 20_000_000, "p50": 20_000_000, "p95": 65_000_000,
             "p99": 65_000_000, "max": 65_000_000},
        )
        self.assertEqual(
            summary["absolute_deadline_error_ns"],
            {"p50": 0, "p95": 45_000_000, "p99": 45_000_000,
             "max": 45_000_000},
        )
        self.assertEqual(summary["rebase_count"], 1)
        self.assertEqual(summary["rebase_indexes"], [1])
        self.assertEqual(summary["injected_interval_ns"]["p50"], 65_000_000)
        self.assertEqual(summary["post_oversleep_interval_ns"]["p50"], 20_000_000)
        self.assertEqual(
            summary["checks"],
            {
                "all_sample_rates_match_expected": True,
                "all_packet_samples_match_expected": True,
                "all_packet_durations_match_expected": True,
                "no_unexpected_target_deadlines": True,
                "no_incoherent_rebase_flags": True,
                "no_unexpected_timestamp_deltas": True,
                "no_post_severe_interval_below_75_percent": True,
                "p99_deadline_error_within_bound": False,
            },
        )
        self.assertEqual(summary["expected_sample_rate"], 8000)
        self.assertEqual(summary["expected_samples"], 160)
        self.assertEqual(summary["expected_packet_duration_ns"], 20_000_000)
        self.assertEqual(summary["severe_lateness_threshold_ns"], 20_000_000)
        self.assertEqual(
            summary["post_severe_minimum_interval_ns"], 15_000_000
        )
        self.assertEqual(summary["deadline_error_limit_ns"], 2_000_000)
        self.assertFalse(summary["pass"])

    def test_legacy_summary_exposes_catch_up_and_deadline_error_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = self.make_rows(directory, "legacy")
            summary = self.summarizer.summarize_timing(rows)

        self.assertEqual(summary["rebase_count"], 0)
        self.assertEqual(summary["post_oversleep_interval_ns"]["min"], 0)
        self.assertFalse(
            summary["checks"]["no_post_severe_interval_below_75_percent"]
        )
        self.assertFalse(summary["checks"]["p99_deadline_error_within_bound"])
        self.assertFalse(summary["pass"])

    def test_timestamp_mismatch_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = self.make_rows(directory, "rebase")
        rows[2]["rtp_timestamp"] += 1

        summary = self.summarizer.summarize_timing(rows)

        self.assertEqual(summary["deadline_error_limit_ns"], 2_000_000)
        self.assertEqual(
            summary["unexpected_timestamp_deltas"],
            [
                {"packet_index": 2, "expected": 160, "actual": 161},
                {"packet_index": 3, "expected": 160, "actual": 159},
            ],
        )
        self.assertFalse(summary["checks"]["no_unexpected_timestamp_deltas"])

    def test_auxiliary_jitter_bounds_cannot_widen_acceptance_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = self.make_rows(directory, "rebase")
        for row in rows:
            row["configured_jitter_bound_ns"] = 100_000_000

        without_gst = self.summarizer.summarize_timing(rows)
        with_gst = self.summarizer.summarize_timing(
            rows,
            gst_summary={
                "inter_arrival_ns": {"p99": 9_000_000},
                "deadline_error_jitter_bound_ns": 100_000_000,
            },
        )
        with_sub_floor_gst = self.summarizer.summarize_timing(
            rows,
            gst_summary={
                "inter_arrival_ns": {"p99": 500_000},
                "deadline_error_jitter_bound_ns": 100_000_000,
            },
        )

        self.assertEqual(without_gst["deadline_error_limit_ns"], 2_000_000)
        self.assertFalse(
            without_gst["checks"]["p99_deadline_error_within_bound"]
        )
        self.assertEqual(with_gst["deadline_error_limit_ns"], 10_000_000)
        self.assertFalse(with_gst["checks"]["p99_deadline_error_within_bound"])
        self.assertEqual(
            with_sub_floor_gst["deadline_error_limit_ns"], 2_000_000
        )
        self.assertFalse(
            with_sub_floor_gst["checks"]["p99_deadline_error_within_bound"]
        )

    def test_gst_inter_arrival_p99_sets_required_bound_with_semantic_caveat(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = self.make_rows(directory, "rebase")
            gst_path = pathlib.Path(directory) / "gst-summary.json"
            gst_path.write_text(json.dumps({
                "packet_count": 4,
                "inter_arrival_ns": {"p99": 49_244_216.34},
            }))
            gst = self.summarizer.load_gst_summary(gst_path)
            summary = self.summarizer.summarize_timing(rows, gst_summary=gst)

        self.assertEqual(summary["deadline_error_limit_ns"], 50_244_216.34)
        self.assertEqual(
            summary["gst_reference"]["acceptance_metric"],
            "inter_arrival_ns.p99 + 1000000ns",
        )
        self.assertEqual(
            summary["gst_reference"]["comparison"], "semantic_caveat"
        )
        self.assertIn("inter-arrival", summary["gst_reference"]["note"])
        self.assertIn("deadline-error", summary["gst_reference"]["note"])
        self.assertTrue(summary["checks"]["p99_deadline_error_within_bound"])
        self.assertTrue(summary["pass"])

    def test_gst_summary_requires_finite_nonnegative_inter_arrival_p99(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = self.make_rows(directory, "rebase")

        for p99 in (None, True, -1, float("nan"), float("inf")):
            with self.subTest(p99=p99), self.assertRaisesRegex(
                ValueError, "inter_arrival_ns.p99"
            ):
                self.summarizer.summarize_timing(
                    rows,
                    gst_summary={"inter_arrival_ns": {"p99": p99}},
                )

    def test_cli_rejects_overflowing_gst_p99_without_traceback_or_output(self):
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        with tempfile.TemporaryDirectory() as directory:
            directory = pathlib.Path(directory)
            self.make_rows(directory, "rebase")
            timing = directory / "rebase.jsonl"
            gst_summary = directory / "gst-summary.json"
            gst_summary.write_text(
                '{"inter_arrival_ns":{"p99":' + "9" * 400 + "}}"
            )
            output = directory / "summary.json"
            output.write_text("stale\n")

            completed = subprocess.run(
                [
                    sys.executable,
                    str(SUMMARIZE_TIMING_TOOL),
                    "--input", str(timing),
                    "--gst-summary", str(gst_summary),
                    "--output", str(output),
                ],
                cwd=ROOT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 1)
            self.assertIn(
                "inter_arrival_ns.p99 must be a finite nonnegative number",
                completed.stderr,
            )
            self.assertNotIn("Traceback", completed.stderr)
            self.assertFalse(output.exists())
            self.assertFalse(
                list(output.parent.glob(f".{output.name}.*.tmp"))
            )

    def test_fixed_samples_reject_self_declared_delta_and_cannot_be_overridden(
        self,
    ):
        clock = FakePacerClock()
        with tempfile.TemporaryDirectory() as directory:
            rows = self.runner.run_pacer(
                duration=0.04,
                sample_rate=8000,
                packet_samples=80,
                mode="rebase",
                inject_after_packet=None,
                inject_ms=0,
                output=pathlib.Path(directory) / "timing.jsonl",
                monotonic_ns=clock.monotonic_ns,
                sleeper=clock.sleep,
            )

        summary = self.summarizer.summarize_timing(rows)

        self.assertEqual(summary["expected_samples"], 160)
        self.assertEqual(
            summary["unexpected_packet_samples"],
            [
                {"packet_index": index, "expected": 160, "actual": 80}
                for index in range(4)
            ],
        )
        self.assertEqual(
            summary["unexpected_timestamp_deltas"],
            [
                {"packet_index": index, "expected": 160, "actual": 80}
                for index in range(1, 4)
            ],
        )
        self.assertFalse(
            summary["checks"]["all_packet_samples_match_expected"]
        )
        self.assertFalse(summary["checks"]["no_unexpected_timestamp_deltas"])
        self.assertFalse(summary["pass"])

        with self.assertRaises(TypeError):
            self.summarizer.summarize_timing(rows, expected_samples=80)

    def test_fixed_profile_rejects_rate_and_packet_duration_mismatches(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = self.make_rows(directory, "rebase")
        gst_summary = {"inter_arrival_ns": {"p99": 49_000_000}}

        for row in rows:
            row["sample_rate"] = 16_000
            row["packet_duration_ns"] = 20_000_000
        wrong_rate = self.summarizer.summarize_timing(
            rows, gst_summary=gst_summary
        )

        for row in rows:
            row["sample_rate"] = 8_000
            row["packet_duration_ns"] = 10_000_000
        wrong_duration = self.summarizer.summarize_timing(
            rows, gst_summary=gst_summary
        )

        self.assertFalse(wrong_rate["checks"]["all_sample_rates_match_expected"])
        self.assertEqual(len(wrong_rate["unexpected_sample_rates"]), 4)
        self.assertFalse(wrong_rate["pass"])
        self.assertFalse(
            wrong_duration["checks"]["all_packet_durations_match_expected"]
        )
        self.assertEqual(len(wrong_duration["unexpected_packet_durations"]), 4)
        self.assertFalse(wrong_duration["pass"])

    def test_severe_and_post_severe_thresholds_ignore_row_metadata(self):
        def make_rows(*, lateness_ns, duration_ns, flagged):
            row_one = {
                "packet_index": 1,
                "rtp_timestamp": 160,
                "samples": 160,
                "sample_rate": 8000,
                "packet_duration_ns": duration_ns,
                "target_monotonic_ns": 20_000_000 - lateness_ns,
                "actual_monotonic_ns": 20_000_000,
                "lateness_ns": lateness_ns,
                "interval_ns": 20_000_000,
                "rebased": flagged,
            }
            if flagged:
                row_one["injected_oversleep_ns"] = 100_000_000
            return [
                {
                    "packet_index": 0,
                    "rtp_timestamp": 0,
                    "samples": 160,
                    "sample_rate": 8000,
                    "packet_duration_ns": 20_000_000,
                    "target_monotonic_ns": 0,
                    "actual_monotonic_ns": 0,
                    "lateness_ns": 0,
                    "interval_ns": None,
                    "rebased": False,
                },
                row_one,
                {
                    "packet_index": 2,
                    "rtp_timestamp": 320,
                    "samples": 160,
                    "sample_rate": 8000,
                    "packet_duration_ns": 20_000_000,
                    "target_monotonic_ns": 34_000_000,
                    "actual_monotonic_ns": 34_000_000,
                    "lateness_ns": 0,
                    "interval_ns": 14_000_000,
                    "rebased": False,
                },
            ]

        severe = self.summarizer.summarize_timing(make_rows(
            lateness_ns=20_000_000,
            duration_ns=1,
            flagged=False,
        ))
        merely_flagged = self.summarizer.summarize_timing(make_rows(
            lateness_ns=0,
            duration_ns=20_000_000,
            flagged=True,
        ))

        self.assertFalse(
            severe["checks"]["no_post_severe_interval_below_75_percent"]
        )
        self.assertEqual(severe["post_oversleep_interval_ns"]["min"], 14_000_000)
        self.assertTrue(
            merely_flagged["checks"][
                "no_post_severe_interval_below_75_percent"
            ]
        )

    def test_cli_atomically_writes_summary_and_direct_help_works(self):
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        with tempfile.TemporaryDirectory() as directory:
            self.make_rows(directory, "rebase")
            timing = pathlib.Path(directory) / "rebase.jsonl"
            gst_summary = pathlib.Path(directory) / "gst-summary.json"
            gst_summary.write_text(json.dumps({
                "packet_count": 4,
                "inter_arrival_ns": {"p99": 49_000_000},
            }))
            output = pathlib.Path(directory) / "summary.json"
            completed = subprocess.run(
                [sys.executable, str(SUMMARIZE_TIMING_TOOL),
                 "--input", str(timing),
                 "--gst-summary", str(gst_summary),
                 "--output", str(output)],
                cwd=ROOT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(json.loads(output.read_text())["pass"])
            self.assertFalse(list(output.parent.glob(f".{output.name}.*.tmp")))

        help_result = subprocess.run(
            [sys.executable, str(SUMMARIZE_TIMING_TOOL), "--help"],
            cwd=ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("--gst-summary", help_result.stdout)
        self.assertNotIn("--expected-samples", help_result.stdout)

    def test_cli_returns_nonzero_after_atomically_writing_failing_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = pathlib.Path(directory)
            self.make_rows(directory, "legacy")
            timing = directory / "legacy.jsonl"
            output = directory / "summary.json"
            output.write_text("stale\n")

            result = self.summarizer.main([
                "--input", str(timing),
                "--output", str(output),
            ])

            self.assertEqual(result, 1)
            summary = json.loads(output.read_text())
            self.assertFalse(summary["pass"])
            self.assertFalse(list(output.parent.glob(f".{output.name}.*.tmp")))

    def test_cli_rejects_output_aliases_before_deleting_inputs(self):
        for option in ("--input", "--gst-summary"):
            for alias_type in ("same", "symlink", "hardlink"):
                with self.subTest(
                    option=option, alias_type=alias_type
                ), tempfile.TemporaryDirectory() as directory:
                    directory = pathlib.Path(directory)
                    self.make_rows(directory, "rebase")
                    timing = directory / "rebase.jsonl"
                    gst_summary = directory / "gst-summary.json"
                    gst_summary.write_text(json.dumps({
                        "packet_count": 4,
                        "inter_arrival_ns": {"p99": 9_000_000},
                    }))
                    source = timing if option == "--input" else gst_summary
                    if alias_type == "same":
                        output = source
                    else:
                        output = directory / f"output-{alias_type}.json"
                        if alias_type == "symlink":
                            output.symlink_to(source)
                        else:
                            output.hardlink_to(source)
                    source_contents = source.read_bytes()
                    output_contents = output.read_bytes()
                    stderr = StringIO()
                    arguments = [
                        "--input", str(timing),
                        "--gst-summary", str(gst_summary),
                        "--output", str(output),
                    ]
                    with redirect_stderr(stderr):
                        result = self.summarizer.main(arguments)

                    self.assertEqual(result, 1)
                    self.assertIn("same file", stderr.getvalue())
                    self.assertTrue(source.exists())
                    self.assertTrue(output.exists())
                    self.assertEqual(source.read_bytes(), source_contents)
                    self.assertEqual(output.read_bytes(), output_contents)

    def test_malformed_and_oversized_jsonl_remove_stale_output(self):
        cases = (
            (b"not-json\n", "malformed JSONL"),
            (b"[]\n", "not a JSON object"),
            (b"{" + b'"x":"' + b"a" * 20_000 + b'"}\n', "line 1 exceeds"),
        )
        for contents, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                timing = pathlib.Path(directory) / "timing.jsonl"
                output = pathlib.Path(directory) / "summary.json"
                timing.write_bytes(contents)
                output.write_text("stale\n")
                stderr = StringIO()
                with redirect_stderr(stderr):
                    result = self.summarizer.main([
                        "--input", str(timing), "--output", str(output)
                    ])

                self.assertEqual(result, 1)
                self.assertIn(message, stderr.getvalue())
                self.assertFalse(output.exists())

    def test_row_count_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "timing.jsonl"
            path.write_text("{}\n{}\n{}\n")
            with mock.patch.object(self.summarizer, "MAX_TIMING_ROWS", 2):
                with self.assertRaisesRegex(ValueError, "row count exceeds 2"):
                    self.summarizer.load_timing_jsonl(path)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX FIFO support")
    def test_cli_rejects_fifos_without_blocking(self):
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        with tempfile.TemporaryDirectory() as directory:
            directory = pathlib.Path(directory)
            self.make_rows(directory, "rebase")
            timing = directory / "rebase.jsonl"

            for option in ("--input", "--gst-summary"):
                with self.subTest(option=option):
                    fifo = directory / f"{option[2:]}.fifo"
                    os.mkfifo(fifo)
                    output = directory / f"{option[2:]}-summary.json"
                    arguments = [
                        sys.executable,
                        str(SUMMARIZE_TIMING_TOOL),
                        "--input",
                        str(fifo if option == "--input" else timing),
                        "--output",
                        str(output),
                    ]
                    if option == "--gst-summary":
                        arguments.extend(["--gst-summary", str(fifo)])
                    try:
                        completed = subprocess.run(
                            arguments,
                            cwd=ROOT,
                            env=environment,
                            check=False,
                            capture_output=True,
                            text=True,
                            timeout=1,
                        )
                    except subprocess.TimeoutExpired:
                        completed = None

                    self.assertIsNotNone(completed, "summarizer blocked on FIFO")
                    self.assertEqual(completed.returncode, 1)
                    self.assertIn("not a regular file", completed.stderr)

    def test_loaders_enforce_cumulative_bytes_after_zero_size_stat(self):
        metadata = mock.Mock(st_mode=stat.S_IFREG | 0o600, st_size=0)
        with tempfile.TemporaryDirectory() as directory:
            directory = pathlib.Path(directory)
            timing = directory / "timing.jsonl"
            timing.write_bytes(b"{}\n" * 4)
            gst_summary = directory / "gst-summary.json"
            gst_summary.write_text('{"inter_arrival_ns":{"p99":1}}')

            cases = (
                (self.summarizer.load_timing_jsonl, timing, "MAX_TIMING_BYTES"),
                (self.summarizer.load_gst_summary, gst_summary,
                 "MAX_GST_SUMMARY_BYTES"),
            )
            for loader, path, limit_name in cases:
                with self.subTest(loader=loader.__name__), mock.patch.object(
                    pathlib.Path, "stat", return_value=metadata
                ), mock.patch.object(
                    self.summarizer.os, "fstat", return_value=metadata
                ), mock.patch.object(
                    self.summarizer, limit_name, 10
                ), self.assertRaisesRegex(ValueError, "exceeds 10 bytes"):
                    loader(path)

    def test_gst_summary_enforces_line_bound_while_reading(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "gst-summary.json"
            path.write_text('{"inter_arrival_ns":{"p99":1}}')

            with mock.patch.object(
                self.summarizer,
                "MAX_GST_SUMMARY_LINE_BYTES",
                8,
            ), self.assertRaisesRegex(
                ValueError, "GST summary line 1 exceeds 8 bytes"
            ):
                self.summarizer.load_gst_summary(path)


class ParseRtpPacketTests(unittest.TestCase):
    def test_parses_fixed_header_and_payload_digest(self):
        payload = b"\x00\x01PCMA payload"
        packet = make_rtp_packet(payload, marker=True)

        meta = parse_rtp_packet(packet)

        self.assertEqual(meta.version, 2)
        self.assertEqual(meta.payload_type, 8)
        self.assertTrue(meta.marker)
        self.assertEqual(meta.sequence, 0x1234)
        self.assertEqual(meta.timestamp, 0x10203040)
        self.assertEqual(meta.ssrc, 0x50607080)
        self.assertEqual(meta.payload_size, len(payload))
        self.assertEqual(meta.payload_sha256, hashlib.sha256(payload).hexdigest())

    def test_parses_csrc_extension_and_padding_without_including_them_in_payload(self):
        payload = b"audio"
        packet = make_rtp_packet(
            payload,
            csrcs=(0x11111111, 0x22222222),
            extension=(0xBEDE, b"\x10\x20\x30\x40"),
            padding=4,
        )

        meta = parse_rtp_packet(packet)

        self.assertEqual(meta.csrc_count, 2)
        self.assertTrue(meta.has_extension)
        self.assertEqual(meta.extension_profile, 0xBEDE)
        self.assertEqual(meta.extension_size, 4)
        self.assertEqual(meta.padding_size, 4)
        self.assertEqual(meta.payload_size, len(payload))
        self.assertEqual(meta.payload_sha256, hashlib.sha256(payload).hexdigest())

    def test_rejects_packet_shorter_than_fixed_header(self):
        with self.assertRaisesRegex(ValueError, "shorter than 12-byte RTP header"):
            parse_rtp_packet(b"\x80" * 11)

    def test_rejects_non_version_two_packet(self):
        packet = bytearray(make_rtp_packet())
        packet[0] = 0x40

        with self.assertRaisesRegex(ValueError, "unsupported RTP version 1"):
            parse_rtp_packet(bytes(packet))

    def test_rejects_truncated_csrc_list(self):
        packet = bytearray(make_rtp_packet(b""))
        packet[0] |= 2

        with self.assertRaisesRegex(ValueError, "truncated RTP CSRC list"):
            parse_rtp_packet(bytes(packet))

    def test_rejects_truncated_extension_header(self):
        packet = bytearray(make_rtp_packet(b"\x01\x02"))
        packet[0] |= 0x10

        with self.assertRaisesRegex(ValueError, "truncated RTP extension header"):
            parse_rtp_packet(bytes(packet))

    def test_rejects_truncated_extension_data(self):
        packet = bytearray(make_rtp_packet(b""))
        packet[0] |= 0x10
        packet.extend(struct.pack("!HH", 0xBEDE, 2))
        packet.extend(b"\x00\x01\x02\x03")

        with self.assertRaisesRegex(ValueError, "truncated RTP extension data"):
            parse_rtp_packet(bytes(packet))

    def test_rejects_zero_padding_length(self):
        packet = bytearray(make_rtp_packet(b"audio"))
        packet[0] |= 0x20
        packet[-1] = 0

        with self.assertRaisesRegex(ValueError, "invalid RTP padding length 0"):
            parse_rtp_packet(bytes(packet))

    def test_rejects_padding_larger_than_remaining_body(self):
        packet = bytearray(make_rtp_packet(b"audio"))
        packet[0] |= 0x20
        packet[-1] = 6

        with self.assertRaisesRegex(ValueError, "exceeds RTP body"):
            parse_rtp_packet(bytes(packet))


class CaptureFileTests(unittest.TestCase):
    def test_reads_big_endian_length_prefixed_packets(self):
        packets = [make_rtp_packet(b"one"), make_rtp_packet(b"two")]
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "packets.bin"
            path.write_bytes(b"".join(struct.pack("!I", len(p)) + p for p in packets))

            self.assertEqual(list(read_length_prefixed_packets(path)), packets)

    def test_rejects_truncated_four_byte_length_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "packets.bin"
            path.write_bytes(b"\x00\x00\x10")

            with self.assertRaisesRegex(ValueError, "truncated 4-byte packet length prefix"):
                list(read_length_prefixed_packets(path))

    def test_rejects_truncated_packet_body(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "packets.bin"
            path.write_bytes(struct.pack("!I", 20) + b"short")

            with self.assertRaisesRegex(ValueError, "packet 0.*expected 20 bytes, found 5"):
                list(read_length_prefixed_packets(path))

    def test_rejects_zero_length_before_reading_packet_body(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "packets.bin"
            path.write_bytes(struct.pack("!I", 0))

            with self.assertRaisesRegex(ValueError, "invalid RTP packet length 0"):
                list(read_length_prefixed_packets(path))

    def test_rejects_length_above_rtp_limit_before_allocation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "packets.bin"
            path.write_bytes(struct.pack("!I", 0xFFFFFFFF))

            with self.assertRaisesRegex(
                ValueError,
                f"exceeds maximum RTP packet size {MAX_RTP_PACKET_SIZE}",
            ):
                list(read_length_prefixed_packets(path))

    def test_loads_jsonl_objects(self):
        rows = [{"packet_index": 0}, {"packet_index": 1}]
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "manifest.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))

            self.assertEqual(load_manifest(path), rows)

    def test_rejects_malformed_jsonl_with_line_number(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "manifest.jsonl"
            path.write_text('{"packet_index": 0}\nnot-json\n')

            with self.assertRaisesRegex(ValueError, "malformed JSONL at line 2"):
                load_manifest(path)

    def test_rejects_non_object_manifest_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "manifest.jsonl"
            path.write_text("[]\n")

            with self.assertRaisesRegex(ValueError, "manifest line 1 is not a JSON object"):
                load_manifest(path)


class ExtractPayloadTests(unittest.TestCase):
    def test_extracts_two_payloads_using_extension_and_padding_boundaries(self):
        packets = [
            make_rtp_packet(
                b"first",
                sequence=10,
                extension=(0xBEDE, b"\x10\x20\x30\x40"),
                padding=4,
            ),
            make_rtp_packet(b"second", sequence=11, padding=8),
        ]

        self.assertEqual(extract_payloads(packets), b"firstsecond")

    def test_rejects_empty_malformed_wrong_payload_type_and_ssrc_change(self):
        cases = {
            "empty input": ([], "at least one RTP packet"),
            "malformed": ([b"short"], "packet 0.*shorter than"),
            "wrong payload type": (
                [make_rtp_packet(payload_type=0)],
                "packet 0 payload type 0 does not match 8",
            ),
            "SSRC change": (
                [make_rtp_packet(ssrc=1), make_rtp_packet(ssrc=2)],
                "packet 1 SSRC.*does not match",
            ),
        }
        for name, (packets, message) in cases.items():
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, message):
                extract_payloads(packets)

    def test_can_explicitly_allow_ssrc_changes(self):
        packets = [
            make_rtp_packet(b"one", ssrc=1),
            make_rtp_packet(b"two", ssrc=2),
        ]

        self.assertEqual(
            extract_payloads(packets, allow_ssrc_change=True), b"onetwo"
        )

    def test_extract_payload_cli_atomically_replaces_output_and_reports_json(self):
        packets = [
            make_rtp_packet(b"one", sequence=1),
            make_rtp_packet(b"two", sequence=2),
        ]
        with tempfile.TemporaryDirectory() as directory:
            directory = pathlib.Path(directory)
            capture = directory / "packets.bin"
            output = directory / "payload.pcma"
            capture.write_bytes(
                b"".join(struct.pack("!I", len(packet)) + packet for packet in packets)
            )
            output.write_bytes(b"stale")
            environment = os.environ.copy()
            environment.pop("PYTHONPATH", None)

            completed = subprocess.run(
                [
                    sys.executable,
                    str(RTP_REFERENCE),
                    "extract-payload",
                    str(capture),
                    "--output",
                    str(output),
                ],
                cwd=ROOT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(output.read_bytes(), b"onetwo")
            self.assertEqual(
                json.loads(completed.stdout),
                {
                    "bytes": 6,
                    "packet_count": 2,
                    "payload_type": 8,
                    "sha256": hashlib.sha256(b"onetwo").hexdigest(),
                },
            )
            self.assertFalse(list(directory.glob(f".{output.name}.*.tmp")))


class SummaryCliTests(unittest.TestCase):
    def test_summarize_reports_timing_sender_and_discontinuity_metrics(self):
        rows = []
        sequences = [100, 101, 103, 104, 105]
        timestamps = [1000, 1160, 1480, 1640, 1800]
        ssrcs = [7, 7, 7, 9, 9]
        for index in range(5):
            rows.append(
                {
                    "packet_index": index,
                    "relative_monotonic_ns": index * 10,
                    "buffer_pts_ns": index * 20_000_000,
                    "buffer_duration_ns": 20_000_000,
                    "packet_size": 172,
                    "payload_type": 8,
                    "marker": index == 0,
                    "sequence": sequences[index],
                    "timestamp": timestamps[index],
                    "ssrc": ssrcs[index],
                    "payload_size": 160,
                    "payload_sha256": "0" * 64,
                }
            )

        with tempfile.TemporaryDirectory() as directory:
            manifest = pathlib.Path(directory) / "manifest.jsonl"
            output = pathlib.Path(directory) / "summary.json"
            manifest.write_text("".join(json.dumps(row) + "\n" for row in rows))

            completed = subprocess.run(
                [
                    sys.executable,
                    str(RTP_REFERENCE),
                    "summarize",
                    str(manifest),
                    "--output",
                    str(output),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(output.read_text())
            self.assertEqual(summary["packet_count"], 5)
            self.assertEqual(summary["duration_ns"], 40)
            self.assertEqual(summary["duration_seconds"], 0.00000004)
            self.assertEqual(summary["payload_size_histogram"], {"160": 5})
            self.assertEqual(
                summary["timestamp_delta_histogram"], {"160": 2, "320": 1}
            )
            self.assertEqual(
                summary["inter_arrival_ns"],
                {"p50": 10, "p95": 10, "p99": 10, "max": 10},
            )
            self.assertEqual(summary["marker_count"], 1)
            self.assertEqual(
                summary["sender_tuples"],
                [
                    {"payload_type": 8, "ssrc": 7, "packet_count": 3},
                    {"payload_type": 8, "ssrc": 9, "packet_count": 2},
                ],
            )
            self.assertEqual(
                summary["ssrc_changes"],
                [{"packet_index": 3, "from_ssrc": 7, "to_ssrc": 9}],
            )
            self.assertEqual(
                summary["discontinuities"],
                [
                    {
                        "packet_index": 2,
                        "expected_sequence": 102,
                        "actual_sequence": 103,
                        "missing_packets": 1,
                    }
                ],
            )
            rendered = output.read_text()
            self.assertEqual(rendered, json.dumps(summary, indent=2, sort_keys=True) + "\n")

    def test_summarize_writes_json_to_stdout_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = pathlib.Path(directory) / "manifest.jsonl"
            manifest.write_text("")

            completed = subprocess.run(
                [sys.executable, str(RTP_REFERENCE), "summarize", str(manifest)],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(completed.stdout)["packet_count"], 0)

    def test_summarize_resets_sequence_and_timestamp_at_ssrc_change(self):
        rows = []
        for index, (sequence, timestamp, ssrc) in enumerate(
            [(65000, 4_000_000_000, 10), (7, 1000, 20), (8, 1160, 20)]
        ):
            rows.append(
                {
                    "packet_index": index,
                    "relative_monotonic_ns": index * 20_000_000,
                    "payload_type": 8,
                    "marker": False,
                    "sequence": sequence,
                    "timestamp": timestamp,
                    "ssrc": ssrc,
                    "payload_size": 160,
                }
            )

        summary = summarize_manifest(rows)

        self.assertEqual(summary["timestamp_delta_histogram"], {"160": 1})
        self.assertEqual(summary["discontinuities"], [])
        self.assertEqual(
            summary["ssrc_changes"],
            [{"packet_index": 1, "from_ssrc": 10, "to_ssrc": 20}],
        )


class CaptureToolTests(unittest.TestCase):
    def test_help_does_not_require_site_packages_or_gi(self):
        completed = subprocess.run(
            [sys.executable, "-S", str(CAPTURE_TOOL), "--help"],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--file", completed.stdout)
        self.assertIn("--volume", completed.stdout)
        self.assertIn("--output", completed.stdout)
        self.assertIn("--url", completed.stdout)

    def test_arguments_require_file_and_output_with_optional_url(self):
        arguments = build_argument_parser().parse_args(
            ["--file", "tone.mp3", "--output", "capture"]
        )

        self.assertEqual(arguments.file, pathlib.Path("tone.mp3"))
        self.assertEqual(arguments.output, pathlib.Path("capture"))
        self.assertEqual(arguments.volume, 0.05)
        self.assertIsNone(arguments.url)

    def test_capture_run_without_url_has_clear_error_before_gi_import(self):
        environment = os.environ.copy()
        environment.pop("ONVIF_RTSP_URL", None)

        completed = subprocess.run(
            [
                sys.executable,
                "-S",
                str(CAPTURE_TOOL),
                "--file",
                "missing.mp3",
                "--output",
                "capture",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )

        self.assertEqual(completed.returncode, 2)
        self.assertIn("provide --url or set ONVIF_RTSP_URL", completed.stderr)
        self.assertNotIn("No module named 'gi'", completed.stderr)

    def test_endpoint_uses_cli_then_environment(self):
        cli_url = "rtsp://cli-user:cli-pass@example.invalid/live"
        env_url = "rtsp://env-user:env-pass@example.invalid/live"

        self.assertEqual(
            resolve_endpoint(cli_url, {"ONVIF_RTSP_URL": env_url}), cli_url
        )
        self.assertEqual(resolve_endpoint(None, {"ONVIF_RTSP_URL": env_url}), env_url)

    def test_redact_uri_removes_username_and_password(self):
        redacted = redact_uri(
            "rtsp://camera-user:camera-pass@example.invalid:8554/video"
            "?token=secret&vendor_session=opaque#private-fragment"
        )

        self.assertEqual(
            redacted,
            "rtsp://example.invalid:8554/video"
            "?token=%3Credacted%3E&vendor_session=%3Credacted%3E",
        )
        self.assertNotIn("camera-user", redacted)
        self.assertNotIn("camera-pass", redacted)
        self.assertNotIn("secret", redacted)
        self.assertNotIn("opaque", redacted)
        self.assertNotIn("private-fragment", redacted)

    def test_session_metadata_redacts_endpoint_and_command_url(self):
        endpoint = "rtsp://camera-user:camera-pass@example.invalid/live?token=secret"
        arguments = build_argument_parser().parse_args(
            [
                "--file",
                "tone.mp3",
                "--output",
                "capture",
                "--url",
                endpoint,
            ]
        )

        session = build_session_metadata(
            arguments,
            source_path=pathlib.Path("/tmp/tone.mp3"),
            output_path=pathlib.Path("/tmp/capture"),
            source_sha256="a" * 64,
            endpoint=endpoint,
        )

        rendered = json.dumps(session)
        self.assertEqual(session["endpoint"], "rtsp://example.invalid/live?token=%3Credacted%3E")
        self.assertEqual(
            session["command"]["arguments"]["url"],
            "rtsp://example.invalid/live?token=%3Credacted%3E",
        )
        self.assertNotIn("camera-user", rendered)
        self.assertNotIn("camera-pass", rendered)
        self.assertNotIn("secret", rendered)

    def test_sha256_file_hashes_source_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "source.mp3"
            path.write_bytes(b"source bytes")

            self.assertEqual(
                sha256_file(path), hashlib.sha256(b"source bytes").hexdigest()
            )

    def test_capture_generation_has_exact_artifacts_and_consistency_hashes(self):
        packet = make_rtp_packet(b"audio", marker=True)
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "capture"
            artifacts = CaptureArtifacts(output)
            artifacts.record_packet(
                packet,
                relative_monotonic_ns=123,
                buffer_pts_ns=456,
                buffer_duration_ns=789,
            )
            artifacts.finalize({"status": "complete"})

            self.assertEqual(
                list(read_length_prefixed_packets(output / "packets.bin")), [packet]
            )
            self.assertEqual(
                load_manifest(output / "manifest.jsonl"),
                [
                    {
                        "packet_index": 0,
                        "relative_monotonic_ns": 123,
                        "buffer_pts_ns": 456,
                        "buffer_duration_ns": 789,
                        "packet_size": len(packet),
                        "payload_type": 8,
                        "marker": True,
                        "sequence": 0x1234,
                        "timestamp": 0x10203040,
                        "ssrc": 0x50607080,
                        "payload_size": 5,
                        "payload_sha256": hashlib.sha256(b"audio").hexdigest(),
                    }
                ],
            )
            session = json.loads((output / "session.json").read_text())
            self.assertEqual(session["run_id"], artifacts.run_id)
            for filename in ("packets.bin", "manifest.jsonl"):
                contents = (output / filename).read_bytes()
                self.assertEqual(session["artifacts"][filename]["size"], len(contents))
                self.assertEqual(
                    session["artifacts"][filename]["sha256"],
                    hashlib.sha256(contents).hexdigest(),
                )
            self.assertEqual(set(path.name for path in output.iterdir()), {
                "packets.bin", "manifest.jsonl", "session.json"
            })
            self.assertEqual(list(output.parent.glob(".capture.*.tmp")), [])

    def test_capture_refuses_existing_output_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "capture"
            output.mkdir()

            with self.assertRaisesRegex(FileExistsError, "output already exists"):
                CaptureArtifacts(output)

            self.assertEqual(list(output.parent.glob(".capture.*.tmp")), [])

    def test_partial_record_failure_removes_staging_and_preserves_error(self):
        class FailingWriter:
            def __init__(self, wrapped):
                self.wrapped = wrapped

            @property
            def closed(self):
                return self.wrapped.closed

            def write(self, _value):
                raise ValueError("manifest write failed")

            def close(self):
                self.wrapped.close()
                raise OSError("close also failed")

        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "capture"
            artifacts = CaptureArtifacts(output)
            artifacts._manifest_file = FailingWriter(artifacts._manifest_file)

            with self.assertRaisesRegex(ValueError, "manifest write failed"):
                artifacts.record_packet(
                    make_rtp_packet(),
                    relative_monotonic_ns=1,
                    buffer_pts_ns=2,
                    buffer_duration_ns=3,
                )

            self.assertFalse(output.exists())
            self.assertEqual(list(output.parent.glob(".capture.*.tmp")), [])

    def test_invalid_packet_record_removes_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "capture"
            artifacts = CaptureArtifacts(output)

            with self.assertRaisesRegex(ValueError, "invalid RTP packet length 0"):
                artifacts.record_packet(
                    b"",
                    relative_monotonic_ns=1,
                    buffer_pts_ns=2,
                    buffer_duration_ns=3,
                )

            self.assertFalse(output.exists())
            self.assertEqual(list(output.parent.glob(".capture.*.tmp")), [])

    def test_finalization_failure_publishes_nothing_and_removes_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "capture"
            artifacts = CaptureArtifacts(output)
            artifacts.record_packet(
                make_rtp_packet(),
                relative_monotonic_ns=1,
                buffer_pts_ns=2,
                buffer_duration_ns=3,
            )

            with mock.patch(
                "tools.capture_gst_backchannel._write_session_file",
                side_effect=OSError("session write failed"),
            ):
                with self.assertRaisesRegex(OSError, "session write failed"):
                    artifacts.finalize({"status": "complete"})

            self.assertFalse(output.exists())
            self.assertEqual(list(output.parent.glob(".capture.*.tmp")), [])

    def test_platform_loader_uses_discovery_then_dylib_fallback(self):
        loaded = object()
        attempts = []

        def fake_cdll(candidate):
            attempts.append(candidate)
            if candidate == "libgstreamer-1.0.dylib":
                return loaded
            raise OSError("not found")

        result = load_gstreamer_library(
            find_library=lambda name: None,
            cdll=fake_cdll,
            platform="darwin",
        )

        self.assertIs(result, loaded)
        self.assertEqual(
            attempts,
            ["libgstreamer-1.0.0.dylib", "libgstreamer-1.0.dylib"],
        )

    def test_legacy_configuration_is_lazy_and_sets_ref_signature(self):
        class FakeFunction:
            argtypes = None
            restype = None

        class FakeLibrary:
            gst_mini_object_ref = FakeFunction()

        calls = []
        library = FakeLibrary()

        self.assertIsNone(
            configure_legacy_push(True, loader=lambda: calls.append("loaded"))
        )
        self.assertEqual(calls, [])
        self.assertIs(
            configure_legacy_push(
                False, loader=lambda: calls.append("loaded") or library
            ),
            library,
        )
        self.assertEqual(calls, ["loaded"])
        self.assertEqual(len(library.gst_mini_object_ref.argtypes), 1)
        self.assertIs(library.gst_mini_object_ref.restype, library.gst_mini_object_ref.argtypes[0])

    def test_push_result_accepts_ok_and_rejects_non_ok_results(self):
        self.assertEqual(
            ensure_push_succeeded("OK", "OK", "push-backchannel-sample"), "OK"
        )

        for result in ("FLUSHING", "ERROR"):
            with self.subTest(result=result):
                with self.assertRaisesRegex(
                    BackchannelPushError,
                    f"push-backchannel-buffer returned {result}",
                ):
                    ensure_push_succeeded(result, "OK", "push-backchannel-buffer")

    def test_owned_files_only_contain_example_invalid_rtsp_literals(self):
        marker = "rtsp:" + "//"
        for path in (RTP_REFERENCE, CAPTURE_TOOL, pathlib.Path(__file__)):
            contents = path.read_text()
            for token in re.findall(re.escape(marker) + r"[^\s\"']+", contents):
                self.assertIn("example.invalid", token, (path, token))


if __name__ == "__main__":
    unittest.main()
