import importlib
import io
import json
import pathlib
import tomllib
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch


class LibraryApiTests(unittest.TestCase):
    def test_exports_one_shot_playback_api(self):
        library = importlib.import_module("onvif_backchannel")

        self.assertTrue(callable(getattr(library, "play_file", None)))
        self.assertIsNotNone(getattr(library, "PlaybackResult", None))
        self.assertTrue(callable(getattr(library, "discover_devices", None)))
        self.assertTrue(callable(getattr(library, "get_stream_uris", None)))
        self.assertIsNotNone(getattr(library, "DiscoveredDevice", None))
        self.assertIsNotNone(getattr(library, "StreamUri", None))

    def test_plays_pcma_in_40ms_packets_and_closes_the_session(self):
        from onvif_backchannel import PlaybackResult, play_file
        from onvif_backchannel import playback

        payload = bytes([0xD5]) * 640

        class FakeSession:
            send_track = (
                "m=audio 0 RTP/AVP 8\r\n"
                "a=rtpmap:8 PCMA/8000\r\n"
                "a=sendonly\r\n"
            )
            rtp_channel = 10

            def __init__(self):
                self.sent = []
                self.closed = 0
                self.keepalive_checks = 0

            def send_rtp(self, packet):
                self.sent.append(packet)

            def check_keepalive(self):
                self.keepalive_checks += 1

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                self.closed += 1

        class FakePacer:
            def __init__(self):
                self.waited = []
                self.finished = 0

            def wait(self, samples):
                self.waited.append(samples)

            def finish(self):
                self.finished += 1

        session = FakeSession()
        pacer = FakePacer()
        decode = Mock(return_value=payload)
        open_session = Mock(return_value=session)

        with (
            patch.object(playback, "file_audio", decode, create=True),
            patch.object(
                playback,
                "open_backchannel_transport",
                open_session,
                create=True,
            ),
            patch.object(
                playback,
                "RtpPacer",
                Mock(return_value=pacer),
                create=True,
            ),
        ):
            result = play_file(
                host="camera",
                user="admin",
                password="secret",
                file="event.mp3",
                volume=0.05,
            )

        self.assertEqual(
            result,
            PlaybackResult(
                codec="PCMA",
                sample_rate=8000,
                payload_type=8,
                rtp_channel=10,
                encoded_bytes=640,
                packets_sent=2,
                duration_seconds=0.08,
            ),
        )
        decode.assert_called_once_with(
            "event.mp3", "pcma", 0.05, 8000, encoder="python-alaw"
        )
        open_session.assert_called_once_with(
            "camera", "admin", "secret", transport="tcp"
        )
        self.assertEqual([len(packet) for packet in session.sent], [332, 332])
        self.assertEqual(session.sent[0][1] & 0x80, 0x80)
        self.assertEqual(session.sent[1][1] & 0x80, 0)
        self.assertEqual(pacer.waited, [320, 320])
        self.assertEqual(pacer.finished, 1)
        self.assertEqual(session.closed, 1)

    def test_declares_installable_wheel_metadata(self):
        metadata = tomllib.loads(
            pathlib.Path("python/pyproject.toml").read_text(encoding="utf-8")
        )

        self.assertEqual(metadata["project"]["name"], "onvif-backchannel")
        self.assertEqual(metadata["project"]["version"], "0.1.0")
        self.assertEqual(metadata["project"]["requires-python"], ">=3.11")
        self.assertEqual(metadata["project"]["license"], "MIT OR Apache-2.0")
        self.assertEqual(metadata["project"]["readme"], "README.md")
        self.assertEqual(
            metadata["project"]["license-files"],
            [
                "LICENSE",
                "LICENSE-MIT",
                "LICENSE-APACHE",
                "THIRD_PARTY_NOTICES.md",
            ],
        )
        for filename in metadata["project"]["license-files"]:
            self.assertTrue(pathlib.Path("python", filename).is_file())
        self.assertEqual(
            metadata["project"]["scripts"]["onvif-backchannel"],
            "onvif_backchannel.cli:main",
        )
        self.assertEqual(
            metadata["tool"]["setuptools"]["py-modules"],
            ["backchannel_audio", "backchannel_rtp", "onvif_play"],
        )

    def test_installed_cli_delegates_to_the_public_play_file_api(self):
        cli = importlib.import_module("onvif_backchannel.cli")
        result = Mock(packets_sent=2)

        with (
            patch.object(cli, "play_file", return_value=result) as play,
            redirect_stdout(io.StringIO()) as output,
        ):
            cli.main(
                [
                    "--host",
                    "camera",
                    "--user",
                    "admin",
                    "--pass",
                    "secret",
                    "--file",
                    "event.mp3",
                    "--volume",
                    "0.05",
                ]
            )

        play.assert_called_once_with(
            host="camera",
            user="admin",
            password="secret",
            file="event.mp3",
            volume=0.05,
        )
        self.assertEqual(output.getvalue(), "sent 2 RTP packets\n")

    def test_installed_cli_dispatches_discovery_as_json_lines(self):
        cli = importlib.import_module("onvif_backchannel.cli")
        library = importlib.import_module("onvif_backchannel.onvif")
        device = library.DiscoveredDevice(
            ip="10.128.10.141",
            xaddrs=["http://10.128.10.141/onvif/device_service"],
            scopes=["onvif://www.onvif.org/name/Front%20Door"],
            name="Front Door",
            endpoint_reference="urn:uuid:camera-1",
        )

        with (
            patch.object(
                cli, "discover_devices", return_value=[device]
            ) as discover,
            redirect_stdout(io.StringIO()) as output,
        ):
            cli.main(
                [
                    "discover",
                    "--timeout-ms",
                    "1500",
                    "--interface",
                    "10.0.0.10",
                    "--interface",
                    "192.168.0.20",
                ]
            )

        discover.assert_called_once_with(
            timeout=1.5,
            interfaces=["10.0.0.10", "192.168.0.20"],
        )
        self.assertEqual(
            json.loads(output.getvalue()),
            {
                "ip": "10.128.10.141",
                "xaddrs": ["http://10.128.10.141/onvif/device_service"],
                "scopes": ["onvif://www.onvif.org/name/Front%20Door"],
                "name": "Front Door",
                "endpointReference": "urn:uuid:camera-1",
            },
        )

    def test_installed_cli_dispatches_stream_lookup_as_json_lines(self):
        cli = importlib.import_module("onvif_backchannel.cli")
        library = importlib.import_module("onvif_backchannel.onvif")
        stream = library.StreamUri(
            profile_token="main",
            profile_name="Main Stream",
            uri="rtsp://camera/live?channel=1&stream=main",
        )

        with (
            patch.object(
                cli, "get_stream_uris", return_value=[stream]
            ) as lookup,
            redirect_stdout(io.StringIO()) as output,
        ):
            cli.main(
                [
                    "streams",
                    "--host",
                    "camera",
                    "--user",
                    "admin@example.com",
                    "--pass",
                    "p@ss:/?#[]",
                    "--device-url",
                    "http://camera/onvif/device_service",
                ]
            )

        lookup.assert_called_once_with(
            host="camera",
            user="admin@example.com",
            password="p@ss:/?#[]",
            device_urls=["http://camera/onvif/device_service"],
        )
        self.assertEqual(
            json.loads(output.getvalue()),
            {
                "profileToken": "main",
                "profileName": "Main Stream",
                "uri": "rtsp://camera/live?channel=1&stream=main",
            },
        )


if __name__ == "__main__":
    unittest.main()
