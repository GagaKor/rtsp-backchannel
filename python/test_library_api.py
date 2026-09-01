import importlib
import io
import json
import os
import pathlib
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock, call, patch


def _minimal_capability_report():
    from rtsp_backchannel.capabilities import (
        CameraCapabilityReport,
        Media2CapabilityReport,
        PtzCapabilityReport,
    )
    from rtsp_backchannel.onvif import DeviceInfo

    return CameraCapabilityReport(
        device=DeviceInfo(),
        scopes=(),
        declared_profiles=(),
        service_discovery="unavailable",
        services=(),
        profiles=(),
        ptz=PtzCapabilityReport(
            detected=None,
            pan_tilt_supported=None,
            zoom_supported=None,
            profile_tokens=(),
            service_capabilities=None,
            nodes=(),
        ),
        media2=Media2CapabilityReport(
            detected=None,
            encodings=(),
            h265_supported=None,
        ),
        warnings=(),
    )


class LibraryApiTests(unittest.TestCase):
    def test_cli_password_defaults_ignore_environment(self):
        cli = importlib.import_module("rtsp_backchannel.cli")

        with patch.dict(os.environ, {"ONVIF_PASSWORD": "must-not-default"}):
            play_args = cli._parser().parse_args(
                ["--host", "camera", "--file", "event.mp3"]
            )
            stream_args = cli._streams_parser().parse_args(["--host", "camera"])

        self.assertEqual(play_args.password, "")
        self.assertEqual(stream_args.password, "")

    def test_exports_one_shot_playback_api(self):
        library = importlib.import_module("rtsp_backchannel")

        self.assertTrue(callable(getattr(library, "play_file", None)))
        self.assertIsNotNone(getattr(library, "PlaybackResult", None))
        self.assertTrue(callable(getattr(library, "discover_devices", None)))
        self.assertTrue(callable(getattr(library, "get_stream_uris", None)))
        self.assertIsNotNone(getattr(library, "DiscoveredDevice", None))
        self.assertIsNotNone(getattr(library, "StreamUri", None))

    def test_exports_complete_camera_capability_reporting_contract(self):
        library = importlib.import_module("rtsp_backchannel")
        capabilities = importlib.import_module(
            "rtsp_backchannel.capabilities"
        )
        onvif = importlib.import_module("rtsp_backchannel.onvif")
        capability_exports = (
            "CameraCapabilityVersion",
            "CameraCapabilityService",
            "CameraCapabilityWarning",
            "CameraCapabilityProfile",
            "PtzSpaces",
            "PtzNode",
            "PtzServiceCapabilities",
            "PtzCapabilityReport",
            "Media2CapabilityReport",
            "CameraCapabilityReport",
            "get_camera_capabilities",
        )

        for name in capability_exports:
            with self.subTest(name=name):
                self.assertIs(
                    getattr(library, name, None),
                    getattr(capabilities, name),
                )
                self.assertIn(name, library.__all__)
        self.assertIs(getattr(library, "DeviceInfo", None), onvif.DeviceInfo)
        self.assertIn("DeviceInfo", library.__all__)

    def test_exports_complete_ptz_session_contract(self):
        library = importlib.import_module("rtsp_backchannel")
        ptz = importlib.import_module("rtsp_backchannel.ptz")
        ptz_exports = (
            "open_ptz_session",
            "PtzSession",
            "PtzSessionOptions",
            "PtzStatus",
            "PtzVector",
        )

        for name in ptz_exports:
            with self.subTest(name=name):
                self.assertIs(
                    getattr(library, name, None),
                    getattr(ptz, name),
                )
                self.assertIn(name, library.__all__)

    def test_capabilities_parser_preserves_defaults_and_repeatable_values(self):
        cli = importlib.import_module("rtsp_backchannel.cli")
        parser_factory = getattr(cli, "_capabilities_parser", None)
        self.assertTrue(callable(parser_factory))
        parser = parser_factory()

        defaults = parser.parse_args(["--host", "camera.local"])
        explicit = parser.parse_args(
            [
                "--host",
                "camera.local",
                "--user",
                "operator",
                "--pass",
                "",
                "--device-url",
                "http://camera.local/onvif/device_service",
                "--device-url",
                "http://camera.local:8000/onvif/device_service",
                "--timeout-ms",
                "2500.5",
            ]
        )

        self.assertEqual(defaults.host, "camera.local")
        self.assertEqual(defaults.user, "")
        self.assertIsNone(defaults.password)
        self.assertIsNone(defaults.device_urls)
        self.assertIsNone(defaults.timeout_ms)
        self.assertEqual(explicit.user, "operator")
        self.assertEqual(explicit.password, "")
        self.assertEqual(
            explicit.device_urls,
            [
                "http://camera.local/onvif/device_service",
                "http://camera.local:8000/onvif/device_service",
            ],
        )
        self.assertEqual(explicit.timeout_ms, 2500.5)

        for timeout_arguments in (
            ["--timeout-ms", "86400000"],
            ["--timeout-ms=86400000"],
            ["--timeout-ms", "86400000.000000001"],
            ["--timeout-ms=86400000.000000001"],
        ):
            with self.subTest(timeout_arguments=timeout_arguments):
                boundary = parser.parse_args(
                    ["--host", "camera.local", *timeout_arguments]
                )
                self.assertEqual(boundary.timeout_ms, 86_400_000.0)

    def test_capabilities_help_is_specific_and_never_opens_network(self):
        cli = importlib.import_module("rtsp_backchannel.cli")

        with (
            patch.object(
                cli, "get_camera_capabilities", create=True
            ) as capabilities,
            redirect_stdout(io.StringIO()) as output,
            redirect_stderr(io.StringIO()) as errors,
            self.assertRaises(SystemExit) as stopped,
        ):
            cli.main(
                [
                    "capabilities",
                    "--help",
                    "--pass",
                    "help-only-secret",
                ]
            )

        self.assertEqual(stopped.exception.code, 0)
        capabilities.assert_not_called()
        help_text = output.getvalue()
        for expected in (
            "rtsp-backchannel capabilities",
            "--host",
            "--user",
            "--pass",
            "--device-url",
            "--timeout-ms",
            "per-request",
            "repeatable",
        ):
            self.assertIn(expected, help_text)
        self.assertIn("capabilities", cli._parser().format_help())
        self.assertNotRegex(
            help_text + errors.getvalue(), "help-only-secret"
        )

    def test_plays_pcma_in_40ms_packets_and_closes_the_session(self):
        from rtsp_backchannel import PlaybackResult, play_file
        from rtsp_backchannel import playback

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

    def test_auto_codec_priority_is_independent_of_sdp_payload_order(self):
        from rtsp_backchannel import playback

        offers = {
            "pcma": (104, "PCMA/8000", None),
            "pcmu": (105, "PCMU/8000", None),
            "g726-32": (106, "G726-32/8000", None),
            "g726-24": (107, "G726-24/8000", None),
            "g726-16": (108, "G726-16/8000", None),
            "g726-40": (109, "G726-40/8000", None),
            "aac": (
                110,
                "MPEG4-GENERIC/8000/1",
                (
                    "streamtype=5; profile-level-id=1; mode=AAC-hbr; "
                    "config=1588; SizeLength=13; IndexLength=3; "
                    "IndexDeltaLength=3"
                ),
            ),
        }

        def send_track(names):
            selected = [offers[name] for name in names]
            lines = [
                "m=audio 0 RTP/AVP "
                + " ".join(str(payload_type) for payload_type, _, _ in selected)
            ]
            for payload_type, encoding, fmtp in selected:
                lines.append(f"a=rtpmap:{payload_type} {encoding}")
                if fmtp is not None:
                    lines.append(f"a=fmtp:{payload_type} {fmtp}")
            lines.append("a=sendonly")
            return "\r\n".join(lines) + "\r\n"

        cases = [
            (
                [
                    "aac",
                    "g726-40",
                    "g726-16",
                    "g726-24",
                    "g726-32",
                    "pcmu",
                    "pcma",
                ],
                "pcma",
            ),
            (
                [
                    "aac",
                    "g726-40",
                    "g726-16",
                    "g726-24",
                    "g726-32",
                    "pcmu",
                ],
                "pcmu",
            ),
            (
                ["aac", "g726-40", "g726-16", "g726-24", "g726-32"],
                "g726-32",
            ),
            (["aac", "g726-40", "g726-16", "g726-24"], "g726-24"),
            (["aac", "g726-40", "g726-16"], "g726-16"),
            (["aac", "g726-40"], "g726-40"),
            (["aac"], "aac"),
        ]
        for names, expected in cases:
            with self.subTest(names=names):
                selected = playback._select_codec(send_track(names), "auto")
                self.assertEqual(selected.codec, expected)
                self.assertEqual(
                    selected.payload_type,
                    offers[expected][0],
                )

    def test_static_g711_payloads_are_supported_without_rtpmap(self):
        from rtsp_backchannel import playback

        track = "m=audio 0 RTP/AVP 0 8\r\na=sendonly\r\n"

        automatic = playback._select_codec(track, "auto")
        explicit = playback._select_codec(track, "pcmu")

        self.assertEqual(
            (automatic.codec, automatic.payload_type, automatic.sample_rate),
            ("pcma", 8, 8000),
        )
        self.assertEqual(
            (explicit.codec, explicit.payload_type, explicit.sample_rate),
            ("pcmu", 0, 8000),
        )

    def test_never_selects_wrong_rate_or_incompatible_aac_fmtp(self):
        from rtsp_backchannel import playback

        track = (
            "m=audio 0 RTP/AVP 96 97 98\r\n"
            "a=rtpmap:96 G726-32/16000\r\n"
            "a=rtpmap:97 MPEG4-GENERIC/8000/1\r\n"
            "a=fmtp:97 mode=AAC-lbr; config=1588; SizeLength=13; "
            "IndexLength=3; IndexDeltaLength=3\r\n"
            "a=rtpmap:98 OPUS/48000/2\r\n"
            "a=sendonly\r\n"
        )

        with self.assertRaisesRegex(RuntimeError, "no supported.*codec"):
            playback._select_codec(track, "auto")
        with self.assertRaisesRegex(RuntimeError, "AAC-hbr"):
            playback._select_codec(track, "aac")
        with self.assertRaisesRegex(RuntimeError, "g726-24.*not offered"):
            playback._select_codec(track, "g726-24")

    def test_aac_requires_matching_lc_config_and_rfc3640_header_lengths(self):
        from rtsp_backchannel import playback

        valid = (
            "m=audio 0 RTP/AVP 97\r\n"
            "a=rtpmap:97 MPEG4-GENERIC/8000/1\r\n"
            "a=fmtp:97 StreamType=5; MODE=AAC-hbr; CONFIG=1588; "
            "SizeLength=13; IndexLength=3; IndexDeltaLength=3\r\n"
            "a=sendonly\r\n"
        )
        selected = playback._select_codec(valid, "aac")
        self.assertEqual(
            (selected.codec, selected.payload_type, selected.sample_rate),
            ("aac", 97, 8000),
        )

        incompatible = [
            valid.replace("CONFIG=1588", "CONFIG=1210"),
            valid.replace("SizeLength=13", "SizeLength=12"),
            valid.replace("IndexLength=3", "IndexLength=2"),
            valid.replace("IndexDeltaLength=3", "IndexDeltaLength=2"),
        ]
        for track in incompatible:
            with self.subTest(track=track), self.assertRaisesRegex(
                RuntimeError, "unsupported AAC"
            ):
                playback._select_codec(track, "aac")

        missing_streamtype = valid.replace("StreamType=5; ", "")
        with self.assertRaisesRegex(RuntimeError, "streamtype must be 5"):
            playback._select_codec(missing_streamtype, "aac")

    def test_aac_rejects_non_1024_gaspecificconfig_modes(self):
        from rtsp_backchannel import playback

        template = (
            "m=audio 0 RTP/AVP 97\r\n"
            "a=rtpmap:97 MPEG4-GENERIC/8000/1\r\n"
            "a=fmtp:97 streamtype=5; mode=AAC-hbr; config={config}; "
            "SizeLength=13; IndexLength=3; IndexDeltaLength=3\r\n"
            "a=sendonly\r\n"
        )
        incompatible = {
            "158C": "frameLengthFlag",
            "158A": "dependsOnCoreCoder",
            "1589": "extensionFlag",
        }

        for config, diagnostic in incompatible.items():
            with self.subTest(config=config), self.assertRaisesRegex(
                RuntimeError, diagnostic
            ):
                playback._select_codec(
                    template.format(config=config), "aac"
                )

    def test_mp4a_latm_is_recognized_but_explicitly_rejected(self):
        from rtsp_backchannel import playback

        track = (
            "m=audio 0 RTP/AVP 96\r\n"
            "a=rtpmap:96 MP4A-LATM/8000/1\r\n"
            "a=sendonly\r\n"
        )

        for preference in ("auto", "aac"):
            with self.subTest(preference=preference), self.assertRaisesRegex(
                RuntimeError, "MP4A-LATM.*not supported"
            ):
                playback._select_codec(track, preference)

    def test_g726_uses_bit_rate_boundaries_for_rtp_timestamps_and_pacing(self):
        from rtsp_backchannel import PlaybackResult, play_file
        from rtsp_backchannel import playback

        payload = bytes(range(240))

        class FakeSession:
            send_track = (
                "m=audio 0 RTP/AVP 101\r\n"
                "a=rtpmap:101 G726-24/8000\r\n"
                "a=sendonly\r\n"
            )
            rtp_channel = 4

            def __init__(self):
                self.sent = []

            def send_rtp(self, packet):
                self.sent.append(packet)

            def check_keepalive(self):
                return None

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return None

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
        with (
            patch.object(playback, "file_g726", return_value=payload) as encode,
            patch.object(
                playback,
                "open_backchannel_transport",
                return_value=session,
            ),
            patch.object(playback, "RtpPacer", return_value=pacer),
        ):
            result = play_file(
                host="camera",
                user="",
                password="",
                file="event.mp3",
                codec="auto",
            )

        self.assertEqual(
            result,
            PlaybackResult(
                codec="G726-24",
                sample_rate=8000,
                payload_type=101,
                rtp_channel=4,
                encoded_bytes=240,
                packets_sent=2,
                duration_seconds=0.08,
            ),
        )
        encode.assert_called_once_with("event.mp3", 0.05, 8000, 3)
        self.assertEqual([packet[12:] for packet in session.sent], [payload[:120], payload[120:]])
        timestamps = [int.from_bytes(packet[4:8], "big") for packet in session.sent]
        self.assertEqual((timestamps[1] - timestamps[0]) & 0xFFFFFFFF, 320)
        self.assertEqual([bool(packet[1] & 0x80) for packet in session.sent], [True, False])
        self.assertEqual(pacer.waited, [320, 320])
        self.assertEqual(pacer.finished, 1)

    def test_aac_reuses_adts_frames_and_rfc3640_au_headers(self):
        from rtsp_backchannel import PlaybackResult, play_file
        from rtsp_backchannel import playback

        frames = [b"first", b"end"]

        class FakeSession:
            send_track = (
                "m=audio 0 RTP/AVP 97\r\n"
                "a=rtpmap:97 MPEG4-GENERIC/8000/1\r\n"
                "a=fmtp:97 streamtype=5; mode=AAC-hbr; config=1588; "
                "SizeLength=13; IndexLength=3; IndexDeltaLength=3\r\n"
                "a=sendonly\r\n"
            )
            rtp_channel = 6

            def __init__(self):
                self.sent = []

            def send_rtp(self, packet):
                self.sent.append(packet)

            def check_keepalive(self):
                return None

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return None

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
        with (
            patch.object(playback, "file_aac", return_value=frames) as encode,
            patch.object(
                playback,
                "open_backchannel_transport",
                return_value=session,
            ),
            patch.object(playback, "RtpPacer", return_value=pacer),
        ):
            result = play_file(
                host="camera",
                user="",
                password="",
                file="event.mp3",
                codec="aac",
            )

        self.assertEqual(
            result,
            PlaybackResult(
                codec="AAC",
                sample_rate=8000,
                payload_type=97,
                rtp_channel=6,
                encoded_bytes=8,
                packets_sent=2,
                duration_seconds=0.256,
            ),
        )
        encode.assert_called_once_with("event.mp3", 0.05, 8000, 0)
        self.assertEqual(
            [packet[12:] for packet in session.sent],
            [
                b"\x00\x10\x00\x28first",
                b"\x00\x10\x00\x18end",
            ],
        )
        timestamps = [int.from_bytes(packet[4:8], "big") for packet in session.sent]
        self.assertEqual((timestamps[1] - timestamps[0]) & 0xFFFFFFFF, 1024)
        self.assertEqual([bool(packet[1] & 0x80) for packet in session.sent], [True, True])
        self.assertEqual(pacer.waited, [1024, 1024])
        self.assertEqual(pacer.finished, 1)

    def test_declares_installable_wheel_metadata(self):
        metadata = tomllib.loads(
            pathlib.Path("python/pyproject.toml").read_text(encoding="utf-8")
        )

        self.assertEqual(metadata["project"]["name"], "rtsp-backchannel")
        self.assertEqual(metadata["project"]["version"], "0.4.0")
        self.assertEqual(metadata["project"]["requires-python"], ">=3.11")
        self.assertEqual(metadata["project"]["license"], "MIT OR Apache-2.0")
        self.assertEqual(metadata["project"]["readme"], "README.md")
        for filename in ["README.md", "README.ko.md"]:
            readme = pathlib.Path("python", filename)
            self.assertTrue(readme.is_file())
            contents = readme.read_text(encoding="utf-8")
            self.assertIn("Python", contents)
            self.assertIn("python/README.md", contents)
            self.assertIn("python/README.ko.md", contents)
            self.assertNotIn("```typescript", contents)
            self.assertNotIn("```rust", contents)
            self.assertIn("cidrs", contents)
            self.assertIn("10.0.0.0/24", contents)
            self.assertIn("10.128.0.10", contents)
            self.assertIn("--cidr", contents)
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
            metadata["project"]["scripts"]["rtsp-backchannel"],
            "rtsp_backchannel.cli:main",
        )
        self.assertEqual(
            metadata["project"]["urls"]["Repository"],
            "https://github.com/GagaKor/rtsp-backchannel.git",
        )
        self.assertEqual(
            metadata["tool"]["setuptools"]["py-modules"],
            ["backchannel_audio", "backchannel_rtp", "onvif_play"],
        )

    def test_installed_cli_requires_only_camera_target_and_audio_file(self):
        cli = importlib.import_module("rtsp_backchannel.cli")
        cases = [
            (["--file", "event.mp3"], "--host"),
            (["--host", "camera"], "--file"),
            (["streams"], "--host"),
        ]

        with patch.dict(os.environ, {}, clear=True):
            for arguments, missing in cases:
                with self.subTest(arguments=arguments):
                    with (
                        redirect_stderr(io.StringIO()) as errors,
                        self.assertRaises(SystemExit),
                    ):
                        cli.main(arguments)
                    self.assertIn(missing, errors.getvalue())

        result = Mock(packets_sent=0)
        with (
            patch.object(cli, "play_file", return_value=result) as play,
            patch.dict(os.environ, {}, clear=True),
            redirect_stdout(io.StringIO()),
        ):
            cli.main(
                [
                    "--host",
                    "rtsp://camera/live",
                    "--file",
                    "event.mp3",
                ]
            )

        play.assert_called_once_with(
            host="rtsp://camera/live",
            user="",
            password="",
            file="event.mp3",
            volume=0.05,
            codec="auto",
        )

    def test_installed_cli_delegates_to_the_public_play_file_api(self):
        cli = importlib.import_module("rtsp_backchannel.cli")
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
            codec="auto",
        )
        self.assertEqual(output.getvalue(), "sent 2 RTP packets\n")

    def test_installed_cli_accepts_every_supported_codec_preference(self):
        cli = importlib.import_module("rtsp_backchannel.cli")
        result = Mock(packets_sent=0)

        for codec in (
            "auto",
            "pcma",
            "pcmu",
            "g726-16",
            "g726-24",
            "g726-32",
            "g726-40",
            "aac",
        ):
            with self.subTest(codec=codec), patch.object(
                cli, "play_file", return_value=result
            ) as play, redirect_stdout(io.StringIO()):
                cli.main(
                    [
                        "--host",
                        "camera",
                        "--file",
                        "event.mp3",
                        "--codec",
                        codec,
                    ]
                )

            play.assert_called_once_with(
                host="camera",
                user="",
                password="",
                file="event.mp3",
                volume=0.05,
                codec=codec,
            )

    def test_installed_cli_dispatches_discovery_as_json_lines(self):
        cli = importlib.import_module("rtsp_backchannel.cli")
        library = importlib.import_module("rtsp_backchannel.onvif")
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

    def test_installed_cli_dispatches_explicit_cidr_discovery(self):
        cli = importlib.import_module("rtsp_backchannel.cli")

        with patch.object(
            cli, "discover_devices", return_value=[]
        ) as discover:
            cli.main(
                [
                    "discover",
                    "--timeout-ms",
                    "1500",
                    "--cidr",
                    "10.128.10.0/24",
                    "--cidr",
                    "192.168.20.0/24",
                    "--port",
                    "80",
                    "--port",
                    "8000",
                    "--concurrency",
                    "16",
                ]
            )

        discover.assert_called_once_with(
            timeout=1.5,
            interfaces=None,
            cidrs=["10.128.10.0/24", "192.168.20.0/24"],
            ports=[80, 8000],
            concurrency=16,
        )

    def test_installed_cli_dispatches_stream_lookup_as_json_lines(self):
        cli = importlib.import_module("rtsp_backchannel.cli")
        library = importlib.import_module("rtsp_backchannel.onvif")
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

    def test_capabilities_cli_applies_environment_password_only_when_omitted(self):
        cli = importlib.import_module("rtsp_backchannel.cli")
        report = _minimal_capability_report()

        with (
            patch.object(
                cli,
                "get_camera_capabilities",
                return_value=report,
                create=True,
            ) as capabilities,
            patch.dict(
                os.environ,
                {"ONVIF_PASSWORD": "environment-only-secret"},
                clear=True,
            ),
            redirect_stdout(io.StringIO()) as output,
        ):
            cli.main(["capabilities", "--host", "camera.local"])
            cli.main(
                [
                    "capabilities",
                    "--host",
                    "camera.local",
                    "--pass",
                    "explicit-secret",
                ]
            )
            cli.main(
                [
                    "capabilities",
                    "--host",
                    "camera.local",
                    "--pass",
                    "",
                ]
            )
            del os.environ["ONVIF_PASSWORD"]
            cli.main(["capabilities", "--host", "camera.local"])

        self.assertEqual(
            capabilities.call_args_list,
            [
                call(
                    host="camera.local",
                    user="",
                    password="environment-only-secret",
                ),
                call(
                    host="camera.local",
                    user="",
                    password="explicit-secret",
                ),
                call(host="camera.local", user="", password=""),
                call(host="camera.local", user="", password=""),
            ],
        )
        self.assertEqual(len(output.getvalue().splitlines()), 4)
        self.assertNotRegex(
            output.getvalue(), "environment-only-secret|explicit-secret"
        )

    def test_capabilities_cli_calls_once_and_prints_shared_camel_case_json(self):
        cli = importlib.import_module("rtsp_backchannel.cli")
        from rtsp_backchannel.capabilities import (
            CameraCapabilityProfile,
            CameraCapabilityReport,
            CameraCapabilityService,
            CameraCapabilityVersion,
            CameraCapabilityWarning,
            Media2CapabilityReport,
            PtzCapabilityReport,
            PtzNode,
            PtzServiceCapabilities,
            PtzSpaces,
        )
        from rtsp_backchannel.onvif import DeviceInfo

        report = CameraCapabilityReport(
            device=DeviceInfo(
                manufacturer="예시 제조사",
                firmware="1.2.3",
            ),
            scopes=("onvif://www.onvif.org/Profile/Streaming",),
            declared_profiles=("S", "T"),
            service_discovery="getServices",
            services=(
                CameraCapabilityService(
                    namespace="http://www.onvif.org/ver20/media/wsdl",
                    xaddr="http://camera.local/onvif/media2",
                    version=CameraCapabilityVersion(2, 0),
                ),
                CameraCapabilityService(
                    namespace="urn:vendor",
                    xaddr="http://camera.local/vendor",
                ),
            ),
            profiles=(
                CameraCapabilityProfile(
                    token="main",
                    source="media1",
                    has_audio_encoder=True,
                    has_audio_output=False,
                    has_audio_source=True,
                    ptz_configuration_token="ptz-main",
                ),
            ),
            ptz=PtzCapabilityReport(
                detected=None,
                pan_tilt_supported=False,
                zoom_supported=True,
                profile_tokens=("main",),
                service_capabilities=PtzServiceCapabilities(
                    e_flip=False,
                    move_status=True,
                ),
                nodes=(
                    PtzNode(
                        token="zoom-node",
                        spaces=PtzSpaces(
                            absolute_zoom=True,
                            continuous_zoom=True,
                        ),
                        maximum_presets=0,
                        home_supported=False,
                        auxiliary_commands=("LightOn",),
                    ),
                ),
            ),
            media2=Media2CapabilityReport(
                detected=None,
                encodings=("H264",),
                h265_supported=None,
            ),
            warnings=(
                CameraCapabilityWarning(
                    operation="Media2 GetProfiles",
                    message="request timeout",
                ),
            ),
        )

        with (
            patch.object(
                cli,
                "get_camera_capabilities",
                return_value=report,
                create=True,
            ) as capabilities,
            redirect_stdout(io.StringIO()) as output,
        ):
            cli.main(
                [
                    "capabilities",
                    "--host",
                    "camera.local",
                    "--user",
                    "operator",
                    "--pass",
                    "command-only-secret",
                    "--device-url",
                    "http://camera.local/onvif/device_service",
                    "--device-url",
                    "http://camera.local:8000/onvif/device_service",
                    "--timeout-ms",
                    "2500",
                ]
            )

        capabilities.assert_called_once_with(
            host="camera.local",
            user="operator",
            password="command-only-secret",
            device_urls=[
                "http://camera.local/onvif/device_service",
                "http://camera.local:8000/onvif/device_service",
            ],
            timeout=2.5,
        )
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertIn("예시 제조사", output.getvalue())
        self.assertNotIn("command-only-secret", output.getvalue())
        self.assertEqual(
            json.loads(output.getvalue()),
            {
                "device": {
                    "manufacturer": "예시 제조사",
                    "firmware": "1.2.3",
                },
                "scopes": ["onvif://www.onvif.org/Profile/Streaming"],
                "declaredProfiles": ["S", "T"],
                "serviceDiscovery": "getServices",
                "services": [
                    {
                        "namespace": "http://www.onvif.org/ver20/media/wsdl",
                        "xaddr": "http://camera.local/onvif/media2",
                        "version": {"major": 2, "minor": 0},
                    },
                    {
                        "namespace": "urn:vendor",
                        "xaddr": "http://camera.local/vendor",
                    },
                ],
                "profiles": [
                    {
                        "token": "main",
                        "source": "media1",
                        "hasAudioEncoder": True,
                        "hasAudioOutput": False,
                        "hasAudioSource": True,
                        "ptzConfigurationToken": "ptz-main",
                    }
                ],
                "ptz": {
                    "detected": None,
                    "panTiltSupported": False,
                    "zoomSupported": True,
                    "profileTokens": ["main"],
                    "serviceCapabilities": {
                        "eFlip": False,
                        "moveStatus": True,
                    },
                    "nodes": [
                        {
                            "token": "zoom-node",
                            "spaces": {
                                "absolutePanTilt": False,
                                "absoluteZoom": True,
                                "relativePanTilt": False,
                                "relativeZoom": False,
                                "continuousPanTilt": False,
                                "continuousZoom": True,
                            },
                            "maximumPresets": 0,
                            "homeSupported": False,
                            "auxiliaryCommands": ["LightOn"],
                        }
                    ],
                },
                "media2": {
                    "detected": None,
                    "encodings": ["H264"],
                    "h265Supported": None,
                },
                "warnings": [
                    {
                        "operation": "Media2 GetProfiles",
                        "message": "request timeout",
                    }
                ],
            },
        )

    def test_capability_json_omits_only_optional_members(self):
        cli = importlib.import_module("rtsp_backchannel.cli")
        mapper = getattr(cli, "_camera_capability_json", None)
        self.assertTrue(callable(mapper))

        self.assertEqual(
            mapper(_minimal_capability_report()),
            {
                "device": {},
                "scopes": [],
                "declaredProfiles": [],
                "serviceDiscovery": "unavailable",
                "services": [],
                "profiles": [],
                "ptz": {
                    "detected": None,
                    "panTiltSupported": None,
                    "zoomSupported": None,
                    "profileTokens": [],
                    "nodes": [],
                },
                "media2": {
                    "detected": None,
                    "encodings": [],
                    "h265Supported": None,
                },
                "warnings": [],
            },
        )

    def test_capabilities_cli_rejects_unsafe_argument_shapes_before_dispatch(self):
        cli = importlib.import_module("rtsp_backchannel.cli")
        cases = (
            (["--pass", "validation-only-secret"], "--host"),
            (
                [
                    "--host",
                    "camera.local",
                    "--pass",
                    "--timeout-ms",
                    "50",
                ],
                "missing value for --pass",
            ),
            (
                ["--host", "camera.local", "--device-url"],
                "expected one argument",
            ),
            (
                ["--host", "camera.local", "--user", "--timeout-ms", "50"],
                "expected one argument",
            ),
            (
                ["--host", "camera.local", "--timeout-ms", "--user", "admin"],
                "expected one argument",
            ),
            (["--host", ""], "must not be empty"),
            (["--host", "camera.local", "--user", ""], "must not be empty"),
            (
                ["--host", "camera.local", "--device-url", ""],
                "must not be empty",
            ),
        )
        cases += tuple(
            (
                [
                    "--host",
                    "camera.local",
                    "--pass",
                    "validation-only-secret",
                    "--timeout-ms",
                    value,
                ],
                "timeout-ms must be finite and greater than 0",
            )
            for value in ("0", "-1", "5e-324", "NaN", "Infinity")
        )

        with (
            patch.object(
                cli, "get_camera_capabilities", create=True
            ) as capabilities,
            patch.dict(
                os.environ,
                {"ONVIF_PASSWORD": "environment-only-secret"},
                clear=True,
            ),
        ):
            for arguments, expected in cases:
                with self.subTest(arguments=arguments):
                    with (
                        redirect_stdout(io.StringIO()) as output,
                        redirect_stderr(io.StringIO()) as errors,
                        self.assertRaises(SystemExit) as stopped,
                    ):
                        cli.main(["capabilities", *arguments])
                    self.assertEqual(stopped.exception.code, 2)
                    diagnostic = output.getvalue() + errors.getvalue()
                    self.assertIn(expected, diagnostic)
                    self.assertNotRegex(
                        diagnostic,
                        "validation-only-secret|environment-only-secret",
                    )

        capabilities.assert_not_called()

    def test_capabilities_cli_enforces_inclusive_24_hour_timeout_before_seconds_conversion(self):
        cli = importlib.import_module("rtsp_backchannel.cli")
        report = _minimal_capability_report()

        with (
            patch.object(
                cli,
                "get_camera_capabilities",
                return_value=report,
                create=True,
            ) as capabilities,
            patch.dict(
                os.environ,
                {"ONVIF_PASSWORD": "environment-only-secret"},
                clear=True,
            ),
            redirect_stdout(io.StringIO()),
        ):
            cli.main(
                [
                    "capabilities",
                    "--host",
                    "camera.local",
                    "--timeout-ms",
                    "86400000",
                ]
            )
            cli.main(
                [
                    "capabilities",
                    "--host",
                    "camera.local",
                    "--timeout-ms=86400000",
                ]
            )

        self.assertEqual(
            capabilities.call_args_list,
            [
                call(
                    host="camera.local",
                    user="",
                    password="environment-only-secret",
                    timeout=86_400.0,
                ),
                call(
                    host="camera.local",
                    user="",
                    password="environment-only-secret",
                    timeout=86_400.0,
                ),
            ],
        )

        rejected = (
            (["--timeout-ms", "86400000.00000001"], "86400000.00000001"),
            (["--timeout-ms=86400000.00000001"], "86400000.00000001"),
            (["--timeout-ms", "86400000.00000049"], "86400000.00000049"),
            (["--timeout-ms=86400000.00000049"], "86400000.00000049"),
            (["--timeout-ms", "86400001"], "86400001"),
            (["--timeout-ms=86400001"], "86400001"),
            (["--timeout-ms", "1e22"], "1e22"),
            (["--timeout-ms=1e22"], "1e22"),
        )
        with (
            patch.object(
                cli,
                "get_camera_capabilities",
                return_value=report,
                create=True,
            ) as rejected_capabilities,
            patch.dict(
                os.environ,
                {"ONVIF_PASSWORD": "environment-only-secret"},
                clear=True,
            ),
        ):
            for timeout_arguments, reflected in rejected:
                with self.subTest(timeout_arguments=timeout_arguments):
                    with (
                        redirect_stdout(io.StringIO()) as output,
                        redirect_stderr(io.StringIO()) as errors,
                        self.assertRaises(SystemExit) as stopped,
                    ):
                        cli.main(
                            [
                                "capabilities",
                                "--host",
                                "camera.local",
                                "--pass",
                                "argv-password-secret",
                                *timeout_arguments,
                            ]
                        )
                    self.assertEqual(stopped.exception.code, 2)
                    diagnostic = output.getvalue() + errors.getvalue()
                    self.assertIn(
                        "timeout-ms exceeds the 24-hour maximum",
                        diagnostic,
                    )
                    self.assertNotIn(reflected, diagnostic)
                    self.assertNotRegex(
                        diagnostic,
                        "argv-password-secret|environment-only-secret",
                    )
        rejected_capabilities.assert_not_called()

    def test_capabilities_cli_rejects_bare_terminator_without_reflecting_values(self):
        cli = importlib.import_module("rtsp_backchannel.cli")
        cases = (
            (
                [
                    "--host",
                    "camera.local",
                    "--pass",
                    "control-password-secret",
                    "--",
                ],
                "capabilities does not accept an argument terminator",
            ),
            (
                [
                    "--host",
                    "camera.local",
                    "--pass",
                    "--",
                    "--pass=trailing-equals-secret",
                ],
                "missing value for --pass",
            ),
            (
                ["--host", "camera.local", "--"],
                "capabilities does not accept an argument terminator",
            ),
        )

        with (
            patch.object(
                cli, "get_camera_capabilities", create=True
            ) as capabilities,
            patch.dict(
                os.environ,
                {"ONVIF_PASSWORD": "environment-only-secret"},
                clear=True,
            ),
        ):
            for arguments, expected in cases:
                with self.subTest(arguments=arguments):
                    with (
                        redirect_stdout(io.StringIO()) as output,
                        redirect_stderr(io.StringIO()) as errors,
                        self.assertRaises(SystemExit) as stopped,
                    ):
                        cli.main(["capabilities", *arguments])
                    self.assertEqual(stopped.exception.code, 2)
                    diagnostic = output.getvalue() + errors.getvalue()
                    self.assertIn(expected, diagnostic)
                    self.assertNotRegex(
                        diagnostic,
                        "control-password-secret|trailing-equals-secret|environment-only-secret",
                    )

        capabilities.assert_not_called()

    def test_capabilities_cli_keeps_hyphen_prefixed_passwords_opaque(self):
        cli = importlib.import_module("rtsp_backchannel.cli")
        report = _minimal_capability_report()
        password_arguments = (
            (["--pass", "--separate-password-secret"], "--separate-password-secret"),
            (["--pass=--equals-password-secret"], "--equals-password-secret"),
            (["--pass=--"], "--"),
        )

        with (
            patch.object(
                cli,
                "get_camera_capabilities",
                return_value=report,
                create=True,
            ) as capabilities,
            patch.dict(
                os.environ,
                {"ONVIF_PASSWORD": "environment-only-secret"},
                clear=True,
            ),
            redirect_stdout(io.StringIO()) as output,
        ):
            for arguments, expected_password in password_arguments:
                with self.subTest(arguments=arguments):
                    try:
                        cli.main(
                            [
                                "capabilities",
                                "--host",
                                "camera.local",
                                *arguments,
                            ]
                        )
                    except SystemExit as error:
                        self.fail(
                            "opaque password form was rejected with "
                            f"exit {error.code}"
                        )
                    self.assertEqual(
                        capabilities.call_args.kwargs["password"],
                        expected_password,
                    )

        self.assertEqual(capabilities.call_count, len(password_arguments))
        self.assertNotRegex(
            output.getvalue(),
            "separate-password-secret|equals-password-secret|environment-only-secret",
        )

        capabilities.reset_mock()
        with (
            redirect_stdout(io.StringIO()) as output,
            redirect_stderr(io.StringIO()) as errors,
            self.assertRaises(SystemExit) as stopped,
        ):
            cli.main(
                [
                    "capabilities",
                    "--host",
                    "camera.local",
                    "--pass",
                    "--timeout-ms",
                    "50",
                ]
            )
        self.assertEqual(stopped.exception.code, 2)
        self.assertIn(
            "missing value for --pass",
            output.getvalue() + errors.getvalue(),
        )
        capabilities.assert_not_called()

    def test_documents_camera_capabilities_in_both_python_readmes(self):
        english = pathlib.Path("python/README.md").read_text(
            encoding="utf-8"
        )
        korean = pathlib.Path("python/README.ko.md").read_text(
            encoding="utf-8"
        )

        for readme in (english, korean):
            for expected in (
                "get_camera_capabilities",
                "CameraCapabilityReport",
                "CameraCapabilityVersion",
                "declared_profiles",
                "declaredProfiles",
                "service_discovery",
                "Profile T",
                "media2.detected",
                "media2.h265Supported",
                "pan_tilt_supported",
                "profile_tokens",
                "warnings",
                "warning.message",
                "true",
                "false",
                "null",
                "GetServices",
                "GetCapabilities",
                "ONVIF_PASSWORD",
                '`--pass ""`',
                "rtsp-backchannel capabilities",
                "--device-url",
                "--timeout-ms",
                "XAddr",
                "86,400,000",
                "4,096",
            ):
                self.assertIn(expected, readme)
            self.assertRegex(
                readme,
                r"warning\.message[\s\S]{0,500}generic canonical",
            )
            for excluded in (
                "credentials",
                "WSSE digest material",
                "URL userinfo",
                "raw or real camera response payload",
            ):
                self.assertIn(excluded, readme)

        self.assertRegex(
            english,
            r"Initial connection and authentication failures are fatal",
        )
        self.assertRegex(korean, r"최초 연결 또는\s+인증 실패는 치명적")
        self.assertRegex(
            english,
            r"same-origin[\s\S]{0,500}scheme[\s\S]{0,500}hostname",
        )
        self.assertRegex(
            korean,
            r"동일 출처[\s\S]{0,500}스킴[\s\S]{0,500}호스트",
        )
        self.assertRegex(
            english,
            r"permits at most 64\s+element levels",
        )
        self.assertRegex(
            korean,
            r"최대 64단계",
        )
        self.assertRegex(
            english,
            r"inclusive 24-hour\s+maximum \(86,400,000 ms\)",
        )
        self.assertRegex(
            korean,
            r"24시간 상한\(86,400,000ms\)",
        )


if __name__ == "__main__":
    unittest.main()
