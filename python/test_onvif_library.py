import http.server
import io
import os
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request
from email.message import Message
from unittest.mock import patch


FIRST_RESPONSE = b"""<?xml version="1.0"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
 xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">
 <s:Body><d:ProbeMatches><d:ProbeMatch>
  <a:EndpointReference><a:Address>urn:uuid:camera-1</a:Address></a:EndpointReference>
  <d:Types>dn:NetworkVideoTransmitter</d:Types>
  <d:Scopes>onvif://www.onvif.org/name/Front%20Door onvif://www.onvif.org/hardware/SM-DM-4M2W</d:Scopes>
  <d:XAddrs>http://10.128.10.141/onvif/device_service http://camera.local/onvif/device_service</d:XAddrs>
 </d:ProbeMatch></d:ProbeMatches></s:Body>
</s:Envelope>"""

SECOND_RESPONSE = b"""<?xml version="1.0"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
 xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:wsd="http://schemas.xmlsoap.org/ws/2005/04/discovery">
 <e:Body><wsd:ProbeMatches><wsd:ProbeMatch>
  <w:EndpointReference><w:Address>urn:uuid:camera-1</w:Address></w:EndpointReference>
  <wsd:Types>tds:NetworkVideoTransmitter</wsd:Types>
  <wsd:Scopes>onvif://www.onvif.org/name/Front%20Door onvif://www.onvif.org/location/Entrance</wsd:Scopes>
  <wsd:XAddrs>http://10.128.10.141:8000/onvif/device_service</wsd:XAddrs>
 </wsd:ProbeMatch></wsd:ProbeMatches></e:Body>
</e:Envelope>"""

SYSTEM_TIME_RESPONSE = (
    '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
    "<s:Body><GetSystemDateAndTimeResponse>"
    "<SystemDateAndTime><UTCDateTime>"
    "<Time><Hour>6</Hour><Minute>30</Minute><Second>0</Second></Time>"
    "<Date><Year>2026</Year><Month>7</Month><Day>20</Day></Date>"
    "</UTCDateTime></SystemDateAndTime>"
    "</GetSystemDateAndTimeResponse></s:Body></s:Envelope>"
).encode("utf-8")


class FakeSoapResponse:
    def __init__(self, body: bytes, *, content_lengths=()):
        self.body = io.BytesIO(body)
        self.headers = Message()
        for content_length in content_lengths:
            self.headers.add_header("Content-Length", content_length)
        self.read_sizes = []
        self.closed = False

    def read(self, size=-1):
        self.read_sizes.append(size)
        return self.body.read(size)

    def close(self):
        self.closed = True
        self.body.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


class OnvifLibraryTests(unittest.TestCase):
    def test_parses_namespace_independent_probe_match_metadata(self):
        from rtsp_backchannel.onvif import parse_probe_matches

        devices = parse_probe_matches(FIRST_RESPONSE, "10.128.10.141")

        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0].ip, "10.128.10.141")
        self.assertEqual(
            devices[0].xaddrs,
            [
                "http://10.128.10.141/onvif/device_service",
                "http://camera.local/onvif/device_service",
            ],
        )
        self.assertEqual(
            devices[0].scopes,
            [
                "onvif://www.onvif.org/name/Front%20Door",
                "onvif://www.onvif.org/hardware/SM-DM-4M2W",
            ],
        )
        self.assertEqual(devices[0].name, "Front Door")
        self.assertEqual(devices[0].hardware, "SM-DM-4M2W")
        self.assertEqual(devices[0].endpoint_reference, "urn:uuid:camera-1")

    def test_probes_interfaces_to_one_deadline_and_merges_duplicates(self):
        from rtsp_backchannel import onvif

        calls = []

        def probe(source, payload, deadline, on_message):
            calls.append((source, deadline, payload))
            response = FIRST_RESPONSE if source.endswith(".10") else SECOND_RESPONSE
            on_message(response, "10.128.10.141")

        with (
            patch.object(onvif.time, "monotonic", return_value=100.0),
            patch.object(onvif, "_probe_interface", side_effect=probe),
        ):
            devices = onvif.discover_devices(
                timeout=3.0,
                interfaces=["10.0.0.10", "192.168.0.20"],
            )

        self.assertEqual(
            sorted(call[0] for call in calls),
            ["10.0.0.10", "192.168.0.20"],
        )
        self.assertEqual([call[1] for call in calls], [103.0, 103.0])
        self.assertTrue(
            all(b"NetworkVideoTransmitter" in call[2] for call in calls)
        )
        self.assertEqual(len(devices), 1)
        self.assertEqual(
            set(devices[0].xaddrs),
            {
                "http://10.128.10.141/onvif/device_service",
                "http://camera.local/onvif/device_service",
                "http://10.128.10.141:8000/onvif/device_service",
            },
        )
        self.assertIn(
            "onvif://www.onvif.org/location/Entrance", devices[0].scopes
        )

    def test_expands_every_selected_ip_and_cidr_and_removes_overlaps(self):
        from rtsp_backchannel import onvif

        self.assertEqual(
            onvif._cidr_hosts(
                ["10.0.0.0/30", "10.128.0.10", "10.0.0.1"]
            ),
            ["10.0.0.1", "10.0.0.2", "10.128.0.10"],
        )

    def test_actively_discovers_a_selected_onvif_device_service(self):
        from rtsp_backchannel import onvif

        requests = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                requests.append(self.rfile.read(length).decode("utf-8"))
                body = (
                    '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
                    "<s:Body><GetSystemDateAndTimeResponse>"
                    "<SystemDateAndTime><UTCDateTime>"
                    "<Time><Hour>6</Hour><Minute>30</Minute><Second>0</Second></Time>"
                    "<Date><Year>2026</Year><Month>7</Month><Day>20</Day></Date>"
                    "</UTCDateTime></SystemDateAndTime>"
                    "</GetSystemDateAndTimeResponse></s:Body></s:Envelope>"
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/soap+xml")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format, *args):
                return None

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            devices = onvif.discover_devices(
                cidrs=["127.0.0.1", "127.0.0.1/32"],
                ports=[server.server_port],
                timeout=0.25,
                concurrency=1,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

        self.assertEqual(
            devices,
            [
                onvif.DiscoveredDevice(
                    ip="127.0.0.1",
                    xaddrs=[
                        f"http://127.0.0.1:{server.server_port}"
                        "/onvif/device_service"
                    ],
                    scopes=[],
                ),
            ],
        )
        self.assertEqual(len(requests), 1)
        self.assertIn("GetSystemDateAndTime", requests[0])

    def test_rejects_invalid_explicit_ipv4_cidr_before_probing(self):
        from rtsp_backchannel import onvif

        with self.assertRaisesRegex(ValueError, "invalid IPv4 CIDR"):
            onvif.discover_devices(
                cidrs=["10.128.10.0/not-a-prefix"], timeout=0.001
            )

    def test_cidr_discovery_ignores_environment_http_proxies(self):
        from rtsp_backchannel import onvif

        direct_requests = []
        proxy_requests = []

        class DirectHandler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                direct_requests.append(self.path)
                self.send_response(200)
                self.send_header("Content-Length", str(len(SYSTEM_TIME_RESPONSE)))
                self.end_headers()
                self.wfile.write(SYSTEM_TIME_RESPONSE)

            def log_message(self, _format, *args):
                return None

        class ProxyHandler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                proxy_requests.append(self.path)
                self.send_response(200)
                self.send_header("Content-Length", str(len(SYSTEM_TIME_RESPONSE)))
                self.end_headers()
                self.wfile.write(SYSTEM_TIME_RESPONSE)

            def log_message(self, _format, *args):
                return None

        direct = http.server.ThreadingHTTPServer(("0.0.0.0", 0), DirectHandler)
        proxy = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
        threads = [
            threading.Thread(target=direct.serve_forever, daemon=True),
            threading.Thread(target=proxy.serve_forever, daemon=True),
        ]
        for thread in threads:
            thread.start()
        proxy_url = f"http://127.0.0.1:{proxy.server_port}"
        try:
            with patch.dict(
                os.environ,
                {
                    "http_proxy": proxy_url,
                    "HTTP_PROXY": proxy_url,
                    "no_proxy": "",
                    "NO_PROXY": "",
                },
            ), patch.object(urllib.request, "_opener", None), patch.object(
                urllib.request, "proxy_bypass", return_value=False
            ):
                devices = onvif.discover_devices(
                    cidrs=["127.0.0.1"],
                    ports=[direct.server_port],
                    timeout=0.5,
                    concurrency=1,
                )
        finally:
            direct.shutdown()
            proxy.shutdown()
            direct.server_close()
            proxy.server_close()
            for thread in threads:
                thread.join()

        self.assertEqual(len(devices), 1)
        self.assertEqual(direct_requests, ["/onvif/device_service"])
        self.assertEqual(proxy_requests, [])

    def test_cidr_discovery_does_not_follow_http_redirects(self):
        from rtsp_backchannel import onvif

        requests = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append(("POST", self.path))
                self.send_response(302)
                self.send_header(
                    "Location",
                    f"http://127.0.0.1:{self.server.server_port}/redirected",
                )
                self.end_headers()

            def do_GET(self):
                requests.append(("GET", self.path))
                self.send_response(200)
                self.send_header("Content-Length", str(len(SYSTEM_TIME_RESPONSE)))
                self.end_headers()
                self.wfile.write(SYSTEM_TIME_RESPONSE)

            def log_message(self, _format, *args):
                return None

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(urllib.request, "_opener", None):
                devices = onvif.discover_devices(
                    cidrs=["127.0.0.1"],
                    ports=[server.server_port],
                    timeout=0.5,
                    concurrency=1,
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

        self.assertEqual(devices, [])
        self.assertEqual(requests, [("POST", "/onvif/device_service")])

    def test_cidr_discovery_uses_one_absolute_deadline_for_every_host(self):
        from rtsp_backchannel import onvif

        calls = []

        def probe(ip, ports, deadline):
            calls.append((ip, tuple(ports), deadline))
            return None

        with patch.object(onvif.time, "monotonic", return_value=100.0), patch.object(
            onvif, "_probe_cidr_host", side_effect=probe
        ):
            devices = onvif.discover_devices(
                cidrs=["10.0.0.0/30"],
                ports=[80, 8000],
                timeout=2.0,
                concurrency=1,
            )

        self.assertEqual(devices, [])
        self.assertEqual(
            calls,
            [
                ("10.0.0.1", (80, 8000), 102.0),
                ("10.0.0.2", (80, 8000), 102.0),
            ],
        )

    def test_cidr_discovery_rejects_oversized_valid_soap_response(self):
        from rtsp_backchannel import onvif

        padding = b" " * (1024 * 1024)
        body = SYSTEM_TIME_RESPONSE.replace(b"</s:Body>", padding + b"</s:Body>")

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, _format, *args):
                return None

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            devices = onvif.discover_devices(
                cidrs=["127.0.0.1"],
                ports=[server.server_port],
                timeout=0.5,
                concurrency=1,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

        self.assertEqual(devices, [])

    def test_parses_profile_names_and_audio_capabilities(self):
        from rtsp_backchannel.onvif import parse_profiles

        profiles = parse_profiles(
            """
            <trt:GetProfilesResponse xmlns:trt="urn:media" xmlns:tt="urn:schema">
              <trt:Profiles token="main">
                <tt:Name>Main &amp; Stream</tt:Name>
                <tt:AudioEncoderConfiguration />
                <tt:AudioOutputConfiguration />
              </trt:Profiles>
            </trt:GetProfilesResponse>
            """
        )

        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0].token, "main")
        self.assertEqual(profiles[0].name, "Main & Stream")
        self.assertTrue(profiles[0].has_audio_encoder)
        self.assertTrue(profiles[0].has_audio_output)
        self.assertFalse(profiles[0].has_audio_source)

    def test_returns_all_stream_uris_unchanged_with_transport_only_credentials(self):
        from rtsp_backchannel import onvif
        from rtsp_backchannel.onvif import OnvifProfile

        created = []

        class FakeDevice:
            def __init__(
                self, host, user, password, *, device_urls=None, timeout=8.0
            ):
                created.append(
                    (host, user, password, device_urls, timeout)
                )

            def connect(self):
                return None

            def get_profiles(self):
                return [
                    OnvifProfile("main", "Main Stream", True, False, True),
                    OnvifProfile("sub", "Sub Stream", False, False, False),
                ]

            def get_stream_uri(self, token):
                return {
                    "main": "rtsp://camera/live?channel=1&stream=main",
                    "sub": "rtsp://camera/live?channel=1&stream=sub",
                }[token]

        with patch.object(onvif, "OnvifDevice", FakeDevice):
            streams = onvif.get_stream_uris(
                host="camera",
                user="admin@example.com",
                password="p@ss:/?#[]",
                device_urls=["http://camera/onvif/device_service"],
                timeout=1.5,
            )

        self.assertEqual(
            created,
            [
                (
                    "camera",
                    "admin@example.com",
                    "p@ss:/?#[]",
                    ["http://camera/onvif/device_service"],
                    1.5,
                )
            ],
        )
        self.assertEqual(streams[0].profile_token, "main")
        self.assertEqual(streams[0].profile_name, "Main Stream")
        self.assertEqual(
            streams[0].uri,
            "rtsp://camera/live?channel=1&stream=main",
        )
        self.assertEqual(streams[1].profile_token, "sub")
        self.assertNotIn("p@ss", streams[0].uri)

    def test_stream_lookup_credentials_default_to_empty(self):
        from rtsp_backchannel import onvif

        created = []

        class FakeDevice:
            def __init__(
                self, host, user, password, *, device_urls=None, timeout=8.0
            ):
                created.append((host, user, password, device_urls, timeout))

            def connect(self):
                return None

            def get_profiles(self):
                return []

        with patch.object(onvif, "OnvifDevice", FakeDevice):
            self.assertEqual(onvif.get_stream_uris(host="camera"), [])

        self.assertEqual(created, [("camera", "", "", None, 8.0)])

    def test_stream_lookup_removes_uri_userinfo_and_fragment(self):
        from rtsp_backchannel import onvif

        class FakeDevice:
            def __init__(self, *args, **kwargs):
                return None

            def connect(self):
                return None

            def get_profiles(self):
                return [onvif.OnvifProfile("main", "Main", True, False, False)]

            def get_stream_uri(self, _profile_token):
                return "rtsp://operator%40site:p@ss@word@camera/live?x=%40#secret"

        with patch.object(onvif, "OnvifDevice", FakeDevice):
            streams = onvif.get_stream_uris(host="camera")

        self.assertEqual(streams[0].uri, "rtsp://camera/live?x=%40")
        self.assertNotIn("p@ss@word", streams[0].uri)

    def test_soap_request_bounds_declared_and_streamed_success_bodies(self):
        from rtsp_backchannel import onvif

        cases = (
            FakeSoapResponse(b"", content_lengths=("5",)),
            FakeSoapResponse(b"12345"),
        )
        for response in cases:
            with self.subTest(declared=bool(response.headers)), patch.object(
                onvif, "_MAX_SOAP_RESPONSE_BYTES", 4
            ), patch.object(
                onvif.urllib.request, "urlopen", return_value=response
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "ONVIF SOAP response is too large"
                ):
                    onvif._soap_request(
                        "http://camera/onvif/device_service",
                        "<Request/>",
                        "",
                        1.0,
                    )

            self.assertTrue(response.closed)

    def test_soap_request_uses_one_absolute_deadline_for_open_and_read(self):
        from rtsp_backchannel import onvif

        now = [100.0]

        class AdvancingResponse(FakeSoapResponse):
            def read(self, size=-1):
                now[0] = 101.01
                return super().read(size)

        response = AdvancingResponse(b"<Response/>")

        def open_response(*args, **kwargs):
            now[0] = 100.75
            return response

        with patch.object(
            onvif.time, "monotonic", side_effect=lambda: now[0]
        ), patch.object(
            onvif.urllib.request, "urlopen", side_effect=open_response
        ) as urlopen, patch.object(
            onvif, "_set_response_timeout"
        ) as set_timeout:
            with self.assertRaisesRegex(
                TimeoutError, "ONVIF SOAP deadline exceeded"
            ):
                onvif._soap_request(
                    "http://camera/onvif/device_service",
                    "<Request/>",
                    "",
                    1.0,
                )

        self.assertEqual(urlopen.call_args.kwargs["timeout"], 1.0)
        self.assertAlmostEqual(set_timeout.call_args.args[1], 0.25)
        self.assertTrue(response.closed)

    def test_soap_reader_prefers_one_underlying_read_when_available(self):
        from rtsp_backchannel import onvif

        class ReadOneResponse(FakeSoapResponse):
            def __init__(self, body):
                super().__init__(body)
                self.read1_sizes = []

            def read1(self, size=-1):
                self.read1_sizes.append(size)
                return self.body.read(min(size, 2))

        response = ReadOneResponse(b"<Response/>")
        with patch.object(
            onvif.urllib.request, "urlopen", return_value=response
        ):
            result = onvif._soap_request(
                "http://camera/onvif/device_service",
                "<Request/>",
                "",
                1.0,
            )

        self.assertEqual(result, "<Response/>")
        self.assertGreater(len(response.read1_sizes), 1)
        self.assertEqual(response.read_sizes, [])
        self.assertTrue(response.closed)

    def test_soap_request_deadline_stops_real_trickle_body(self):
        from rtsp_backchannel import onvif

        payload = b"<Response/>!"

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                self.send_response(200)
                self.send_header("Content-Type", "application/soap+xml")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                for index, value in enumerate(payload):
                    try:
                        self.wfile.write(bytes([value]))
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        break
                    if index + 1 < len(payload):
                        time.sleep(0.06)

            def log_message(self, _format, *args):
                return None

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(
                TimeoutError, "ONVIF SOAP deadline exceeded"
            ):
                onvif._soap_request(
                    f"http://127.0.0.1:{server.server_port}/onvif",
                    "<Request/>",
                    "",
                    0.10,
                )
        finally:
            elapsed = time.monotonic() - started
            server.shutdown()
            server.server_close()
            thread.join()

        self.assertGreaterEqual(elapsed, 0.07)
        self.assertLess(elapsed, 0.35)

    def test_soap_request_normalizes_wrapped_network_timeout(self):
        from rtsp_backchannel import onvif

        error = urllib.error.URLError(socket.timeout("timed out"))
        with patch.object(
            onvif.urllib.request, "urlopen", side_effect=error
        ):
            with self.assertRaisesRegex(
                TimeoutError, "ONVIF SOAP deadline exceeded"
            ):
                onvif._soap_request(
                    "http://camera/onvif/device_service",
                    "<Request/>",
                    "",
                    1.0,
                )

    def test_soap_request_bounds_and_closes_http_error_bodies(self):
        from rtsp_backchannel import onvif

        body = io.BytesIO(b"12345")
        error = urllib.error.HTTPError(
            "http://camera/onvif/device_service",
            500,
            "Internal Server Error",
            Message(),
            body,
        )

        with patch.object(
            onvif, "_MAX_SOAP_RESPONSE_BYTES", 4
        ), patch.object(
            onvif.urllib.request, "urlopen", side_effect=error
        ):
            with self.assertRaisesRegex(
                RuntimeError, "ONVIF SOAP response is too large"
            ):
                onvif._soap_request(
                    "http://camera/onvif/device_service",
                    "<Request/>",
                    "",
                    1.0,
                )

        self.assertTrue(body.closed)

    def test_soap_request_returns_bounded_http_error_xml(self):
        from rtsp_backchannel import onvif

        body = io.BytesIO(b"<Fault>denied</Fault>")
        error = urllib.error.HTTPError(
            "http://camera/onvif/device_service",
            401,
            "Unauthorized",
            {},
            body,
        )

        with patch.object(
            onvif.urllib.request, "urlopen", side_effect=error
        ):
            result = onvif._soap_request(
                "http://camera/onvif/device_service",
                "<Request/>",
                "",
                1.0,
            )

        self.assertEqual(result, "<Fault>denied</Fault>")
        self.assertTrue(body.closed)

    def test_omits_ws_security_only_when_both_credentials_are_empty(self):
        from rtsp_backchannel import onvif

        with patch.object(onvif, "_soap_request", return_value="ok") as soap:
            device = onvif.OnvifDevice("camera", "", "")
            self.assertEqual(device._call("http://camera", "<Request/>") , "ok")

        self.assertEqual(soap.call_args.args[2], "")

        with patch.object(onvif, "_soap_request", return_value="ok") as soap:
            device = onvif.OnvifDevice("camera", "admin", "")
            device._call("http://camera", "<Request/>")

        header = soap.call_args.args[2]
        self.assertIn("PasswordDigest", header)
        self.assertIn("<wsse:Username>admin</wsse:Username>", header)


if __name__ == "__main__":
    unittest.main()
