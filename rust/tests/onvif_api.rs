use std::io::{Read, Write};
use std::net::{Ipv4Addr, TcpListener};
use std::process::Command;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use rtsp_backchannel::cli::{Invocation, parse_invocation_from};
use rtsp_backchannel::discovery::{DiscoveryOptions, parse_probe_matches};
use rtsp_backchannel::onvif::{StreamUriOptions, get_stream_uris, parse_profiles};

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
            "<Envelope><GetStreamUriResponse><Uri>rtsp://camera/live?channel=1&amp;stream=main</Uri></GetStreamUriResponse></Envelope>".to_owned(),
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
