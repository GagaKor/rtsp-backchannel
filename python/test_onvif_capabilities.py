from __future__ import annotations

import dataclasses
import http.server
import io
import json
import threading
import unittest
import urllib.error
from types import SimpleNamespace
from unittest.mock import patch

from rtsp_backchannel.capabilities import (
    CameraCapabilityProfile,
    CameraCapabilityReport,
    CameraCapabilityService,
    CameraCapabilityVersion,
    CameraCapabilityWarning,
    EventCapabilityReport,
    EventServiceCapabilities,
    Media2CapabilityReport,
    PtzCapabilityReport,
    PtzNode,
    PtzServiceCapabilities,
    PtzSpaces,
    _OnvifResponseError,
    _merge_event_service_capabilities,
    _parse_capabilities_response,
    _parse_event_properties_response,
    _parse_event_service_capabilities_response,
    _parse_media1_profiles_response,
    _parse_media2_options_response,
    _parse_media2_profiles_response,
    _parse_ptz_nodes_response,
    _parse_ptz_service_capabilities_response,
    _parse_scopes_response,
    _parse_services_response,
    _select_service,
    get_camera_capabilities,
)
from rtsp_backchannel import onvif
from rtsp_backchannel.onvif import DeviceInfo, OnvifDevice, OnvifProfile


SOAP12_NS = "http://www.w3.org/2003/05/soap-envelope"
SOAP11_NS = "http://schemas.xmlsoap.org/soap/envelope/"
DEVICE_NS = "http://www.onvif.org/ver10/device/wsdl"
SCHEMA_NS = "http://www.onvif.org/ver10/schema"
MEDIA1_NS = "http://www.onvif.org/ver10/media/wsdl"
MEDIA2_NS = "http://www.onvif.org/ver20/media/wsdl"
PTZ_NS = "http://www.onvif.org/ver20/ptz/wsdl"
EVENTS_NS = "http://www.onvif.org/ver10/events/wsdl"
WSTOP_NS = "http://docs.oasis-open.org/wsn/t-1"
TOPICS_NS = "http://www.onvif.org/ver10/topics"
NBSP = "\N{NO-BREAK SPACE}"

GET_SCOPES = f'<GetScopes xmlns="{DEVICE_NS}"/>'
GET_SERVICES = (
    f'<GetServices xmlns="{DEVICE_NS}">'
    "<IncludeCapability>true</IncludeCapability></GetServices>"
)
GET_ALL_CAPABILITIES = (
    f'<GetCapabilities xmlns="{DEVICE_NS}">'
    "<Category>All</Category></GetCapabilities>"
)
MEDIA1_GET_PROFILES = f'<GetProfiles xmlns="{MEDIA1_NS}"/>'
MEDIA2_GET_PROFILES = (
    f'<GetProfiles xmlns="{MEDIA2_NS}"><Type>All</Type></GetProfiles>'
)
MEDIA2_GET_OPTIONS = (
    f'<GetVideoEncoderConfigurationOptions xmlns="{MEDIA2_NS}"/>'
)
PTZ_GET_CAPABILITIES = f'<GetServiceCapabilities xmlns="{PTZ_NS}"/>'
PTZ_GET_NODES = f'<GetNodes xmlns="{PTZ_NS}"/>'
EVENTS_GET_CAPABILITIES = f'<GetServiceCapabilities xmlns="{EVENTS_NS}"/>'
EVENTS_GET_PROPERTIES = f'<GetEventProperties xmlns="{EVENTS_NS}"/>'


def soap(body: str) -> str:
    return (
        f'<s:Envelope xmlns:s="{SOAP12_NS}" xmlns:tds="{DEVICE_NS}" '
        f'xmlns:tt="{SCHEMA_NS}" xmlns:trt="{MEDIA1_NS}" '
        f'xmlns:tr2="{MEDIA2_NS}" xmlns:tptz="{PTZ_NS}" '
        f'xmlns:tev="{EVENTS_NS}" xmlns:wstop="{WSTOP_NS}" '
        f'xmlns:tns="{TOPICS_NS}" xmlns:vendor="urn:vendor">'
        f"<s:Body>{body}</s:Body></s:Envelope>"
    )


def soap11(body: str) -> str:
    return (
        f'<env:Envelope xmlns:env="{SOAP11_NS}" '
        'xmlns:ter="http://www.onvif.org/ver10/error">'
        f"<env:Body>{body}</env:Body></env:Envelope>"
    )


class CapabilityParserTests(unittest.TestCase):
    def test_public_report_dataclasses_are_frozen_and_deeply_immutable(self):
        self.assertEqual(
            [field.name for field in dataclasses.fields(OnvifProfile)],
            [
                "token",
                "name",
                "has_audio_encoder",
                "has_audio_output",
                "has_audio_source",
            ],
        )
        service = CameraCapabilityService(
            namespace=MEDIA2_NS,
            xaddr="http://camera/media2",
            version=CameraCapabilityVersion(major=2, minor=0),
        )
        report = CameraCapabilityReport(
            device=DeviceInfo(manufacturer="Fixture Camera", model="C1"),
            scopes=("onvif://www.onvif.org/Profile/Streaming",),
            declared_profiles=("S",),
            service_discovery="getServices",
            services=(service,),
            profiles=(),
            ptz=PtzCapabilityReport(
                detected=None,
                pan_tilt_supported=None,
                zoom_supported=None,
                profile_tokens=(),
                service_capabilities=None,
                nodes=(),
            ),
            events=EventCapabilityReport(
                detected=None,
                service_capabilities=None,
                topics=(),
            ),
            media2=Media2CapabilityReport(
                detected=True,
                encodings=("H265",),
                h265_supported=True,
            ),
            warnings=(CameraCapabilityWarning("GetScopes", "request timeout"),),
        )

        for value in (
            report,
            report.device,
            service,
            service.version,
            report.ptz,
            report.events,
            report.media2,
            report.warnings[0],
        ):
            self.assertTrue(dataclasses.is_dataclass(value))
            self.assertTrue(value.__dataclass_params__.frozen)
        self.assertIsInstance(report.services, tuple)
        self.assertIsInstance(report.warnings, tuple)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            report.service_discovery = "unavailable"

    def test_parses_nested_scopes_with_stable_dedup_and_profile_aliases(self):
        streaming = "onvif://www.onvif.org/Profile/Streaming"
        profile_t = "onvif://www.onvif.org/Profile/%74"
        vendor = "onvif://www.onvif.org/Profile/vendor%2Dplus"
        hardware = "onvif://www.onvif.org/hardware/Camera%201"
        parsed = _parse_scopes_response(
            soap(
                f"""
                <tds:GetScopesResponse>
                  <tds:Scopes><tt:ScopeDef>Fixed</tt:ScopeDef><tt:ScopeItem>{streaming}</tt:ScopeItem></tds:Scopes>
                  <tds:Scopes><tt:ScopeItem>{hardware}</tt:ScopeItem></tds:Scopes>
                  <tds:Scopes><tt:ScopeItem>{streaming}</tt:ScopeItem></tds:Scopes>
                  <tds:Scopes><tt:ScopeItem>{profile_t}</tt:ScopeItem></tds:Scopes>
                  <tds:Scopes><tt:ScopeItem>{vendor}</tt:ScopeItem></tds:Scopes>
                  <tds:Scopes><tt:ScopeItem>onvif://www.onvif.org/Profile/%ZZ</tt:ScopeItem></tds:Scopes>
                  <tds:Scopes><tt:ScopeItem>onvif://www.onvif.org/Profile/T/extra</tt:ScopeItem></tds:Scopes>
                  <tds:Scopes><tt:ScopeItem>onvif://www.onvif.org/Profile/T?x=1</tt:ScopeItem></tds:Scopes>
                  <tds:Scopes><tt:ScopeItem>onvif://www.onvif.org/profile/R</tt:ScopeItem></tds:Scopes>
                  <vendor:Scopes><tt:ScopeItem>onvif://www.onvif.org/Profile/Q</tt:ScopeItem></vendor:Scopes>
                </tds:GetScopesResponse>
                """
            )
        )

        self.assertEqual(
            parsed.scopes,
            (
                streaming,
                hardware,
                profile_t,
                vendor,
                "onvif://www.onvif.org/Profile/%ZZ",
                "onvif://www.onvif.org/Profile/T/extra",
                "onvif://www.onvif.org/Profile/T?x=1",
                "onvif://www.onvif.org/profile/R",
            ),
        )
        self.assertEqual(parsed.declared_profiles, ("S", "T", "VENDOR-PLUS"))


def service(namespace: str, xaddr: str, major: int = 1, minor: int = 0) -> str:
    return (
        f"<tds:Service><tds:Namespace>{namespace}</tds:Namespace>"
        f"<tds:XAddr>{xaddr}</tds:XAddr><tds:Version>"
        f"<tt:Major>{major}</tt:Major><tt:Minor>{minor}</tt:Minor>"
        "</tds:Version></tds:Service>"
    )


def raw_response(body: str, status_code: int = 200):
    return SimpleNamespace(status_code=status_code, xml=soap(body))


class FakeCapabilityDevice:
    def __init__(self, responder, *, media_url="http://camera/connected-media"):
        self.responder = responder
        self.media_url = media_url
        self.calls = []
        self.connect_count = 0

    def connect(self):
        self.connect_count += 1
        return DeviceInfo(manufacturer="Fixture Camera", model="C1")

    def read_only_call(self, body, endpoint=None):
        self.calls.append((body, endpoint))
        return self.responder(body, endpoint)

    def _required_media_url(self):
        return self.media_url


class CapabilityOrchestrationTests(unittest.TestCase):
    def _run_with_fake(self, fake, **options):
        constructor_calls = []

        def construct(host, user="", password="", **kwargs):
            constructor_calls.append((host, user, password, kwargs))
            return fake

        with patch("rtsp_backchannel.capabilities.OnvifDevice", side_effect=construct):
            report = get_camera_capabilities(**options)
        return report, constructor_calls

    def test_routes_exact_read_only_operations_to_selected_service_endpoints(self):
        def respond(body, endpoint):
            if body == GET_SCOPES:
                return raw_response(
                    "<tds:GetScopesResponse><tds:Scopes><tt:ScopeItem>"
                    "onvif://www.onvif.org/Profile/Streaming"
                    "</tt:ScopeItem></tds:Scopes></tds:GetScopesResponse>"
                )
            if body == GET_SERVICES:
                return raw_response(
                    "<tds:GetServicesResponse>"
                    + service(MEDIA1_NS, "http://camera/media1", 2, 0)
                    + service(PTZ_NS, "http://camera/ptz", 2, 0)
                    + service(EVENTS_NS, "http://camera/events", 2, 0)
                    + service(MEDIA2_NS, "http://camera/media2", 2, 0)
                    + "</tds:GetServicesResponse>"
                )
            if body == MEDIA1_GET_PROFILES and endpoint == "http://camera/media1":
                return raw_response(
                    '<trt:GetProfilesResponse><trt:Profiles token="legacy">'
                    "<tt:Name>Legacy</tt:Name><tt:PTZConfiguration token=\"ptz-legacy\">"
                    "<tt:NodeToken>node-1</tt:NodeToken></tt:PTZConfiguration>"
                    "</trt:Profiles></trt:GetProfilesResponse>"
                )
            if body == PTZ_GET_CAPABILITIES and endpoint == "http://camera/ptz":
                return raw_response(
                    '<tptz:GetServiceCapabilitiesResponse><tptz:Capabilities EFlip="true"/>'
                    "</tptz:GetServiceCapabilitiesResponse>"
                )
            if body == PTZ_GET_NODES and endpoint == "http://camera/ptz":
                return raw_response(
                    '<tptz:GetNodesResponse><tptz:PTZNode token="node-1">'
                    "<tt:SupportedPTZSpaces><tt:AbsolutePanTiltPositionSpace/>"
                    "<tt:AbsoluteZoomPositionSpace/></tt:SupportedPTZSpaces>"
                    "</tptz:PTZNode></tptz:GetNodesResponse>"
                )
            if body == EVENTS_GET_CAPABILITIES and endpoint == "http://camera/events":
                return raw_response(
                    '<tev:GetServiceCapabilitiesResponse><tev:Capabilities WSPullPointSupport="true"/>'
                    "</tev:GetServiceCapabilitiesResponse>"
                )
            if body == EVENTS_GET_PROPERTIES and endpoint == "http://camera/events":
                return raw_response(
                    '<tev:GetEventPropertiesResponse><wstop:TopicSet><tns:Motion wstop:topic="true"/>'
                    "</wstop:TopicSet></tev:GetEventPropertiesResponse>"
                )
            if body == MEDIA2_GET_PROFILES and endpoint == "http://camera/media2":
                return raw_response(
                    '<tr2:GetProfilesResponse><tr2:Profiles token="modern">'
                    '<tr2:Configurations><tr2:PTZ token="ptz-modern"/>'
                    "</tr2:Configurations></tr2:Profiles></tr2:GetProfilesResponse>"
                )
            if body == MEDIA2_GET_OPTIONS and endpoint == "http://camera/media2":
                return raw_response(
                    "<tr2:GetVideoEncoderConfigurationOptionsResponse>"
                    "<tr2:Options><tt:Encoding>H264</tt:Encoding></tr2:Options>"
                    "<tr2:Options><tt:Encoding>H265</tt:Encoding></tr2:Options>"
                    "</tr2:GetVideoEncoderConfigurationOptionsResponse>"
                )
            raise AssertionError(f"unexpected operation {body!r} at {endpoint!r}")

        fake = FakeCapabilityDevice(respond)
        report, constructor_calls = self._run_with_fake(
            fake,
            host="camera",
            user="admin",
            password="password",
            device_urls=["http://camera/device"],
            timeout=1.25,
        )

        self.assertEqual(
            constructor_calls,
            [
                (
                    "camera",
                    "admin",
                    "password",
                    {
                        "device_urls": ["http://camera/device"],
                        "timeout": 1.25,
                    },
                )
            ],
        )
        self.assertEqual(fake.connect_count, 1)
        self.assertEqual(
            fake.calls,
            [
                (GET_SCOPES, None),
                (GET_SERVICES, None),
                (MEDIA1_GET_PROFILES, "http://camera/media1"),
                (PTZ_GET_CAPABILITIES, "http://camera/ptz"),
                (PTZ_GET_NODES, "http://camera/ptz"),
                (EVENTS_GET_CAPABILITIES, "http://camera/events"),
                (EVENTS_GET_PROPERTIES, "http://camera/events"),
                (MEDIA2_GET_PROFILES, "http://camera/media2"),
                (MEDIA2_GET_OPTIONS, "http://camera/media2"),
            ],
        )
        self.assertEqual(report.device, DeviceInfo("Fixture Camera", "C1"))
        self.assertEqual(report.declared_profiles, ("S",))
        self.assertEqual(report.service_discovery, "getServices")
        self.assertEqual(
            tuple((profile.token, profile.source) for profile in report.profiles),
            (("legacy", "media1"), ("modern", "media2")),
        )
        self.assertEqual(report.ptz.profile_tokens, ("legacy", "modern"))
        self.assertEqual(report.ptz.detected, True)
        self.assertEqual(report.ptz.pan_tilt_supported, True)
        self.assertEqual(report.ptz.zoom_supported, True)
        self.assertEqual(report.ptz.service_capabilities.e_flip, True)
        self.assertEqual(report.events.detected, True)
        self.assertEqual(
            report.events.service_capabilities.ws_pull_point_support, True
        )
        self.assertEqual(
            tuple((topic.namespace, topic.path) for topic in report.events.topics),
            ((TOPICS_NS, "Motion"),),
        )
        self.assertEqual(report.media2, Media2CapabilityReport(True, ("H264", "H265"), True))
        self.assertEqual(report.warnings, ())

    def test_falls_back_to_get_capabilities_all_and_preserves_tristate_unknowns(self):
        def respond(body, endpoint):
            if body == GET_SCOPES:
                return raw_response("<tds:GetScopesResponse/>")
            if body == GET_SERVICES:
                return raw_response(
                    "<s:Fault><s:Code><s:Value>s:Sender</s:Value><s:Subcode>"
                    '<s:Value xmlns:ter="http://www.onvif.org/ver10/error">'
                    "ter:ActionNotSupported</s:Value></s:Subcode></s:Code></s:Fault>",
                    500,
                )
            if body == GET_ALL_CAPABILITIES:
                return raw_response(
                    "<tds:GetCapabilitiesResponse><tds:Capabilities><tt:Media>"
                    "<tt:XAddr>http://camera/legacy-media</tt:XAddr></tt:Media>"
                    "</tds:Capabilities></tds:GetCapabilitiesResponse>"
                )
            if body == MEDIA1_GET_PROFILES and endpoint == "http://camera/legacy-media":
                return raw_response("<trt:GetProfilesResponse/>")
            raise AssertionError(body)

        fake = FakeCapabilityDevice(respond)
        report, _ = self._run_with_fake(fake, host="camera")

        self.assertEqual(report.service_discovery, "getCapabilities")
        self.assertEqual(
            tuple(warning.operation for warning in report.warnings),
            ("GetServices",),
        )
        self.assertEqual(
            tuple(body for body, _ in fake.calls),
            (GET_SCOPES, GET_SERVICES, GET_ALL_CAPABILITIES, MEDIA1_GET_PROFILES),
        )
        self.assertEqual(report.ptz.detected, False)
        self.assertIsNone(report.ptz.pan_tilt_supported)
        self.assertIsNone(report.ptz.zoom_supported)
        self.assertEqual(report.events.detected, False)
        self.assertIsNone(report.media2.detected)
        self.assertIsNone(report.media2.h265_supported)

    def test_authentication_fault_is_fatal_and_never_runs_fallback(self):
        secret_fault = soap11(
            "<env:Fault><faultcode>env:Client</faultcode>"
            "<faultstring>ter:NotAuthorized viewer url-secret payload-secret</faultstring>"
            "<detail><Value>ter:ActionNotSupported</Value></detail></env:Fault>"
        )

        def respond(body, endpoint):
            if body == GET_SCOPES:
                return raw_response("<tds:GetScopesResponse/>")
            if body == GET_SERVICES:
                return SimpleNamespace(status_code=500, xml=secret_fault)
            raise AssertionError("fallback must not run")

        fake = FakeCapabilityDevice(respond)
        with self.assertRaisesRegex(_OnvifResponseError, "SOAP Fault: NotAuthorized") as caught:
            self._run_with_fake(fake, host="camera")
        self.assertEqual(str(caught.exception), "SOAP Fault: NotAuthorized")
        self.assertNotRegex(str(caught.exception), "viewer|secret|payload")
        self.assertEqual(
            tuple(body for body, _ in fake.calls), (GET_SCOPES, GET_SERVICES)
        )

    def test_http_authentication_status_is_fatal_before_parsing(self):
        def respond(body, endpoint):
            if body == GET_SCOPES:
                return raw_response("<tds:GetScopesResponse/>")
            if body == GET_SERVICES:
                return SimpleNamespace(
                    status_code=401,
                    xml="viewer:top-secret@camera payload-secret",
                )
            raise AssertionError("fallback must not run")

        fake = FakeCapabilityDevice(respond)
        with self.assertRaisesRegex(RuntimeError, "HTTP 401") as caught:
            self._run_with_fake(fake, host="camera")
        self.assertEqual(str(caught.exception), "HTTP 401")
        self.assertNotIn("secret", str(caught.exception))

    def test_http_auth_status_is_fatal_without_reading_an_unusable_body(self):
        class UnreadableBody(io.BytesIO):
            def __init__(self):
                super().__init__(b"payload-secret")
                self.read_calls = 0

            def read(self, *args, **kwargs):
                self.read_calls += 1
                raise TimeoutError("body read must not run")

            def read1(self, *args, **kwargs):
                self.read_calls += 1
                raise TimeoutError("body read must not run")

        for headers in (
            {"Content-Length": str(1024 * 1024 + 1)},
            {},
        ):
            with self.subTest(headers=headers):
                device = OnvifDevice("camera")
                device.device_url = "http://camera/onvif/device_service"
                device.media_url = "http://camera/onvif/media_service"
                bodies = []

                def reject(*args, **kwargs):
                    body = UnreadableBody()
                    bodies.append(body)
                    raise urllib.error.HTTPError(
                        "http://camera/onvif/device_service",
                        401,
                        "Unauthorized",
                        headers,
                        body,
                    )

                with (
                    patch(
                        "rtsp_backchannel.capabilities.OnvifDevice",
                        return_value=device,
                    ),
                    patch.object(
                        device,
                        "connect",
                        return_value=DeviceInfo(manufacturer="Fixture Camera"),
                    ),
                    patch.object(
                        onvif.urllib.request,
                        "urlopen",
                        side_effect=reject,
                    ) as urlopen,
                ):
                    with self.assertRaisesRegex(RuntimeError, "HTTP 401"):
                        get_camera_capabilities(host="camera")

                self.assertEqual(urlopen.call_count, 1)
                self.assertEqual(len(bodies), 1)
                self.assertEqual(bodies[0].read_calls, 0)
                self.assertTrue(bodies[0].closed)

    def test_discovery_failures_are_sanitized_and_media1_stays_available(self):
        def respond(body, endpoint):
            if body == GET_SCOPES:
                raise RuntimeError(
                    "request http://viewer:top-secret@camera/scopes used password password"
                )
            if body == GET_SERVICES:
                return raw_response("<tds:GetServicesResponse/>")
            if body == GET_ALL_CAPABILITIES:
                raise RuntimeError(
                    "admin <Password>payload-secret</Password> PasswordDigest digest-token"
                )
            if body == MEDIA1_GET_PROFILES and endpoint == "http://camera/connected-media":
                return raw_response(
                    '<trt:GetProfilesResponse><trt:Profiles token="fallback"/>'
                    "</trt:GetProfilesResponse>"
                )
            raise AssertionError(body)

        fake = FakeCapabilityDevice(respond)
        report, _ = self._run_with_fake(
            fake,
            host="camera",
            user="admin",
            password="password",
        )

        self.assertEqual(report.service_discovery, "unavailable")
        self.assertEqual(tuple(profile.token for profile in report.profiles), ("fallback",))
        self.assertIsNone(report.ptz.detected)
        self.assertIsNone(report.events.detected)
        self.assertIsNone(report.media2.detected)
        self.assertEqual(
            tuple(warning.operation for warning in report.warnings),
            ("GetScopes", "GetServices", "GetCapabilities"),
        )
        warning_text = json.dumps(
            [dataclasses.asdict(warning) for warning in report.warnings]
        )
        self.assertNotRegex(
            warning_text,
            "admin|password|viewer|top-secret|@camera|payload-secret|PasswordDigest|digest-token",
        )

    def test_media2_profiles_recover_after_media1_ptz_and_event_failures(self):
        def respond(body, endpoint):
            if body == GET_SCOPES:
                return raw_response("<tds:GetScopesResponse/>")
            if body == GET_SERVICES:
                return raw_response(
                    "<tds:GetServicesResponse>"
                    + service(MEDIA1_NS, "http://camera/media1")
                    + service(PTZ_NS, "http://camera/ptz")
                    + service(EVENTS_NS, "http://camera/events")
                    + service(MEDIA2_NS, "http://camera/media2")
                    + "</tds:GetServicesResponse>"
                )
            if body == MEDIA1_GET_PROFILES:
                raise RuntimeError(
                    "media1 failed at http://operator:camera-pass@camera/media1"
                )
            if body == PTZ_GET_CAPABILITIES:
                return raw_response(
                    '<tptz:GetServiceCapabilitiesResponse><tptz:Capabilities Reverse="1"/>'
                    "</tptz:GetServiceCapabilitiesResponse>"
                )
            if body == PTZ_GET_NODES:
                raise TimeoutError("request timeout")
            if body == EVENTS_GET_CAPABILITIES:
                return raw_response(
                    "<tev:GetServiceCapabilitiesResponse><tev:Capabilities/>"
                    "</tev:GetServiceCapabilitiesResponse>",
                    500,
                )
            if body == EVENTS_GET_PROPERTIES:
                return raw_response(
                    "<tev:GetEventPropertiesResponse><wstop:TopicSet/>"
                    "</tev:GetEventPropertiesResponse>"
                )
            if body == MEDIA2_GET_PROFILES and endpoint == "http://camera/media2":
                return raw_response(
                    '<tr2:GetProfilesResponse><tr2:Profiles token="media2-only">'
                    "<tr2:Configurations><tr2:AudioEncoder/></tr2:Configurations>"
                    "</tr2:Profiles></tr2:GetProfilesResponse>"
                )
            if body == MEDIA2_GET_OPTIONS and endpoint == "http://camera/media2":
                return raw_response(
                    "<tr2:GetVideoEncoderConfigurationOptionsResponse>"
                    "<tr2:Options><tt:Encoding>H264</tt:Encoding></tr2:Options>"
                    "</tr2:GetVideoEncoderConfigurationOptionsResponse>"
                )
            raise AssertionError(f"unexpected operation {body!r} at {endpoint!r}")

        fake = FakeCapabilityDevice(respond)
        report, _ = self._run_with_fake(
            fake,
            host="camera",
            user="operator",
            password="camera-pass",
        )

        self.assertEqual(
            tuple((profile.token, profile.source) for profile in report.profiles),
            (("media2-only", "media2"),),
        )
        self.assertTrue(report.profiles[0].has_audio_encoder)
        self.assertEqual(report.ptz.detected, True)
        self.assertIsNone(report.ptz.pan_tilt_supported)
        self.assertIsNone(report.ptz.zoom_supported)
        self.assertEqual(report.ptz.nodes, ())
        self.assertEqual(report.ptz.service_capabilities.reverse, True)
        self.assertEqual(report.events.detected, True)
        self.assertEqual(report.events.topics, ())
        self.assertEqual(report.media2.detected, True)
        self.assertEqual(report.media2.encodings, ("H264",))
        self.assertEqual(report.media2.h265_supported, False)
        self.assertEqual(
            tuple(warning.operation for warning in report.warnings),
            (
                "Media1 GetProfiles",
                "PTZ GetNodes",
                "Events GetServiceCapabilities",
            ),
        )
        warning_text = json.dumps(
            [dataclasses.asdict(warning) for warning in report.warnings]
        )
        self.assertNotRegex(warning_text, "operator|camera-pass|@camera")

    def test_invalid_getservices_response_also_uses_legacy_fallback(self):
        def respond(body, endpoint):
            if body == GET_SCOPES:
                return raw_response("<tds:GetScopesResponse/>")
            if body == GET_SERVICES:
                return raw_response("<tds:GetServicesResponse/>")
            if body == GET_ALL_CAPABILITIES:
                return raw_response(
                    "<tds:GetCapabilitiesResponse><tds:Capabilities><tt:Media>"
                    "<tt:XAddr>http://camera/media</tt:XAddr></tt:Media>"
                    "</tds:Capabilities></tds:GetCapabilitiesResponse>"
                )
            if body == MEDIA1_GET_PROFILES:
                return raw_response("<trt:GetProfilesResponse/>")
            raise AssertionError(body)

        fake = FakeCapabilityDevice(respond)
        report, _ = self._run_with_fake(fake, host="camera")

        self.assertEqual(report.service_discovery, "getCapabilities")
        self.assertEqual(report.warnings[0].operation, "GetServices")
        self.assertEqual(
            tuple(body for body, _ in fake.calls),
            (GET_SCOPES, GET_SERVICES, GET_ALL_CAPABILITIES, MEDIA1_GET_PROFILES),
        )

    def test_authentication_token_substrings_do_not_block_legacy_fallback(self):
        def respond(body, endpoint):
            if body == GET_SCOPES:
                return raw_response("<tds:GetScopesResponse/>")
            if body == GET_SERVICES:
                return raw_response(
                    "<s:Fault><s:Code><s:Value>s:Sender</s:Value><s:Subcode>"
                    '<s:Value xmlns:ter="http://www.onvif.org/ver10/error">'
                    "ter:UnauthorizedOperation</s:Value></s:Subcode></s:Code>"
                    "</s:Fault>",
                    500,
                )
            if body == GET_ALL_CAPABILITIES:
                return raw_response(
                    "<tds:GetCapabilitiesResponse><tds:Capabilities><tt:Media>"
                    "<tt:XAddr>http://camera/media</tt:XAddr></tt:Media>"
                    "</tds:Capabilities></tds:GetCapabilitiesResponse>"
                )
            if body == MEDIA1_GET_PROFILES:
                return raw_response("<trt:GetProfilesResponse/>")
            raise AssertionError(body)

        fake = FakeCapabilityDevice(respond)
        report, _ = self._run_with_fake(fake, host="camera")

        self.assertEqual(report.service_discovery, "getCapabilities")
        self.assertEqual(report.warnings[0].message, "SOAP Fault: UnauthorizedOperation")

    def test_connect_failure_is_fatal_before_any_capability_request(self):
        class ConnectFailureDevice(FakeCapabilityDevice):
            def connect(self):
                self.connect_count += 1
                raise RuntimeError("ONVIF connect failed")

        fake = ConnectFailureDevice(
            lambda body, endpoint: (_ for _ in ()).throw(
                AssertionError("no capability request expected")
            )
        )
        with self.assertRaisesRegex(RuntimeError, "ONVIF connect failed"):
            self._run_with_fake(fake, host="camera")

        self.assertEqual(fake.connect_count, 1)
        self.assertEqual(fake.calls, [])


class OnvifDeviceInformationTests(unittest.TestCase):
    @staticmethod
    def _system_time_response():
        return soap(
            "<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime>"
            "<tt:UTCDateTime><tt:Date><tt:Year>2026</tt:Year><tt:Month>8</tt:Month>"
            "<tt:Day>6</tt:Day></tt:Date><tt:Time><tt:Hour>12</tt:Hour>"
            "<tt:Minute>0</tt:Minute><tt:Second>0</tt:Second></tt:Time>"
            "</tt:UTCDateTime></tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>"
        )

    def test_device_information_uses_exact_response_and_direct_device_children(self):
        information = soap(
            "<vendor:GetDeviceInformationResponse>"
            "<vendor:Manufacturer>Vendor Response Decoy</vendor:Manufacturer>"
            "</vendor:GetDeviceInformationResponse>"
            "<tds:GetDeviceInformationResponse>"
            "<vendor:Manufacturer>Vendor Child Decoy</vendor:Manufacturer>"
            "<tds:Manufacturer>Real Camera</tds:Manufacturer>"
            "<tds:Model>C1</tds:Model>"
            "</tds:GetDeviceInformationResponse>"
        )
        media = soap(
            "<tds:GetCapabilitiesResponse><tds:Capabilities><tt:Media>"
            "<tt:XAddr>http://camera/media</tt:XAddr></tt:Media>"
            "</tds:Capabilities></tds:GetCapabilitiesResponse>"
        )
        device = OnvifDevice(
            "camera", device_urls=["http://camera/onvif/device_service"]
        )

        with patch.object(
            onvif,
            "_soap_request",
            side_effect=[self._system_time_response(), information, media],
        ):
            info = device.connect()

        self.assertEqual(
            info,
            DeviceInfo(manufacturer="Real Camera", model="C1"),
        )

    def test_vendor_local_name_response_cannot_satisfy_device_authentication(self):
        device = OnvifDevice(
            "camera", device_urls=["http://camera/onvif/device_service"]
        )
        vendor_response = soap(
            "<vendor:GetDeviceInformationResponse>"
            "<vendor:Manufacturer>Decoy</vendor:Manufacturer>"
            "</vendor:GetDeviceInformationResponse>"
        )

        with patch.object(
            onvif,
            "_soap_request",
            side_effect=[self._system_time_response(), vendor_response],
        ) as request:
            with self.assertRaisesRegex(RuntimeError, "ONVIF connect failed"):
                device.connect()

        self.assertEqual(request.call_count, 2)


class _OnvifFixtureHandler(http.server.BaseHTTPRequestHandler):
    requests = []

    def log_message(self, format, *args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        type(self).requests.append((self.path, body))
        if "GetSystemDateAndTime" in body:
            payload = soap(
                "<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime>"
                "<tt:UTCDateTime><tt:Time><tt:Hour>12</tt:Hour><tt:Minute>30</tt:Minute>"
                "<tt:Second>0</tt:Second></tt:Time><tt:Date><tt:Year>2026</tt:Year>"
                "<tt:Month>8</tt:Month><tt:Day>6</tt:Day></tt:Date>"
                "</tt:UTCDateTime></tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>"
            )
            status = 200
        elif "GetDeviceInformation" in body:
            payload = soap(
                "<tds:GetDeviceInformationResponse><tds:Manufacturer>Test Camera</tds:Manufacturer>"
                "<tds:Model>C1</tds:Model><tds:FirmwareVersion>1.2.3</tds:FirmwareVersion>"
                "<tds:SerialNumber>serial-1</tds:SerialNumber>"
                "</tds:GetDeviceInformationResponse>"
            )
            status = 200
        elif "<Category>Media</Category>" in body:
            port = self.server.server_port
            payload = soap(
                "<tds:GetCapabilitiesResponse><tds:Capabilities><tt:Media><tt:XAddr>"
                f"http://127.0.0.1:{port}/advertised/media"
                "</tt:XAddr></tt:Media></tds:Capabilities></tds:GetCapabilitiesResponse>"
            )
            status = 200
        elif self.path == "/auth-fault":
            payload = soap(
                "<s:Fault><s:Reason><s:Text>Not authorized payload-secret</s:Text>"
                "</s:Reason></s:Fault>"
            )
            status = 401
        else:
            payload = soap("<tds:GetScopesResponse/>")
            status = 200
        encoded = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/soap+xml")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class OnvifCapabilityTransportTests(unittest.TestCase):
    def setUp(self):
        _OnvifFixtureHandler.requests = []
        self.server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), _OnvifFixtureHandler
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_connect_return_and_status_aware_calls_keep_the_three_call_sequence(self):
        port = self.server.server_port
        selected_url = f"http://127.0.0.1:{port}/selected/device"
        device = OnvifDevice(
            "camera",
            "admin",
            "password",
            device_urls=[selected_url],
        )

        info = device.connect()
        selected = device.read_only_call(GET_SCOPES)
        auth_fault = device.read_only_call(
            GET_SCOPES, f"http://127.0.0.1:{port}/auth-fault"
        )

        self.assertEqual(
            info,
            DeviceInfo(
                manufacturer="Test Camera",
                model="C1",
                firmware="1.2.3",
                serial="serial-1",
            ),
        )
        requests = _OnvifFixtureHandler.requests
        self.assertEqual(
            [path for path, _ in requests],
            [
                "/selected/device",
                "/selected/device",
                "/selected/device",
                "/selected/device",
                "/auth-fault",
            ],
        )
        self.assertEqual(
            [
                next(part.split("</s:Body>", 1) for part in body.split("<s:Body>") if "</s:Body>" in part)[0]
                for _, body in requests[:3]
            ],
            [
                f'<GetSystemDateAndTime xmlns="{DEVICE_NS}"/>',
                f'<GetDeviceInformation xmlns="{DEVICE_NS}"/>',
                f'<GetCapabilities xmlns="{DEVICE_NS}"><Category>Media</Category></GetCapabilities>',
            ],
        )
        self.assertEqual(selected.status_code, 200)
        self.assertRegex(selected.xml, "GetScopesResponse")
        self.assertEqual(auth_fault.status_code, 401)
        self.assertEqual(auth_fault.xml, "")
        self.assertRegex(requests[3][1], "wsse:Security")
        self.assertRegex(requests[4][1], "wsse:Security")

    def test_rejects_unsafe_service_urls_before_wsse_or_network(self):
        device = OnvifDevice("camera", "viewer", "camera-secret")
        device.device_url = f"http://127.0.0.1:{self.server.server_port}/selected/device"
        endpoints = (
            f"ftp://127.0.0.1:{self.server.server_port}/must-not-reach",
            f"http://viewer:url-secret@127.0.0.1:{self.server.server_port}/must-not-reach",
            f"http://127.0.0.1:{self.server.server_port}/must-not-reach\n",
            "/relative/onvif/device_service",
        )

        with patch.object(onvif, "_wsse_header", wraps=onvif._wsse_header) as wsse:
            messages = []
            for endpoint in endpoints:
                with self.assertRaises(RuntimeError) as caught:
                    device.read_only_call(GET_SCOPES, endpoint)
                messages.append(str(caught.exception))

        self.assertEqual(messages, ["invalid ONVIF service URL"] * len(endpoints))
        self.assertEqual(wsse.call_count, 0)
        self.assertEqual(_OnvifFixtureHandler.requests, [])
        self.assertNotRegex(
            json.dumps(messages), "viewer|camera-secret|url-secret|must-not-reach"
        )

    def test_allows_an_advertised_endpoint_on_a_different_host(self):
        device = OnvifDevice("connected-camera", "admin", "password")
        device.device_url = "http://connected-camera.invalid/onvif/device_service"
        endpoint = f"http://127.0.0.1:{self.server.server_port}/foreign-service"

        response = device.read_only_call(GET_SCOPES, endpoint)

        self.assertEqual(response.status_code, 200)
        self.assertRegex(response.xml, "GetScopesResponse")
        self.assertEqual(
            [path for path, _ in _OnvifFixtureHandler.requests],
            ["/foreign-service"],
        )

    def test_connect_failure_does_not_expose_credential_like_candidates(self):
        devices = (
            OnvifDevice("viewer:top-secret@camera", "admin", "camera-secret"),
            OnvifDevice(
                "camera",
                "admin",
                "camera-secret",
                device_urls=[
                    "http://viewer:url-secret@camera/onvif/device_service"
                ],
            ),
        )
        messages = []

        with patch.object(onvif, "_wsse_header", wraps=onvif._wsse_header) as wsse:
            for device in devices:
                with self.assertRaises(RuntimeError) as caught:
                    device.connect()
                messages.append(str(caught.exception))

        self.assertEqual(messages, ["ONVIF connect failed"] * len(devices))
        self.assertEqual(wsse.call_count, 0)
        self.assertEqual(_OnvifFixtureHandler.requests, [])
        self.assertNotRegex(
            json.dumps(messages), "viewer|secret|@camera|url-secret"
        )


class CapabilityProtocolParserTests(unittest.TestCase):

    def test_extremely_large_integer_lexemes_are_treated_as_out_of_range(self):
        huge = "9" * 5000
        padded_one = "0" * 5000 + "1"
        parsed = _parse_services_response(
            soap(
                "<tds:GetServicesResponse><tds:Service>"
                f"<tds:Namespace>urn:huge</tds:Namespace>"
                "<tds:XAddr>http://camera/huge</tds:XAddr><tds:Version>"
                f"<tt:Major>{huge}</tt:Major><tt:Minor>0</tt:Minor>"
                "</tds:Version></tds:Service><tds:Service>"
                "<tds:Namespace>urn:padded</tds:Namespace>"
                "<tds:XAddr>http://camera/padded</tds:XAddr><tds:Version>"
                f"<tt:Major>{padded_one}</tt:Major><tt:Minor>0</tt:Minor>"
                "</tds:Version></tds:Service><tds:Service>"
                "<tds:Namespace>urn:unicode</tds:Namespace>"
                "<tds:XAddr>http://camera/unicode</tds:XAddr><tds:Version>"
                "<tt:Major>\N{ARABIC-INDIC DIGIT ONE}</tt:Major>"
                "<tt:Minor>0</tt:Minor></tds:Version></tds:Service>"
                "</tds:GetServicesResponse>"
            )
        )

        versions = {
            service.namespace: service.version for service in parsed.services
        }
        self.assertIsNone(versions["urn:huge"])
        self.assertEqual(versions["urn:padded"], CameraCapabilityVersion(1, 0))
        self.assertIsNone(versions["urn:unicode"])

    def test_services_use_direct_association_strict_scalars_and_stable_selection(self):
        parsed = _parse_services_response(
            soap(
                f"""
                <tds:GetServicesResponse>
                  <tds:Service><tds:Namespace>{MEDIA1_NS}</tds:Namespace><tds:XAddr>http://camera/media-z</tds:XAddr>
                    <tds:Version><tt:Major>2</tt:Major><tt:Minor>9</tt:Minor></tds:Version></tds:Service>
                  <tds:Service><tds:Namespace>{MEDIA1_NS}</tds:Namespace><tds:XAddr>http://camera/media-b</tds:XAddr>
                    <tds:Version><tt:Major>2</tt:Major><tt:Minor>10</tt:Minor></tds:Version></tds:Service>
                  <tds:Service><tds:Namespace>{MEDIA1_NS}</tds:Namespace><tds:XAddr>http://camera/media-a</tds:XAddr>
                    <tds:Version><tt:Major>2</tt:Major><tt:Minor>10</tt:Minor></tds:Version></tds:Service>
                  <tds:Service><tds:Namespace>urn:plus</tds:Namespace><tds:XAddr>http://camera/plus</tds:XAddr>
                    <tds:Version><tt:Major> \t+1\r\n</tt:Major><tt:Minor>+2</tt:Minor></tds:Version></tds:Service>
                  <tds:Service><tds:Namespace>urn:nbsp</tds:Namespace><tds:XAddr>http://camera/nbsp</tds:XAddr>
                    <tds:Version><tt:Major>{NBSP}+1{NBSP}</tt:Major><tt:Minor>2</tt:Minor></tds:Version></tds:Service>
                  <tds:Service><tds:Namespace>urn:overflow</tds:Namespace><tds:XAddr>http://camera/overflow</tds:XAddr>
                    <tds:Version><tt:Major>2147483648</tt:Major><tt:Minor>0</tt:Minor></tds:Version></tds:Service>
                  <tds:Wrapper><tds:Service><tds:Namespace>urn:decoy</tds:Namespace>
                    <tds:XAddr>http://camera/decoy</tds:XAddr></tds:Service></tds:Wrapper>
                </tds:GetServicesResponse>
                """
            )
        )

        self.assertEqual(
            tuple((item.namespace, item.xaddr, item.version) for item in parsed.services),
            (
                (
                    MEDIA1_NS,
                    "http://camera/media-a",
                    CameraCapabilityVersion(2, 10),
                ),
                (
                    MEDIA1_NS,
                    "http://camera/media-b",
                    CameraCapabilityVersion(2, 10),
                ),
                (
                    MEDIA1_NS,
                    "http://camera/media-z",
                    CameraCapabilityVersion(2, 9),
                ),
                ("urn:nbsp", "http://camera/nbsp", None),
                ("urn:overflow", "http://camera/overflow", None),
                ("urn:plus", "http://camera/plus", CameraCapabilityVersion(1, 2)),
            ),
        )
        self.assertEqual(
            _select_service(parsed.services, MEDIA1_NS),
            CameraCapabilityService(
                MEDIA1_NS,
                "http://camera/media-a",
                CameraCapabilityVersion(2, 10),
            ),
        )

        for malformed in (
            f"<tds:GetServicesResponse><tds:Service><tds:Namespace>{MEDIA1_NS}</tds:Namespace></tds:Service></tds:GetServicesResponse>",
            '<tds:GetServicesResponse><tds:Service><tds:XAddr>http://camera/media</tds:XAddr></tds:Service></tds:GetServicesResponse>',
        ):
            with self.subTest(malformed=malformed):
                with self.assertRaisesRegex(_OnvifResponseError, "invalid GetServices"):
                    _parse_services_response(soap(malformed))

    def test_selected_event_service_supplies_embedded_capabilities(self):
        parsed = _parse_services_response(
            soap(
                f"""
                <tds:GetServicesResponse>
                  <tds:Service><tds:Namespace>{EVENTS_NS}</tds:Namespace><tds:XAddr>http://camera/events-z</tds:XAddr>
                    <tds:Capabilities><tev:Capabilities WSPullPointSupport="false"/></tds:Capabilities>
                    <tds:Version><tt:Major>1</tt:Major><tt:Minor>0</tt:Minor></tds:Version></tds:Service>
                  <tds:Service><tds:Namespace>{EVENTS_NS}</tds:Namespace><tds:XAddr>http://camera/events-b</tds:XAddr>
                    <tds:Capabilities><tev:Capabilities WSPullPointSupport="true"/></tds:Capabilities>
                    <tds:Version><tt:Major>2</tt:Major><tt:Minor>0</tt:Minor></tds:Version></tds:Service>
                  <tds:Service><tds:Namespace>{EVENTS_NS}</tds:Namespace><tds:XAddr>http://camera/events-a</tds:XAddr>
                    <tds:Capabilities><tev:Capabilities WSPullPointSupport="false"/></tds:Capabilities>
                    <tds:Version><tt:Major>2</tt:Major><tt:Minor>0</tt:Minor></tds:Version></tds:Service>
                  <tds:Service><tds:Namespace>{EVENTS_NS}</tds:Namespace><tds:XAddr>http://camera/events-a</tds:XAddr>
                    <tds:Capabilities><tev:Capabilities WSPullPointSupport="true"/></tds:Capabilities>
                    <tds:Version><tt:Major>2</tt:Major><tt:Minor>0</tt:Minor></tds:Version></tds:Service>
                </tds:GetServicesResponse>
                """
            )
        )

        self.assertEqual(
            parsed.event_service_capabilities,
            EventServiceCapabilities(ws_pull_point_support=False),
        )

    def test_legacy_event_capabilities_follow_the_selected_xaddr(self):
        parsed = _parse_capabilities_response(
            soap(
                "<tds:GetCapabilitiesResponse><tds:Capabilities>"
                "<tt:Events><tt:XAddr>http://camera/events-a</tt:XAddr>"
                "<tt:WSPullPointSupport>false</tt:WSPullPointSupport>"
                "</tt:Events><tt:Events><tt:XAddr>http://camera/events-b</tt:XAddr>"
                "<tt:WSPullPointSupport>true</tt:WSPullPointSupport>"
                "</tt:Events></tds:Capabilities></tds:GetCapabilitiesResponse>"
            )
        )

        self.assertEqual(
            _select_service(parsed.services, EVENTS_NS).xaddr,
            "http://camera/events-a",
        )
        self.assertEqual(
            parsed.event_service_capabilities,
            EventServiceCapabilities(ws_pull_point_support=False),
        )

    def test_maps_all_legacy_services_and_strict_legacy_event_values(self):
        parsed = _parse_capabilities_response(
            soap(
                f"""
                <tds:GetCapabilitiesResponse><tds:Capabilities>
                  <tt:Device><tt:XAddr>http://camera/device</tt:XAddr></tt:Device>
                  <tt:Media><tt:XAddr>http://camera/media</tt:XAddr></tt:Media>
                  <tt:PTZ><tt:XAddr>http://camera/ptz</tt:XAddr></tt:PTZ>
                  <tt:Events><tt:XAddr>http://camera/events</tt:XAddr>
                    <tt:WSSubscriptionPolicySupport>{NBSP}true{NBSP}</tt:WSSubscriptionPolicySupport>
                    <tt:WSPullPointSupport> \t1\r\n</tt:WSPullPointSupport>
                    <tt:WSPausableSubscriptionManagerInterfaceSupport>false</tt:WSPausableSubscriptionManagerInterfaceSupport>
                    <tt:PersistentNotificationStorage>true</tt:PersistentNotificationStorage>
                  </tt:Events>
                  <tt:Imaging><tt:XAddr>http://camera/imaging</tt:XAddr></tt:Imaging>
                  <tt:Analytics><tt:XAddr>http://camera/analytics</tt:XAddr></tt:Analytics>
                  <tt:Extension>
                    <tt:DeviceIO><tt:XAddr>http://camera/deviceio</tt:XAddr></tt:DeviceIO>
                    <tt:Recording><tt:XAddr>http://camera/recording</tt:XAddr></tt:Recording>
                    <tt:Search><tt:XAddr>http://camera/search</tt:XAddr></tt:Search>
                    <tt:Replay><tt:XAddr>http://camera/replay</tt:XAddr></tt:Replay>
                    <tt:Receiver><tt:XAddr>http://camera/receiver</tt:XAddr></tt:Receiver>
                    <tt:Display><tt:XAddr>http://camera/display</tt:XAddr></tt:Display>
                  </tt:Extension>
                  <vendor:Media><tt:XAddr>http://camera/decoy</tt:XAddr></vendor:Media>
                </tds:Capabilities></tds:GetCapabilitiesResponse>
                """
            )
        )

        self.assertEqual(
            {service.namespace: service.xaddr for service in parsed.services},
            {
                "http://www.onvif.org/ver20/analytics/wsdl": "http://camera/analytics",
                DEVICE_NS: "http://camera/device",
                "http://www.onvif.org/ver10/deviceIO/wsdl": "http://camera/deviceio",
                "http://www.onvif.org/ver10/display/wsdl": "http://camera/display",
                EVENTS_NS: "http://camera/events",
                "http://www.onvif.org/ver20/imaging/wsdl": "http://camera/imaging",
                MEDIA1_NS: "http://camera/media",
                PTZ_NS: "http://camera/ptz",
                "http://www.onvif.org/ver10/receiver/wsdl": "http://camera/receiver",
                "http://www.onvif.org/ver10/recording/wsdl": "http://camera/recording",
                "http://www.onvif.org/ver10/replay/wsdl": "http://camera/replay",
                "http://www.onvif.org/ver10/search/wsdl": "http://camera/search",
            },
        )
        self.assertEqual(
            parsed.event_service_capabilities,
            EventServiceCapabilities(
                ws_pull_point_support=True,
                ws_pausable_subscription_manager_interface_support=False,
                persistent_notification_storage=True,
            ),
        )

    def test_validates_soap_operations_faults_and_malformed_xml_without_payloads(self):
        cases = (
            ("<broken>", "invalid XML document"),
            (soap("<tds:GetScopesResponse/>"), "invalid GetServices response"),
            (soap("<tds:GetServicesResponse/>"), "no services"),
        )
        for payload, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(_OnvifResponseError, message) as caught:
                    _parse_services_response(payload)
                self.assertNotIn(payload, str(caught.exception))

        action_fault = soap(
            """
            <s:Fault><s:Code><s:Value>s:Sender</s:Value><s:Subcode>
              <s:Value xmlns:ter="http://www.onvif.org/ver10/error">ter:ActionNotSupported</s:Value>
            </s:Subcode></s:Code><s:Reason><s:Text>detail marker</s:Text></s:Reason></s:Fault>
            """
        )
        with self.assertRaisesRegex(_OnvifResponseError, "SOAP Fault: ActionNotSupported") as caught:
            _parse_services_response(action_fault)
        self.assertEqual(caught.exception.kind, "fault")
        self.assertEqual(caught.exception.fault_code, "ActionNotSupported")
        self.assertNotIn("detail marker", str(caught.exception))

        auth_fault = soap11(
            """
            <env:Fault><faultcode>env:Client</faultcode>
              <faultstring>Request rejected: notauthorized; detail marker</faultstring>
            </env:Fault>
            """
        )
        with self.assertRaisesRegex(_OnvifResponseError, "SOAP Fault: NotAuthorized") as caught:
            _parse_services_response(auth_fault)
        self.assertEqual(caught.exception.fault_code, "NotAuthorized")
        self.assertNotIn("detail marker", str(caught.exception))

        for code in ("NotAuthorized2", "foo.NotAuthorized"):
            with self.subTest(code=code):
                non_auth_fault = soap(
                    "<s:Fault><s:Code><s:Value>s:Sender</s:Value><s:Subcode>"
                    f"<s:Value>{code}</s:Value></s:Subcode></s:Code></s:Fault>"
                )
                with self.assertRaises(_OnvifResponseError) as non_auth:
                    _parse_services_response(non_auth_fault)
                self.assertEqual(non_auth.exception.fault_code, code)

    def test_parses_media1_and_media2_profiles_with_namespace_association(self):
        media1 = _parse_media1_profiles_response(
            soap(
                """
                <trt:GetProfilesResponse>
                  <trt:Profiles token="main&amp;special"><tt:Name>Main</tt:Name>
                    <tt:AudioSourceConfiguration/><tt:AudioEncoderConfiguration/><tt:AudioOutputConfiguration/>
                    <tt:PTZConfiguration token="ptz-one"><tt:NodeToken>node-one</tt:NodeToken></tt:PTZConfiguration>
                  </trt:Profiles>
                  <trt:Profiles token="vendor-only"><vendor:AudioEncoderConfiguration/>
                    <vendor:PTZConfiguration token="decoy"/></trt:Profiles>
                  <trt:Wrapper><trt:Profiles token="nested"/></trt:Wrapper>
                </trt:GetProfilesResponse>
                """
            )
        )
        self.assertEqual(
            media1,
            (
                CameraCapabilityProfile(
                    token="main&special",
                    source="media1",
                    has_audio_encoder=True,
                    has_audio_output=True,
                    has_audio_source=True,
                    name="Main",
                    ptz_configuration_token="ptz-one",
                    ptz_node_token="node-one",
                ),
                CameraCapabilityProfile(
                    token="vendor-only",
                    source="media1",
                    has_audio_encoder=False,
                    has_audio_output=False,
                    has_audio_source=False,
                ),
            ),
        )

        media2 = _parse_media2_profiles_response(
            soap(
                """
                <tr2:GetProfilesResponse>
                  <tr2:Profiles token="main"><tr2:Name>Media2 Main</tr2:Name><tr2:Configurations>
                    <tr2:AudioSource/><tr2:AudioEncoder/><tr2:AudioOutput/>
                    <tr2:PTZ token="ptz-two"><tt:NodeToken>node-two</tt:NodeToken></tr2:PTZ>
                  </tr2:Configurations></tr2:Profiles>
                  <tr2:Profiles token="vendor-only"><tr2:Configurations>
                    <vendor:AudioEncoder/><vendor:PTZ token="decoy"/>
                  </tr2:Configurations></tr2:Profiles>
                </tr2:GetProfilesResponse>
                """
            )
        )
        self.assertEqual(
            media2,
            (
                CameraCapabilityProfile(
                    token="main",
                    source="media2",
                    has_audio_encoder=True,
                    has_audio_output=True,
                    has_audio_source=True,
                    name="Media2 Main",
                    ptz_configuration_token="ptz-two",
                    ptz_node_token="node-two",
                ),
                CameraCapabilityProfile(
                    token="vendor-only",
                    source="media2",
                    has_audio_encoder=False,
                    has_audio_output=False,
                    has_audio_source=False,
                ),
            ),
        )

        with self.assertRaisesRegex(_OnvifResponseError, "invalid Media1 GetProfiles"):
            _parse_media1_profiles_response(
                soap("<trt:GetProfilesResponse><trt:Profiles/></trt:GetProfilesResponse>")
            )

    def test_profile_reference_tokens_are_preserved_and_results_are_sorted(self):
        profiles = _parse_media1_profiles_response(
            soap(
                "<trt:GetProfilesResponse>"
                '<trt:Profiles token="z-token"/>'
                '<trt:Profiles token=" a-token "><tt:PTZConfiguration token=" ptz-token "/>'
                "</trt:Profiles></trt:GetProfilesResponse>"
            )
        )

        self.assertEqual(
            tuple(
                (profile.token, profile.ptz_configuration_token)
                for profile in profiles
            ),
            ((" a-token ", " ptz-token "), ("z-token", None)),
        )

    def test_strict_ptz_capabilities_and_nodes_keep_zoom_distinct(self):
        capabilities = _parse_ptz_service_capabilities_response(
            soap(
                f"""
                <tptz:GetServiceCapabilitiesResponse><tptz:Capabilities
                  EFlip="true" Reverse="false" GetCompatibleConfigurations="1"
                  MoveStatus="0" StatusPosition="TRUE" vendor:EFlip="true"/>
                </tptz:GetServiceCapabilitiesResponse>
                """
            )
        )
        self.assertEqual(
            capabilities,
            PtzServiceCapabilities(
                e_flip=True,
                reverse=False,
                get_compatible_configurations=True,
                move_status=False,
            ),
        )
        whitespace = _parse_ptz_service_capabilities_response(
            soap(
                f'<tptz:GetServiceCapabilitiesResponse><tptz:Capabilities '
                f'EFlip="{NBSP}true{NBSP}" Reverse=" \tfalse\r\n"/>'
                "</tptz:GetServiceCapabilitiesResponse>"
            )
        )
        self.assertEqual(whitespace, PtzServiceCapabilities(reverse=False))

        parsed = _parse_ptz_nodes_response(
            soap(
                f"""
                <tptz:GetNodesResponse>
                  <tptz:PTZNode token="pan"><tt:Name>Pan</tt:Name><tt:SupportedPTZSpaces>
                    <tt:AbsolutePanTiltPositionSpace/><tt:ContinuousPanTiltVelocitySpace/>
                  </tt:SupportedPTZSpaces><tt:MaximumNumberOfPresets>+8</tt:MaximumNumberOfPresets>
                    <tt:HomeSupported>true</tt:HomeSupported><tt:AuxiliaryCommands>LightOn</tt:AuxiliaryCommands>
                    <tt:AuxiliaryCommands>LightOff</tt:AuxiliaryCommands><tt:AuxiliaryCommands>LightOn</tt:AuxiliaryCommands>
                  </tptz:PTZNode>
                  <tptz:PTZNode token="zoom"><tt:SupportedPTZSpaces>
                    <tt:RelativeZoomTranslationSpace/><tt:ContinuousZoomVelocitySpace/>
                  </tt:SupportedPTZSpaces><tt:MaximumNumberOfPresets>2147483648</tt:MaximumNumberOfPresets>
                    <tt:HomeSupported>0</tt:HomeSupported></tptz:PTZNode>
                  <tptz:PTZNode token="nbsp"><tt:SupportedPTZSpaces/>
                    <tt:MaximumNumberOfPresets>{NBSP}+1{NBSP}</tt:MaximumNumberOfPresets>
                    <tt:HomeSupported>{NBSP}true{NBSP}</tt:HomeSupported></tptz:PTZNode>
                </tptz:GetNodesResponse>
                """
            )
        )
        self.assertTrue(parsed.pan_tilt_supported)
        self.assertTrue(parsed.zoom_supported)
        self.assertEqual(
            parsed.nodes[0],
            PtzNode(
                token="nbsp",
                spaces=PtzSpaces(),
            ),
        )
        self.assertEqual(parsed.nodes[1].maximum_presets, 8)
        self.assertEqual(parsed.nodes[1].auxiliary_commands, ("LightOff", "LightOn"))
        self.assertEqual(parsed.nodes[2].home_supported, False)
        self.assertIsNone(parsed.nodes[2].maximum_presets)

        zoom_only = _parse_ptz_nodes_response(
            soap(
                """
                <tptz:GetNodesResponse><tptz:PTZNode token="zoom-only"><tt:SupportedPTZSpaces>
                  <tt:AbsoluteZoomPositionSpace/>
                </tt:SupportedPTZSpaces></tptz:PTZNode></tptz:GetNodesResponse>
                """
            )
        )
        self.assertFalse(zoom_only.pan_tilt_supported)
        self.assertTrue(zoom_only.zoom_supported)

        for malformed in (
            "<tptz:GetNodesResponse><tptz:PTZNode><tt:SupportedPTZSpaces/></tptz:PTZNode></tptz:GetNodesResponse>",
            '<tptz:GetNodesResponse><tptz:PTZNode token="missing"/></tptz:GetNodesResponse>',
        ):
            with self.subTest(malformed=malformed):
                with self.assertRaisesRegex(_OnvifResponseError, "invalid PTZ GetNodes"):
                    _parse_ptz_nodes_response(soap(malformed))

    def test_modern_event_capabilities_are_strict_and_override_legacy_values(self):
        modern = _parse_event_service_capabilities_response(
            soap(
                f"""
                <tev:GetServiceCapabilitiesResponse><tev:Capabilities
                  WSSubscriptionPolicySupport="false" WSPullPointSupport="{NBSP}true{NBSP}"
                  WSPausableSubscriptionManagerInterfaceSupport="1"
                  PersistentNotificationStorage="true" MaxNotificationProducers="+12"
                  MaxPullPoints="-1" MaxEventBrokers="2147483648"
                  EventBrokerProtocols="mqtt mqtts mqtt" vendor:MaxPullPoints="99"/>
                </tev:GetServiceCapabilitiesResponse>
                """
            )
        )
        self.assertEqual(
            modern,
            EventServiceCapabilities(
                ws_subscription_policy_support=False,
                ws_pausable_subscription_manager_interface_support=True,
                persistent_notification_storage=True,
                max_notification_producers=12,
                event_broker_protocols=("mqtt", "mqtts"),
            ),
        )
        self.assertEqual(
            _merge_event_service_capabilities(
                EventServiceCapabilities(
                    ws_subscription_policy_support=True,
                    ws_pull_point_support=True,
                ),
                modern,
            ),
            EventServiceCapabilities(
                ws_subscription_policy_support=False,
                ws_pull_point_support=True,
                ws_pausable_subscription_manager_interface_support=True,
                persistent_notification_storage=True,
                max_notification_producers=12,
                event_broker_protocols=("mqtt", "mqtts"),
            ),
        )

    def test_event_topics_require_direct_topicset_and_namespaced_markers(self):
        topics = _parse_event_properties_response(
            soap(
                """
                <tev:GetEventPropertiesResponse>
                  <vendor:Wrapper><wstop:TopicSet><vendor:Decoy wstop:topic="true"/></wstop:TopicSet></vendor:Wrapper>
                  <wstop:TopicSet>
                    <tns:Device wstop:topic="true"><tns:Trigger><vendor:Motion wstop:topic="1">
                      <tt:MessageDescription><tt:Source><tt:SimpleItemDescription Name="Token"/></tt:Source>
                        <tt:Data><tt:SimpleItemDescription Name="State"/></tt:Data></tt:MessageDescription>
                    </vendor:Motion></tns:Trigger></tns:Device>
                    <vendor:Deep><vendor:Branch><vendor:Leaf wstop:topic="true"/>
                      <vendor:Leaf wstop:topic="true"/></vendor:Branch></vendor:Deep>
                    <Same wstop:topic="true"/><tns:Same wstop:topic="true"/>
                    <tns:Ignored topic="true"/>
                  </wstop:TopicSet>
                </tev:GetEventPropertiesResponse>
                """
            )
        )
        self.assertEqual(
            tuple((topic.namespace, topic.path) for topic in topics),
            (
                ("urn:vendor", "Deep/Branch/Leaf"),
                (TOPICS_NS, "Device"),
                ("urn:vendor", "Device/Trigger/Motion"),
                (None, "Same"),
                (TOPICS_NS, "Same"),
            ),
        )

        with self.assertRaisesRegex(_OnvifResponseError, "invalid Events GetEventProperties"):
            _parse_event_properties_response(
                soap(
                    """
                    <tev:GetEventPropertiesResponse><vendor:Wrapper><wstop:TopicSet>
                      <vendor:Decoy wstop:topic="true"/>
                    </wstop:TopicSet></vendor:Wrapper></tev:GetEventPropertiesResponse>
                    """
                )
            )

    def test_event_topic_walk_is_iterative_at_large_depth(self):
        depth = 5000
        topics = _parse_event_properties_response(
            soap(
                "<tev:GetEventPropertiesResponse><wstop:TopicSet>"
                + "<tns:L>" * depth
                + '<vendor:Leaf wstop:topic="true"/>'
                + "</tns:L>" * depth
                + "</wstop:TopicSet></tev:GetEventPropertiesResponse>"
            )
        )

        self.assertEqual(len(topics), 1)
        self.assertEqual(topics[0].namespace, "urn:vendor")
        self.assertEqual(len(topics[0].path.split("/")), depth + 1)

    def test_media2_options_are_standard_namespace_aware_sorted_and_deduplicated(self):
        encodings = _parse_media2_options_response(
            soap(
                """
                <tr2:GetVideoEncoderConfigurationOptionsResponse>
                  <tr2:Options><tt:Encoding>H264</tt:Encoding></tr2:Options>
                  <tr2:Options Encoding="h265"><tt:Encoding>H264</tt:Encoding></tr2:Options>
                  <tr2:Options Encoding="VP9"><tt:Encoding>h265</tt:Encoding></tr2:Options>
                  <tr2:Options vendor:Encoding="H266"><vendor:Encoding>H267</vendor:Encoding></tr2:Options>
                </tr2:GetVideoEncoderConfigurationOptionsResponse>
                """
            )
        )
        self.assertEqual(encodings, ("H264", "H265", "VP9"))

        with self.assertRaisesRegex(
            _OnvifResponseError,
            "invalid Media2 GetVideoEncoderConfigurationOptions",
        ):
            _parse_media2_options_response(
                soap(
                    """
                    <tr2:GetVideoEncoderConfigurationOptionsResponse>
                      <tr2:Options><vendor:Encoding>H267</vendor:Encoding></tr2:Options>
                    </tr2:GetVideoEncoderConfigurationOptionsResponse>
                    """
                )
            )


if __name__ == "__main__":
    unittest.main()
