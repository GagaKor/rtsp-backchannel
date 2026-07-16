use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::TcpStream;
use std::time::{Duration, Instant};

use base64::Engine;

pub const BACKCHANNEL_REQUIRE: &str = "www.onvif.org/ver20/backchannel";
const MAX_RTSP_HEADER_BYTES: usize = 64 * 1024;
const MAX_RTSP_BODY_BYTES: usize = 1024 * 1024;
const MAX_RTSP_RECEIVE_BUFFER_BYTES: usize = MAX_RTSP_HEADER_BYTES + 4 + MAX_RTSP_BODY_BYTES;

#[derive(Debug)]
pub struct RtspResponse {
    pub status: u16,
    pub status_line: String,
    pub headers: HashMap<String, String>,
    pub body: Vec<u8>,
}

#[derive(Debug)]
pub struct SetupResult {
    pub rtp_channel: u8,
}

struct DigestChallenge {
    realm: String,
    nonce: String,
    qop: Option<String>,
    opaque: Option<String>,
    cnonce: String,
    nonce_count: u32,
}

enum Authentication {
    Basic,
    Digest(DigestChallenge),
}

pub struct RtspClient {
    stream: TcpStream,
    receive_buffer: Vec<u8>,
    cseq: u32,
    session: Option<String>,
    session_timeout: Duration,
    response_timeout: Duration,
    user: String,
    password: String,
    authentication: Option<Authentication>,
}

impl RtspClient {
    pub fn connect(
        host: &str,
        port: u16,
        user: &str,
        password: &str,
        timeout: Duration,
    ) -> Result<Self, String> {
        let stream = TcpStream::connect((host, port))
            .map_err(|error| format!("RTSP connect failed: {error}"))?;
        stream
            .set_read_timeout(Some(timeout))
            .map_err(|error| format!("failed to set RTSP read timeout: {error}"))?;
        stream
            .set_write_timeout(Some(timeout))
            .map_err(|error| format!("failed to set RTSP write timeout: {error}"))?;
        Ok(Self {
            stream,
            receive_buffer: Vec::new(),
            cseq: 0,
            session: None,
            session_timeout: Duration::from_secs(60),
            response_timeout: timeout,
            user: user.to_owned(),
            password: password.to_owned(),
            authentication: None,
        })
    }

    pub fn session_timeout(&self) -> Duration {
        self.session_timeout
    }

    pub fn options(&mut self, uri: &str) -> Result<RtspResponse, String> {
        self.request("OPTIONS", uri, Vec::new())
    }

    pub fn describe(&mut self, uri: &str) -> Result<RtspResponse, String> {
        self.request(
            "DESCRIBE",
            uri,
            vec![
                ("Accept".to_owned(), "application/sdp".to_owned()),
                ("Require".to_owned(), BACKCHANNEL_REQUIRE.to_owned()),
            ],
        )
    }

    pub fn setup(
        &mut self,
        track_uri: &str,
        rtp_channel: u8,
        backchannel: bool,
    ) -> Result<SetupResult, String> {
        let mut headers = vec![(
            "Transport".to_owned(),
            format!(
                "RTP/AVP/TCP;unicast;interleaved={rtp_channel}-{}",
                rtp_channel + 1
            ),
        )];
        if let Some(session) = &self.session {
            headers.push(("Session".to_owned(), session.clone()));
        }
        if backchannel {
            headers.push(("Require".to_owned(), BACKCHANNEL_REQUIRE.to_owned()));
        }
        let response = self.request("SETUP", track_uri, headers)?;
        if response.status != 200 {
            return Err(format!("SETUP failed: {}", response.status_line));
        }
        let session_header = response
            .headers
            .get("session")
            .ok_or("RTSP SETUP returned no Session header")?;
        let mut session_fields = session_header.split(';');
        let session = session_fields.next().unwrap_or_default().trim();
        if session.is_empty() {
            return Err("RTSP SETUP returned an empty Session ID".to_owned());
        }
        self.session = Some(session.to_owned());
        for field in session_fields {
            if let Some(value) = field.trim().strip_prefix("timeout=")
                && let Ok(seconds) = value.parse::<u64>()
                && seconds > 0
            {
                self.session_timeout = Duration::from_secs(seconds);
            }
        }
        let selected = response
            .headers
            .get("transport")
            .and_then(|header| parse_interleaved_channel(header))
            .unwrap_or(rtp_channel);
        Ok(SetupResult {
            rtp_channel: selected,
        })
    }

    pub fn play(&mut self, uri: &str) -> Result<RtspResponse, String> {
        let session = self.require_session()?;
        self.request(
            "PLAY",
            uri,
            vec![
                ("Session".to_owned(), session),
                ("Range".to_owned(), "npt=now-".to_owned()),
                ("Require".to_owned(), BACKCHANNEL_REQUIRE.to_owned()),
            ],
        )
    }

    pub fn keep_alive(&mut self, uri: &str) -> Result<RtspResponse, String> {
        let session = self.require_session()?;
        self.request("OPTIONS", uri, vec![("Session".to_owned(), session)])
    }

    pub fn teardown(&mut self, uri: &str) -> Result<(), String> {
        let Some(session) = self.session.clone() else {
            return Ok(());
        };
        let response = self.request("TEARDOWN", uri, vec![("Session".to_owned(), session)])?;
        if response.status != 200 {
            return Err(format!("TEARDOWN failed: {}", response.status_line));
        }
        self.session = None;
        Ok(())
    }

    pub fn send_interleaved(&mut self, frame: &[u8]) -> Result<(), String> {
        self.stream
            .write_all(frame)
            .map_err(|error| format!("RTSP interleaved write failed: {error}"))
    }

    pub fn drain_interleaved(&mut self) -> Result<usize, String> {
        self.stream
            .set_nonblocking(true)
            .map_err(|error| format!("failed to enable nonblocking RTSP reads: {error}"))?;
        let result = (|| {
            let mut drained = self.discard_complete_interleaved();
            let mut chunk = [0u8; 16 * 1024];
            loop {
                match self.stream.read(&mut chunk) {
                    Ok(0) => break,
                    Ok(read) => {
                        self.receive_buffer.extend_from_slice(&chunk[..read]);
                        drained += self.discard_complete_interleaved();
                        self.enforce_receive_buffer_limit()?;
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => break,
                    Err(error) => {
                        return Err(format!("RTSP media drain failed: {error}"));
                    }
                }
            }
            Ok(drained)
        })();
        let restore = self
            .stream
            .set_nonblocking(false)
            .map_err(|error| format!("failed to restore blocking RTSP reads: {error}"));
        match (result, restore) {
            (Err(error), _) => Err(error),
            (Ok(_), Err(error)) => Err(error),
            (Ok(drained), Ok(())) => Ok(drained),
        }
    }

    fn require_session(&self) -> Result<String, String> {
        self.session
            .clone()
            .ok_or_else(|| "SETUP must precede this RTSP request".to_owned())
    }

    fn request(
        &mut self,
        method: &str,
        uri: &str,
        headers: Vec<(String, String)>,
    ) -> Result<RtspResponse, String> {
        let mut response = self.send_request(method, uri, &headers)?;
        if response.status == 401
            && let Some(challenge) = response.headers.get("www-authenticate")
        {
            self.authentication = parse_authentication(challenge);
            if self.authentication.is_some() {
                response = self.send_request(method, uri, &headers)?;
            }
        }
        Ok(response)
    }

    fn send_request(
        &mut self,
        method: &str,
        uri: &str,
        headers: &[(String, String)],
    ) -> Result<RtspResponse, String> {
        self.cseq = self.cseq.wrapping_add(1);
        let mut request = format!(
            "{method} {uri} RTSP/1.0\r\nCSeq: {}\r\nUser-Agent: onvif-backchannel-rs\r\n",
            self.cseq
        );
        for (name, value) in headers {
            request.push_str(&format!("{name}: {value}\r\n"));
        }
        if let Some(authorization) = self.authorization(method, uri) {
            request.push_str(&format!("Authorization: {authorization}\r\n"));
        }
        request.push_str("\r\n");
        self.stream
            .write_all(request.as_bytes())
            .map_err(|error| format!("RTSP request write failed: {error}"))?;
        self.read_response()
    }

    fn authorization(&mut self, method: &str, uri: &str) -> Option<String> {
        match self.authentication.as_mut()? {
            Authentication::Basic => Some(format!(
                "Basic {}",
                base64::engine::general_purpose::STANDARD
                    .encode(format!("{}:{}", self.user, self.password))
            )),
            Authentication::Digest(challenge) => {
                let ha1 = md5_hex(&format!(
                    "{}:{}:{}",
                    self.user, challenge.realm, self.password
                ));
                let ha2 = md5_hex(&format!("{method}:{uri}"));
                let mut fields = vec![
                    format!("username=\"{}\"", self.user),
                    format!("realm=\"{}\"", challenge.realm),
                    format!("nonce=\"{}\"", challenge.nonce),
                    format!("uri=\"{uri}\""),
                ];
                let response = if challenge.qop.as_deref() == Some("auth") {
                    if challenge.nonce_count == u32::MAX {
                        challenge.cnonce = format!("{:016x}", rand::random::<u64>());
                        challenge.nonce_count = 0;
                    }
                    challenge.nonce_count += 1;
                    let nonce_count = format!("{:08x}", challenge.nonce_count);
                    let response = md5_hex(&format!(
                        "{ha1}:{}:{nonce_count}:{}:auth:{ha2}",
                        challenge.nonce, challenge.cnonce
                    ));
                    fields.extend([
                        "qop=auth".to_owned(),
                        format!("nc={nonce_count}"),
                        format!("cnonce=\"{}\"", challenge.cnonce),
                    ]);
                    response
                } else {
                    md5_hex(&format!("{ha1}:{}:{ha2}", challenge.nonce))
                };
                fields.push(format!("response=\"{response}\""));
                if let Some(opaque) = &challenge.opaque {
                    fields.push(format!("opaque=\"{opaque}\""));
                }
                Some(format!("Digest {}", fields.join(", ")))
            }
        }
    }

    fn read_response(&mut self) -> Result<RtspResponse, String> {
        let deadline = Instant::now() + self.response_timeout;
        loop {
            if Instant::now() >= deadline {
                return Err("RTSP response deadline exceeded".to_owned());
            }
            while self.receive_buffer.first() == Some(&0x24) {
                if self.receive_buffer.len() < 4 {
                    self.read_more(deadline)?;
                    continue;
                }
                let frame_length =
                    u16::from_be_bytes([self.receive_buffer[2], self.receive_buffer[3]]) as usize;
                if self.receive_buffer.len() < frame_length + 4 {
                    self.read_more(deadline)?;
                    continue;
                }
                self.receive_buffer.drain(..frame_length + 4);
            }

            let Some(header_end) = find_bytes(&self.receive_buffer, b"\r\n\r\n") else {
                if self.receive_buffer.len() > MAX_RTSP_HEADER_BYTES {
                    return Err(format!(
                        "RTSP response header exceeds {MAX_RTSP_HEADER_BYTES} byte limit"
                    ));
                }
                self.read_more(deadline)?;
                continue;
            };
            if header_end > MAX_RTSP_HEADER_BYTES {
                return Err(format!(
                    "RTSP response header exceeds {MAX_RTSP_HEADER_BYTES} byte limit"
                ));
            }
            let header = String::from_utf8(self.receive_buffer[..header_end].to_vec())
                .map_err(|_| "RTSP response header is not UTF-8".to_owned())?;
            let mut lines = header.split("\r\n");
            let status_line = lines.next().unwrap_or_default().to_owned();
            let status = status_line
                .split_whitespace()
                .nth(1)
                .and_then(|value| value.parse::<u16>().ok())
                .ok_or_else(|| format!("invalid RTSP status line: {status_line}"))?;
            let mut headers = HashMap::new();
            for line in lines {
                if let Some((name, value)) = line.split_once(':') {
                    headers.insert(name.trim().to_ascii_lowercase(), value.trim().to_owned());
                }
            }
            let content_length = headers
                .get("content-length")
                .and_then(|value| value.parse::<usize>().ok())
                .unwrap_or(0);
            if content_length > MAX_RTSP_BODY_BYTES {
                return Err(format!(
                    "RTSP response body exceeds {MAX_RTSP_BODY_BYTES} byte limit"
                ));
            }
            let response_length = header_end + 4 + content_length;
            if self.receive_buffer.len() < response_length {
                self.read_more(deadline)?;
                continue;
            }
            let body = self.receive_buffer[header_end + 4..response_length].to_vec();
            self.receive_buffer.drain(..response_length);
            return Ok(RtspResponse {
                status,
                status_line,
                headers,
                body,
            });
        }
    }

    fn discard_complete_interleaved(&mut self) -> usize {
        let mut drained = 0;
        while self.receive_buffer.first() == Some(&0x24) && self.receive_buffer.len() >= 4 {
            let frame_length =
                u16::from_be_bytes([self.receive_buffer[2], self.receive_buffer[3]]) as usize;
            if self.receive_buffer.len() < frame_length + 4 {
                break;
            }
            self.receive_buffer.drain(..frame_length + 4);
            drained += 1;
        }
        drained
    }

    fn enforce_receive_buffer_limit(&self) -> Result<(), String> {
        if self.receive_buffer.len() > MAX_RTSP_RECEIVE_BUFFER_BYTES {
            Err(format!(
                "RTSP receive buffer exceeds {MAX_RTSP_RECEIVE_BUFFER_BYTES} byte limit"
            ))
        } else {
            Ok(())
        }
    }

    fn read_more(&mut self, deadline: Instant) -> Result<(), String> {
        let remaining = deadline
            .checked_duration_since(Instant::now())
            .filter(|remaining| !remaining.is_zero())
            .ok_or_else(|| "RTSP response deadline exceeded".to_owned())?;
        self.stream
            .set_read_timeout(Some(remaining))
            .map_err(|error| format!("failed to set RTSP response deadline: {error}"))?;
        let mut chunk = [0u8; 16 * 1024];
        let read = match self.stream.read(&mut chunk) {
            Ok(read) => read,
            Err(error)
                if matches!(
                    error.kind(),
                    std::io::ErrorKind::TimedOut | std::io::ErrorKind::WouldBlock
                ) =>
            {
                return Err("RTSP response deadline exceeded".to_owned());
            }
            Err(error) => return Err(format!("RTSP response read failed: {error}")),
        };
        if read == 0 {
            return Err("RTSP connection closed while reading a response".to_owned());
        }
        self.receive_buffer.extend_from_slice(&chunk[..read]);
        self.enforce_receive_buffer_limit()
    }
}

fn parse_interleaved_channel(transport: &str) -> Option<u8> {
    transport.split(';').find_map(|field| {
        field
            .trim()
            .strip_prefix("interleaved=")?
            .split('-')
            .next()?
            .parse()
            .ok()
    })
}

fn find_bytes(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack
        .windows(needle.len())
        .position(|window| window == needle)
}

fn md5_hex(value: &str) -> String {
    format!("{:x}", md5::compute(value.as_bytes()))
}

fn parse_authentication(header: &str) -> Option<Authentication> {
    if header
        .trim_start()
        .to_ascii_lowercase()
        .starts_with("basic")
        && !header.to_ascii_lowercase().contains("digest")
    {
        return Some(Authentication::Basic);
    }
    let digest_start = header.to_ascii_lowercase().find("digest")?;
    let digest = &header[digest_start + "digest".len()..];
    let realm = auth_parameter(digest, "realm")?;
    let nonce = auth_parameter(digest, "nonce")?;
    let qop = auth_parameter(digest, "qop").and_then(|value| {
        value
            .split(',')
            .map(str::trim)
            .find(|value| *value == "auth")
            .map(str::to_owned)
    });
    Some(Authentication::Digest(DigestChallenge {
        realm,
        nonce,
        qop,
        opaque: auth_parameter(digest, "opaque"),
        cnonce: format!("{:016x}", rand::random::<u64>()),
        nonce_count: 0,
    }))
}

fn auth_parameter(challenge: &str, key: &str) -> Option<String> {
    challenge.split(',').find_map(|field| {
        let (name, value) = field.trim().split_once('=')?;
        name.trim()
            .eq_ignore_ascii_case(key)
            .then(|| value.trim().trim_matches('"').trim_matches('\'').to_owned())
    })
}

#[cfg(test)]
mod tests {
    use std::collections::HashMap;
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::sync::{Arc, Mutex};
    use std::thread;
    use std::time::{Duration, Instant};

    use super::{BACKCHANNEL_REQUIRE, RtspClient};

    #[derive(Clone, Debug)]
    struct CapturedRequest {
        method: String,
        headers: HashMap<String, String>,
    }

    #[test]
    fn shares_one_session_for_setup_play_keepalive_and_teardown() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let captured = Arc::new(Mutex::new(Vec::<CapturedRequest>::new()));
        let server_captured = Arc::clone(&captured);
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut pending = Vec::new();
            loop {
                let mut chunk = [0u8; 4096];
                let read = stream.read(&mut chunk).unwrap();
                if read == 0 {
                    return;
                }
                pending.extend_from_slice(&chunk[..read]);
                while let Some(end) = pending.windows(4).position(|window| window == b"\r\n\r\n") {
                    let raw = String::from_utf8(pending.drain(..end + 4).collect()).unwrap();
                    let mut lines = raw.split("\r\n");
                    let method = lines
                        .next()
                        .unwrap()
                        .split_whitespace()
                        .next()
                        .unwrap()
                        .to_owned();
                    let mut headers = HashMap::new();
                    for line in lines.filter(|line| !line.is_empty()) {
                        if let Some((name, value)) = line.split_once(':') {
                            headers.insert(name.to_ascii_lowercase(), value.trim().to_owned());
                        }
                    }
                    let cseq = headers.get("cseq").unwrap().clone();
                    server_captured.lock().unwrap().push(CapturedRequest {
                        method: method.clone(),
                        headers,
                    });

                    if method == "SETUP" {
                        let setup_count = server_captured
                            .lock()
                            .unwrap()
                            .iter()
                            .filter(|request| request.method == "SETUP")
                            .count();
                        let channels = if setup_count == 1 { "0-1" } else { "2-3" };
                        write!(
                            stream,
                            "RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nSession: test-session;timeout=60\r\n\
                             Transport: RTP/AVP/TCP;unicast;interleaved={channels}\r\nContent-Length: 0\r\n\r\n"
                        )
                        .unwrap();
                    } else {
                        stream.write_all(&[0x24, 0, 0, 2, 0xaa, 0xbb]).unwrap();
                        write!(
                            stream,
                            "RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nContent-Length: 0\r\n\r\n"
                        )
                        .unwrap();
                    }
                    if method == "TEARDOWN" {
                        return;
                    }
                }
            }
        });

        let mut client =
            RtspClient::connect("127.0.0.1", port, "admin", "pass", Duration::from_secs(2))
                .unwrap();
        assert_eq!(client.options("rtsp://camera/live").unwrap().status, 200);
        assert_eq!(client.describe("rtsp://camera/live").unwrap().status, 200);
        let first = client.setup("rtsp://camera/trackID=0", 0, false).unwrap();
        let backchannel = client.setup("rtsp://camera/trackID=5", 2, true).unwrap();
        assert_eq!(first.rtp_channel, 0);
        assert_eq!(backchannel.rtp_channel, 2);
        assert_eq!(client.session_timeout(), Duration::from_secs(60));
        assert_eq!(client.play("rtsp://camera/live").unwrap().status, 200);
        assert_eq!(client.keep_alive("rtsp://camera/live").unwrap().status, 200);
        client.teardown("rtsp://camera/live").unwrap();
        server.join().unwrap();

        let requests = captured.lock().unwrap();
        assert_eq!(
            requests
                .iter()
                .map(|request| request.method.as_str())
                .collect::<Vec<_>>(),
            [
                "OPTIONS", "DESCRIBE", "SETUP", "SETUP", "PLAY", "OPTIONS", "TEARDOWN"
            ]
        );
        assert_eq!(requests[1].headers["accept"], "application/sdp");
        assert_eq!(requests[1].headers["require"], BACKCHANNEL_REQUIRE);
        assert!(!requests[2].headers.contains_key("session"));
        assert!(!requests[2].headers.contains_key("require"));
        assert_eq!(requests[3].headers["session"], "test-session");
        assert_eq!(requests[3].headers["require"], BACKCHANNEL_REQUIRE);
        assert_eq!(requests[4].headers["range"], "npt=now-");
        assert_eq!(requests[4].headers["require"], BACKCHANNEL_REQUIRE);
        assert_eq!(requests[5].headers["session"], "test-session");
    }

    #[test]
    fn increments_digest_nonce_count_across_authenticated_requests() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let authorizations = Arc::new(Mutex::new(Vec::new()));
        let server_authorizations = Arc::clone(&authorizations);
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut pending = Vec::new();
            for attempt in 0..3 {
                loop {
                    let mut chunk = [0u8; 4096];
                    let read = stream.read(&mut chunk).unwrap();
                    pending.extend_from_slice(&chunk[..read]);
                    let Some(end) = pending.windows(4).position(|window| window == b"\r\n\r\n")
                    else {
                        continue;
                    };
                    let raw = String::from_utf8(pending.drain(..end + 4).collect()).unwrap();
                    let cseq = raw
                        .lines()
                        .find_map(|line| line.strip_prefix("CSeq: "))
                        .unwrap()
                        .trim();
                    if attempt == 0 {
                        write!(
                            stream,
                            "RTSP/1.0 401 Unauthorized\r\nCSeq: {cseq}\r\n\
                             WWW-Authenticate: Digest realm=\"camera\", nonce=\"abcdef\", qop=\"auth\"\r\n\
                             Content-Length: 0\r\n\r\n"
                        )
                        .unwrap();
                    } else {
                        let auth = raw
                            .lines()
                            .find_map(|line| line.strip_prefix("Authorization: "))
                            .unwrap()
                            .trim()
                            .to_owned();
                        server_authorizations.lock().unwrap().push(auth);
                        write!(
                            stream,
                            "RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nContent-Length: 0\r\n\r\n"
                        )
                        .unwrap();
                    }
                    break;
                }
            }
        });

        let mut client =
            RtspClient::connect("127.0.0.1", port, "admin", "pass", Duration::from_secs(2))
                .unwrap();
        let response = client.options("rtsp://camera/live").unwrap();
        assert_eq!(response.status, 200);
        let response = client.describe("rtsp://camera/live").unwrap();
        assert_eq!(response.status, 200);
        server.join().unwrap();

        let authorizations = authorizations.lock().unwrap();
        assert_eq!(authorizations.len(), 2);
        for authorization in authorizations.iter() {
            assert!(authorization.starts_with("Digest "));
            assert!(authorization.contains("username=\"admin\""));
            assert!(authorization.contains("realm=\"camera\""));
            assert!(authorization.contains("nonce=\"abcdef\""));
            assert!(authorization.contains("uri=\"rtsp://camera/live\""));
            assert!(authorization.contains("qop=auth"));
            assert!(authorization.contains("response=\""));
            assert!(!authorization.contains("pass"));
        }
        assert!(authorizations[0].contains("nc=00000001"));
        assert!(authorizations[1].contains("nc=00000002"));
        let cnonce = |authorization: &str| {
            authorization
                .split("cnonce=\"")
                .nth(1)
                .unwrap()
                .split('"')
                .next()
                .unwrap()
                .to_owned()
        };
        assert_eq!(cnonce(&authorizations[0]), cnonce(&authorizations[1]));
    }

    #[test]
    fn drains_incoming_interleaved_media_while_sending_backchannel_rtp() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            stream
                .write_all(&[0x24, 0, 0, 2, 0xaa, 0xbb, 0x24, 1, 0, 3, 0xcc, 0xdd, 0xee])
                .unwrap();
            let mut sent = [0u8; 7];
            stream.read_exact(&mut sent).unwrap();
            sent
        });

        let mut client =
            RtspClient::connect("127.0.0.1", port, "admin", "pass", Duration::from_secs(2))
                .unwrap();
        thread::sleep(Duration::from_millis(20));
        assert_eq!(client.drain_interleaved().unwrap(), 2);
        client.send_interleaved(&[0x24, 6, 0, 3, 1, 2, 3]).unwrap();

        assert_eq!(server.join().unwrap(), [0x24, 6, 0, 3, 1, 2, 3]);
    }

    #[test]
    fn rejects_oversized_rtsp_response_headers() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0u8; 1024];
            let _ = stream.read(&mut request).unwrap();
            let padding = "x".repeat(64 * 1024);
            write!(
                stream,
                "RTSP/1.0 200 OK\r\nX-Padding: {padding}\r\nContent-Length: 0\r\n\r\n"
            )
            .unwrap();
        });

        let mut client =
            RtspClient::connect("127.0.0.1", port, "admin", "pass", Duration::from_secs(1))
                .unwrap();
        let error = client.options("rtsp://camera/live").unwrap_err();
        server.join().unwrap();

        assert!(error.contains("header exceeds 65536 byte limit"));
    }

    #[test]
    fn rejects_oversized_rtsp_response_bodies_before_reading_them() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0u8; 1024];
            let _ = stream.read(&mut request).unwrap();
            stream
                .write_all(b"RTSP/1.0 200 OK\r\nContent-Length: 1048577\r\n\r\n")
                .unwrap();
        });

        let mut client =
            RtspClient::connect("127.0.0.1", port, "admin", "pass", Duration::from_secs(1))
                .unwrap();
        let error = client.options("rtsp://camera/live").unwrap_err();
        server.join().unwrap();

        assert!(error.contains("body exceeds 1048576 byte limit"));
    }

    #[test]
    fn applies_one_deadline_to_the_complete_rtsp_response() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0u8; 1024];
            let _ = stream.read(&mut request).unwrap();
            for byte in b"RTSP/1.0 200 OK\r\nContent-Length: 0\r\n\r\n" {
                if stream.write_all(&[*byte]).is_err() {
                    break;
                }
                thread::sleep(Duration::from_millis(20));
            }
        });

        let mut client = RtspClient::connect(
            "127.0.0.1",
            port,
            "admin",
            "pass",
            Duration::from_millis(60),
        )
        .unwrap();
        let started = Instant::now();
        let response = client.options("rtsp://camera/live");
        let elapsed = started.elapsed();
        drop(client);
        server.join().unwrap();

        assert!(response.unwrap_err().contains("response deadline exceeded"));
        assert!(elapsed < Duration::from_millis(300));
    }
}
