import unittest
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


if __name__ == "__main__":
    unittest.main()
