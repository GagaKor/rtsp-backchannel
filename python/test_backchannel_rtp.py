import hashlib
import json
import os
import pathlib
import re
import struct
import subprocess
import sys
import tempfile
import unittest
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
    load_manifest,
    parse_rtp_packet,
    read_length_prefixed_packets,
    summarize_manifest,
)


ROOT = pathlib.Path(__file__).resolve().parents[1]
RTP_REFERENCE = ROOT / "tools" / "rtp_reference.py"
CAPTURE_TOOL = ROOT / "tools" / "capture_gst_backchannel.py"


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
