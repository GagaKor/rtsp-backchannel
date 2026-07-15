import hashlib
import json
import math
import os
import pathlib
import socket
import struct
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import patch

import onvif_play
from tools import replay_rtp_reference
from tools.rtp_reference import parse_rtp_packet


ROOT = pathlib.Path(__file__).resolve().parents[1]
REPLAY_TOOL = ROOT / "tools" / "replay_rtp_reference.py"


def make_rtp_packet(payload, *, sequence, timestamp, ssrc=0x01020304, marker=False):
    return struct.pack(
        "!BBHII",
        0x80,
        8 | (0x80 if marker else 0),
        sequence,
        timestamp,
        ssrc,
    ) + payload


def manifest_row(packet, packet_index, relative_monotonic_ns):
    meta = parse_rtp_packet(packet)
    return {
        "packet_index": packet_index,
        "relative_monotonic_ns": relative_monotonic_ns,
        "buffer_pts_ns": packet_index * 40_000_000,
        "buffer_duration_ns": 40_000_000,
        "packet_size": len(packet),
        "payload_type": meta.payload_type,
        "marker": meta.marker,
        "sequence": meta.sequence,
        "timestamp": meta.timestamp,
        "ssrc": meta.ssrc,
        "payload_size": meta.payload_size,
        "payload_sha256": meta.payload_sha256,
    }


def write_reference(directory, packets, rows):
    packet_path = pathlib.Path(directory) / "packets.bin"
    manifest_path = pathlib.Path(directory) / "manifest.jsonl"
    packet_path.write_bytes(
        b"".join(struct.pack("!I", len(packet)) + packet for packet in packets)
    )
    manifest_path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    return packet_path, manifest_path


class FakeRtsp:
    instances = []

    def __init__(self, host, port, user, password):
        self.host = host
        self.port = port
        self.requests = []
        self.events = []
        self.session = None
        self.closed = False
        self.instances.append(self)

    def request(self, method, uri, headers=None):
        headers = headers or {}
        self.requests.append((method, uri, headers))
        self.events.append(("request", method, uri, headers))
        if method == "DESCRIBE":
            sdp = (
                "v=0\r\n"
                "m=video 0 RTP/AVP 96\r\n"
                "a=control:trackID=0\r\n"
                "a=recvonly\r\n"
                "m=audio 0 RTP/AVP 8\r\n"
                "a=control:trackID=1\r\n"
                "a=rtpmap:8 PCMA/8000\r\n"
                "a=recvonly\r\n"
                "m=audio 0 RTP/AVP 8\r\n"
                "a=control:trackID=5\r\n"
                "a=rtpmap:8 PCMA/8000\r\n"
                "a=sendonly\r\n"
            )
            return 200, {"content-base": uri + "/"}, sdp
        if method == "SETUP":
            if uri.endswith("trackID=0"):
                channel = "0-1"
            elif uri.endswith("trackID=1"):
                channel = "2-3"
            else:
                channel = "6-7"
            return 200, {
                "session": "test-session;timeout=60",
                "transport": (
                    "RTP/AVP/TCP;unicast;"
                    f"interleaved={channel};ssrc=01020304"
                ),
            }, ""
        if method == "PLAY":
            return 200, {"rtp-info": "url=trackID=5;seq=0;rtptime=0"}, ""
        return 200, {}, ""

    def send_interleaved(self, channel, payload):
        self.events.append(("media", channel, payload))

    def close(self):
        self.closed = True
        self.events.append(("close",))


class FakeClock:
    def __init__(self, now_ns=1_000_000_000, sleep_overshoot_ns=0):
        self.start_ns = now_ns
        self.now_ns = now_ns
        self.sleep_overshoot_ns = sleep_overshoot_ns
        self.sleeps = []
        self.sleep_deadlines_ns = []

    def monotonic_ns(self):
        return self.now_ns

    def monotonic(self):
        return self.now_ns / 1_000_000_000

    def time(self):
        return 1_700_000_000 + self.monotonic()

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now_ns += (
            round(seconds * 1_000_000_000) + self.sleep_overshoot_ns
        )
        self.sleep_deadlines_ns.append(self.now_ns)


class FakeUdpRtsp(FakeRtsp):
    def request(self, method, uri, headers=None):
        if method == "SETUP":
            headers = headers or {}
            self.requests.append((method, uri, headers))
            self.events.append(("request", method, uri, headers))
            return 200, {
                "session": "udp-session;timeout=60",
                "transport": "RTP/AVP;unicast;server_port=61000-61001",
            }, ""
        return super().request(method, uri, headers)


class FakeRtspWithoutIdentity(FakeRtsp):
    def request(self, method, uri, headers=None):
        status, response_headers, body = super().request(method, uri, headers)
        if method == "SETUP":
            response_headers["transport"] = response_headers[
                "transport"
            ].replace(";ssrc=01020304", "")
        elif method == "PLAY":
            response_headers = {}
        return status, response_headers, body


class FakeAacRtsp(FakeRtsp):
    def request(self, method, uri, headers=None):
        if method == "DESCRIBE":
            headers = headers or {}
            self.requests.append((method, uri, headers))
            self.events.append(("request", method, uri, headers))
            sdp = (
                "v=0\r\n"
                "m=video 0 RTP/AVP 96\r\n"
                "a=control:trackID=0\r\n"
                "a=recvonly\r\n"
                "m=audio 0 RTP/AVP 8\r\n"
                "a=control:trackID=1\r\n"
                "a=rtpmap:8 PCMA/8000\r\n"
                "a=recvonly\r\n"
                "m=audio 0 RTP/AVP 97\r\n"
                "a=control:trackID=5\r\n"
                "a=rtpmap:97 MPEG4-GENERIC/8000\r\n"
                "a=sendonly\r\n"
            )
            return 200, {"content-base": uri + "/"}, sdp
        return super().request(method, uri, headers)


class TeardownFailureRtsp(FakeRtsp):
    def request(self, method, uri, headers=None):
        if method == "TEARDOWN":
            headers = headers or {}
            self.requests.append((method, uri, headers))
            self.events.append(("request", method, uri, headers))
            return 500, {}, ""
        return super().request(method, uri, headers)


class FakeUdpSocket:
    def __init__(self):
        self.bound = None
        self.sent = []
        self.closed = False

    def bind(self, address):
        self.bound = address

    def sendto(self, payload, target):
        self.sent.append((payload, target))

    def close(self):
        self.closed = True


class ScriptedRtspSocket:
    def __init__(self, recv_results=(), send_error=None):
        self.recv_results = list(recv_results)
        self.send_error = send_error
        self.timeouts = []
        self.closed = False
        self.shutdown_called = False
        self.sent = []

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def recv(self, size):
        if not self.recv_results:
            return b""
        result = self.recv_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def sendall(self, payload):
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(payload)

    def shutdown(self, how):
        self.shutdown_called = True

    def close(self):
        self.closed = True


class RtspSafetyTest(unittest.TestCase):
    def test_response_queue_overflow_fails_closed_without_blocking_reader(self):
        response = b"RTSP/1.0 200 OK\r\n\r\n"
        fake_socket = ScriptedRtspSocket(
            recv_results=[response * 65, b""]
        )

        with patch.object(
            onvif_play.socket, "create_connection", return_value=fake_socket
        ):
            rtsp = onvif_play.Rtsp(
                "example.invalid", 554, "fake-user", "fake-password"
            )
            rtsp.reader.join(timeout=1)
            try:
                self.assertEqual(onvif_play.RTSP_MAX_QUEUED_RESPONSES, 64)
                self.assertFalse(rtsp.reader.is_alive())
                self.assertLessEqual(
                    rtsp.responses.qsize(),
                    onvif_play.RTSP_MAX_QUEUED_RESPONSES,
                )
                with self.assertRaisesRegex(
                    RuntimeError, "RTSP response queue exceeded 64"
                ):
                    rtsp._read()
                self.assertTrue(fake_socket.shutdown_called)
                self.assertTrue(fake_socket.closed)
            finally:
                rtsp.close()

    def test_normal_request_flow_uses_bounded_response_queue(self):
        fake_socket = ScriptedRtspSocket(
            recv_results=[b"RTSP/1.0 200 OK\r\nX-Test: yes\r\n\r\n", b""]
        )

        with patch.object(
            onvif_play.socket, "create_connection", return_value=fake_socket
        ):
            rtsp = onvif_play.Rtsp(
                "example.invalid", 554, "fake-user", "fake-password"
            )
            try:
                self.assertEqual(
                    rtsp.request("OPTIONS", "rtsp://example.invalid/live"),
                    (200, {"x-test": "yes"}, ""),
                )
                self.assertEqual(rtsp.responses.maxsize, 64)
                self.assertEqual(len(fake_socket.sent), 1)
            finally:
                rtsp.close()

    def test_digest_401_retry_flow_uses_bounded_response_queue(self):
        responses = (
            b"RTSP/1.0 401 Unauthorized\r\n"
            b'WWW-Authenticate: Digest realm="camera", nonce="nonce"\r\n\r\n'
            b"RTSP/1.0 200 OK\r\n\r\n"
        )
        fake_socket = ScriptedRtspSocket(recv_results=[responses, b""])

        with patch.object(
            onvif_play.socket, "create_connection", return_value=fake_socket
        ):
            rtsp = onvif_play.Rtsp(
                "example.invalid", 554, "fake-user", "fake-password"
            )
            try:
                self.assertEqual(
                    rtsp.request("OPTIONS", "rtsp://example.invalid/live"),
                    (200, {}, ""),
                )
                self.assertEqual(rtsp.responses.maxsize, 64)
                self.assertEqual(len(fake_socket.sent), 2)
                self.assertIn(b"Authorization: Digest", fake_socket.sent[1])
            finally:
                rtsp.close()

    def test_send_timeout_propagates_with_finite_socket_timeout(self):
        fake_socket = ScriptedRtspSocket(
            recv_results=[b""], send_error=socket.timeout("send timed out")
        )

        with patch.object(
            onvif_play.socket, "create_connection", return_value=fake_socket
        ):
            rtsp = onvif_play.Rtsp(
                "example.invalid", 554, "fake-user", "fake-password"
            )
            try:
                self.assertEqual(
                    fake_socket.timeouts, [onvif_play.RTSP_IO_TIMEOUT_SECONDS]
                )
                with self.assertRaisesRegex(socket.timeout, "send timed out"):
                    rtsp.send_interleaved(0, b"packet")
            finally:
                rtsp.close()

    def test_idle_recv_timeout_does_not_stop_reader(self):
        response = b"RTSP/1.0 200 OK\r\nContent-Length: 2\r\n\r\nok"
        fake_socket = ScriptedRtspSocket(
            recv_results=[socket.timeout("idle"), response, b""]
        )

        with patch.object(
            onvif_play.socket, "create_connection", return_value=fake_socket
        ):
            rtsp = onvif_play.Rtsp(
                "example.invalid", 554, "fake-user", "fake-password"
            )
            try:
                self.assertEqual(rtsp._read(), (200, {"content-length": "2"}, "ok"))
            finally:
                rtsp.close()

    def test_stream_parser_accepts_split_response_and_interleaved_frame(self):
        parser = onvif_play.RtspStreamParser()
        interleaved = b"$\x06\x00\x04rtp!"
        response = (
            b"RTSP/1.0 200 OK\r\nContent-Length: 4\r\nX-Test: yes\r\n\r\nbody"
        )

        parsed = []
        for chunk in (
            interleaved[:2],
            interleaved[2:5],
            interleaved[5:] + response[:11],
            response[11:39],
            response[39:],
        ):
            parsed.extend(parser.feed(chunk))

        self.assertEqual(
            parsed,
            [(200, {"content-length": "4", "x-test": "yes"}, "body")],
        )

    def test_stream_parser_rejects_unterminated_header_over_limit(self):
        parser = onvif_play.RtspStreamParser(
            max_header_bytes=16, max_body_bytes=64, max_buffer_bytes=128
        )

        with self.assertRaisesRegex(RuntimeError, "RTSP header exceeds"):
            parser.feed(b"RTSP/1.0 200 OK\r\nX-Test: still-open")

    def test_stream_parser_rejects_declared_body_over_limit_immediately(self):
        parser = onvif_play.RtspStreamParser(
            max_header_bytes=128, max_body_bytes=4, max_buffer_bytes=256
        )

        with self.assertRaisesRegex(RuntimeError, "RTSP body exceeds"):
            parser.feed(b"RTSP/1.0 200 OK\r\nContent-Length: 5\r\n\r\n")

    def test_stream_parser_rejects_aggregate_buffer_over_limit(self):
        parser = onvif_play.RtspStreamParser(
            max_header_bytes=128, max_body_bytes=64, max_buffer_bytes=12
        )

        with self.assertRaisesRegex(RuntimeError, "RTSP buffer exceeds"):
            parser.feed(b"RTSP/1.0 200X")

    def test_close_joins_reader_even_when_socket_close_fails(self):
        class FailingCloseSocket:
            close_called = False

            def shutdown(self, how):
                pass

            def close(self):
                self.close_called = True
                raise RuntimeError("socket close failed")

        class FakeReader:
            join_called = False

            def join(self, timeout=None):
                self.join_called = True

        rtsp = onvif_play.Rtsp.__new__(onvif_play.Rtsp)
        rtsp.closed = False
        rtsp.s = FailingCloseSocket()
        rtsp.reader = FakeReader()

        with self.assertRaisesRegex(RuntimeError, "socket close failed"):
            rtsp.close()

        self.assertTrue(rtsp.closed)
        self.assertTrue(rtsp.s.close_called)
        self.assertTrue(rtsp.reader.join_called)


class BackchannelTransportTest(unittest.TestCase):
    def setUp(self):
        FakeRtsp.instances.clear()

    def test_opens_tracks_before_play_and_honors_returned_channel(self):
        stream_uri = "rtsp://example.invalid/live"
        packet = make_rtp_packet(b"captured", sequence=7, timestamp=900)

        with onvif_play.open_backchannel_transport(
            "example.invalid",
            "fake-user",
            "fake-password",
            stream_uri=stream_uri,
            rtsp_factory=FakeRtsp,
        ) as transport:
            self.assertEqual(transport.stream_uri, stream_uri)
            self.assertEqual(transport.session, "test-session")
            self.assertEqual(transport.rtsp.session, "test-session")
            self.assertEqual(transport.rtp_channel, 6)
            self.assertIsNone(transport.udp_target)
            transport.send_rtp(packet)

        client = FakeRtsp.instances[0]
        self.assertEqual(
            [request[0] for request in client.requests],
            ["OPTIONS", "DESCRIBE", "SETUP", "SETUP", "SETUP", "PLAY", "TEARDOWN"],
        )
        self.assertEqual(
            [request[1].rsplit("/", 1)[-1] for request in client.requests[2:5]],
            ["trackID=0", "trackID=1", "trackID=5"],
        )
        media_index = next(i for i, event in enumerate(client.events) if event[0] == "media")
        play_index = next(
            i for i, event in enumerate(client.events)
            if event[:2] == ("request", "PLAY")
        )
        self.assertGreater(media_index, play_index)
        self.assertEqual(client.events[media_index], ("media", 6, packet))
        self.assertTrue(client.closed)

        describe = client.requests[1]
        backchannel_setup = client.requests[4]
        play = client.requests[5]
        teardown = client.requests[6]
        for request in (describe, backchannel_setup, play, teardown):
            self.assertEqual(request[2].get("Require"), onvif_play.BACKCHANNEL)

    def test_teardown_occurs_when_replay_raises(self):
        with self.assertRaisesRegex(RuntimeError, "send failed"):
            with onvif_play.open_backchannel_transport(
                "example.invalid",
                "fake-user",
                "fake-password",
                stream_uri="rtsp://example.invalid/live",
                rtsp_factory=FakeRtsp,
            ):
                raise RuntimeError("send failed")

        client = FakeRtsp.instances[0]
        self.assertEqual(client.requests[-1][0], "TEARDOWN")
        self.assertEqual(client.requests[-1][2]["Require"], onvif_play.BACKCHANNEL)
        self.assertTrue(client.closed)

    def test_original_error_survives_teardown_failure(self):
        with self.assertRaisesRegex(RuntimeError, "send failed") as raised:
            with onvif_play.open_backchannel_transport(
                "example.invalid",
                "fake-user",
                "fake-password",
                stream_uri="rtsp://example.invalid/live",
                rtsp_factory=TeardownFailureRtsp,
            ):
                raise RuntimeError("send failed")

        self.assertTrue(
            any("TEARDOWN failed" in note for note in raised.exception.__notes__)
        )
        self.assertTrue(FakeRtsp.instances[0].closed)

    def test_udp_transport_returns_server_target_and_preserves_packet_bytes(self):
        sockets = []

        def make_socket(*args):
            udp_socket = FakeUdpSocket()
            sockets.append(udp_socket)
            return udp_socket

        packet = make_rtp_packet(b"udp", sequence=8, timestamp=1220)
        with patch.object(onvif_play.socket, "socket", side_effect=make_socket):
            with onvif_play.open_backchannel_transport(
                "example.invalid",
                "fake-user",
                "fake-password",
                transport="udp",
                stream_uri="rtsp://example.invalid/live",
                rtsp_factory=FakeUdpRtsp,
                client_rtp_port=51000,
            ) as transport:
                self.assertEqual(transport.session, "udp-session")
                self.assertIsNone(transport.rtp_channel)
                self.assertEqual(transport.udp_target, ("example.invalid", 61000))
                transport.send_rtp(packet)

        client = FakeRtsp.instances[0]
        self.assertEqual(
            [request[0] for request in client.requests],
            ["OPTIONS", "DESCRIBE", "SETUP", "PLAY", "TEARDOWN"],
        )
        self.assertEqual(sockets[0].bound, ("", 51000))
        self.assertEqual(sockets[0].sent, [(packet, ("example.invalid", 61000))])
        self.assertTrue(all(udp_socket.closed for udp_socket in sockets))


class ReplayReferenceTest(unittest.TestCase):
    def make_reference(self, directory):
        packets = [
            make_rtp_packet(b"first", sequence=10, timestamp=1000, marker=True),
            make_rtp_packet(b"second", sequence=11, timestamp=1320),
            make_rtp_packet(b"third", sequence=12, timestamp=1640),
        ]
        relative_times = [8_000_000, 48_000_000, 109_000_000]
        rows = [
            manifest_row(packet, index, relative_times[index])
            for index, packet in enumerate(packets)
        ]
        paths = write_reference(directory, packets, rows)
        return packets, rows, paths

    def test_replays_unchanged_packets_at_normalized_pre_emit_deadlines(self):
        with tempfile.TemporaryDirectory() as directory:
            packets, rows, paths = self.make_reference(directory)
            reference = replay_rtp_reference.load_and_validate_reference(*paths)
            clock = FakeClock()
            sent = []
            transport = type("Transport", (), {"send_rtp": lambda _, packet: sent.append(packet)})()
            send_log = pathlib.Path(directory) / "send-times.jsonl"

            replay_rtp_reference.replay_reference(
                reference,
                transport,
                send_log,
                settle_seconds=4.0,
                monotonic_ns=clock.monotonic_ns,
                sleeper=clock.sleep,
            )

            self.assertEqual(sent, packets)
            self.assertEqual(clock.sleeps, [4.0, 0.04, 0.061])
            logged = [json.loads(line) for line in send_log.read_text().splitlines()]
            self.assertEqual(
                [row["target_monotonic_ns"] for row in logged],
                [5_000_000_000, 5_040_000_000, 5_101_000_000],
            )
            self.assertEqual(
                [row["actual_monotonic_ns"] for row in logged],
                [5_000_000_000, 5_040_000_000, 5_101_000_000],
            )
            self.assertEqual([row["lateness_ns"] for row in logged], [0, 0, 0])
            self.assertEqual(
                [row["captured_relative_ns"] for row in logged],
                [manifest["relative_monotonic_ns"] for manifest in rows],
            )
            self.assertEqual([row["packet_size"] for row in logged], list(map(len, packets)))
            self.assertEqual([row["seq"] for row in logged], [10, 11, 12])

    def test_validation_rejects_all_reference_mismatches(self):
        with tempfile.TemporaryDirectory() as directory:
            packets, rows, _ = self.make_reference(directory)
            cases = {
                "zero packets": ([], [], "nonzero"),
                "unequal counts": (packets, rows[:-1], "counts"),
                "packet index": (packets, [{**rows[0], "packet_index": 1}, *rows[1:]], "packet_index"),
                "decreasing time": (
                    packets,
                    [rows[0], {**rows[1], "relative_monotonic_ns": 1}, rows[2]],
                    "nondecreasing",
                ),
                "packet size": (packets, [{**rows[0], "packet_size": 1}, *rows[1:]], "packet_size"),
                "payload digest": (
                    packets,
                    [{**rows[0], "payload_sha256": hashlib.sha256(b"wrong").hexdigest()}, *rows[1:]],
                    "payload_sha256",
                ),
                "RTP metadata": (
                    packets,
                    [{**rows[0], "timestamp": rows[0]["timestamp"] + 1}, *rows[1:]],
                    "timestamp",
                ),
            }
            for name, (case_packets, case_rows, message) in cases.items():
                with self.subTest(name=name), tempfile.TemporaryDirectory(dir=directory) as case_dir:
                    paths = write_reference(case_dir, case_packets, case_rows)
                    with self.assertRaisesRegex(ValueError, message):
                        replay_rtp_reference.load_and_validate_reference(*paths)

    def test_reference_file_and_count_limits_are_explicit(self):
        self.assertEqual(
            replay_rtp_reference.MAX_PACKET_CAPTURE_BYTES, 128 * 1024 * 1024
        )
        self.assertEqual(
            replay_rtp_reference.MAX_MANIFEST_BYTES, 64 * 1024 * 1024
        )
        self.assertEqual(replay_rtp_reference.MAX_MANIFEST_LINE_BYTES, 16 * 1024)
        self.assertEqual(replay_rtp_reference.MAX_REFERENCE_PACKET_COUNT, 1_000_000)
        self.assertEqual(replay_rtp_reference.MAX_SETTLE_SECONDS, 60.0)
        self.assertEqual(
            replay_rtp_reference.MAX_INTER_PACKET_GAP_NS, 10_000_000_000
        )
        self.assertEqual(
            replay_rtp_reference.MAX_TOTAL_REFERENCE_DURATION_NS,
            6 * 60 * 60 * 1_000_000_000,
        )

    def test_settle_validation_rejects_wrong_types(self):
        for value in (True, "4"):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, "settle seconds"
            ):
                replay_rtp_reference._validate_settle_seconds(value)

    def test_rejects_oversized_capture_by_stat_without_reading_sparse_file(self):
        with tempfile.TemporaryDirectory() as directory:
            packet_path = pathlib.Path(directory) / "packets.bin"
            manifest_path = pathlib.Path(directory) / "manifest.jsonl"
            with packet_path.open("wb") as output:
                output.truncate(
                    replay_rtp_reference.MAX_PACKET_CAPTURE_BYTES + 1
                )
            manifest_path.write_text("{}\n")

            with self.assertRaisesRegex(ValueError, "packet capture.*exceeds"):
                replay_rtp_reference.load_and_validate_reference(
                    packet_path, manifest_path
                )

    def test_rejects_oversized_manifest_by_stat_without_reading_sparse_file(self):
        with tempfile.TemporaryDirectory() as directory:
            packet = make_rtp_packet(b"one", sequence=1, timestamp=1)
            packet_path, manifest_path = write_reference(
                directory, [packet], [manifest_row(packet, 0, 0)]
            )
            with manifest_path.open("wb") as output:
                output.truncate(replay_rtp_reference.MAX_MANIFEST_BYTES + 1)

            with self.assertRaisesRegex(ValueError, "manifest.*exceeds"):
                replay_rtp_reference.load_and_validate_reference(
                    packet_path, manifest_path
                )

    def test_rejects_manifest_line_over_limit_with_bounded_read(self):
        with tempfile.TemporaryDirectory() as directory:
            packet = make_rtp_packet(b"one", sequence=1, timestamp=1)
            packet_path = pathlib.Path(directory) / "packets.bin"
            manifest_path = pathlib.Path(directory) / "manifest.jsonl"
            packet_path.write_bytes(struct.pack("!I", len(packet)) + packet)
            manifest_path.write_bytes(
                b"{" + b" " * replay_rtp_reference.MAX_MANIFEST_LINE_BYTES
            )

            with self.assertRaisesRegex(ValueError, "manifest line 1 exceeds"):
                replay_rtp_reference.load_and_validate_reference(
                    packet_path, manifest_path
                )

    def test_rejects_packet_count_limit_without_loading_extra_packets(self):
        with tempfile.TemporaryDirectory() as directory:
            packets = [
                make_rtp_packet(bytes([index]), sequence=index, timestamp=index)
                for index in range(3)
            ]
            rows = [manifest_row(packet, index, index) for index, packet in enumerate(packets)]
            paths = write_reference(directory, packets, rows)

            with patch.object(
                replay_rtp_reference, "MAX_REFERENCE_PACKET_COUNT", 2, create=True
            ), self.assertRaisesRegex(ValueError, "packet count exceeds 2"):
                replay_rtp_reference.load_and_validate_reference(*paths)

    def test_send_error_does_not_publish_a_complete_log(self):
        with tempfile.TemporaryDirectory() as directory:
            _, _, paths = self.make_reference(directory)
            reference = replay_rtp_reference.load_and_validate_reference(*paths)
            send_log = pathlib.Path(directory) / "send-times.jsonl"
            send_log.write_text("stale complete log\n")
            clock = FakeClock()

            class FailingTransport:
                count = 0

                def send_rtp(self, packet):
                    self.count += 1
                    if self.count == 2:
                        raise RuntimeError("send failed")

            with self.assertRaisesRegex(RuntimeError, "send failed"):
                replay_rtp_reference.replay_reference(
                    reference,
                    FailingTransport(),
                    send_log,
                    settle_seconds=0,
                    monotonic_ns=clock.monotonic_ns,
                    sleeper=clock.sleep,
                )

            self.assertFalse(send_log.exists())

    def test_invalid_settle_does_not_leave_stale_log(self):
        with tempfile.TemporaryDirectory() as directory:
            _, _, paths = self.make_reference(directory)
            reference = replay_rtp_reference.load_and_validate_reference(*paths)
            send_log = pathlib.Path(directory) / "send-times.jsonl"
            send_log.write_text("stale complete log\n")

            with self.assertRaisesRegex(ValueError, "settle seconds"):
                replay_rtp_reference.replay_reference(
                    reference,
                    object(),
                    send_log,
                    settle_seconds=float("nan"),
                )

            self.assertFalse(send_log.exists())

    def test_cli_validates_before_opening_network_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            packets = [make_rtp_packet(b"one", sequence=1, timestamp=1)]
            rows = [manifest_row(packets[0], 4, 0)]
            packet_path, manifest_path = write_reference(directory, packets, rows)
            send_log = pathlib.Path(directory) / "send.jsonl"

            with patch.object(replay_rtp_reference, "open_backchannel_transport") as opener, \
                    redirect_stderr(StringIO()):
                result = replay_rtp_reference.main([
                    "--host", "example.invalid",
                    "--user", "fake-user",
                    "--pass", "fake-password",
                    "--packets", str(packet_path),
                    "--manifest", str(manifest_path),
                    "--send-log", str(send_log),
                ])

            self.assertEqual(result, 1)
            opener.assert_not_called()
            self.assertFalse(send_log.exists())

    def test_cli_has_required_endpoint_arguments_and_rejects_udp(self):
        parser = replay_rtp_reference.build_argument_parser()
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            parser.parse_args([])
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            parser.parse_args([
                "--host", "example.invalid",
                "--user", "fake-user",
                "--pass", "fake-password",
                "--packets", "packets.bin",
                "--manifest", "manifest.jsonl",
                "--send-log", "send.jsonl",
                "--transport", "udp",
            ])

    def test_cli_help_runs_from_repo_root_without_pythonpath(self):
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)

        completed = subprocess.run(
            [sys.executable, str(REPLAY_TOOL), "--help"],
            cwd=ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--packets", completed.stdout)
        self.assertIn("--manifest", completed.stdout)

    def test_cli_redacts_credentials_from_transport_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            _, _, paths = self.make_reference(directory)
            send_log = pathlib.Path(directory) / "send.jsonl"
            error_output = StringIO()

            with patch.object(
                replay_rtp_reference,
                "open_backchannel_transport",
                side_effect=RuntimeError(
                    "raw=admin-secret user=admin mixed=admin%2Dsecret "
                    "encoded=%61%64%6D%69%6E%2D%73%65%63%72%65%74"
                ),
            ), redirect_stderr(error_output):
                result = replay_rtp_reference.main([
                    "--host", "example.invalid",
                    "--user", "admin",
                    "--pass", "admin-secret",
                    "--packets", str(paths[0]),
                    "--manifest", str(paths[1]),
                    "--send-log", str(send_log),
                ])

            self.assertEqual(result, 1)
            self.assertNotIn("admin", error_output.getvalue().lower())
            self.assertNotIn("-secret", error_output.getvalue().lower())
            self.assertNotIn("%2dsecret", error_output.getvalue().lower())
            self.assertNotIn("%61%64", error_output.getvalue().lower())
            self.assertFalse(send_log.exists())

    def test_error_redaction_covers_plus_encoded_credentials(self):
        redacted = replay_rtp_reference._redacted_error(
            RuntimeError("credential=admin+user"), ("admin user",)
        )

        self.assertNotIn("admin+user", redacted)

    def test_error_redaction_prefers_encoded_password_over_username_prefix(self):
        redacted = replay_rtp_reference._redacted_error(
            RuntimeError("password=%2D"), ("%2", "-")
        )

        self.assertEqual(redacted, "password=<redacted>")

    def test_error_redaction_prefers_utf8_password_over_encoded_username_prefix(self):
        redacted = replay_rtp_reference._redacted_error(
            RuntimeError("password=%C3%A9"), ("%C3", "é")
        )

        self.assertEqual(redacted, "password=<redacted>")

    def test_error_redaction_normalizes_mixed_encodings(self):
        cases = (
            ("password=admin%2D%73ecret", ("admin", "admin-secret")),
            ("password=%61dmin%2Dsecret", ("admin", "admin-secret")),
            ("password=a+b%20c", ("a b c",)),
        )

        for message, credentials in cases:
            with self.subTest(message=message):
                redacted = replay_rtp_reference._redacted_error(
                    RuntimeError(message), credentials
                )

                self.assertEqual(redacted, "password=<redacted>")

    def test_error_redaction_normalizes_nested_encoding(self):
        redacted = replay_rtp_reference._redacted_error(
            RuntimeError("password=admin%25252Dsecret"),
            ("admin", "admin-secret"),
        )

        self.assertEqual(redacted, "password=<redacted>")

    def test_error_redaction_handles_literal_plus_and_percent_credentials(self):
        cases = (
            ("credential=a+b", ("a+b",)),
            ("credential=%2D", ("%2D",)),
            ("credential=key%value", ("key%value",)),
        )

        for message, credentials in cases:
            with self.subTest(message=message):
                redacted = replay_rtp_reference._redacted_error(
                    RuntimeError(message), credentials
                )

                self.assertEqual(redacted, "credential=<redacted>")

    def test_settle_bounds_fail_before_network(self):
        with tempfile.TemporaryDirectory() as directory:
            _, _, paths = self.make_reference(directory)
            for settle in ("-0.1", "60.1", "nan", "inf"):
                with self.subTest(settle=settle), patch.object(
                    replay_rtp_reference, "open_backchannel_transport"
                ) as opener, patch.object(
                    replay_rtp_reference, "replay_reference"
                ), redirect_stderr(StringIO()):
                    result = replay_rtp_reference.main([
                        "--host", "example.invalid",
                        "--user", "fake-user",
                        "--pass", "fake-password",
                        "--packets", str(paths[0]),
                        "--manifest", str(paths[1]),
                        "--send-log", str(pathlib.Path(directory) / "send.jsonl"),
                        "--settle-seconds", settle,
                    ])

                self.assertEqual(result, 1)
                opener.assert_not_called()

    def test_reference_timing_bounds_fail_before_network(self):
        with tempfile.TemporaryDirectory() as directory:
            packets, rows, _ = self.make_reference(directory)
            cases = {
                "type": [{**rows[0], "relative_monotonic_ns": "0"}, *rows[1:]],
                "negative": [{**rows[0], "relative_monotonic_ns": -1}, *rows[1:]],
                "gap": [
                    {**rows[0], "relative_monotonic_ns": 0},
                    {**rows[1], "relative_monotonic_ns": 10_000_000_001},
                    {**rows[2], "relative_monotonic_ns": 10_000_000_002},
                ],
            }
            for name, case_rows in cases.items():
                with self.subTest(name=name), tempfile.TemporaryDirectory(
                    dir=directory
                ) as case_directory:
                    paths = write_reference(case_directory, packets, case_rows)
                    send_log = pathlib.Path(case_directory) / "send.jsonl"
                    with patch.object(
                        replay_rtp_reference, "open_backchannel_transport"
                    ) as opener, patch.object(
                        replay_rtp_reference, "replay_reference"
                    ), redirect_stderr(StringIO()):
                        result = replay_rtp_reference.main([
                            "--host", "example.invalid",
                            "--user", "fake-user",
                            "--pass", "fake-password",
                            "--packets", str(paths[0]),
                            "--manifest", str(paths[1]),
                            "--send-log", str(send_log),
                        ])
                    self.assertEqual(result, 1)
                    opener.assert_not_called()
                    self.assertFalse(send_log.exists())

    def test_total_reference_duration_fails_before_network(self):
        with tempfile.TemporaryDirectory() as directory:
            packets, rows, _ = self.make_reference(directory)
            rows = [
                {**rows[0], "relative_monotonic_ns": 0},
                {**rows[1], "relative_monotonic_ns": 10},
                {**rows[2], "relative_monotonic_ns": 20},
            ]
            paths = write_reference(directory, packets, rows)

            with patch.object(
                replay_rtp_reference, "MAX_TOTAL_REFERENCE_DURATION_NS", 15, create=True
            ), patch.object(
                replay_rtp_reference, "open_backchannel_transport"
            ) as opener, patch.object(
                replay_rtp_reference, "replay_reference"
            ), redirect_stderr(StringIO()):
                result = replay_rtp_reference.main([
                    "--host", "example.invalid",
                    "--user", "fake-user",
                    "--pass", "fake-password",
                    "--packets", str(paths[0]),
                    "--manifest", str(paths[1]),
                    "--send-log", str(pathlib.Path(directory) / "send.jsonl"),
                ])

            self.assertEqual(result, 1)
            opener.assert_not_called()

    def test_teardown_failure_removes_completed_log_and_fails_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            _, _, paths = self.make_reference(directory)
            send_log = pathlib.Path(directory) / "send.jsonl"
            rtsp = TeardownFailureRtsp(
                "example.invalid", 554, "fake-user", "fake-password"
            )
            rtsp.session = "test-session"
            transport = onvif_play.BackchannelTransport(
                "rtsp://example.invalid/live", rtsp, "example.invalid", "tcp"
            )
            transport.rtp_channel = 6

            with patch.object(
                replay_rtp_reference,
                "open_backchannel_transport",
                return_value=transport,
            ), redirect_stderr(StringIO()):
                result = replay_rtp_reference.main([
                    "--host", "example.invalid",
                    "--user", "fake-user",
                    "--pass", "fake-password",
                    "--packets", str(paths[0]),
                    "--manifest", str(paths[1]),
                    "--send-log", str(send_log),
                    "--settle-seconds", "0",
                ])

            self.assertEqual(result, 1)
            self.assertFalse(send_log.exists())
            self.assertTrue(rtsp.closed)


class RtpSenderMainTest(unittest.TestCase):
    RANDOM_VALUES = (
        b"\x11\x22\x33\x44",
        b"\x55\x66",
        b"\x77\x88\x99\xaa",
    )

    def run_main(
        self, *extra_args, random_values=None, rtsp_type=FakeRtsp, clock=None
    ):
        FakeRtsp.instances.clear()
        argv = [
            "onvif_play.py",
            "--host", "example.invalid",
            "--user", "fake-user",
            "--pass", "fake-password",
            "--ms", "25",
            "--preroll-ms", "0",
            "--rtcp-interval", "0",
            *extra_args,
        ]
        output = StringIO()
        clock = clock or FakeClock()
        self.clock = clock
        values = self.RANDOM_VALUES if random_values is None else random_values
        with patch.object(sys, "argv", argv), \
                patch.object(onvif_play, "onvif_stream_uri", return_value=(
                    "rtsp://fake-user:fake-password@example.invalid/live",
                    "test-camera",
                )), patch.object(onvif_play, "Rtsp", rtsp_type), \
                patch.object(onvif_play, "time", clock), \
                patch.object(os, "urandom", side_effect=values) as urandom, \
                redirect_stdout(output):
            onvif_play.main()

        events = FakeRtsp.instances[0].events
        rtp_packets = [event[2] for event in events
                       if event[0] == "media" and event[1] == 6]
        rtcp_packets = [event[2] for event in events
                        if event[0] == "media" and event[1] == 7]
        return rtp_packets, rtcp_packets, output.getvalue(), urandom

    def test_default_sender_identity_ignores_server_advertised_tuple(self):
        packets, _, output, urandom = self.run_main()

        first = parse_rtp_packet(packets[0])
        self.assertEqual(first.ssrc, 0x11223344)
        self.assertEqual(first.sequence, 0x5566)
        self.assertEqual(first.timestamp, 0x778899AA)
        self.assertEqual([call.args for call in urandom.call_args_list],
                         [(4,), (2,), (4,)])
        self.assertIn(
            "Server-advertised RTP: seq=0 rtptime=0 ssrc=01020304", output
        )
        self.assertIn(
            "Selected sender RTP: seq=21862 rtptime=2005440938 ssrc=11223344",
            output,
        )
        self.assertNotIn("fake-user", output)
        self.assertNotIn("fake-password", output)

    def test_legacy_identity_uses_server_advertised_tuple(self):
        packets, _, output, urandom = self.run_main(
            "--rtp-identity", "legacy", random_values=()
        )

        first = parse_rtp_packet(packets[0])
        self.assertEqual((first.ssrc, first.sequence, first.timestamp),
                         (0x01020304, 0, 0))
        urandom.assert_not_called()
        self.assertIn(
            "Selected sender RTP: seq=0 rtptime=0 ssrc=01020304 (legacy)",
            output,
        )

    def test_legacy_identity_randomizes_missing_values_in_prior_order(self):
        random_values = (
            b"\x01\x02",
            b"\x03\x04\x05\x06",
            b"\x07\x08\x09\x0a",
        )

        packets, _, _, urandom = self.run_main(
            "--rtp-identity", "legacy",
            random_values=random_values,
            rtsp_type=FakeRtspWithoutIdentity,
        )

        first = parse_rtp_packet(packets[0])
        self.assertEqual((first.sequence, first.timestamp, first.ssrc),
                         (0x0102, 0x03040506, 0x0708090A))
        self.assertEqual([call.args for call in urandom.call_args_list],
                         [(2,), (4,), (4,)])

    def test_default_marker_is_only_on_first_packet_across_preroll(self):
        packets, _, _, _ = self.run_main(
            "--ms", "20", "--preroll-ms", "20"
        )

        self.assertEqual([parse_rtp_packet(packet).marker for packet in packets],
                         [True, False])

    def test_audio_start_marker_preserves_preroll_transition(self):
        packets, _, _, _ = self.run_main(
            "--ms", "20",
            "--preroll-ms", "20",
            "--marker-mode", "audio-start",
        )

        self.assertEqual([parse_rtp_packet(packet).marker for packet in packets],
                         [True, True])

    def test_audio_start_without_preroll_marks_only_the_first_packet(self):
        packets, _, _, _ = self.run_main(
            "--ms", "20", "--marker-mode", "audio-start"
        )

        self.assertEqual([parse_rtp_packet(packet).marker for packet in packets],
                         [True])

    def test_packet_ms_drives_packet_samples_and_pacing(self):
        packets, _, _, _ = self.run_main(
            "--ms", "30", "--packet-ms", "12.5"
        )
        metadata = [parse_rtp_packet(packet) for packet in packets]

        self.assertEqual([meta.payload_size for meta in metadata], [100, 100, 40])
        self.assertEqual(
            [meta.timestamp for meta in metadata],
            [0x778899AA, 0x778899AA + 100, 0x778899AA + 200],
        )
        for actual, expected in zip(
            self.clock.sleeps, (0.0125, 0.0125, 0.005), strict=True
        ):
            self.assertAlmostEqual(actual, expected)
        self.assertEqual(
            [deadline - self.clock.start_ns
             for deadline in self.clock.sleep_deadlines_ns],
            [12_500_000, 25_000_000, 30_000_000],
        )
        self.assertEqual(self.clock.now_ns - self.clock.start_ns, 30_000_000)

    def test_aac_packetization_is_isolated_from_g711_packet_controls(self):
        frames = [b"\x11\x22", b"\x33"]

        for packet_ms in ("0.1", "nan", "1e100"):
            with self.subTest(packet_ms=packet_ms):
                with patch.object(
                    onvif_play, "file_aac", return_value=frames
                ) as encoder:
                    packets, reports, _, _ = self.run_main(
                        "--codec", "aac",
                        "--file", "fake.aac",
                        "--packet-ms", packet_ms,
                        "--preroll-ms", "37",
                        "--marker-mode", "first",
                        "--rtcp-interval", "10",
                        rtsp_type=FakeAacRtsp,
                    )

                metadata = [parse_rtp_packet(packet) for packet in packets]
                encoder.assert_called_once_with("fake.aac", 0.25, 8000, 37)
                self.assertEqual([packet[12:] for packet in packets], [
                    b"\x00\x10\x00\x10\x11\x22",
                    b"\x00\x10\x00\x08\x33",
                ])
                self.assertEqual(
                    [meta.timestamp for meta in metadata],
                    [0x778899AA, 0x778899AA + 1024],
                )
                # Each packet carries one complete AAC access unit, so every packet is marked.
                self.assertEqual([meta.marker for meta in metadata], [True, True])
                for actual in self.clock.sleeps:
                    self.assertAlmostEqual(actual, 1024 / 8000)
                report_timestamps = [
                    struct.unpack_from("!BBHIIIIII", report)[6]
                    for report in reports
                ]
                self.assertEqual(
                    report_timestamps,
                    [0x778899AA, (0x778899AA + 2048) & 0xFFFFFFFF],
                )

    def test_rtcp_maps_first_periodic_and_final_reports_to_send_timeline(self):
        packets, reports, _, _ = self.run_main(
            "--ms", "30",
            "--packet-ms", "12.5",
            "--rtcp-interval", "0.01",
        )

        self.assertEqual(len(packets), 3)
        fields = [struct.unpack_from("!BBHIIIIII", report) for report in reports]
        self.assertEqual([report[3] for report in fields], [0x11223344] * 4)
        mono_start = self.clock.start_ns / 1_000_000_000
        elapsed_ns = [0, 12_500_000, 25_000_000, 30_000_000]
        sent_samples = [100, 200, 240, 240]
        mapped_samples = [
            min(
                math.floor(
                    ((self.clock.start_ns + elapsed) / 1_000_000_000
                     - mono_start) * 8000
                ),
                sent,
            )
            for elapsed, sent in zip(elapsed_ns, sent_samples, strict=True)
        ]
        self.assertEqual(
            [report[6] for report in fields],
            [(0x778899AA + samples) & 0xFFFFFFFF
             for samples in mapped_samples],
        )
        self.assertEqual([report[7] for report in fields], [1, 2, 3, 3])
        self.assertEqual([report[8] for report in fields], [100, 200, 240, 240])

    def test_rtcp_uses_session_clock_under_sleep_overshoot_and_timestamp_wrap(self):
        initial_timestamp = 0xFFFFFF80
        clock = FakeClock(sleep_overshoot_ns=3_906_250)
        random_values = (
            b"\x11\x22\x33\x44",
            b"\x55\x66",
            initial_timestamp.to_bytes(4, "big"),
        )

        packets, reports, _, _ = self.run_main(
            "--ms", "45",
            "--packet-ms", "15.625",
            "--rtcp-interval", "0.01",
            random_values=random_values,
            clock=clock,
        )

        self.assertEqual(
            [parse_rtp_packet(packet).payload_size for packet in packets],
            [125, 125, 110],
        )
        fields = [struct.unpack_from("!BBHIIIIII", report) for report in reports]
        elapsed_ns = [0, 19_531_250, 35_156_250, 48_906_250]
        sent_samples = [125, 250, 360, 360]
        mapped_samples = [
            min((elapsed * 8000) // 1_000_000_000, sent)
            for elapsed, sent in zip(elapsed_ns, sent_samples, strict=True)
        ]
        self.assertEqual(mapped_samples, [0, 156, 281, 360])
        self.assertEqual(
            [report[6] for report in fields],
            [(initial_timestamp + samples) & 0xFFFFFFFF
             for samples in mapped_samples],
        )
        self.assertEqual([report[7] for report in fields], [1, 2, 3, 3])
        self.assertEqual([report[8] for report in fields], [125, 250, 360, 360])

        wall_start = 1_700_000_001.0
        for report, elapsed in zip(fields, elapsed_ns, strict=True):
            actual_unix_time = (
                report[4] - 2_208_988_800 + report[5] / (1 << 32)
            )
            self.assertAlmostEqual(
                actual_unix_time,
                wall_start + elapsed / 1_000_000_000,
                places=6,
            )

        final_packet = parse_rtp_packet(packets[-1])
        self.assertEqual(
            fields[-1][6],
            (final_packet.timestamp + final_packet.payload_size) & 0xFFFFFFFF,
        )

    def test_rtcp_before_any_rtp_uses_initial_timestamp_and_zero_counters(self):
        packets, reports, _, _ = self.run_main(
            "--ms", "0", "--rtcp-interval", "10"
        )

        self.assertEqual(packets, [])
        self.assertEqual(len(reports), 1)
        fields = struct.unpack_from("!BBHIIIIII", reports[0])
        self.assertEqual(fields[3], 0x11223344)
        self.assertEqual(fields[6], 0x778899AA)
        self.assertEqual(fields[7], 0)
        self.assertEqual(fields[8], 0)

    def test_rejects_oversized_non_aac_packets_before_network(self):
        cases = (
            ("pcma", "tcp", "8190.5", "65535"),
            ("l16", "tcp", "4095.25", "65535"),
            ("pcma", "udp", "8187", "65507"),
            ("l16", "udp", "4093.5", "65507"),
        )

        for codec, transport, packet_ms, limit in cases:
            argv = [
                "onvif_play.py",
                "--codec", codec,
                "--transport", transport,
                "--packet-ms", packet_ms,
            ]
            with self.subTest(codec=codec, transport=transport):
                with patch.object(sys, "argv", argv), patch.object(
                    onvif_play,
                    "onvif_stream_uri",
                    side_effect=AssertionError("network resolver called"),
                ) as resolver, redirect_stderr(StringIO()) as stderr, \
                        self.assertRaises(SystemExit):
                    onvif_play.main()

                resolver.assert_not_called()
                self.assertIn("RTP packet size", stderr.getvalue())
                self.assertIn(limit, stderr.getvalue())

    def test_accepts_transport_packet_boundaries_and_common_durations(self):
        cases = (
            ("pcma", "tcp", "8190.375"),
            ("l16", "tcp", "4095.125"),
            ("pcma", "udp", "8186.875"),
            ("l16", "udp", "4093.375"),
            ("pcma", "tcp", "20"),
            ("l16", "tcp", "40"),
            ("pcma", "udp", "40"),
            ("l16", "udp", "20"),
        )

        class ResolverReached(RuntimeError):
            pass

        for codec, transport, packet_ms in cases:
            argv = [
                "onvif_play.py",
                "--codec", codec,
                "--transport", transport,
                "--packet-ms", packet_ms,
            ]
            with self.subTest(
                codec=codec, transport=transport, packet_ms=packet_ms
            ), patch.object(sys, "argv", argv), patch.object(
                onvif_play, "onvif_stream_uri", side_effect=ResolverReached
            ) as resolver, redirect_stdout(StringIO()), self.assertRaises(
                ResolverReached
            ):
                onvif_play.main()

            resolver.assert_called_once()

    def test_rejects_nonpositive_or_nonintegral_packet_duration_before_network(self):
        for value, message in (("0", "positive"), ("0.1", "integral sample count")):
            with self.subTest(value=value), patch.object(
                sys, "argv", ["onvif_play.py", "--packet-ms", value]
            ), patch.object(onvif_play, "onvif_stream_uri") as resolver, \
                    redirect_stderr(StringIO()) as stderr, \
                    self.assertRaises(SystemExit):
                onvif_play.main()
            resolver.assert_not_called()
            self.assertIn(message, stderr.getvalue())

    def test_pacer_only_accepts_legacy(self):
        with patch.object(
            sys, "argv", ["onvif_play.py", "--pacer", "adaptive"]
        ), patch.object(onvif_play, "onvif_stream_uri") as resolver, \
                redirect_stderr(StringIO()) as stderr, \
                self.assertRaises(SystemExit):
            onvif_play.main()

        resolver.assert_not_called()
        self.assertIn("invalid choice", stderr.getvalue())


class BackchannelRequestTest(unittest.TestCase):
    def test_play_request_requires_onvif_backchannel(self):
        FakeRtsp.instances.clear()
        argv = [
            "onvif_play.py",
            "--host", "example.invalid",
            "--user", "fake-user",
            "--pass", "fake-password",
            "--ms", "0",
            "--preroll-ms", "0",
            "--rtcp-interval", "0",
        ]

        with patch.object(sys, "argv", argv), \
                patch.object(onvif_play, "onvif_stream_uri",
                             return_value=("rtsp://example.invalid/live", "test-camera")), \
                patch.object(onvif_play, "Rtsp", FakeRtsp), \
                redirect_stdout(StringIO()):
            onvif_play.main()

        play = next(request for request in FakeRtsp.instances[0].requests
                    if request[0] == "PLAY")
        self.assertEqual(play[2].get("Require"), onvif_play.BACKCHANNEL)

    def test_main_does_not_log_credentials_embedded_in_stream_uri(self):
        FakeRtsp.instances.clear()
        argv = [
            "onvif_play.py",
            "--host", "example.invalid",
            "--user", "fake-user",
            "--pass", "fake-password",
            "--ms", "0",
            "--preroll-ms", "0",
            "--rtcp-interval", "0",
        ]

        output = StringIO()
        with patch.object(sys, "argv", argv), \
                patch.object(onvif_play, "onvif_stream_uri", return_value=(
                    "rtsp://fake-user:fake-password@example.invalid/live",
                    "test-camera",
                )), patch.object(onvif_play, "Rtsp", FakeRtsp), redirect_stdout(output):
            onvif_play.main()

        self.assertNotIn("fake-user", output.getvalue())
        self.assertNotIn("fake-password", output.getvalue())
        self.assertIn("rtsp://example.invalid/live", output.getvalue())


if __name__ == "__main__":
    unittest.main()
