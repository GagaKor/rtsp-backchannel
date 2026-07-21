use base64::Engine;
use chrono::{DateTime, Duration as ChronoDuration, TimeZone, Utc};
use reqwest::blocking::Client;
use serde::Serialize;
use sha1::{Digest, Sha1};
use std::io::Read;
use std::time::Duration;

use crate::rtsp::{has_rtsp_scheme, sanitize_rtsp_uri};

const DEVICE_NS: &str = "http://www.onvif.org/ver10/device/wsdl";
const MEDIA_NS: &str = "http://www.onvif.org/ver10/media/wsdl";
const SCHEMA_NS: &str = "http://www.onvif.org/ver10/schema";
const MAX_ONVIF_RESPONSE_BYTES: usize = 1024 * 1024;

const WSSE_NS: &str =
    "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd";
const WSU_NS: &str =
    "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd";
const PASSWORD_DIGEST: &str = concat!(
    "http://docs.oasis-open.org/wss/2004/01/",
    "oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
);

fn xml_escape(value: &str) -> String {
    value
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&apos;")
}

pub fn wsse_header(user: &str, password: &str, created: DateTime<Utc>, nonce: &[u8]) -> String {
    let created = created.format("%Y-%m-%dT%H:%M:%SZ").to_string();
    let mut digest = Sha1::new();
    digest.update(nonce);
    digest.update(created.as_bytes());
    digest.update(password.as_bytes());
    let digest = base64::engine::general_purpose::STANDARD.encode(digest.finalize());
    let nonce = base64::engine::general_purpose::STANDARD.encode(nonce);
    format!(
        "<wsse:Security xmlns:wsse=\"{WSSE_NS}\" xmlns:wsu=\"{WSU_NS}\">\
         <wsse:UsernameToken><wsse:Username>{}</wsse:Username>\
         <wsse:Password Type=\"{PASSWORD_DIGEST}\">{digest}</wsse:Password>\
         <wsse:Nonce>{nonce}</wsse:Nonce><wsu:Created>{created}</wsu:Created>\
         </wsse:UsernameToken></wsse:Security>",
        xml_escape(user)
    )
}

fn descendant_text<'a>(node: roxmltree::Node<'a, 'a>, name: &str) -> Option<&'a str> {
    node.descendants()
        .find(|child| child.is_element() && child.tag_name().name() == name)?
        .text()
}

pub fn parse_device_time(xml: &str) -> Result<DateTime<Utc>, String> {
    let document = roxmltree::Document::parse(xml)
        .map_err(|error| format!("invalid ONVIF time XML: {error}"))?;
    let utc = document
        .descendants()
        .find(|node| node.is_element() && node.tag_name().name() == "UTCDateTime")
        .ok_or("ONVIF response has no UTCDateTime")?;
    let number = |name: &str| -> Result<u32, String> {
        descendant_text(utc, name)
            .ok_or_else(|| format!("ONVIF UTCDateTime has no {name}"))?
            .parse()
            .map_err(|_| format!("ONVIF UTCDateTime has invalid {name}"))
    };
    Utc.with_ymd_and_hms(
        number("Year")? as i32,
        number("Month")?,
        number("Day")?,
        number("Hour")?,
        number("Minute")?,
        number("Second")?,
    )
    .single()
    .ok_or_else(|| "ONVIF returned an invalid UTCDateTime".to_owned())
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OnvifProfile {
    pub token: String,
    pub name: Option<String>,
    pub has_audio_encoder: bool,
    pub has_audio_output: bool,
    pub has_audio_source: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StreamUriOptions {
    pub host: String,
    pub user: String,
    pub password: String,
    pub device_urls: Vec<String>,
    pub timeout: Duration,
}

impl StreamUriOptions {
    pub fn new(
        host: impl Into<String>,
        user: impl Into<String>,
        password: impl Into<String>,
    ) -> Self {
        Self {
            host: host.into(),
            user: user.into(),
            password: password.into(),
            device_urls: Vec::new(),
            timeout: Duration::from_secs(8),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct StreamUri {
    pub profile_token: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub profile_name: Option<String>,
    pub uri: String,
}

pub fn parse_profiles(xml: &str) -> Result<Vec<OnvifProfile>, String> {
    let document = roxmltree::Document::parse(xml)
        .map_err(|error| format!("invalid GetProfiles XML: {error}"))?;
    Ok(document
        .descendants()
        .filter(|node| node.is_element() && node.tag_name().name() == "Profiles")
        .filter_map(|node| {
            let token = node.attribute("token")?.to_owned();
            let has_element = |name: &str| {
                node.descendants()
                    .any(|child| child.is_element() && child.tag_name().name() == name)
            };
            Some(OnvifProfile {
                token,
                name: descendant_text(node, "Name").map(str::to_owned),
                has_audio_encoder: has_element("AudioEncoderConfiguration"),
                has_audio_output: has_element("AudioOutputConfiguration"),
                has_audio_source: has_element("AudioSourceConfiguration"),
            })
        })
        .collect())
}

pub fn parse_profile_tokens(xml: &str) -> Result<Vec<String>, String> {
    Ok(parse_profiles(xml)?
        .into_iter()
        .map(|profile| profile.token)
        .collect())
}

pub struct OnvifDevice {
    host: String,
    user: String,
    password: String,
    device_urls: Vec<String>,
    client: Client,
    media_url: Option<String>,
    clock_offset: ChronoDuration,
}

impl OnvifDevice {
    pub fn new(host: &str, user: &str, password: &str) -> Result<Self, String> {
        Self::with_device_urls(
            host,
            user,
            password,
            vec![
                format!("http://{host}/onvif/device_service"),
                format!("https://{host}/onvif/device_service"),
                format!("http://{host}:8000/onvif/device_service"),
            ],
        )
    }

    pub fn with_device_urls(
        host: &str,
        user: &str,
        password: &str,
        device_urls: Vec<String>,
    ) -> Result<Self, String> {
        Self::with_device_urls_and_timeout(
            host,
            user,
            password,
            device_urls,
            Duration::from_secs(8),
        )
    }

    pub fn with_device_urls_and_timeout(
        host: &str,
        user: &str,
        password: &str,
        device_urls: Vec<String>,
        timeout: Duration,
    ) -> Result<Self, String> {
        if timeout.is_zero() {
            return Err("ONVIF timeout must be greater than zero".to_owned());
        }
        let client = Client::builder()
            .danger_accept_invalid_certs(true)
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(timeout)
            .build()
            .map_err(|error| format!("failed to build ONVIF HTTP client: {error}"))?;
        Ok(Self {
            host: host.to_owned(),
            user: user.to_owned(),
            password: password.to_owned(),
            device_urls,
            client,
            media_url: None,
            clock_offset: ChronoDuration::zero(),
        })
    }

    pub fn connect(&mut self) -> Result<(), String> {
        let mut last_error = None;
        for device_url in self.device_urls.clone() {
            let result = (|| {
                let time_xml = self.soap(
                    &device_url,
                    &format!("<GetSystemDateAndTime xmlns=\"{DEVICE_NS}\"/>"),
                    false,
                )?;
                let device_time = parse_device_time(&time_xml)?;
                self.clock_offset = device_time.signed_duration_since(Utc::now());

                let information = self.soap(
                    &device_url,
                    &format!("<GetDeviceInformation xmlns=\"{DEVICE_NS}\"/>"),
                    true,
                )?;
                if !information.contains("GetDeviceInformationResponse") {
                    return Err("GetDeviceInformation was rejected".to_owned());
                }

                let capabilities = self.soap(
                    &device_url,
                    &format!(
                        "<GetCapabilities xmlns=\"{DEVICE_NS}\"><Category>Media</Category></GetCapabilities>"
                    ),
                    true,
                )?;
                let media_url = parse_first_text(&capabilities, "XAddr")
                    .filter(|value| value.to_ascii_lowercase().contains("media"))
                    .unwrap_or_else(|| device_url.replace("device_service", "media_service"));
                Ok::<String, String>(media_url)
            })();
            match result {
                Ok(media_url) => {
                    self.media_url = Some(media_url);
                    return Ok(());
                }
                Err(error) => last_error = Some(error),
            }
        }
        Err(format!(
            "ONVIF connect failed for {}: {}",
            safe_host(&self.host),
            last_error.unwrap_or_else(|| "no device service candidates".to_owned())
        ))
    }

    pub fn profile_tokens(&self) -> Result<Vec<String>, String> {
        Ok(self
            .profiles()?
            .into_iter()
            .map(|profile| profile.token)
            .collect())
    }

    pub fn profiles(&self) -> Result<Vec<OnvifProfile>, String> {
        let xml = self.soap(
            self.require_media_url()?,
            &format!("<GetProfiles xmlns=\"{MEDIA_NS}\"/>"),
            true,
        )?;
        let profiles = parse_profiles(&xml)?;
        if profiles.is_empty() {
            return Err("ONVIF returned no media profiles".to_owned());
        }
        Ok(profiles)
    }

    pub fn stream_uri(&self, profile_token: &str) -> Result<String, String> {
        let body = format!(
            "<GetStreamUri xmlns=\"{MEDIA_NS}\"><StreamSetup>\
             <Stream xmlns=\"{SCHEMA_NS}\">RTP-Unicast</Stream>\
             <Transport xmlns=\"{SCHEMA_NS}\"><Protocol>RTSP</Protocol></Transport>\
             </StreamSetup><ProfileToken>{}</ProfileToken></GetStreamUri>",
            xml_escape(profile_token)
        );
        let xml = self.soap(self.require_media_url()?, &body, true)?;
        parse_first_text(&xml, "Uri").ok_or_else(|| "GetStreamUri returned no Uri".to_owned())
    }

    fn require_media_url(&self) -> Result<&str, String> {
        self.media_url
            .as_deref()
            .ok_or_else(|| "call ONVIF connect() first".to_owned())
    }

    fn soap(&self, url: &str, body: &str, authenticated: bool) -> Result<String, String> {
        let (status, text) = self.soap_response(url, body, authenticated)?;
        if status.is_server_error() && !text.contains("Envelope") {
            return Err(format!(
                "ONVIF request to {} returned HTTP {status}",
                safe_url(url)
            ));
        }
        Ok(text)
    }

    fn soap_response(
        &self,
        url: &str,
        body: &str,
        authenticated: bool,
    ) -> Result<(reqwest::StatusCode, String), String> {
        let security = if authenticated && !(self.user.is_empty() && self.password.is_empty()) {
            let mut nonce = [0u8; 16];
            rand::fill(&mut nonce[..]);
            wsse_header(
                &self.user,
                &self.password,
                Utc::now() + self.clock_offset,
                &nonce,
            )
        } else {
            String::new()
        };
        let envelope = format!(
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\
             <s:Envelope xmlns:s=\"http://www.w3.org/2003/05/soap-envelope\">\
             <s:Header>{security}</s:Header><s:Body>{body}</s:Body></s:Envelope>"
        );
        let response = self
            .client
            .post(url)
            .header("Content-Type", "application/soap+xml; charset=utf-8")
            .body(envelope)
            .send()
            .map_err(|error| format!("ONVIF request to {} failed: {error}", safe_url(url)))?;
        let status = response.status();
        if response
            .content_length()
            .is_some_and(|length| length > MAX_ONVIF_RESPONSE_BYTES as u64)
        {
            return Err(format!(
                "ONVIF response body from {} exceeds {MAX_ONVIF_RESPONSE_BYTES} byte limit",
                safe_url(url)
            ));
        }
        let mut bytes = Vec::new();
        response
            .take((MAX_ONVIF_RESPONSE_BYTES + 1) as u64)
            .read_to_end(&mut bytes)
            .map_err(|error| {
                format!(
                    "failed to read ONVIF response from {}: {error}",
                    safe_url(url)
                )
            })?;
        if bytes.len() > MAX_ONVIF_RESPONSE_BYTES {
            return Err(format!(
                "ONVIF response body from {} exceeds {MAX_ONVIF_RESPONSE_BYTES} byte limit",
                safe_url(url)
            ));
        }
        let text = String::from_utf8(bytes)
            .map_err(|_| format!("ONVIF response from {} is not UTF-8", safe_url(url)))?;
        Ok((status, text))
    }
}

pub(crate) fn probe_device_service(url: &str, timeout: Duration) -> Result<(), String> {
    let device =
        OnvifDevice::with_device_urls_and_timeout("", "", "", vec![url.to_owned()], timeout)?;
    let (status, xml) = device.soap_response(
        url,
        &format!("<GetSystemDateAndTime xmlns=\"{DEVICE_NS}\"/>"),
        false,
    )?;
    if !status.is_success() {
        return Err(format!(
            "ONVIF discovery request to {} returned HTTP {status}",
            safe_url(url)
        ));
    }
    parse_device_time(&xml).map(|_| ())
}

pub fn get_stream_uris(options: &StreamUriOptions) -> Result<Vec<StreamUri>, String> {
    let device_urls = if options.device_urls.is_empty() {
        vec![
            format!("http://{}/onvif/device_service", options.host),
            format!("https://{}/onvif/device_service", options.host),
            format!("http://{}:8000/onvif/device_service", options.host),
        ]
    } else {
        options.device_urls.clone()
    };
    let mut device = OnvifDevice::with_device_urls_and_timeout(
        &options.host,
        &options.user,
        &options.password,
        device_urls,
        options.timeout,
    )?;
    device.connect()?;
    device
        .profiles()?
        .into_iter()
        .map(|profile| {
            let uri = sanitize_rtsp_uri(&device.stream_uri(&profile.token)?)?;
            Ok(StreamUri {
                profile_token: profile.token,
                profile_name: profile.name,
                uri,
            })
        })
        .collect()
}

fn safe_url(url: &str) -> String {
    if let Ok(mut parsed) = url::Url::parse(url) {
        let _ = parsed.set_username("");
        let _ = parsed.set_password(None);
        parsed.set_fragment(None);
        parsed.to_string()
    } else {
        "<invalid-url>".to_owned()
    }
}

fn safe_host(host: &str) -> String {
    if has_rtsp_scheme(host) {
        safe_url(host)
    } else {
        host.to_owned()
    }
}

fn parse_first_text(xml: &str, name: &str) -> Option<String> {
    let document = roxmltree::Document::parse(xml).ok()?;
    document
        .descendants()
        .find(|node| node.is_element() && node.tag_name().name() == name)?
        .text()
        .map(str::to_owned)
}

#[cfg(test)]
mod tests {
    use std::io::{ErrorKind, Read, Write};
    use std::net::TcpListener;
    use std::sync::{Arc, Mutex};
    use std::thread;

    use chrono::{TimeZone, Utc};

    use super::{
        OnvifDevice, parse_device_time, parse_profile_tokens, probe_device_service, wsse_header,
    };

    #[test]
    fn builds_a_deterministic_wsse_password_digest() {
        let created = Utc.with_ymd_and_hms(2026, 7, 16, 0, 0, 0).unwrap();

        let header = wsse_header(
            "admin",
            "pass",
            created,
            &[0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
        );

        assert!(header.contains("i9BQe26F+iQzWat9eChlLdU8dbU="));
        assert!(header.contains("AAECAwQFBgcICQoLDA0ODw=="));
        assert!(header.contains("2026-07-16T00:00:00Z"));
        assert!(header.contains("<wsse:Username>admin</wsse:Username>"));
        assert!(!header.contains(">pass<"));
    }

    #[test]
    fn parses_namespaced_device_time_and_profile_tokens() {
        let time = parse_device_time(
            r#"<s:Envelope xmlns:s="urn:s"><s:Body><tds:GetSystemDateAndTimeResponse xmlns:tds="urn:d"><tds:UTCDateTime><tt:Time xmlns:tt="urn:t"><tt:Hour>13</tt:Hour><tt:Minute>14</tt:Minute><tt:Second>15</tt:Second></tt:Time><tt:Date xmlns:tt="urn:t"><tt:Year>2026</tt:Year><tt:Month>7</tt:Month><tt:Day>16</tt:Day></tt:Date></tds:UTCDateTime></tds:GetSystemDateAndTimeResponse></s:Body></s:Envelope>"#,
        )
        .unwrap();
        assert_eq!(time, Utc.with_ymd_and_hms(2026, 7, 16, 13, 14, 15).unwrap());

        let profiles = parse_profile_tokens(
            r#"<trt:GetProfilesResponse xmlns:trt="urn:m"><trt:Profiles token="main"/><trt:Profiles token="sub"/></trt:GetProfilesResponse>"#,
        )
        .unwrap();
        assert_eq!(profiles, ["main", "sub"]);
    }

    #[test]
    fn resolves_a_stream_uri_through_authenticated_onvif_calls() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let device_url = format!("http://127.0.0.1:{port}/onvif/device_service");
        let media_url = format!("http://127.0.0.1:{port}/onvif/media_service");
        let requests = Arc::new(Mutex::new(Vec::new()));
        let server_requests = Arc::clone(&requests);
        let server = thread::spawn(move || {
            let responses = [
                "<Envelope><UTCDateTime><Time><Hour>13</Hour><Minute>14</Minute><Second>15</Second></Time><Date><Year>2026</Year><Month>7</Month><Day>16</Day></Date></UTCDateTime></Envelope>".to_owned(),
                "<Envelope><GetDeviceInformationResponse><Model>camera</Model></GetDeviceInformationResponse></Envelope>".to_owned(),
                format!("<Envelope><Capabilities><Media><XAddr>{media_url}</XAddr></Media></Capabilities></Envelope>"),
                "<Envelope><GetProfilesResponse><Profiles token=\"main\"/><Profiles token=\"sub\"/></GetProfilesResponse></Envelope>".to_owned(),
                "<Envelope><GetStreamUriResponse><MediaUri><Uri>rtsp://127.0.0.1/live</Uri></MediaUri></GetStreamUriResponse></Envelope>".to_owned(),
            ];
            for response in responses {
                let (mut stream, _) = listener.accept().unwrap();
                let request = read_http_request(&mut stream);
                server_requests.lock().unwrap().push(request);
                write!(
                    stream,
                    "HTTP/1.1 200 OK\r\nContent-Type: application/soap+xml\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                    response.len(),
                    response
                )
                .unwrap();
            }
        });

        let mut device =
            OnvifDevice::with_device_urls("camera", "admin", "pass", vec![device_url]).unwrap();
        device.connect().unwrap();
        assert_eq!(device.profile_tokens().unwrap(), ["main", "sub"]);
        assert_eq!(device.stream_uri("main").unwrap(), "rtsp://127.0.0.1/live");
        server.join().unwrap();

        let requests = requests.lock().unwrap();
        assert!(requests[0].contains("GetSystemDateAndTime"));
        assert!(!requests[0].contains("wsse:Security"));
        for request in &requests[1..] {
            assert!(request.contains("wsse:Security"));
            assert!(!request.contains(">pass<"));
        }
        assert!(requests[4].contains("<ProfileToken>main</ProfileToken>"));
    }

    #[test]
    fn omits_ws_security_when_both_credentials_are_empty() {
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
            ];
            for response in responses {
                let (mut stream, _) = listener.accept().unwrap();
                server_requests
                    .lock()
                    .unwrap()
                    .push(read_http_request(&mut stream));
                write!(
                    stream,
                    "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                    response.len(),
                    response
                )
                .unwrap();
            }
        });

        let mut device = OnvifDevice::with_device_urls("camera", "", "", vec![device_url]).unwrap();
        device.connect().unwrap();
        server.join().unwrap();

        assert!(
            requests
                .lock()
                .unwrap()
                .iter()
                .all(|request| !request.contains("wsse:Security"))
        );
    }

    #[test]
    fn rejects_an_onvif_response_body_that_exceeds_the_limit_without_content_length() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let device_url = format!("http://127.0.0.1:{port}/onvif/device_service");
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let _ = read_http_request(&mut stream);
            stream
                .write_all(b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n")
                .unwrap();
            let oversized = vec![b'x'; 1024 * 1024 + 1];
            let _ = stream.write_all(&oversized);
        });
        let device =
            OnvifDevice::with_device_urls("127.0.0.1", "", "", vec![device_url.clone()]).unwrap();

        let error = device
            .soap(&device_url, "<GetSystemDateAndTime/>", false)
            .unwrap_err();
        server.join().unwrap();

        assert!(error.contains("response body"));
        assert!(error.contains("limit"));
    }

    #[test]
    fn device_service_probe_requires_a_success_http_status() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let _ = read_http_request(&mut stream);
            let body = concat!(
                "<Envelope><UTCDateTime><Time><Hour>1</Hour>",
                "<Minute>2</Minute><Second>3</Second></Time><Date>",
                "<Year>2026</Year><Month>7</Month><Day>20</Day>",
                "</Date></UTCDateTime></Envelope>"
            );
            write!(
                stream,
                "HTTP/1.1 404 Not Found\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            )
            .unwrap();
        });

        let error = probe_device_service(
            &format!("http://127.0.0.1:{port}/onvif/device_service"),
            std::time::Duration::from_secs(1),
        )
        .unwrap_err();
        server.join().unwrap();

        assert!(error.contains("404"));
    }

    #[test]
    fn device_service_probe_does_not_follow_http_redirects() {
        let redirect_listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let redirect_port = redirect_listener.local_addr().unwrap().port();
        let target_listener = TcpListener::bind("127.0.0.1:0").unwrap();
        target_listener.set_nonblocking(true).unwrap();
        let target_port = target_listener.local_addr().unwrap().port();

        let redirect_server = thread::spawn(move || {
            let (mut stream, _) = redirect_listener.accept().unwrap();
            let _ = read_http_request(&mut stream);
            write!(
                stream,
                "HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:{target_port}/redirected\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            )
            .unwrap();
        });
        let target_server = thread::spawn(move || {
            let deadline = std::time::Instant::now() + std::time::Duration::from_millis(500);
            while std::time::Instant::now() < deadline {
                match target_listener.accept() {
                    Ok((mut stream, _)) => {
                        let mut request = [0u8; 4096];
                        let _ = stream.read(&mut request).unwrap();
                        let body = concat!(
                            "<Envelope><UTCDateTime><Time><Hour>1</Hour>",
                            "<Minute>2</Minute><Second>3</Second></Time><Date>",
                            "<Year>2026</Year><Month>7</Month><Day>20</Day>",
                            "</Date></UTCDateTime></Envelope>"
                        );
                        write!(
                            stream,
                            "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                            body.len()
                        )
                        .unwrap();
                        return true;
                    }
                    Err(error) if error.kind() == ErrorKind::WouldBlock => {
                        thread::sleep(std::time::Duration::from_millis(5));
                    }
                    Err(error) => panic!("redirect target failed: {error}"),
                }
            }
            false
        });

        let result = probe_device_service(
            &format!("http://127.0.0.1:{redirect_port}/onvif/device_service"),
            std::time::Duration::from_secs(1),
        );
        redirect_server.join().unwrap();
        let target_contacted = target_server.join().unwrap();

        assert!(result.is_err());
        assert!(!target_contacted);
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
        let header = String::from_utf8_lossy(&request[..header_end]);
        let content_length = header
            .lines()
            .find_map(|line| {
                let (name, value) = line.split_once(':')?;
                name.eq_ignore_ascii_case("content-length")
                    .then(|| value.trim().to_owned())
            })
            .unwrap()
            .trim()
            .parse::<usize>()
            .unwrap();
        let total = header_end + 4 + content_length;
        while request.len() < total {
            let read = stream.read(&mut chunk).unwrap();
            request.extend_from_slice(&chunk[..read]);
        }
        String::from_utf8(request[..total].to_vec()).unwrap()
    }
}
