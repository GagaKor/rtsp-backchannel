import importlib
import io
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


if __name__ == "__main__":
    unittest.main()
