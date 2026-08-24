from __future__ import annotations

import dataclasses
import json
import math
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from rtsp_backchannel.onvif import DeviceInfo
from rtsp_backchannel.ptz import (
    PtzSession,
    PtzSessionOptions,
    PtzStatus,
    PtzVector,
    format_ptz_duration,
    format_ptz_number,
    open_ptz_session,
)

SOAP_NS = "http://www.w3.org/2003/05/soap-envelope"
DEV_NS = "http://www.onvif.org/ver10/device/wsdl"
SCHEMA_NS = "http://www.onvif.org/ver10/schema"
MEDIA1_NS = "http://www.onvif.org/ver10/media/wsdl"
MEDIA2_NS = "http://www.onvif.org/ver20/media/wsdl"
PTZ_NS = "http://www.onvif.org/ver20/ptz/wsdl"

GET_SERVICES = (
    f'<GetServices xmlns="{DEV_NS}">'
    "<IncludeCapability>false</IncludeCapability></GetServices>"
)
GET_NODES = f'<GetNodes xmlns="{PTZ_NS}"/>'
MEDIA1_GET_PROFILES = f'<GetProfiles xmlns="{MEDIA1_NS}"/>'
MEDIA2_GET_PROFILES = (
    f'<GetProfiles xmlns="{MEDIA2_NS}"><Type>All</Type></GetProfiles>'
)

_DEFAULT_SPACES = {
    "absolute_pan_tilt": False,
    "absolute_zoom": False,
    "relative_pan_tilt": False,
    "relative_zoom": False,
    "continuous_pan_tilt": False,
    "continuous_zoom": False,
}

_SPACE_ELEMENTS = (
    ("absolute_pan_tilt", "AbsolutePanTiltPositionSpace"),
    ("absolute_zoom", "AbsoluteZoomPositionSpace"),
    ("relative_pan_tilt", "RelativePanTiltTranslationSpace"),
    ("relative_zoom", "RelativeZoomTranslationSpace"),
    ("continuous_pan_tilt", "ContinuousPanTiltVelocitySpace"),
    ("continuous_zoom", "ContinuousZoomVelocitySpace"),
)


def soap(body: str) -> str:
    return (
        f'<s:Envelope xmlns:s="{SOAP_NS}" xmlns:tds="{DEV_NS}" '
        f'xmlns:tt="{SCHEMA_NS}" xmlns:trt="{MEDIA1_NS}" xmlns:tr2="{MEDIA2_NS}" '
        f'xmlns:tptz="{PTZ_NS}">'
        f"<s:Body>{body}</s:Body></s:Envelope>"
    )


def response(xml: str, status_code: int = 200) -> SimpleNamespace:
    return SimpleNamespace(status_code=status_code, xml=soap(xml))


def space_elements(spaces: dict) -> str:
    return "".join(
        f"<tt:{tag}/>" for field, tag in _SPACE_ELEMENTS if spaces.get(field)
    )


def default_operation_response(body: str) -> str:
    if body.startswith("<ContinuousMove "):
        return "<tptz:ContinuousMoveResponse/>"
    if body.startswith("<AbsoluteMove "):
        return "<tptz:AbsoluteMoveResponse/>"
    if body.startswith("<RelativeMove "):
        return "<tptz:RelativeMoveResponse/>"
    if body.startswith("<Stop "):
        return "<tptz:StopResponse/>"
    if body.startswith("<GetStatus "):
        return "<tptz:GetStatusResponse><tptz:PTZStatus/></tptz:GetStatusResponse>"
    raise AssertionError(f"fake PTZ responder: unexpected request body: {body}")


class FakePtzDevice:
    """Records every service_call and answers PTZ session lifecycle calls.

    Mirrors FakeCapabilityDevice in test_onvif_capabilities.py: the fake
    stands in for OnvifDevice itself (patched at construction time), not
    for a separate dependency-injection seam.
    """

    def __init__(
        self,
        calls: list,
        spaces: dict | None = None,
        *,
        ptz_xaddr: str = "http://camera/ptz",
        media_url: str = "http://camera/media",
        profile_token: str = "main",
        omit_ptz_service: bool = False,
        nodes_xml: str | None = None,
        profiles_xml: str | None = None,
        media2_xaddr: str | None = None,
        media2_profiles_xml: str | None = None,
        respond=None,
    ) -> None:
        self.calls = calls
        self.spaces = {**_DEFAULT_SPACES, **(spaces or {})}
        self.ptz_xaddr = ptz_xaddr
        self.media_url = media_url
        self.profile_token = profile_token
        self.omit_ptz_service = omit_ptz_service
        self.nodes_xml = nodes_xml
        self.profiles_xml = profiles_xml
        self.media2_xaddr = media2_xaddr
        self.media2_profiles_xml = media2_profiles_xml
        self.respond = respond

    def connect(self):
        return DeviceInfo()

    def _required_media_url(self):
        return self.media_url

    def service_call(self, body, endpoint=None):
        self.calls.append((body, endpoint))
        if self.respond is not None:
            custom = self.respond(body, endpoint)
            if custom is not None:
                return custom
        if body == GET_SERVICES:
            ptz_service = (
                ""
                if self.omit_ptz_service
                else (
                    f"<tds:Service><tds:Namespace>{PTZ_NS}</tds:Namespace>"
                    f"<tds:XAddr>{self.ptz_xaddr}</tds:XAddr></tds:Service>"
                )
            )
            media2_service = (
                (
                    f"<tds:Service><tds:Namespace>{MEDIA2_NS}</tds:Namespace>"
                    f"<tds:XAddr>{self.media2_xaddr}</tds:XAddr></tds:Service>"
                )
                if self.media2_xaddr
                else ""
            )
            return response(
                f"<tds:GetServicesResponse>{ptz_service}{media2_service}"
                "</tds:GetServicesResponse>"
            )
        if body == GET_NODES and endpoint == self.ptz_xaddr:
            return response(
                self.nodes_xml
                or (
                    '<tptz:GetNodesResponse><tptz:PTZNode token="node-1">'
                    "<tt:SupportedPTZSpaces>"
                    + space_elements(self.spaces)
                    + "</tt:SupportedPTZSpaces></tptz:PTZNode></tptz:GetNodesResponse>"
                )
            )
        if body == MEDIA1_GET_PROFILES and endpoint == self.media_url:
            return response(
                self.profiles_xml
                or (
                    f'<trt:GetProfilesResponse><trt:Profiles token="{self.profile_token}">'
                    "<tt:PTZConfiguration token=\"ptz-config\"/>"
                    "</trt:Profiles></trt:GetProfilesResponse>"
                )
            )
        if body == MEDIA2_GET_PROFILES and endpoint == self.media2_xaddr:
            return response(self.media2_profiles_xml or "<tr2:GetProfilesResponse/>")
        return response(default_operation_response(body))


def open_session(fake: FakePtzDevice, **option_overrides) -> PtzSession:
    options = PtzSessionOptions(
        **{"host": "camera", "user": "operator", "password": "secret", **option_overrides}
    )
    with patch("rtsp_backchannel.ptz.OnvifDevice", return_value=fake):
        return open_ptz_session(options)


class FormatterTests(unittest.TestCase):
    def test_formats_ptz_numbers_as_fixed_six_decimals(self):
        self.assertEqual(format_ptz_number(0.5), "0.500000")
        self.assertEqual(format_ptz_number(-1), "-1.000000")
        self.assertEqual(format_ptz_number(-0.0), "0.000000")
        self.assertEqual(format_ptz_number(0), "0.000000")
        self.assertEqual(format_ptz_number(0.1 + 0.2), "0.300000")

    def test_rejects_non_finite_ptz_numbers(self):
        for bad in (math.nan, math.inf, -math.inf):
            with self.subTest(value=bad):
                with self.assertRaisesRegex(ValueError, "^PTZ value must be finite$"):
                    format_ptz_number(bad)

    def test_formats_whole_seconds_without_a_fraction_and_the_rest_to_three_decimals(self):
        self.assertEqual(format_ptz_duration(1000), "PT1S")
        self.assertEqual(format_ptz_duration(2000), "PT2S")
        self.assertEqual(format_ptz_duration(1500), "PT1.500S")
        self.assertEqual(format_ptz_duration(250), "PT0.250S")

    def test_rejects_non_positive_or_non_finite_ptz_durations(self):
        for bad in (0, -1, math.nan, math.inf):
            with self.subTest(value=bad):
                with self.assertRaisesRegex(
                    ValueError, "^PTZ timeout must be finite and greater than 0$"
                ):
                    format_ptz_duration(bad)

    def test_rejects_a_ptz_duration_above_the_60000ms_ceiling_but_accepts_the_boundary(self):
        self.assertEqual(format_ptz_duration(60_000), "PT60S")
        for bad in (60_001, 600_000):
            with self.subTest(value=bad):
                with self.assertRaisesRegex(
                    ValueError, "^PTZ timeout must not exceed 60000 ms$"
                ):
                    format_ptz_duration(bad)


class PtzSessionLifecycleTests(unittest.TestCase):
    def test_fails_to_open_when_no_ptz_service_is_advertised(self):
        calls: list = []
        fake = FakePtzDevice(calls, omit_ptz_service=True)
        with self.assertRaisesRegex(RuntimeError, "^no ONVIF PTZ service$"):
            open_session(fake)
        self.assertEqual([body for body, _ in calls], [GET_SERVICES])

    def test_fails_to_open_when_get_nodes_returns_no_node(self):
        calls: list = []
        fake = FakePtzDevice(calls, nodes_xml="<tptz:GetNodesResponse/>")
        with self.assertRaisesRegex(RuntimeError, "^no ONVIF PTZ node$"):
            open_session(fake)

    def test_resolves_default_profile_token_from_first_ptz_configuration(self):
        calls: list = []
        fake = FakePtzDevice(
            calls,
            profiles_xml=(
                "<trt:GetProfilesResponse>"
                '<trt:Profiles token="no-ptz"/>'
                '<trt:Profiles token="has-ptz"><tt:PTZConfiguration token="cfg"/></trt:Profiles>'
                "</trt:GetProfilesResponse>"
            ),
        )
        session = open_session(fake)
        self.assertEqual(session.profile_token, "has-ptz")
        self.assertEqual(
            [body for body, _ in calls],
            [GET_SERVICES, GET_NODES, MEDIA1_GET_PROFILES],
        )

    def test_fails_to_open_when_no_media_profile_carries_a_ptz_configuration(self):
        calls: list = []
        fake = FakePtzDevice(
            calls,
            profiles_xml=(
                '<trt:GetProfilesResponse><trt:Profiles token="no-ptz"/>'
                "</trt:GetProfilesResponse>"
            ),
        )
        with self.assertRaisesRegex(RuntimeError, "^no ONVIF PTZ profile$"):
            open_session(fake)

    def test_falls_back_to_media2_and_resolves_a_ptz_capable_profile(self):
        calls: list = []
        fake = FakePtzDevice(
            calls,
            profiles_xml=(
                '<trt:GetProfilesResponse><trt:Profiles token="media1-no-ptz"/>'
                "</trt:GetProfilesResponse>"
            ),
            media2_xaddr="http://camera/media2",
            media2_profiles_xml=(
                "<tr2:GetProfilesResponse>"
                '<tr2:Profiles token="media2-no-ptz"><tr2:Configurations/></tr2:Profiles>'
                '<tr2:Profiles token="media2-has-ptz"><tr2:Configurations>'
                '<tr2:PTZ token="ptz-two"/></tr2:Configurations></tr2:Profiles>'
                "</tr2:GetProfilesResponse>"
            ),
        )
        session = open_session(fake)
        self.assertEqual(session.profile_token, "media2-has-ptz")
        self.assertEqual(
            [body for body, _ in calls],
            [GET_SERVICES, GET_NODES, MEDIA1_GET_PROFILES, MEDIA2_GET_PROFILES],
        )

    def test_fails_to_open_when_both_media1_and_media2_have_no_ptz_profile(self):
        calls: list = []
        fake = FakePtzDevice(
            calls,
            profiles_xml=(
                '<trt:GetProfilesResponse><trt:Profiles token="media1-no-ptz"/>'
                "</trt:GetProfilesResponse>"
            ),
            media2_xaddr="http://camera/media2",
            media2_profiles_xml=(
                "<tr2:GetProfilesResponse>"
                '<tr2:Profiles token="media2-no-ptz"><tr2:Configurations/></tr2:Profiles>'
                "</tr2:GetProfilesResponse>"
            ),
        )
        with self.assertRaisesRegex(RuntimeError, "^no ONVIF PTZ profile$"):
            open_session(fake)
        self.assertEqual(
            [body for body, _ in calls],
            [GET_SERVICES, GET_NODES, MEDIA1_GET_PROFILES, MEDIA2_GET_PROFILES],
        )

    def test_skips_media1_get_profiles_when_an_explicit_profile_token_is_given(self):
        calls: list = []
        fake = FakePtzDevice(calls)
        session = open_session(fake, profile_token="explicit-token")
        self.assertEqual(session.profile_token, "explicit-token")
        self.assertEqual([body for body, _ in calls], [GET_SERVICES, GET_NODES])

    def test_exposes_the_cached_ptz_node_and_resolved_profile_token(self):
        calls: list = []
        fake = FakePtzDevice(calls, {"continuous_pan_tilt": True})
        session = open_session(fake, profile_token="fixed-token")
        self.assertEqual(session.profile_token, "fixed-token")
        self.assertEqual(session.node.token, "node-1")
        self.assertTrue(session.node.spaces.continuous_pan_tilt)
        self.assertFalse(session.node.spaces.absolute_pan_tilt)


class PtzGuardTests(unittest.TestCase):
    def test_rejects_an_unsupported_absolute_move_without_sending_a_request(self):
        calls: list = []
        session = open_session(FakePtzDevice(calls, {"absolute_pan_tilt": False}))
        sent_before = len(calls)

        with self.assertRaisesRegex(
            RuntimeError, "^PTZ absolute pan/tilt is not supported$"
        ):
            session.absolute_move(pan_tilt=PtzVector(0.5, 0))

        self.assertEqual(len(calls), sent_before)

    def test_rejects_out_of_range_values_without_sending_a_request(self):
        calls: list = []
        session = open_session(
            FakePtzDevice(calls, {"continuous_pan_tilt": True})
        )
        sent_before = len(calls)
        for bad in (1.5, -1.5, math.nan, math.inf):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    session.continuous_move(pan_tilt=PtzVector(bad, 0))
        self.assertEqual(len(calls), sent_before)

    def test_rejects_a_move_with_neither_pan_tilt_nor_zoom(self):
        calls: list = []
        session = open_session(
            FakePtzDevice(
                calls, {"continuous_pan_tilt": True, "continuous_zoom": True}
            )
        )
        sent_before = len(calls)

        for attempt in (
            session.continuous_move,
            session.absolute_move,
            session.relative_move,
        ):
            with self.assertRaisesRegex(
                ValueError, "^PTZ move requires pan/tilt or zoom$"
            ):
                attempt()

        self.assertEqual(len(calls), sent_before)

    def test_rejects_unsupported_continuous_and_relative_zoom(self):
        calls: list = []
        session = open_session(
            FakePtzDevice(calls, {"relative_pan_tilt": True})
        )
        sent_before = len(calls)

        with self.assertRaisesRegex(
            RuntimeError, "^PTZ continuous zoom is not supported$"
        ):
            session.continuous_move(zoom=0.5)
        with self.assertRaisesRegex(
            RuntimeError, "^PTZ relative zoom is not supported$"
        ):
            session.relative_move(pan_tilt=PtzVector(0, 0), zoom=0.5)

        self.assertEqual(len(calls), sent_before)

    def test_rejects_every_unsupported_guard_with_zero_additional_requests(self):
        cases = (
            (
                "continuous_pan_tilt",
                "PTZ continuous pan/tilt is not supported",
                lambda session: session.continuous_move(pan_tilt=PtzVector(0, 0)),
            ),
            (
                "continuous_zoom",
                "PTZ continuous zoom is not supported",
                lambda session: session.continuous_move(zoom=0.5),
            ),
            (
                "absolute_pan_tilt",
                "PTZ absolute pan/tilt is not supported",
                lambda session: session.absolute_move(pan_tilt=PtzVector(0, 0)),
            ),
            (
                "absolute_zoom",
                "PTZ absolute zoom is not supported",
                lambda session: session.absolute_move(zoom=0.5),
            ),
            (
                "relative_pan_tilt",
                "PTZ relative pan/tilt is not supported",
                lambda session: session.relative_move(pan_tilt=PtzVector(0, 0)),
            ),
            (
                "relative_zoom",
                "PTZ relative zoom is not supported",
                lambda session: session.relative_move(zoom=0.5),
            ),
        )

        for space, message, invoke in cases:
            with self.subTest(space=space):
                calls: list = []
                # Every space defaults to False; only the one under test is
                # named, and it stays False, so this always exercises an
                # unsupported guard.
                session = open_session(FakePtzDevice(calls, {space: False}))
                sent_before = len(calls)

                with self.assertRaisesRegex(RuntimeError, f"^{message}$"):
                    invoke(session)
                self.assertEqual(len(calls), sent_before)

    def test_rejects_an_absolute_zoom_position_out_of_0_to_1_range(self):
        calls: list = []
        session = open_session(
            FakePtzDevice(calls, {"absolute_zoom": True, "continuous_zoom": True})
        )

        with self.assertRaises(ValueError):
            session.absolute_move(zoom=-0.5)
        session.continuous_move(zoom=-0.5)


class PtzSessionCloseTests(unittest.TestCase):
    def test_stops_both_axes_on_close(self):
        calls: list = []
        session = open_session(
            FakePtzDevice(calls, {"continuous_pan_tilt": True})
        )
        session.close()
        stop_body = calls[-1][0]
        self.assertIn("<PanTilt>true</PanTilt>", stop_body)
        self.assertIn("<Zoom>true</Zoom>", stop_body)

    def test_close_swallows_a_failing_stop_and_still_marks_the_session_closed(self):
        calls: list = []

        def respond(body, endpoint):
            if body.startswith("<Stop "):
                return response("<s:Fault/>", 500)
            return None

        session = open_session(
            FakePtzDevice(
                calls, {"continuous_pan_tilt": True}, respond=respond
            )
        )
        session.close()
        with self.assertRaisesRegex(RuntimeError, "^PTZ session is closed$"):
            session.get_status()

    def test_rejects_every_call_after_close_with_a_fixed_message(self):
        calls: list = []
        session = open_session(
            FakePtzDevice(
                calls,
                {
                    "continuous_pan_tilt": True,
                    "absolute_pan_tilt": True,
                    "relative_pan_tilt": True,
                },
            )
        )
        session.close()
        sent_before = len(calls)

        attempts = (
            lambda: session.continuous_move(pan_tilt=PtzVector(0, 0)),
            lambda: session.absolute_move(pan_tilt=PtzVector(0, 0)),
            lambda: session.relative_move(pan_tilt=PtzVector(0, 0)),
            session.stop,
            session.get_status,
        )
        for attempt in attempts:
            with self.assertRaisesRegex(RuntimeError, "^PTZ session is closed$"):
                attempt()

        self.assertEqual(len(calls), sent_before)


class PtzRequestBodyTests(unittest.TestCase):
    def test_sends_every_continuous_move_with_an_explicit_timeout(self):
        calls: list = []
        session = open_session(
            FakePtzDevice(calls, {"continuous_pan_tilt": True})
        )
        session.continuous_move(pan_tilt=PtzVector(0.5, -0.25))

        body = calls[-1][0]
        self.assertEqual(
            body,
            f'<ContinuousMove xmlns="{PTZ_NS}"><ProfileToken>main</ProfileToken>'
            f'<Velocity><PanTilt xmlns="{SCHEMA_NS}" x="0.500000" y="-0.250000"/>'
            "</Velocity><Timeout>PT1S</Timeout></ContinuousMove>",
        )

    def test_continuous_move_sends_an_explicit_per_call_timeout_in_the_body(self):
        # The Timeout element is the runaway guard: it must reach the wire as
        # the value the caller actually asked for, not a hardcoded default.
        # Every other test in this suite uses the 1000ms default, so without
        # this test PT1S could be hardcoded and nothing would notice.
        calls: list = []
        session = open_session(FakePtzDevice(calls, {"continuous_zoom": True}))
        session.continuous_move(zoom=0.5, timeout_ms=1500)

        body = calls[-1][0]
        self.assertIn("<Timeout>PT1.500S</Timeout>", body)

    def test_continuous_move_uses_the_session_level_default_timeout_in_the_body(self):
        calls: list = []
        session = open_session(
            FakePtzDevice(calls, {"continuous_zoom": True}),
            default_move_timeout_ms=250,
        )
        session.continuous_move(zoom=0.5)

        body = calls[-1][0]
        self.assertIn("<Timeout>PT0.250S</Timeout>", body)

    def test_rejects_a_default_move_timeout_ms_above_60000ms_at_session_open(self):
        calls: list = []
        with self.assertRaisesRegex(ValueError, "^PTZ timeout must not exceed 60000 ms$"):
            open_session(
                FakePtzDevice(calls, {"continuous_zoom": True}),
                default_move_timeout_ms=600_000,
            )
        # Rejected at open, before any move: no ContinuousMove should have
        # been sent, since the whole point is to fail even if the caller
        # never calls continuous_move at all.
        self.assertTrue(
            all(not body.startswith("<ContinuousMove ") for body, _ in calls)
        )

    def test_rejects_a_per_call_continuous_move_timeout_ms_above_60000ms(self):
        calls: list = []
        session = open_session(FakePtzDevice(calls, {"continuous_zoom": True}))
        sent_before = len(calls)

        with self.assertRaisesRegex(ValueError, "^PTZ timeout must not exceed 60000 ms$"):
            session.continuous_move(zoom=0.5, timeout_ms=600_000)

        self.assertEqual(len(calls), sent_before)

    def test_builds_an_absolute_move_body_with_position_speed_and_no_timeout(self):
        calls: list = []
        session = open_session(
            FakePtzDevice(
                calls, {"absolute_pan_tilt": True, "absolute_zoom": True}
            )
        )
        session.absolute_move(
            pan_tilt=PtzVector(-1, 1),
            zoom=0.25,
            speed_pan_tilt=PtzVector(0.1, 0.2),
            speed_zoom=0.3,
        )

        body = calls[-1][0]
        self.assertEqual(
            body,
            f'<AbsoluteMove xmlns="{PTZ_NS}"><ProfileToken>main</ProfileToken>'
            f'<Position><PanTilt xmlns="{SCHEMA_NS}" x="-1.000000" y="1.000000"/>'
            f'<Zoom xmlns="{SCHEMA_NS}" x="0.250000"/></Position>'
            f'<Speed><PanTilt xmlns="{SCHEMA_NS}" x="0.100000" y="0.200000"/>'
            f'<Zoom xmlns="{SCHEMA_NS}" x="0.300000"/></Speed></AbsoluteMove>',
        )
        self.assertNotIn("Timeout", body)

    def test_builds_a_relative_move_body_with_translation_and_omits_absent_fields(self):
        calls: list = []
        session = open_session(FakePtzDevice(calls, {"relative_zoom": True}))
        session.relative_move(zoom=-0.5)

        body = calls[-1][0]
        self.assertEqual(
            body,
            f'<RelativeMove xmlns="{PTZ_NS}"><ProfileToken>main</ProfileToken>'
            f'<Translation><Zoom xmlns="{SCHEMA_NS}" x="-0.500000"/></Translation>'
            "</RelativeMove>",
        )
        for absent in ("PanTilt", "Speed", "Timeout"):
            self.assertNotIn(absent, body)

    def test_sends_stop_with_explicit_per_axis_booleans_and_get_status_with_only_the_token(
        self,
    ):
        calls: list = []
        session = open_session(FakePtzDevice(calls))
        session.stop(pan_tilt=True, zoom=False)
        self.assertEqual(
            calls[-1][0],
            f'<Stop xmlns="{PTZ_NS}"><ProfileToken>main</ProfileToken>'
            "<PanTilt>true</PanTilt><Zoom>false</Zoom></Stop>",
        )

        session.get_status()
        self.assertEqual(
            calls[-1][0],
            f'<GetStatus xmlns="{PTZ_NS}"><ProfileToken>main</ProfileToken></GetStatus>',
        )

    def test_escapes_a_profile_token_containing_quotes_and_apostrophes(self):
        # xml.sax.saxutils.escape() only encodes &, <, > — it would leave the
        # quote and apostrophe below untouched, diverging from the
        # TypeScript reference's encodeXml() and breaking byte-for-byte
        # parity across languages for any token containing either character.
        calls: list = []
        session = open_session(FakePtzDevice(calls), profile_token="a\"b'c")
        session.get_status()

        self.assertEqual(
            calls[-1][0],
            f'<GetStatus xmlns="{PTZ_NS}">'
            "<ProfileToken>a&quot;b&apos;c</ProfileToken></GetStatus>",
        )


class PtzStatusParsingTests(unittest.TestCase):
    def test_parses_position_move_status_and_utc_time_from_get_status(self):
        calls: list = []

        def respond(body, endpoint):
            if body.startswith("<GetStatus "):
                return response(
                    "<tptz:GetStatusResponse><tptz:PTZStatus>"
                    '<tt:Position><tt:PanTilt x="0.25" y="-0.5"/><tt:Zoom x="0.75"/></tt:Position>'
                    "<tt:MoveStatus><tt:PanTilt>IDLE</tt:PanTilt><tt:Zoom>MOVING</tt:Zoom></tt:MoveStatus>"
                    "<tt:UtcTime>2026-08-10T00:00:00Z</tt:UtcTime>"
                    "</tptz:PTZStatus></tptz:GetStatusResponse>"
                )
            return None

        session = open_session(FakePtzDevice(calls, respond=respond))
        status = session.get_status()

        self.assertEqual(
            status,
            PtzStatus(
                pan_tilt=PtzVector(0.25, -0.5),
                zoom=0.75,
                pan_tilt_move_status="IDLE",
                zoom_move_status="MOVING",
                utc_time="2026-08-10T00:00:00Z",
            ),
        )

    def test_never_surfaces_a_camera_supplied_get_status_error_anywhere(self):
        calls: list = []
        secret_error = "internal-diagnostic-marker-should-not-leak"

        def respond(body, endpoint):
            if body.startswith("<GetStatus "):
                return response(
                    "<tptz:GetStatusResponse><tptz:PTZStatus>"
                    f"<tt:Error>{secret_error}</tt:Error>"
                    "<tt:UtcTime>2026-08-10T00:00:00Z</tt:UtcTime>"
                    "</tptz:PTZStatus></tptz:GetStatusResponse>"
                )
            return None

        session = open_session(FakePtzDevice(calls, respond=respond))
        status = session.get_status()

        self.assertEqual(status.utc_time, "2026-08-10T00:00:00Z")
        status_text = json.dumps(dataclasses.asdict(status))
        self.assertNotIn(secret_error, status_text)
        self.assertNotIn(secret_error, repr(status))
        field_names = {field.name for field in dataclasses.fields(status)}
        self.assertNotIn("error", field_names)
        self.assertNotIn("Error", field_names)

    def test_classifies_a_ptz_soap_fault_the_same_way_the_capability_report_does(self):
        calls: list = []

        def respond(body, endpoint):
            if body.startswith("<GetStatus "):
                return response(
                    "<s:Fault><s:Code><s:Value>s:Sender</s:Value><s:Subcode>"
                    '<s:Value xmlns:ter="http://www.onvif.org/ver10/error">'
                    "ter:ActionNotSupported</s:Value></s:Subcode></s:Code></s:Fault>",
                    500,
                )
            return None

        session = open_session(FakePtzDevice(calls, respond=respond))
        with self.assertRaisesRegex(ValueError, "^SOAP Fault: ActionNotSupported$"):
            session.get_status()


class PtzRequestParityFixtureTests(unittest.TestCase):
    def test_ptz_request_bodies_match_shared_cross_language_fixture(self):
        fixture_path = (
            Path(__file__).resolve().parents[1]
            / "rust"
            / "tests"
            / "fixtures"
            / "ptz-request-parity.json"
        )
        self.assertTrue(
            fixture_path.is_file(),
            "shared PTZ request parity fixture is missing",
        )
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

        calls: list = []
        session = open_session(
            FakePtzDevice(
                calls,
                {
                    "absolute_pan_tilt": True,
                    "absolute_zoom": True,
                    "relative_pan_tilt": True,
                    "relative_zoom": True,
                    "continuous_pan_tilt": True,
                    "continuous_zoom": True,
                },
                profile_token=fixture["profileToken"],
            ),
            profile_token=fixture["profileToken"],
        )

        pan_tilt = PtzVector(fixture["panTilt"]["x"], fixture["panTilt"]["y"])
        zoom = fixture["zoom"]

        session.continuous_move(pan_tilt=pan_tilt, zoom=zoom)
        self.assertEqual(calls[-1][0], fixture["requests"]["continuousMove"])

        session.absolute_move(pan_tilt=pan_tilt, zoom=zoom)
        self.assertEqual(calls[-1][0], fixture["requests"]["absoluteMove"])

        session.relative_move(pan_tilt=pan_tilt, zoom=zoom)
        self.assertEqual(calls[-1][0], fixture["requests"]["relativeMove"])

        session.stop()
        self.assertEqual(calls[-1][0], fixture["requests"]["stop"])

        session.get_status()
        self.assertEqual(calls[-1][0], fixture["requests"]["getStatus"])


if __name__ == "__main__":
    unittest.main()
