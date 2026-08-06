use std::io::{ErrorKind, Read, Write};
use std::net::{Ipv4Addr, TcpListener};
use std::process::Command;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use rtsp_backchannel::cli::{Invocation, parse_invocation_from};
use rtsp_backchannel::discovery::{DiscoveryOptions, parse_probe_matches};
use rtsp_backchannel::onvif::{
    CameraCapabilityOptions, OnvifDevice, StreamUriOptions, get_camera_capabilities,
    get_stream_uris, parse_profiles,
};

const SOAP12_NS: &str = "http://www.w3.org/2003/05/soap-envelope";
const DEVICE_NS: &str = "http://www.onvif.org/ver10/device/wsdl";
const SCHEMA_NS: &str = "http://www.onvif.org/ver10/schema";
const MEDIA1_NS: &str = "http://www.onvif.org/ver10/media/wsdl";
const MEDIA2_NS: &str = "http://www.onvif.org/ver20/media/wsdl";
const PTZ_NS: &str = "http://www.onvif.org/ver20/ptz/wsdl";
const EVENTS_NS: &str = "http://www.onvif.org/ver10/events/wsdl";
const WSTOP_NS: &str = "http://docs.oasis-open.org/wsn/t-1";
const TOPICS_NS: &str = "http://www.onvif.org/ver10/topics";

const PROBE_RESPONSE: &str = r#"<?xml version="1.0"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
 xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">
 <s:Body><d:ProbeMatches><d:ProbeMatch>
  <a:EndpointReference><a:Address>urn:uuid:camera-1</a:Address></a:EndpointReference>
  <d:Types>dn:NetworkVideoTransmitter</d:Types>
  <d:Scopes>onvif://www.onvif.org/name/Front%20Door onvif://www.onvif.org/hardware/SM-DM-4M2W</d:Scopes>
  <d:XAddrs>http://10.128.10.141/onvif/device_service</d:XAddrs>
 </d:ProbeMatch></d:ProbeMatches></s:Body>
</s:Envelope>"#;

#[test]
fn parses_namespace_independent_probe_match_metadata() {
    let devices = parse_probe_matches(PROBE_RESPONSE, Ipv4Addr::new(10, 128, 10, 141)).unwrap();

    assert_eq!(devices.len(), 1);
    assert_eq!(devices[0].ip, Ipv4Addr::new(10, 128, 10, 141));
    assert_eq!(devices[0].name.as_deref(), Some("Front Door"));
    assert_eq!(devices[0].hardware.as_deref(), Some("SM-DM-4M2W"));
    assert_eq!(
        devices[0].endpoint_reference.as_deref(),
        Some("urn:uuid:camera-1")
    );
}

#[test]
fn exposes_discovery_options_with_a_three_second_default() {
    let options = DiscoveryOptions::default();

    assert_eq!(options.timeout, Duration::from_secs(3));
    assert!(options.interfaces.is_empty());
}

#[test]
fn parses_profile_names_and_audio_capabilities() {
    let profiles = parse_profiles(
        r#"<trt:GetProfilesResponse xmlns:trt="urn:media" xmlns:tt="urn:schema">
          <trt:Profiles token="main"><tt:Name>Main &amp; Stream</tt:Name>
          <tt:AudioEncoderConfiguration/><tt:AudioOutputConfiguration/></trt:Profiles>
        </trt:GetProfilesResponse>"#,
    )
    .unwrap();

    assert_eq!(profiles.len(), 1);
    assert_eq!(profiles[0].token, "main");
    assert_eq!(profiles[0].name.as_deref(), Some("Main & Stream"));
    assert!(profiles[0].has_audio_encoder);
    assert!(profiles[0].has_audio_output);
    assert!(!profiles[0].has_audio_source);
}

#[test]
fn returns_every_profile_uri_unchanged_and_keeps_credentials_transport_only() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let device_url = format!("http://127.0.0.1:{port}/onvif/device_service");
    let media_url = format!("http://127.0.0.1:{port}/onvif/media_service");
    let requests = Arc::new(Mutex::new(Vec::new()));
    let server_requests = Arc::clone(&requests);
    let server = thread::spawn(move || {
        let responses = [
            "<Envelope><UTCDateTime><Time><Hour>13</Hour><Minute>14</Minute><Second>15</Second></Time><Date><Year>2026</Year><Month>7</Month><Day>16</Day></Date></UTCDateTime></Envelope>".to_owned(),
            "<Envelope><GetDeviceInformationResponse/></Envelope>".to_owned(),
            format!("<Envelope><Capabilities><Media><XAddr>{media_url}</XAddr></Media></Capabilities></Envelope>"),
            "<Envelope><GetProfilesResponse><Profiles token=\"main\"><Name>Main Stream</Name></Profiles><Profiles token=\"sub\"><Name>Sub Stream</Name></Profiles></GetProfilesResponse></Envelope>".to_owned(),
            "<Envelope><GetStreamUriResponse><Uri>rtsp://stream-user:stream-pass@camera/live?channel=1&amp;stream=main#fragment</Uri></GetStreamUriResponse></Envelope>".to_owned(),
            "<Envelope><GetStreamUriResponse><Uri>rtsp://camera/live?channel=1&amp;stream=sub</Uri></GetStreamUriResponse></Envelope>".to_owned(),
        ];
        for response in responses {
            let (mut stream, _) = listener.accept().unwrap();
            server_requests
                .lock()
                .unwrap()
                .push(read_http_request(&mut stream));
            write!(
                stream,
                "HTTP/1.1 200 OK\r\nContent-Type: application/soap+xml\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                response.len(),
                response
            )
            .unwrap();
        }
    });

    let streams = get_stream_uris(&StreamUriOptions {
        host: "camera".to_owned(),
        user: "admin@example.com".to_owned(),
        password: "p@ss:/?#[]".to_owned(),
        device_urls: vec![device_url],
        timeout: Duration::from_millis(1_500),
    })
    .unwrap();
    server.join().unwrap();

    assert_eq!(streams.len(), 2);
    assert_eq!(streams[0].profile_token, "main");
    assert_eq!(streams[0].profile_name.as_deref(), Some("Main Stream"));
    assert_eq!(streams[0].uri, "rtsp://camera/live?channel=1&stream=main");
    assert!(!streams[0].uri.contains("stream-pass"));
    assert_eq!(streams[1].profile_token, "sub");
    assert!(!streams.iter().any(|stream| stream.uri.contains("p@ss")));
    assert!(
        requests
            .lock()
            .unwrap()
            .iter()
            .all(|request| !request.contains(">p@ss:/?#[]<"))
    );
}

#[test]
fn parses_discover_and_streams_without_breaking_direct_playback_flags() {
    match parse_invocation_from([
        "rtsp-backchannel",
        "discover",
        "--timeout-ms",
        "1500",
        "--interface",
        "10.0.0.10",
        "--interface",
        "192.168.0.20",
    ])
    .unwrap()
    {
        Invocation::Discover(cli) => {
            assert_eq!(cli.timeout_ms, 1500);
            assert_eq!(cli.interfaces.len(), 2);
        }
        _ => panic!("expected discovery invocation"),
    }

    match parse_invocation_from([
        "rtsp-backchannel",
        "streams",
        "--host",
        "camera",
        "--user",
        "admin",
        "--pass",
        "secret",
        "--device-url",
        "http://camera/onvif/device_service",
    ])
    .unwrap()
    {
        Invocation::Streams(cli) => {
            assert_eq!(cli.host, "camera");
            assert_eq!(cli.device_urls.len(), 1);
        }
        _ => panic!("expected streams invocation"),
    }

    assert!(matches!(
        parse_invocation_from([
            "rtsp-backchannel",
            "--host",
            "camera",
            "--pass",
            "secret",
            "--file",
            "event.mp3",
        ])
        .unwrap(),
        Invocation::Play(_)
    ));
}

#[test]
fn parses_explicit_cidr_discovery_options() {
    match parse_invocation_from([
        "rtsp-backchannel",
        "discover",
        "--timeout-ms",
        "1500",
        "--cidr",
        "10.128.0.10",
        "--cidr",
        "192.168.20.0/24",
        "--port",
        "80",
        "--port",
        "8000",
        "--concurrency",
        "16",
    ])
    .unwrap()
    {
        Invocation::Discover(cli) => {
            assert_eq!(cli.cidrs, ["10.128.0.10", "192.168.20.0/24"]);
            assert_eq!(cli.ports, [80, 8000]);
            assert_eq!(cli.concurrency, 16);
        }
        _ => panic!("expected discovery invocation"),
    }
}

#[test]
fn actively_discovers_an_onvif_device_in_an_explicit_cidr() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let port = listener.local_addr().unwrap().port();
    let requests = Arc::new(Mutex::new(Vec::new()));
    let server_requests = Arc::clone(&requests);
    let server = thread::spawn(move || {
        let deadline = Instant::now() + Duration::from_secs(2);
        loop {
            match listener.accept() {
                Ok((mut stream, _)) => {
                    server_requests
                        .lock()
                        .unwrap()
                        .push(read_http_request(&mut stream));
                    let response = concat!(
                        "<Envelope><GetSystemDateAndTimeResponse>",
                        "<UTCDateTime><Time><Hour>6</Hour><Minute>30</Minute>",
                        "<Second>0</Second></Time><Date><Year>2026</Year>",
                        "<Month>7</Month><Day>20</Day></Date></UTCDateTime>",
                        "</GetSystemDateAndTimeResponse></Envelope>"
                    );
                    write!(
                        stream,
                        "HTTP/1.1 200 OK\r\nContent-Type: application/soap+xml\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                        response.len(),
                        response
                    )
                    .unwrap();
                    return true;
                }
                Err(error) if error.kind() == ErrorKind::WouldBlock => {
                    if Instant::now() >= deadline {
                        return false;
                    }
                    thread::sleep(Duration::from_millis(10));
                }
                Err(error) => panic!("test ONVIF server failed: {error}"),
            }
        }
    });

    let output = Command::new(env!("CARGO_BIN_EXE_rtsp-backchannel"))
        .args([
            "discover",
            "--cidr",
            "127.0.0.1",
            "--cidr",
            "127.0.0.1/32",
            "--port",
            &port.to_string(),
            "--timeout-ms",
            "250",
            "--concurrency",
            "1",
        ])
        .output()
        .unwrap();
    let served = server.join().unwrap();

    assert!(served, "CIDR discovery did not probe the selected address");
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let devices = String::from_utf8(output.stdout)
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str::<serde_json::Value>(line).unwrap())
        .collect::<Vec<_>>();
    assert_eq!(devices[0]["ip"], "127.0.0.1");
    assert_eq!(
        devices[0]["xaddrs"][0],
        format!("http://127.0.0.1:{port}/onvif/device_service")
    );
    assert!(requests.lock().unwrap()[0].contains("GetSystemDateAndTime"));
}

#[test]
fn binary_help_exits_successfully_for_root_and_subcommands() {
    for arguments in [
        &["--help"][..],
        &["discover", "--help"],
        &["streams", "--help"],
    ] {
        let output = Command::new(env!("CARGO_BIN_EXE_rtsp-backchannel"))
            .args(arguments)
            .output()
            .unwrap();

        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        assert!(String::from_utf8_lossy(&output.stdout).contains("Usage:"));
    }
}

#[test]
fn uppercase_direct_rtsp_credentials_are_never_logged() {
    let output = Command::new(env!("CARGO_BIN_EXE_rtsp-backchannel"))
        .args([
            "--host",
            "RTSP://log-user:log-pass@127.0.0.1:0/live#secret",
            "--file",
            "missing.mp3",
        ])
        .output()
        .unwrap();
    let combined = format!(
        "{}{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );

    assert!(!output.status.success());
    assert!(!combined.contains("log-user"));
    assert!(!combined.contains("log-pass"));
    assert!(!combined.contains("#secret"));
}

#[test]
fn public_connect_keeps_its_unit_signature_and_existing_three_requests() {
    let _: fn(&mut OnvifDevice) -> Result<(), String> = OnvifDevice::connect;
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let device_url = format!("http://127.0.0.1:{port}/selected/device");
    let media_url = format!("http://127.0.0.1:{port}/connected/media");
    let requests = Arc::new(Mutex::new(Vec::new()));
    let server = serve_capability_responses(
        listener,
        capability_connect_responses(&media_url),
        Arc::clone(&requests),
    );
    let mut device = OnvifDevice::with_device_urls_and_timeout(
        "camera",
        "admin",
        "password",
        vec![device_url],
        Duration::from_secs(2),
    )
    .unwrap();

    device.connect().unwrap();
    server.join().unwrap();

    let requests = requests.lock().unwrap();
    let paths = request_paths(&requests);
    assert_eq!(
        paths,
        ["/selected/device", "/selected/device", "/selected/device"]
    );
}

#[test]
fn capability_report_routes_exact_operations_to_advertised_service_endpoints() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let device_url = format!("http://127.0.0.1:{port}/selected/device");
    let connected_media = format!("http://127.0.0.1:{port}/connected/media");
    let media1 = format!("http://127.0.0.1:{port}/advertised/media1");
    let ptz = format!("http://127.0.0.1:{port}/advertised/ptz");
    let events = format!("http://127.0.0.1:{port}/advertised/events");
    let media2 = format!("http://127.0.0.1:{port}/advertised/media2");
    let mut responses = capability_connect_responses(&connected_media);
    responses.extend([
        ok(capability_soap(
            "<tds:GetScopesResponse><tds:Scopes><tt:ScopeItem>onvif://www.onvif.org/Profile/Streaming</tt:ScopeItem></tds:Scopes></tds:GetScopesResponse>",
        )),
        ok(capability_soap(&format!(
            "<tds:GetServicesResponse>{}{}{}{}</tds:GetServicesResponse>",
            capability_service(MEDIA1_NS, &media1, 2, 0),
            capability_service(PTZ_NS, &ptz, 2, 0),
            capability_service(EVENTS_NS, &events, 2, 0),
            capability_service(MEDIA2_NS, &media2, 2, 0),
        ))),
        ok(capability_soap(
            "<trt:GetProfilesResponse><trt:Profiles token=\"legacy\"><tt:Name>Legacy</tt:Name><tt:PTZConfiguration token=\"ptz-legacy\"><tt:NodeToken>node-1</tt:NodeToken></tt:PTZConfiguration></trt:Profiles></trt:GetProfilesResponse>",
        )),
        ok(capability_soap(
            "<tptz:GetServiceCapabilitiesResponse><tptz:Capabilities EFlip=\"true\"/></tptz:GetServiceCapabilitiesResponse>",
        )),
        ok(capability_soap(
            "<tptz:GetNodesResponse><tptz:PTZNode token=\"node-1\"><tt:SupportedPTZSpaces><tt:AbsolutePanTiltPositionSpace/><tt:AbsoluteZoomPositionSpace/></tt:SupportedPTZSpaces></tptz:PTZNode></tptz:GetNodesResponse>",
        )),
        ok(capability_soap(
            "<tev:GetServiceCapabilitiesResponse><tev:Capabilities WSPullPointSupport=\"true\"/></tev:GetServiceCapabilitiesResponse>",
        )),
        ok(capability_soap(
            "<tev:GetEventPropertiesResponse><wstop:TopicSet><tns:Motion wstop:topic=\"true\"/></wstop:TopicSet></tev:GetEventPropertiesResponse>",
        )),
        ok(capability_soap(
            "<tr2:GetProfilesResponse><tr2:Profiles token=\"modern\"><tr2:Configurations><tr2:PTZ token=\"ptz-modern\"/></tr2:Configurations></tr2:Profiles></tr2:GetProfilesResponse>",
        )),
        ok(capability_soap(
            "<tr2:GetVideoEncoderConfigurationOptionsResponse><tr2:Options><tt:Encoding>H264</tt:Encoding></tr2:Options><tr2:Options><tt:Encoding>H265</tt:Encoding></tr2:Options></tr2:GetVideoEncoderConfigurationOptionsResponse>",
        )),
    ]);
    let requests = Arc::new(Mutex::new(Vec::new()));
    let server = serve_capability_responses(listener, responses, Arc::clone(&requests));
    let report = get_camera_capabilities(&CameraCapabilityOptions {
        host: "camera".to_owned(),
        user: "admin".to_owned(),
        password: "password".to_owned(),
        device_urls: vec![device_url],
        timeout: Duration::from_secs(2),
    })
    .unwrap();
    server.join().unwrap();

    assert_eq!(
        report.device.manufacturer.as_deref(),
        Some("Fixture Camera")
    );
    assert_eq!(report.declared_profiles, ["S"]);
    assert_eq!(report.service_discovery, "getServices");
    assert_eq!(
        report
            .profiles
            .iter()
            .map(|profile| (profile.token.as_str(), profile.source.as_str()))
            .collect::<Vec<_>>(),
        [("legacy", "media1"), ("modern", "media2")]
    );
    assert_eq!(report.ptz.profile_tokens, ["legacy", "modern"]);
    assert_eq!(report.ptz.detected, Some(true));
    assert_eq!(report.ptz.pan_tilt_supported, Some(true));
    assert_eq!(report.ptz.zoom_supported, Some(true));
    assert_eq!(report.events.detected, Some(true));
    assert_eq!(
        report.events.topics[0].namespace.as_deref(),
        Some(TOPICS_NS)
    );
    assert_eq!(report.events.topics[0].path, "Motion");
    assert_eq!(report.media2.detected, Some(true));
    assert_eq!(report.media2.encodings, ["H264", "H265"]);
    assert_eq!(report.media2.h265_supported, Some(true));
    assert!(report.warnings.is_empty());

    let requests = requests.lock().unwrap();
    assert_eq!(
        request_paths(&requests),
        [
            "/selected/device",
            "/selected/device",
            "/selected/device",
            "/selected/device",
            "/selected/device",
            "/advertised/media1",
            "/advertised/ptz",
            "/advertised/ptz",
            "/advertised/events",
            "/advertised/events",
            "/advertised/media2",
            "/advertised/media2",
        ]
    );
    let bodies = requests
        .iter()
        .map(|request| request_body(request))
        .collect::<Vec<_>>();
    assert!(bodies[3].contains(&format!("<GetScopes xmlns=\"{DEVICE_NS}\"/>")));
    assert!(bodies[4].contains(&format!(
        "<GetServices xmlns=\"{DEVICE_NS}\"><IncludeCapability>true</IncludeCapability></GetServices>"
    )));
    assert!(bodies[5].contains(&format!("<GetProfiles xmlns=\"{MEDIA1_NS}\"/>")));
    assert!(bodies[6].contains(&format!("<GetServiceCapabilities xmlns=\"{PTZ_NS}\"/>")));
    assert!(bodies[7].contains(&format!("<GetNodes xmlns=\"{PTZ_NS}\"/>")));
    assert!(bodies[8].contains(&format!("<GetServiceCapabilities xmlns=\"{EVENTS_NS}\"/>")));
    assert!(bodies[9].contains(&format!("<GetEventProperties xmlns=\"{EVENTS_NS}\"/>")));
    assert!(bodies[10].contains(&format!(
        "<GetProfiles xmlns=\"{MEDIA2_NS}\"><Type>All</Type></GetProfiles>"
    )));
    assert!(bodies[11].contains(&format!(
        "<GetVideoEncoderConfigurationOptions xmlns=\"{MEDIA2_NS}\"/>"
    )));
}

#[test]
fn capability_action_not_supported_falls_back_to_get_capabilities_all() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let device_url = format!("http://127.0.0.1:{port}/selected/device");
    let connected_media = format!("http://127.0.0.1:{port}/connected/media");
    let legacy_media = format!("http://127.0.0.1:{port}/legacy/media");
    let mut responses = capability_connect_responses(&connected_media);
    responses.extend([
        ok(capability_soap("<tds:GetScopesResponse/>")),
        status(
            500,
            capability_soap(
                "<s:Fault><s:Code><s:Value>s:Sender</s:Value><s:Subcode><s:Value xmlns:ter=\"http://www.onvif.org/ver10/error\">ter:ActionNotSupported</s:Value></s:Subcode></s:Code><s:Reason><s:Text>payload-secret</s:Text></s:Reason></s:Fault>",
            ),
        ),
        ok(capability_soap(&format!(
            "<tds:GetCapabilitiesResponse><tds:Capabilities><tt:Media><tt:XAddr>{legacy_media}</tt:XAddr></tt:Media></tds:Capabilities></tds:GetCapabilitiesResponse>"
        ))),
        ok(capability_soap("<trt:GetProfilesResponse/>")),
    ]);
    let requests = Arc::new(Mutex::new(Vec::new()));
    let server = serve_capability_responses(listener, responses, Arc::clone(&requests));

    let report = get_camera_capabilities(&CameraCapabilityOptions {
        host: "camera".to_owned(),
        user: String::new(),
        password: String::new(),
        device_urls: vec![device_url],
        timeout: Duration::from_secs(2),
    })
    .unwrap();
    server.join().unwrap();

    assert_eq!(report.service_discovery, "getCapabilities");
    assert_eq!(report.warnings.len(), 1);
    assert_eq!(report.warnings[0].operation, "GetServices");
    assert_eq!(report.warnings[0].message, "SOAP Fault: ActionNotSupported");
    assert_eq!(report.ptz.detected, Some(false));
    assert_eq!(report.events.detected, Some(false));
    assert_eq!(report.media2.detected, None);
    assert_eq!(report.media2.h265_supported, None);
    let requests = requests.lock().unwrap();
    assert_eq!(
        request_paths(&requests),
        [
            "/selected/device",
            "/selected/device",
            "/selected/device",
            "/selected/device",
            "/selected/device",
            "/selected/device",
            "/legacy/media",
        ]
    );
    assert!(request_body(&requests[5]).contains(&format!(
        "<GetCapabilities xmlns=\"{DEVICE_NS}\"><Category>All</Category></GetCapabilities>"
    )));
}

#[test]
fn capability_malformed_get_services_also_uses_the_legacy_fallback() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let device_url = format!("http://127.0.0.1:{port}/selected/device");
    let connected_media = format!("http://127.0.0.1:{port}/connected/media");
    let legacy_media = format!("http://127.0.0.1:{port}/legacy/media");
    let mut responses = capability_connect_responses(&connected_media);
    responses.extend([
        ok(capability_soap("<tds:GetScopesResponse/>")),
        ok(capability_soap("<tds:GetServicesResponse/>")),
        ok(capability_soap(&format!(
            "<tds:GetCapabilitiesResponse><tds:Capabilities><tt:Media><tt:XAddr>{legacy_media}</tt:XAddr></tt:Media></tds:Capabilities></tds:GetCapabilitiesResponse>"
        ))),
        ok(capability_soap("<trt:GetProfilesResponse/>")),
    ]);
    let requests = Arc::new(Mutex::new(Vec::new()));
    let server = serve_capability_responses(listener, responses, Arc::clone(&requests));

    let report = get_camera_capabilities(&CameraCapabilityOptions {
        host: "camera".to_owned(),
        user: String::new(),
        password: String::new(),
        device_urls: vec![device_url],
        timeout: Duration::from_secs(2),
    })
    .unwrap();
    server.join().unwrap();

    assert_eq!(report.service_discovery, "getCapabilities");
    assert_eq!(report.warnings.len(), 1);
    assert_eq!(report.warnings[0].operation, "GetServices");
    assert_eq!(
        report.warnings[0].message,
        "no services in GetServices response"
    );
    let requests = requests.lock().unwrap();
    assert_eq!(requests.len(), 7);
    assert!(request_body(&requests[5]).contains(&format!(
        "<GetCapabilities xmlns=\"{DEVICE_NS}\"><Category>All</Category></GetCapabilities>"
    )));
}

#[test]
fn capability_authentication_fault_is_fatal_and_never_runs_fallback() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let device_url = format!("http://127.0.0.1:{port}/selected/device");
    let connected_media = format!("http://127.0.0.1:{port}/connected/media");
    let mut responses = capability_connect_responses(&connected_media);
    responses.extend([
        ok(capability_soap("<tds:GetScopesResponse/>")),
        status(
            500,
            capability_soap(
                "<s:Fault><s:Code><s:Value>s:Sender</s:Value></s:Code><s:Reason><s:Text>Not authorized: viewer password payload-secret</s:Text></s:Reason></s:Fault>",
            ),
        ),
    ]);
    let requests = Arc::new(Mutex::new(Vec::new()));
    let server = serve_capability_responses(listener, responses, Arc::clone(&requests));

    let error = get_camera_capabilities(&CameraCapabilityOptions {
        host: "camera".to_owned(),
        user: "viewer".to_owned(),
        password: "camera-secret".to_owned(),
        device_urls: vec![device_url],
        timeout: Duration::from_secs(2),
    })
    .unwrap_err();
    server.join().unwrap();

    assert_eq!(error, "SOAP Fault: NotAuthorized");
    assert!(!error.contains("viewer"));
    assert!(!error.contains("secret"));
    assert_eq!(requests.lock().unwrap().len(), 5);
}

#[test]
fn capability_unknown_soap11_and_soap12_fault_codes_are_redacted_before_fallback() {
    let faults = [
        capability_soap(
            "<s:Fault><s:Code><s:Value>s:Sender</s:Value><s:Subcode><s:Value>vendor:camera-password-marker</s:Value></s:Subcode></s:Code><s:Reason><s:Text>viewer-marker</s:Text></s:Reason><s:Detail>PasswordDigestABC123</s:Detail></s:Fault>",
        ),
        concat!(
            "<env:Envelope xmlns:env=\"http://schemas.xmlsoap.org/soap/envelope/\">",
            "<env:Body><env:Fault><faultcode>camera-password-marker</faultcode>",
            "<faultstring>viewer-marker</faultstring>",
            "<detail>PasswordDigestABC123</detail></env:Fault></env:Body></env:Envelope>"
        )
        .to_owned(),
    ];

    for fault in faults {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let device_url = format!("http://127.0.0.1:{port}/selected/device");
        let connected_media = format!("http://127.0.0.1:{port}/connected/media");
        let mut responses = capability_connect_responses(&connected_media);
        responses.extend([
            ok(capability_soap("<tds:GetScopesResponse/>")),
            status(500, fault),
            ok(capability_soap(&format!(
                "<tds:GetCapabilitiesResponse><tds:Capabilities><tt:Media><tt:XAddr>{connected_media}</tt:XAddr></tt:Media></tds:Capabilities></tds:GetCapabilitiesResponse>"
            ))),
            ok(capability_soap("<trt:GetProfilesResponse/>")),
        ]);
        let requests = Arc::new(Mutex::new(Vec::new()));
        let server = serve_capability_responses(listener, responses, Arc::clone(&requests));

        let report = get_camera_capabilities(&CameraCapabilityOptions {
            host: "camera".to_owned(),
            user: "viewer-marker".to_owned(),
            password: "camera-password-marker".to_owned(),
            device_urls: vec![device_url],
            timeout: Duration::from_secs(2),
        })
        .unwrap();
        server.join().unwrap();

        assert_eq!(report.service_discovery, "getCapabilities");
        assert_eq!(report.warnings.len(), 1);
        assert_eq!(report.warnings[0].operation, "GetServices");
        assert_eq!(report.warnings[0].message, "SOAP Fault: Fault");
        let warning = serde_json::to_string(&report.warnings).unwrap();
        for marker in [
            "viewer-marker",
            "camera-password-marker",
            "PasswordDigestABC123",
        ] {
            assert!(!warning.contains(marker));
        }
        assert_eq!(requests.lock().unwrap().len(), 7);
    }
}

#[test]
fn capability_optional_failures_are_sanitized_and_keep_unknowns() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let device_url = format!("http://127.0.0.1:{port}/selected/device");
    let connected_media = format!("http://127.0.0.1:{port}/connected/media");
    let media1 = format!("http://127.0.0.1:{port}/advertised/media1");
    let ptz = format!("http://127.0.0.1:{port}/advertised/ptz");
    let events = format!("http://127.0.0.1:{port}/advertised/events");
    let media2 = format!("http://127.0.0.1:{port}/advertised/media2");
    let mut responses = capability_connect_responses(&connected_media);
    responses.extend([
        ok("<!DOCTYPE s:Envelope [<!ENTITY injected \"payload-secret\">]><broken>&injected;</broken>".to_owned()),
        ok(capability_soap(&format!(
            "<tds:GetServicesResponse>{}{}{}{}</tds:GetServicesResponse>",
            capability_service(MEDIA1_NS, &media1, 1, 0),
            capability_service(PTZ_NS, &ptz, 1, 0),
            capability_service(EVENTS_NS, &events, 1, 0),
            capability_service(MEDIA2_NS, &media2, 2, 0),
        ))),
        status(500, "operator:camera-pass@camera payload-secret".to_owned()),
        ok(capability_soap(
            "<tptz:GetServiceCapabilitiesResponse><tptz:Capabilities Reverse=\"true\"/></tptz:GetServiceCapabilitiesResponse>",
        )),
        status(500, "payload-secret".to_owned()),
        ok(capability_soap(
            "<tev:GetServiceCapabilitiesResponse><tev:Capabilities/></tev:GetServiceCapabilitiesResponse>",
        )),
        status(500, "payload-secret".to_owned()),
        ok(capability_soap(
            "<tr2:GetProfilesResponse><tr2:Profiles token=\"media2-only\"><tr2:Configurations><tr2:AudioEncoder/></tr2:Configurations></tr2:Profiles></tr2:GetProfilesResponse>",
        )),
        ok(capability_soap(
            "<tr2:GetVideoEncoderConfigurationOptionsResponse><tr2:Options><vendor:Encoding xmlns:vendor=\"urn:vendor\">H265</vendor:Encoding></tr2:Options></tr2:GetVideoEncoderConfigurationOptionsResponse>",
        )),
    ]);
    let requests = Arc::new(Mutex::new(Vec::new()));
    let server = serve_capability_responses(listener, responses, Arc::clone(&requests));

    let report = get_camera_capabilities(&CameraCapabilityOptions {
        host: "camera".to_owned(),
        user: "operator".to_owned(),
        password: "camera-pass".to_owned(),
        device_urls: vec![device_url],
        timeout: Duration::from_secs(2),
    })
    .unwrap();
    server.join().unwrap();

    assert!(report.scopes.is_empty());
    assert_eq!(report.ptz.detected, Some(true));
    assert_eq!(report.ptz.pan_tilt_supported, None);
    assert_eq!(report.ptz.zoom_supported, None);
    assert_eq!(report.events.detected, Some(true));
    assert!(report.events.topics.is_empty());
    assert_eq!(report.media2.detected, Some(true));
    assert_eq!(report.media2.h265_supported, None);
    assert_eq!(report.profiles[0].token, "media2-only");
    assert_eq!(
        report
            .warnings
            .iter()
            .map(|warning| warning.operation.as_str())
            .collect::<Vec<_>>(),
        [
            "GetScopes",
            "Media1 GetProfiles",
            "PTZ GetNodes",
            "Events GetEventProperties",
            "Media2 GetVideoEncoderConfigurationOptions",
        ]
    );
    let warning_text = serde_json::to_string(&report.warnings).unwrap();
    for secret in [
        "operator",
        "camera-pass",
        "payload-secret",
        "PasswordDigest",
        "@camera",
    ] {
        assert!(!warning_text.contains(secret));
    }
}

#[test]
fn capability_credential_service_url_is_rejected_before_network_dispatch() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let device_url = format!("http://127.0.0.1:{port}/selected/device");
    let connected_media = format!("http://127.0.0.1:{port}/connected/media");
    let unsafe_media = format!("http://viewer:url-secret@127.0.0.1:{port}/must-not-reach");
    let mut responses = capability_connect_responses(&connected_media);
    responses.extend([
        ok(capability_soap("<tds:GetScopesResponse/>")),
        ok(capability_soap(&format!(
            "<tds:GetServicesResponse>{}</tds:GetServicesResponse>",
            capability_service(MEDIA1_NS, &unsafe_media, 1, 0),
        ))),
    ]);
    let requests = Arc::new(Mutex::new(Vec::new()));
    let server = serve_capability_responses(listener, responses, Arc::clone(&requests));

    let report = get_camera_capabilities(&CameraCapabilityOptions {
        host: "camera".to_owned(),
        user: "admin".to_owned(),
        password: "camera-secret".to_owned(),
        device_urls: vec![device_url],
        timeout: Duration::from_secs(2),
    })
    .unwrap();
    server.join().unwrap();

    assert_eq!(requests.lock().unwrap().len(), 5);
    assert_eq!(report.warnings.len(), 1);
    assert_eq!(report.warnings[0].operation, "Media1 GetProfiles");
    assert_eq!(report.warnings[0].message, "invalid ONVIF service URL");
    assert!(!report.warnings[0].message.contains("viewer"));
    assert!(!report.warnings[0].message.contains("url-secret"));
}

#[test]
fn capability_cross_origin_service_is_retained_but_never_dispatched() {
    for legacy_fallback in [false, true] {
        let device_listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let device_port = device_listener.local_addr().unwrap().port();
        let attacker_listener = TcpListener::bind("[::1]:0").unwrap();
        attacker_listener.set_nonblocking(true).unwrap();
        let attacker_port = attacker_listener.local_addr().unwrap().port();
        let attacker = thread::spawn(move || {
            let deadline = Instant::now() + Duration::from_millis(750);
            while Instant::now() < deadline {
                match attacker_listener.accept() {
                    Ok((_stream, _)) => return true,
                    Err(error) if error.kind() == ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(5));
                    }
                    Err(error) => panic!("attacker listener failed: {error}"),
                }
            }
            false
        });

        let device_url = format!("http://127.0.0.1:{device_port}/selected/device");
        let connected_media = format!("http://127.0.0.1:{device_port}/connected/media");
        let attacker_media = format!("http://[::1]:{attacker_port}/must-not-reach");
        let mut responses = capability_connect_responses(&connected_media);
        responses.push(ok(capability_soap("<tds:GetScopesResponse/>")));
        if legacy_fallback {
            responses.extend([
                ok(capability_soap("<tds:GetServicesResponse/>")),
                ok(capability_soap(&format!(
                    "<tds:GetCapabilitiesResponse><tds:Capabilities><tt:Media><tt:XAddr>{attacker_media}</tt:XAddr></tt:Media></tds:Capabilities></tds:GetCapabilitiesResponse>"
                ))),
            ]);
        } else {
            responses.push(ok(capability_soap(&format!(
                "<tds:GetServicesResponse>{}</tds:GetServicesResponse>",
                capability_service(MEDIA1_NS, &attacker_media, 1, 0),
            ))));
        }
        let requests = Arc::new(Mutex::new(Vec::new()));
        let server = serve_capability_responses(device_listener, responses, Arc::clone(&requests));

        let report = get_camera_capabilities(&CameraCapabilityOptions {
            host: "camera".to_owned(),
            user: "admin".to_owned(),
            password: "camera-secret".to_owned(),
            device_urls: vec![device_url],
            timeout: Duration::from_secs(2),
        })
        .unwrap();
        server.join().unwrap();
        let attacker_contacted = attacker.join().unwrap();

        assert!(!attacker_contacted);
        assert_eq!(
            requests.lock().unwrap().len(),
            if legacy_fallback { 6 } else { 5 }
        );
        assert_eq!(report.services[0].xaddr, attacker_media);
        assert_eq!(
            report.service_discovery,
            if legacy_fallback {
                "getCapabilities"
            } else {
                "getServices"
            }
        );
        assert!(report.warnings.iter().any(|warning| {
            warning.operation == "Media1 GetProfiles"
                && warning.message == "invalid ONVIF service URL"
        }));
    }
}

#[derive(Clone)]
struct CapabilityHttpResponse {
    status: u16,
    body: String,
}

fn ok(body: String) -> CapabilityHttpResponse {
    status(200, body)
}

fn status(status: u16, body: String) -> CapabilityHttpResponse {
    CapabilityHttpResponse { status, body }
}

fn capability_soap(body: &str) -> String {
    format!(
        "<s:Envelope xmlns:s=\"{SOAP12_NS}\" xmlns:tds=\"{DEVICE_NS}\" \
         xmlns:tt=\"{SCHEMA_NS}\" xmlns:trt=\"{MEDIA1_NS}\" \
         xmlns:tr2=\"{MEDIA2_NS}\" xmlns:tptz=\"{PTZ_NS}\" \
         xmlns:tev=\"{EVENTS_NS}\" xmlns:wstop=\"{WSTOP_NS}\" \
         xmlns:tns=\"{TOPICS_NS}\"><s:Body>{body}</s:Body></s:Envelope>"
    )
}

fn capability_connect_responses(media_url: &str) -> Vec<CapabilityHttpResponse> {
    vec![
        ok(capability_soap(
            "<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime><tt:UTCDateTime><tt:Time><tt:Hour>12</tt:Hour><tt:Minute>30</tt:Minute><tt:Second>0</tt:Second></tt:Time><tt:Date><tt:Year>2026</tt:Year><tt:Month>8</tt:Month><tt:Day>6</tt:Day></tt:Date></tt:UTCDateTime></tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>",
        )),
        ok(capability_soap(
            "<tds:GetDeviceInformationResponse><tds:Manufacturer>Fixture Camera</tds:Manufacturer><tds:Model>C1</tds:Model><tds:FirmwareVersion>1.2.3</tds:FirmwareVersion><tds:SerialNumber>serial-1</tds:SerialNumber></tds:GetDeviceInformationResponse>",
        )),
        ok(capability_soap(&format!(
            "<tds:GetCapabilitiesResponse><tds:Capabilities><tt:Media><tt:XAddr>{media_url}</tt:XAddr></tt:Media></tds:Capabilities></tds:GetCapabilitiesResponse>"
        ))),
    ]
}

fn capability_service(namespace: &str, xaddr: &str, major: i32, minor: i32) -> String {
    format!(
        "<tds:Service><tds:Namespace>{namespace}</tds:Namespace><tds:XAddr>{xaddr}</tds:XAddr><tds:Version><tt:Major>{major}</tt:Major><tt:Minor>{minor}</tt:Minor></tds:Version></tds:Service>"
    )
}

fn serve_capability_responses(
    listener: TcpListener,
    responses: Vec<CapabilityHttpResponse>,
    requests: Arc<Mutex<Vec<String>>>,
) -> thread::JoinHandle<()> {
    listener.set_nonblocking(true).unwrap();
    thread::spawn(move || {
        for response in responses {
            let deadline = Instant::now() + Duration::from_secs(4);
            let mut stream = loop {
                match listener.accept() {
                    Ok((stream, _)) => break stream,
                    Err(error) if error.kind() == ErrorKind::WouldBlock => {
                        assert!(
                            Instant::now() < deadline,
                            "timed out waiting for capability request"
                        );
                        thread::sleep(Duration::from_millis(5));
                    }
                    Err(error) => panic!("capability server accept failed: {error}"),
                }
            };
            stream.set_nonblocking(false).unwrap();
            requests
                .lock()
                .unwrap()
                .push(read_http_request(&mut stream));
            let reason = match response.status {
                200 => "OK",
                401 => "Unauthorized",
                403 => "Forbidden",
                500 => "Internal Server Error",
                _ => "Test Status",
            };
            write!(
                stream,
                "HTTP/1.1 {} {}\r\nContent-Type: application/soap+xml\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                response.status,
                reason,
                response.body.len(),
                response.body
            )
            .unwrap();
        }
    })
}

fn request_paths(requests: &[String]) -> Vec<&str> {
    requests
        .iter()
        .map(|request| {
            request
                .lines()
                .next()
                .unwrap()
                .split_whitespace()
                .nth(1)
                .unwrap()
        })
        .collect()
}

fn request_body(request: &str) -> &str {
    request.split_once("\r\n\r\n").unwrap().1
}

fn read_http_request(stream: &mut impl Read) -> String {
    let mut request = Vec::new();
    let mut chunk = [0u8; 4096];
    let header_end;
    loop {
        let read = stream.read(&mut chunk).unwrap();
        request.extend_from_slice(&chunk[..read]);
        if let Some(end) = request.windows(4).position(|window| window == b"\r\n\r\n") {
            header_end = end;
            break;
        }
    }
    let headers = String::from_utf8_lossy(&request[..header_end]);
    let content_length = headers
        .lines()
        .find_map(|line| {
            let (name, value) = line.split_once(':')?;
            name.eq_ignore_ascii_case("content-length")
                .then(|| value.trim().parse::<usize>().unwrap())
        })
        .unwrap_or(0);
    let body_start = header_end + 4;
    while request.len() < body_start + content_length {
        let read = stream.read(&mut chunk).unwrap();
        request.extend_from_slice(&chunk[..read]);
    }
    String::from_utf8_lossy(&request).into_owned()
}
