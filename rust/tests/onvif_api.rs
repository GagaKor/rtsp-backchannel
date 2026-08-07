use std::io::{ErrorKind, Read, Write};
use std::net::{Ipv4Addr, TcpListener};
use std::process::{Command, Stdio};
use std::sync::{Arc, Mutex, mpsc};
use std::thread;
use std::time::{Duration, Instant};

use rtsp_backchannel::cli::{
    ApplicationInvocation, CapabilitiesCli, Invocation, parse_application_invocation_from,
    parse_invocation_from,
};
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

fn classify_legacy_invocation(invocation: Invocation) -> &'static str {
    match invocation {
        Invocation::Play(_) => "play",
        Invocation::Discover(_) => "discover",
        Invocation::Streams(_) => "streams",
    }
}

#[test]
fn application_parser_adds_capabilities_without_breaking_legacy_invocation_exhaustiveness() {
    let legacy =
        parse_invocation_from(["rtsp-backchannel", "discover", "--timeout-ms", "50"]).unwrap();
    assert_eq!(classify_legacy_invocation(legacy), "discover");

    assert!(
        parse_invocation_from(["rtsp-backchannel", "capabilities", "--host", "camera.local",])
            .is_err()
    );

    assert!(matches!(
        parse_application_invocation_from([
            "rtsp-backchannel",
            "capabilities",
            "--host",
            "camera.local",
        ])
        .unwrap(),
        ApplicationInvocation::Capabilities(_)
    ));
}

#[test]
fn capabilities_cli_parses_defaults_repeated_values_and_exact_command_dispatch() {
    match parse_application_invocation_from([
        "rtsp-backchannel",
        "capabilities",
        "--host",
        "camera.local",
        "--user",
        "operator",
        "--pass",
        "",
        "--device-url",
        "http://camera.local/onvif/device_service",
        "--device-url",
        "http://camera.local:8000/vendor/device",
        "--timeout-ms",
        "2500.5",
    ])
    .unwrap()
    {
        ApplicationInvocation::Capabilities(cli) => {
            let cli: CapabilitiesCli = cli;
            assert_eq!(cli.host, "camera.local");
            assert_eq!(cli.user.as_deref(), Some("operator"));
            assert_eq!(cli.password.as_deref(), Some(""));
            assert_eq!(
                cli.device_urls,
                [
                    "http://camera.local/onvif/device_service",
                    "http://camera.local:8000/vendor/device",
                ]
            );
            assert_eq!(cli.timeout, Some(Duration::from_micros(2_500_500)));
        }
        _ => panic!("expected capabilities invocation"),
    }

    match parse_application_invocation_from([
        "rtsp-backchannel",
        "capabilities",
        "--host",
        "camera.local",
        "--pass",
        "",
    ])
    .unwrap()
    {
        ApplicationInvocation::Capabilities(cli) => {
            assert_eq!(cli.user, None);
            assert_eq!(cli.password.as_deref(), Some(""));
            assert!(cli.device_urls.is_empty());
            assert_eq!(cli.timeout, None);
        }
        _ => panic!("expected capabilities invocation"),
    }

    for arguments in [
        vec![
            "rtsp-backchannel",
            "--host",
            "capabilities",
            "--file",
            "event.mp3",
        ],
        vec![
            "rtsp-backchannel",
            "play",
            "--host",
            "capabilities",
            "--file",
            "event.mp3",
        ],
    ] {
        assert!(matches!(
            parse_application_invocation_from(arguments).unwrap(),
            ApplicationInvocation::Play(_)
        ));
    }
}

#[test]
fn capabilities_cli_rejects_unsafe_values_and_timeout_underflow_before_dispatch() {
    let cases = [
        vec!["rtsp-backchannel", "capabilities"],
        vec!["rtsp-backchannel", "capabilities", "--host", ""],
        vec![
            "rtsp-backchannel",
            "capabilities",
            "--host",
            "--timeout-ms",
            "50",
        ],
        vec![
            "rtsp-backchannel",
            "capabilities",
            "--host",
            "camera.local",
            "--user",
            "",
        ],
        vec![
            "rtsp-backchannel",
            "capabilities",
            "--host",
            "camera.local",
            "--user",
            "--timeout-ms",
            "50",
        ],
        vec![
            "rtsp-backchannel",
            "capabilities",
            "--host",
            "camera.local",
            "--pass",
        ],
        vec![
            "rtsp-backchannel",
            "capabilities",
            "--host",
            "camera.local",
            "--pass",
            "--timeout-ms",
            "50",
        ],
        vec![
            "rtsp-backchannel",
            "capabilities",
            "--host",
            "camera.local",
            "--device-url",
            "",
        ],
        vec![
            "rtsp-backchannel",
            "capabilities",
            "--host",
            "camera.local",
            "--device-url",
            "--timeout-ms",
            "50",
        ],
        vec![
            "rtsp-backchannel",
            "capabilities",
            "--host",
            "camera.local",
            "--timeout-ms",
        ],
        vec![
            "rtsp-backchannel",
            "capabilities",
            "--host",
            "camera.local",
            "--timeout-ms",
            "",
        ],
        vec![
            "rtsp-backchannel",
            "capabilities",
            "--host",
            "camera.local",
            "--timeout-ms",
            "--user",
            "operator",
        ],
    ];
    for arguments in cases {
        let diagnostic = parse_application_invocation_from(arguments)
            .unwrap_err()
            .to_string();
        assert!(!diagnostic.contains("validation-only-secret"));
        assert!(!diagnostic.contains("environment-only-secret"));
    }

    for timeout in ["0", "-1", "5e-324", "NaN", "inf", "1e309"] {
        let diagnostic = parse_application_invocation_from([
            "rtsp-backchannel",
            "capabilities",
            "--host",
            "camera.local",
            "--pass",
            "validation-only-secret",
            "--timeout-ms",
            timeout,
        ])
        .unwrap_err()
        .to_string();
        assert!(!diagnostic.contains("validation-only-secret"));
        assert!(!diagnostic.contains("environment-only-secret"));
    }

    for (arguments, option, secret) in [
        (
            vec![
                "rtsp-backchannel",
                "capabilities",
                "--host",
                "--host-secret-marker",
            ],
            "host",
            "--host-secret-marker",
        ),
        (
            vec![
                "rtsp-backchannel",
                "capabilities",
                "--host",
                "camera.local",
                "--user",
                "--user-secret-marker",
            ],
            "user",
            "--user-secret-marker",
        ),
        (
            vec![
                "rtsp-backchannel",
                "capabilities",
                "--host",
                "camera.local",
                "--device-url",
                "--device-url-secret-marker",
            ],
            "device-url",
            "--device-url-secret-marker",
        ),
        (
            vec![
                "rtsp-backchannel",
                "capabilities",
                "--host",
                "camera.local",
                "--timeout-ms",
                "--timeout-secret-marker",
            ],
            "timeout-ms",
            "--timeout-secret-marker",
        ),
    ] {
        let diagnostic = parse_application_invocation_from(arguments)
            .unwrap_err()
            .to_string();
        assert!(
            diagnostic.contains(&format!("missing value for --{option}")),
            "unexpected diagnostic: {diagnostic}"
        );
        assert!(!diagnostic.contains(secret));
    }

    for (arguments, option, secret) in [
        (
            vec![
                "rtsp-backchannel",
                "capabilities",
                "--host=--host-equals-secret-marker",
            ],
            "host",
            "--host-equals-secret-marker",
        ),
        (
            vec![
                "rtsp-backchannel",
                "capabilities",
                "--host",
                "camera.local",
                "--user=--user-equals-secret-marker",
            ],
            "user",
            "--user-equals-secret-marker",
        ),
        (
            vec![
                "rtsp-backchannel",
                "capabilities",
                "--host",
                "camera.local",
                "--device-url=--device-url-equals-secret-marker",
            ],
            "device-url",
            "--device-url-equals-secret-marker",
        ),
        (
            vec![
                "rtsp-backchannel",
                "capabilities",
                "--host",
                "camera.local",
                "--timeout-ms=--timeout-equals-secret-marker",
            ],
            "timeout-ms",
            "--timeout-equals-secret-marker",
        ),
    ] {
        let diagnostic = parse_application_invocation_from(arguments)
            .unwrap_err()
            .to_string();
        assert!(
            diagnostic.contains(&format!("missing value for --{option}")),
            "unexpected diagnostic: {diagnostic}"
        );
        assert!(!diagnostic.contains(secret));
    }

    for (arguments, expected_password) in [
        (
            vec![
                "rtsp-backchannel",
                "capabilities",
                "--host",
                "camera.local",
                "--pass",
                "--separate-password-secret",
            ],
            "--separate-password-secret",
        ),
        (
            vec![
                "rtsp-backchannel",
                "capabilities",
                "--host",
                "camera.local",
                "--pass=--equals-password-secret",
            ],
            "--equals-password-secret",
        ),
    ] {
        match parse_application_invocation_from(arguments).unwrap() {
            ApplicationInvocation::Capabilities(cli) => {
                assert_eq!(cli.password.as_deref(), Some(expected_password));
            }
            _ => panic!("expected capabilities invocation"),
        }
    }

    for (password_arguments, secret) in [
        (
            vec!["--pass", "--separate-password-secret"],
            "--separate-password-secret",
        ),
        (
            vec!["--pass=--equals-password-secret"],
            "--equals-password-secret",
        ),
    ] {
        let mut arguments = vec!["rtsp-backchannel", "capabilities", "--host", "camera.local"];
        arguments.extend(password_arguments);
        arguments.extend(["--timeout-ms", "0"]);
        let diagnostic = parse_application_invocation_from(arguments)
            .unwrap_err()
            .to_string();
        assert!(!diagnostic.contains(secret));
    }

    let known_flag = parse_application_invocation_from([
        "rtsp-backchannel",
        "capabilities",
        "--host",
        "camera.local",
        "--pass",
        "--timeout-ms",
        "50",
    ])
    .unwrap_err()
    .to_string();
    assert!(known_flag.contains("missing value for --pass"));
}

#[test]
fn capabilities_cli_enforces_an_inclusive_24_hour_timeout_bound_at_parse_time() {
    assert_eq!("86400000.000000001".parse::<f64>().unwrap(), 86_400_000.0);
    for timeout in ["86400000.00000001", "86400000.00000049"] {
        assert!(timeout.parse::<f64>().unwrap() > 86_400_000.0);
    }

    for timeout_arguments in [
        vec!["--timeout-ms", "86400000"],
        vec!["--timeout-ms=86400000"],
        vec!["--timeout-ms", "86400000.000000001"],
        vec!["--timeout-ms=86400000.000000001"],
    ] {
        let mut arguments = vec!["rtsp-backchannel", "capabilities", "--host", "camera.local"];
        arguments.extend(timeout_arguments);
        match parse_application_invocation_from(arguments).unwrap() {
            ApplicationInvocation::Capabilities(cli) => {
                assert_eq!(cli.timeout, Some(Duration::from_secs(86_400)));
            }
            _ => panic!("expected capabilities invocation"),
        }
    }

    for (timeout_arguments, reflected_timeout) in [
        (
            vec!["--timeout-ms", "86400000.00000001"],
            "86400000.00000001",
        ),
        (vec!["--timeout-ms=86400000.00000001"], "86400000.00000001"),
        (
            vec!["--timeout-ms", "86400000.00000049"],
            "86400000.00000049",
        ),
        (vec!["--timeout-ms=86400000.00000049"], "86400000.00000049"),
        (vec!["--timeout-ms", "86400001"], "86400001"),
        (vec!["--timeout-ms=86400001"], "86400001"),
        (vec!["--timeout-ms", "1e22"], "1e22"),
        (vec!["--timeout-ms=1e22"], "1e22"),
    ] {
        let mut arguments = vec!["rtsp-backchannel", "capabilities", "--host", "camera.local"];
        arguments.extend(timeout_arguments);
        let diagnostic = parse_application_invocation_from(arguments)
            .unwrap_err()
            .to_string();

        assert!(
            diagnostic.contains("timeout-ms exceeds the 24-hour maximum"),
            "unexpected diagnostic: {diagnostic}"
        );
        assert!(!diagnostic.contains(reflected_timeout));
    }
}

#[test]
fn capabilities_cli_above_24_hour_timeout_exits_without_panic_or_network_dispatch() {
    for (timeout_arguments, reflected_timeout) in [
        (
            vec!["--timeout-ms", "86400000.00000001"],
            "86400000.00000001",
        ),
        (vec!["--timeout-ms=86400000.00000001"], "86400000.00000001"),
        (
            vec!["--timeout-ms", "86400000.00000049"],
            "86400000.00000049",
        ),
        (vec!["--timeout-ms=86400000.00000049"], "86400000.00000049"),
        (vec!["--timeout-ms", "86400001"], "86400001"),
        (vec!["--timeout-ms=86400001"], "86400001"),
        (vec!["--timeout-ms", "1e22"], "1e22"),
        (vec!["--timeout-ms=1e22"], "1e22"),
    ] {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let device_url =
            format!("http://huge-url-user:huge-url-pass@127.0.0.1:{port}/must-not-reach");
        let (stop_sentinel, sentinel) = start_network_sentinel(listener);

        let mut command = Command::new(env!("CARGO_BIN_EXE_rtsp-backchannel"));
        command.args([
            "capabilities",
            "--host",
            "camera.local",
            "--pass",
            "huge-password-secret",
            "--device-url",
            &device_url,
        ]);
        command.args(timeout_arguments);
        command.env("ONVIF_PASSWORD", "huge-environment-secret");
        let output = command_output_with_timeout(&mut command, Duration::from_secs(10));
        let _ = stop_sentinel.send(());
        let network_contacted = sentinel.join().unwrap();

        assert!(!output.status.success());
        assert_eq!(output.status.code(), Some(2));
        assert!(output.stdout.is_empty());
        let diagnostic = String::from_utf8_lossy(&output.stderr);
        assert!(
            diagnostic.contains("timeout-ms exceeds the 24-hour maximum"),
            "unexpected diagnostic: {diagnostic}"
        );
        for reflected in [
            reflected_timeout,
            "huge-url-user",
            "huge-url-pass",
            "huge-password-secret",
            "huge-environment-secret",
            "panicked",
        ] {
            assert!(!diagnostic.contains(reflected));
        }
        assert!(!network_contacted);
    }
}

#[test]
fn capabilities_cli_treats_bare_terminator_as_control_before_password_rewrite() {
    let diagnostic = parse_application_invocation_from([
        "rtsp-backchannel",
        "capabilities",
        "--host",
        "camera.local",
        "--pass",
        "control-password-secret",
        "--",
    ])
    .unwrap_err()
    .to_string();
    assert!(
        diagnostic.contains("capabilities does not accept an argument terminator"),
        "unexpected diagnostic: {diagnostic}"
    );
    assert!(!diagnostic.contains("control-password-secret"));

    let diagnostic = parse_application_invocation_from([
        "rtsp-backchannel",
        "capabilities",
        "--host",
        "camera.local",
        "--pass",
        "--",
        "--pass=trailing-equals-secret",
    ])
    .unwrap_err()
    .to_string();

    assert!(
        diagnostic.contains("missing value for --pass"),
        "unexpected diagnostic: {diagnostic}"
    );
    assert!(!diagnostic.contains("trailing-equals-secret"));
}

#[test]
fn capabilities_cli_keeps_attached_double_dash_password_opaque() {
    match parse_application_invocation_from([
        "rtsp-backchannel",
        "capabilities",
        "--host",
        "camera.local",
        "--pass=--",
    ])
    .unwrap()
    {
        ApplicationInvocation::Capabilities(cli) => {
            assert_eq!(cli.password.as_deref(), Some("--"));
        }
        _ => panic!("expected capabilities invocation"),
    }
}

#[test]
fn capabilities_cli_sanitizes_control_trailing_credentials_without_network_dispatch() {
    let mut trailing_cases = vec![
        vec![std::ffi::OsString::from("--pass=trailing-equals-secret")],
        vec![
            std::ffi::OsString::from("--pass"),
            std::ffi::OsString::from("trailing-separate-secret"),
        ],
    ];
    #[cfg(unix)]
    {
        use std::os::unix::ffi::OsStringExt;

        trailing_cases.push(vec![std::ffi::OsString::from_vec(
            b"\xffnon-utf8-trailing-secret".to_vec(),
        )]);
    }

    for trailing_arguments in trailing_cases {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let device_url =
            format!("http://control-url-user:control-url-pass@127.0.0.1:{port}/must-not-reach");
        let (stop_sentinel, sentinel) = start_network_sentinel(listener);

        let mut command = Command::new(env!("CARGO_BIN_EXE_rtsp-backchannel"));
        command
            .args([
                "capabilities",
                "--host",
                "camera.local",
                "--pass",
                "control-password-secret",
                "--device-url",
                &device_url,
                "--timeout-ms",
                "50",
                "--",
            ])
            .args(trailing_arguments)
            .env("ONVIF_PASSWORD", "control-environment-secret");
        let output = command_output_with_timeout(&mut command, Duration::from_secs(10));
        let _ = stop_sentinel.send(());
        let network_contacted = sentinel.join().unwrap();

        assert!(!output.status.success());
        assert_eq!(output.status.code(), Some(2));
        assert!(output.stdout.is_empty());
        let diagnostic = String::from_utf8_lossy(&output.stderr);
        assert!(
            diagnostic.contains("capabilities does not accept an argument terminator"),
            "unexpected diagnostic: {diagnostic}"
        );
        for reflected in [
            "trailing-equals-secret",
            "trailing-separate-secret",
            "non-utf8-trailing-secret",
            "control-url-user",
            "control-url-pass",
            "control-password-secret",
            "control-environment-secret",
        ] {
            assert!(!diagnostic.contains(reflected));
        }
        assert!(!network_contacted);
    }
}

#[test]
fn capabilities_cli_help_lists_options_without_network_or_secret_values() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let port = listener.local_addr().unwrap().port();
    let device_url = format!("http://127.0.0.1:{port}/must-not-reach");

    let mut root_command = Command::new(env!("CARGO_BIN_EXE_rtsp-backchannel"));
    root_command
        .arg("--help")
        .env("ONVIF_PASSWORD", "help-environment-secret");
    let root = command_output_with_timeout(&mut root_command, Duration::from_secs(10));
    assert!(root.status.success());
    assert!(String::from_utf8_lossy(&root.stdout).contains("capabilities"));

    let mut subcommand_command = Command::new(env!("CARGO_BIN_EXE_rtsp-backchannel"));
    subcommand_command
        .args([
            "capabilities",
            "--host",
            "camera.local",
            "--device-url",
            &device_url,
            "--pass",
            "help-command-secret",
            "--help",
        ])
        .env("ONVIF_PASSWORD", "help-environment-secret");
    let subcommand = command_output_with_timeout(&mut subcommand_command, Duration::from_secs(10));
    assert!(subcommand.status.success());
    let help = String::from_utf8_lossy(&subcommand.stdout);
    for expected in [
        "rtsp-backchannel capabilities",
        "--host",
        "--user",
        "--pass",
        "--device-url",
        "--timeout-ms",
        "per-request",
        "repeat",
    ] {
        assert!(help.contains(expected), "missing {expected:?} in {help}");
    }
    let combined = format!(
        "{}{}{}{}",
        String::from_utf8_lossy(&root.stdout),
        String::from_utf8_lossy(&root.stderr),
        help,
        String::from_utf8_lossy(&subcommand.stderr),
    );
    for secret in ["help-command-secret", "help-environment-secret"] {
        assert!(!combined.contains(secret));
    }
    assert_eq!(listener.accept().unwrap_err().kind(), ErrorKind::WouldBlock);
}

#[test]
fn capabilities_cli_outputs_one_native_json_line_and_explicit_empty_overrides_environment() {
    let (environment_output, environment_requests, environment_media) =
        run_capabilities_cli_fixture(None, "environment-only-secret");
    assert!(
        environment_output.status.success(),
        "{}",
        String::from_utf8_lossy(&environment_output.stderr)
    );
    assert!(environment_output.stderr.is_empty());
    let stdout = String::from_utf8(environment_output.stdout).unwrap();
    assert!(stdout.ends_with('\n'));
    assert_eq!(stdout.lines().count(), 1);
    assert_eq!(
        serde_json::from_str::<serde_json::Value>(stdout.trim_end()).unwrap(),
        serde_json::json!({
            "device": {
                "manufacturer": "Fixture Camera",
                "model": "C1",
                "firmware": "1.2.3",
                "serial": "serial-1"
            },
            "scopes": [],
            "declaredProfiles": [],
            "serviceDiscovery": "getCapabilities",
            "services": [{"namespace": MEDIA1_NS, "xaddr": environment_media}],
            "profiles": [],
            "ptz": {
                "detected": false,
                "panTiltSupported": null,
                "zoomSupported": null,
                "profileTokens": [],
                "nodes": []
            },
            "media2": {"detected": null, "encodings": [], "h265Supported": null},
            "warnings": [{
                "operation": "GetServices",
                "message": "no services in GetServices response"
            }]
        })
    );
    assert_eq!(environment_requests.len(), 7);
    assert!(
        environment_requests[1..]
            .iter()
            .all(|request| request.contains("<wsse:Security"))
    );

    let (empty_output, empty_requests, _) =
        run_capabilities_cli_fixture(Some(""), "environment-only-secret");
    assert!(
        empty_output.status.success(),
        "{}",
        String::from_utf8_lossy(&empty_output.stderr)
    );
    assert!(empty_output.stderr.is_empty());
    assert_eq!(
        String::from_utf8_lossy(&empty_output.stdout)
            .lines()
            .count(),
        1
    );
    assert_eq!(empty_requests.len(), 7);
    assert!(
        empty_requests
            .iter()
            .all(|request| !request.contains("<wsse:Security"))
    );

    for content in [
        stdout.as_bytes(),
        &environment_output.stderr,
        &empty_output.stdout,
        &empty_output.stderr,
    ] {
        let content = String::from_utf8_lossy(content);
        assert!(!content.contains("environment-only-secret"));
    }
    for request in environment_requests.iter().chain(&empty_requests) {
        assert!(!request.contains("environment-only-secret"));
    }
}

#[test]
fn capabilities_cli_documentation_covers_native_report_and_hardened_semantics() {
    let english = include_str!("../README.md");
    let korean = include_str!("../README.ko.md");

    for readme in [english, korean] {
        for expected in [
            "get_camera_capabilities",
            "CameraCapabilityOptions",
            "CameraCapabilityReport",
            "declared_profiles",
            "declaredProfiles",
            "service_discovery",
            "profile_tokens",
            "pan_tilt_supported",
            "Profile T",
            "media2.detected",
            "media2.h265Supported",
            "true",
            "false",
            "null",
            "GetServices",
            "GetCapabilities",
            "warning.message",
            "same-origin",
            "canonical host",
            "XAddr",
            "WSSE",
            "64",
            "per-request",
            "ONVIF_PASSWORD",
            "`--pass \"\"`",
            "rtsp-backchannel capabilities",
            "--device-url",
            "--timeout-ms",
            "https://www.onvif.org/specs/",
        ] {
            assert!(readme.contains(expected), "missing {expected:?}");
        }
    }

    assert!(english.contains("declared profile self-report"));
    assert!(english.contains("Initial connection and authentication failures are fatal"));
    assert!(english.contains("exactly one camelCase JSON object"));
    assert!(english.contains("different ports and paths remain allowed"));
    assert!(korean.contains("선언 프로필 자체 보고"));
    assert!(korean.contains("초기 연결 및 인증 실패는 치명적 오류"));
    assert!(korean.contains("camelCase JSON 객체를 정확히 한 줄"));
    assert!(korean.contains("서로 다른 포트와 경로는 허용"));
    assert!(english.contains("24-hour maximum"));
    assert!(korean.contains("24시간 상한"));
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
fn capability_report_matches_shared_cross_language_fixture() {
    let fixture_path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests/fixtures/capability-parity.json");
    assert!(
        fixture_path.is_file(),
        "shared capability parity fixture is missing: {}",
        fixture_path.display()
    );

    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let base_url = format!("http://127.0.0.1:{port}");
    let raw_fixture = std::fs::read_to_string(&fixture_path).unwrap();
    let fixture: serde_json::Value =
        serde_json::from_str(&raw_fixture.replace("{{BASE_URL}}", &base_url)).unwrap();
    let operations = fixture
        .get("operations")
        .and_then(serde_json::Value::as_object)
        .expect("fixture operations must be an object");
    let operation_order = [
        "GetSystemDateAndTime",
        "GetDeviceInformation",
        "GetCapabilitiesMedia",
        "GetScopes",
        "GetServices",
        "Media1GetProfiles",
        "PtzGetServiceCapabilities",
        "PtzGetNodes",
        "Media2GetProfiles",
        "Media2GetVideoEncoderConfigurationOptions",
    ];
    let responses = operation_order
        .iter()
        .map(|operation| {
            let body = operations
                .get(*operation)
                .and_then(serde_json::Value::as_str)
                .unwrap_or_else(|| panic!("fixture operation {operation} must be a string"));
            ok(capability_soap(body))
        })
        .collect();
    let requests = Arc::new(Mutex::new(Vec::new()));
    let server = serve_capability_responses(listener, responses, Arc::clone(&requests));

    let report = get_camera_capabilities(&CameraCapabilityOptions {
        host: "camera".to_owned(),
        user: "viewer".to_owned(),
        password: "camera-secret".to_owned(),
        device_urls: vec![format!("{base_url}/device")],
        timeout: Duration::from_secs(2),
    })
    .unwrap();
    server.join().unwrap();

    let requests = requests.lock().unwrap();
    assert_eq!(
        request_paths(&requests),
        [
            "/device", "/device", "/device", "/device", "/device", "/media1", "/ptz", "/ptz",
            "/media2", "/media2",
        ]
    );
    let operation_markers = [
        format!("<GetSystemDateAndTime xmlns=\"{DEVICE_NS}\"/>"),
        format!("<GetDeviceInformation xmlns=\"{DEVICE_NS}\"/>"),
        format!(
            "<GetCapabilities xmlns=\"{DEVICE_NS}\"><Category>Media</Category></GetCapabilities>"
        ),
        format!("<GetScopes xmlns=\"{DEVICE_NS}\"/>"),
        format!(
            "<GetServices xmlns=\"{DEVICE_NS}\"><IncludeCapability>true</IncludeCapability></GetServices>"
        ),
        format!("<GetProfiles xmlns=\"{MEDIA1_NS}\"/>"),
        format!("<GetServiceCapabilities xmlns=\"{PTZ_NS}\"/>"),
        format!("<GetNodes xmlns=\"{PTZ_NS}\"/>"),
        format!("<GetProfiles xmlns=\"{MEDIA2_NS}\"><Type>All</Type></GetProfiles>"),
        format!("<GetVideoEncoderConfigurationOptions xmlns=\"{MEDIA2_NS}\"/>"),
    ];
    for (request, marker) in requests.iter().zip(operation_markers) {
        assert!(
            request_body(request).contains(&marker),
            "capability request did not contain expected operation marker: {marker}"
        );
    }
    assert_eq!(
        serde_json::to_value(report).unwrap(),
        fixture
            .get("expectedReport")
            .cloned()
            .expect("fixture expectedReport is required")
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
    let media2 = format!("http://127.0.0.1:{port}/advertised/media2");
    let mut responses = capability_connect_responses(&connected_media);
    responses.extend([
        ok(capability_soap(
            "<tds:GetScopesResponse><tds:Scopes><tt:ScopeItem>onvif://www.onvif.org/Profile/Streaming</tt:ScopeItem></tds:Scopes></tds:GetScopesResponse>",
        )),
        ok(capability_soap(&format!(
            "<tds:GetServicesResponse>{}{}{}</tds:GetServicesResponse>",
            capability_service(MEDIA1_NS, &media1, 2, 0),
            capability_service(PTZ_NS, &ptz, 2, 0),
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
    assert!(bodies[8].contains(&format!(
        "<GetProfiles xmlns=\"{MEDIA2_NS}\"><Type>All</Type></GetProfiles>"
    )));
    assert!(bodies[9].contains(&format!(
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
    let media2 = format!("http://127.0.0.1:{port}/advertised/media2");
    let mut responses = capability_connect_responses(&connected_media);
    responses.extend([
        ok("<!DOCTYPE s:Envelope [<!ENTITY injected \"payload-secret\">]><broken>&injected;</broken>".to_owned()),
        ok(capability_soap(&format!(
            "<tds:GetServicesResponse>{}{}{}</tds:GetServicesResponse>",
            capability_service(MEDIA1_NS, &media1, 1, 0),
            capability_service(PTZ_NS, &ptz, 1, 0),
            capability_service(MEDIA2_NS, &media2, 2, 0),
        ))),
        status(500, "operator:camera-pass@camera payload-secret".to_owned()),
        ok(capability_soap(
            "<tptz:GetServiceCapabilitiesResponse><tptz:Capabilities Reverse=\"true\"/></tptz:GetServiceCapabilitiesResponse>",
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

fn run_capabilities_cli_fixture(
    explicit_password: Option<&str>,
    environment_password: &str,
) -> (std::process::Output, Vec<String>, String) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let device_url = format!("http://127.0.0.1:{port}/selected/device");
    let connected_media = format!("http://127.0.0.1:{port}/connected/media");
    let unused_device_url = format!("http://127.0.0.1:{port}/unused/device");
    let mut responses = capability_connect_responses(&connected_media);
    responses.extend([
        ok(capability_soap("<tds:GetScopesResponse/>")),
        ok(capability_soap("<tds:GetServicesResponse/>")),
        ok(capability_soap(&format!(
            "<tds:GetCapabilitiesResponse><tds:Capabilities><tt:Media><tt:XAddr>{connected_media}</tt:XAddr></tt:Media></tds:Capabilities></tds:GetCapabilitiesResponse>"
        ))),
        ok(capability_soap("<trt:GetProfilesResponse/>")),
    ]);
    let requests = Arc::new(Mutex::new(Vec::new()));
    let server = serve_capability_responses(listener, responses, Arc::clone(&requests));

    let mut command = Command::new(env!("CARGO_BIN_EXE_rtsp-backchannel"));
    command
        .args([
            "capabilities",
            "--host",
            "camera.local",
            "--device-url",
            &device_url,
            "--device-url",
            &unused_device_url,
            "--timeout-ms",
            "2500.5",
        ])
        .env("ONVIF_PASSWORD", environment_password);
    if let Some(password) = explicit_password {
        command.arg("--pass").arg(password);
    }
    let output = command_output_with_timeout(&mut command, Duration::from_secs(10));
    server.join().unwrap();
    let requests = requests.lock().unwrap().clone();
    (output, requests, connected_media)
}

fn command_output_with_timeout(command: &mut Command, timeout: Duration) -> std::process::Output {
    let mut child = command
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let deadline = Instant::now() + timeout;
    loop {
        if child.try_wait().unwrap().is_some() {
            return child.wait_with_output().unwrap();
        }
        if Instant::now() >= deadline {
            child.kill().unwrap();
            let _ = child.wait_with_output();
            panic!("CLI child process timed out");
        }
        thread::sleep(Duration::from_millis(5));
    }
}

fn start_network_sentinel(listener: TcpListener) -> (mpsc::Sender<()>, thread::JoinHandle<bool>) {
    listener.set_nonblocking(true).unwrap();
    let (stop_sender, stop_receiver) = mpsc::channel();
    let sentinel = thread::spawn(move || {
        loop {
            match listener.accept() {
                Ok((_stream, _address)) => return true,
                Err(error) if error.kind() == ErrorKind::WouldBlock => {}
                Err(error) => panic!("network sentinel failed: {error}"),
            }
            if stop_receiver.try_recv().is_ok() {
                return false;
            }
            thread::sleep(Duration::from_millis(1));
        }
    });
    (stop_sender, sentinel)
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
         xmlns:tr2=\"{MEDIA2_NS}\" xmlns:tptz=\"{PTZ_NS}\"><s:Body>{body}</s:Body></s:Envelope>"
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
            stream
                .set_read_timeout(Some(Duration::from_secs(4)))
                .unwrap();
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
        assert!(read > 0, "unexpected EOF while reading HTTP headers");
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
        assert!(read > 0, "unexpected EOF while reading HTTP body");
        request.extend_from_slice(&chunk[..read]);
    }
    String::from_utf8_lossy(&request).into_owned()
}
