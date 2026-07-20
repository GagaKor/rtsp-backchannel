use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::{Shutdown, TcpStream};
use std::time::{Duration, Instant};

use base64::Engine;

pub const BACKCHANNEL_REQUIRE: &str = "www.onvif.org/ver20/backchannel";
const MAX_RTSP_HEADER_BYTES: usize = 64 * 1024;
const MAX_RTSP_BODY_BYTES: usize = 1024 * 1024;
const MAX_RTSP_RECEIVE_BUFFER_BYTES: usize = MAX_RTSP_HEADER_BYTES + 4 + MAX_RTSP_BODY_BYTES;
const MAX_RTSP_DRAIN_BYTES_PER_CALL: usize = 256 * 1024;
const MAX_RTSP_DRAIN_DURATION: Duration = Duration::from_millis(10);

pub fn has_rtsp_scheme(value: &str) -> bool {
    value
        .get(..7)
        .is_some_and(|scheme| scheme.eq_ignore_ascii_case("rtsp://"))
}

pub fn sanitize_rtsp_uri(uri: &str) -> Result<String, String> {
    let mut parsed = url::Url::parse(uri).map_err(|_| "invalid RTSP URI".to_owned())?;
    if !parsed.scheme().eq_ignore_ascii_case("rtsp") {
        return Err("RTSP URI must use the rtsp:// scheme".to_owned());
    }
    parsed
        .set_username("")
        .map_err(|_| "invalid RTSP URI userinfo".to_owned())?;
    parsed
        .set_password(None)
        .map_err(|_| "invalid RTSP URI password".to_owned())?;
    parsed.set_fragment(None);
    Ok(parsed.to_string())
}

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
    usable: bool,
}

impl RtspClient {
    pub fn connect(
        host: &str,
        port: u16,
        user: &str,
        password: &str,
        timeout: Duration,
    ) -> Result<Self, String> {
        if port == 0 {
            return Err("RTSP port must be between 1 and 65535".to_owned());
        }
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
            usable: true,
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
            if let Some(seconds) = field
                .trim()
                .strip_prefix("timeout=")
                .and_then(|value| value.parse::<u64>().ok())
                .filter(|seconds| *seconds > 0)
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
        if !self.usable {
            return Err("RTSP connection is unusable after a protocol or I/O error".to_owned());
        }
        if let Err(error) = self.enforce_receive_buffer_limit() {
            self.invalidate_connection();
            return Err(error);
        }
        if let Err(error) = self.stream.set_nonblocking(true) {
            self.invalidate_connection();
            return Err(format!("failed to enable nonblocking RTSP reads: {error}"));
        }
        let deadline = Instant::now() + MAX_RTSP_DRAIN_DURATION;
        let result = (|| {
            let mut discard_budget = MAX_RTSP_DRAIN_BYTES_PER_CALL;
            let mut read_budget = MAX_RTSP_DRAIN_BYTES_PER_CALL;
            let mut drained =
                self.discard_complete_interleaved_bounded(&mut discard_budget, deadline);
            let mut chunk = [0u8; 16 * 1024];
            while read_budget > 0 && discard_budget > 0 && Instant::now() < deadline {
                let read_limit = chunk.len().min(read_budget);
                match self.stream.read(&mut chunk[..read_limit]) {
                    Ok(0) => break,
                    Ok(read) => {
                        read_budget -= read;
                        self.receive_buffer.extend_from_slice(&chunk[..read]);
                        drained += self
                            .discard_complete_interleaved_bounded(&mut discard_budget, deadline);
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
        let outcome = match (result, restore) {
            (Err(error), _) => Err(error),
            (Ok(_), Err(error)) => Err(error),
            (Ok(drained), Ok(())) => Ok(drained),
        };
        if outcome.is_err() {
            self.invalidate_connection();
        }
        outcome
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
        if response.status == 401 {
            if let Some(challenge) = response.headers.get("www-authenticate") {
                self.authentication = Some(parse_authentication(challenge)?);
                response = self.send_request(method, uri, &headers)?;
            } else {
                return Err(
                    "RTSP server returned 401 without a supported authentication challenge"
                        .to_owned(),
                );
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
        if !self.usable {
            return Err("RTSP connection is unusable after a protocol or I/O error".to_owned());
        }
        let uri = sanitize_rtsp_uri(uri)?;
        self.cseq = self.cseq.wrapping_add(1);
        let expected_cseq = self.cseq;
        let mut request = format!(
            "{method} {uri} RTSP/1.0\r\nCSeq: {}\r\nUser-Agent: rtsp-backchannel-rs\r\n",
            self.cseq
        );
        for (name, value) in headers {
            request.push_str(&format!("{name}: {value}\r\n"));
        }
        if let Some(authorization) = self.authorization(method, &uri) {
            request.push_str(&format!("Authorization: {authorization}\r\n"));
        }
        request.push_str("\r\n");
        if let Err(error) = self.stream.write_all(request.as_bytes()) {
            self.invalidate_connection();
            return Err(format!("RTSP request write failed: {error}"));
        }
        match self.read_response(expected_cseq) {
            Ok(response) => Ok(response),
            Err(error) => {
                self.invalidate_connection();
                Err(error)
            }
        }
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

    fn read_response(&mut self, expected_cseq: u32) -> Result<RtspResponse, String> {
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
            let mut content_length = None;
            let mut response_cseq = None;
            for line in lines {
                let (name, value) = line
                    .split_once(':')
                    .ok_or_else(|| "invalid RTSP response header field".to_owned())?;
                let name = name.trim().to_ascii_lowercase();
                let value = value.trim();
                if name == "content-length" {
                    let parsed = parse_decimal_header(value, "Content-Length")?;
                    let parsed = usize::try_from(parsed)
                        .map_err(|_| "invalid RTSP Content-Length header".to_owned())?;
                    if content_length.is_some_and(|existing| existing != parsed) {
                        return Err("conflicting RTSP Content-Length headers".to_owned());
                    }
                    content_length = Some(parsed);
                } else if name == "cseq" {
                    let parsed = parse_decimal_header(value, "CSeq")?;
                    let parsed =
                        u32::try_from(parsed).map_err(|_| "invalid RTSP CSeq header".to_owned())?;
                    if response_cseq.is_some_and(|existing| existing != parsed) {
                        return Err("conflicting RTSP CSeq headers".to_owned());
                    }
                    response_cseq = Some(parsed);
                }
                headers.insert(name, value.to_owned());
            }
            let content_length = content_length.unwrap_or(0);
            if content_length > MAX_RTSP_BODY_BYTES {
                return Err(format!(
                    "RTSP response body exceeds {MAX_RTSP_BODY_BYTES} byte limit"
                ));
            }
            let response_cseq = response_cseq.ok_or("RTSP response has no CSeq header")?;
            if response_cseq != expected_cseq {
                return Err(format!(
                    "RTSP response CSeq {response_cseq} does not match request CSeq {expected_cseq}"
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

    fn invalidate_connection(&mut self) {
        self.usable = false;
        self.session = None;
        self.receive_buffer.clear();
        let _ = self.stream.shutdown(Shutdown::Both);
    }

    fn discard_complete_interleaved_bounded(
        &mut self,
        remaining_bytes: &mut usize,
        deadline: Instant,
    ) -> usize {
        let mut consumed = 0usize;
        let mut drained = 0;
        while self.receive_buffer.get(consumed) == Some(&0x24)
            && self.receive_buffer.len().saturating_sub(consumed) >= 4
            && Instant::now() < deadline
        {
            let frame_length = u16::from_be_bytes([
                self.receive_buffer[consumed + 2],
                self.receive_buffer[consumed + 3],
            ]) as usize;
            let frame_bytes = frame_length + 4;
            if self.receive_buffer.len().saturating_sub(consumed) < frame_bytes
                || frame_bytes > *remaining_bytes
            {
                break;
            }
            consumed += frame_bytes;
            *remaining_bytes -= frame_bytes;
            drained += 1;
        }
        if consumed > 0 {
            self.receive_buffer.drain(..consumed);
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

fn parse_decimal_header(value: &str, name: &str) -> Result<u64, String> {
    if value.is_empty() || !value.bytes().all(|byte| byte.is_ascii_digit()) {
        return Err(format!("invalid RTSP {name} header"));
    }
    value
        .parse()
        .map_err(|_| format!("invalid RTSP {name} header"))
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

fn parse_authentication(header: &str) -> Result<Authentication, String> {
    let header = header.trim();
    if strip_auth_scheme(header, "Basic").is_some() {
        return Ok(Authentication::Basic);
    }
    let digest = strip_auth_scheme(header, "Digest")
        .ok_or_else(|| "unsupported RTSP authentication challenge".to_owned())?;
    let parameters = parse_auth_parameters(digest)?;
    let realm = parameters
        .get("realm")
        .cloned()
        .ok_or_else(|| "unsupported RTSP digest challenge: missing realm".to_owned())?;
    let nonce = parameters
        .get("nonce")
        .cloned()
        .ok_or_else(|| "unsupported RTSP digest challenge: missing nonce".to_owned())?;
    if parameters
        .get("algorithm")
        .is_some_and(|algorithm| !algorithm.eq_ignore_ascii_case("MD5"))
    {
        return Err("unsupported RTSP digest algorithm; only MD5 is supported".to_owned());
    }
    let qop_value = parameters.get("qop");
    let qop = qop_value.and_then(|value| {
        value
            .split(',')
            .map(str::trim)
            .find(|value| value.eq_ignore_ascii_case("auth"))
            .map(|_| "auth".to_owned())
    });
    if qop_value.is_some() && qop.is_none() {
        return Err("unsupported RTSP digest qop; only auth is supported".to_owned());
    }
    Ok(Authentication::Digest(DigestChallenge {
        realm,
        nonce,
        qop,
        opaque: parameters.get("opaque").cloned(),
        cnonce: format!("{:016x}", rand::random::<u64>()),
        nonce_count: 0,
    }))
}

fn strip_auth_scheme<'a>(header: &'a str, scheme: &str) -> Option<&'a str> {
    let prefix = header.get(..scheme.len())?;
    if !prefix.eq_ignore_ascii_case(scheme) {
        return None;
    }
    let rest = &header[scheme.len()..];
    (rest.is_empty() || rest.starts_with(char::is_whitespace)).then(|| rest.trim_start())
}

fn parse_auth_parameters(challenge: &str) -> Result<HashMap<String, String>, String> {
    let mut fields = Vec::new();
    let mut start = 0usize;
    let mut quoted = false;
    let mut escaped = false;
    for (index, character) in challenge.char_indices() {
        if escaped {
            escaped = false;
        } else if quoted && character == '\\' {
            escaped = true;
        } else if character == '"' {
            quoted = !quoted;
        } else if character == ',' && !quoted {
            fields.push(&challenge[start..index]);
            start = index + character.len_utf8();
        }
    }
    if quoted || escaped {
        return Err("malformed quoted RTSP authentication parameter".to_owned());
    }
    fields.push(&challenge[start..]);

    let mut parameters = HashMap::new();
    for field in fields {
        let (name, raw_value) = field
            .trim()
            .split_once('=')
            .ok_or_else(|| "malformed RTSP authentication parameter".to_owned())?;
        let name = name.trim().to_ascii_lowercase();
        if name.is_empty() {
            return Err("malformed RTSP authentication parameter name".to_owned());
        }
        let value = parse_auth_parameter_value(raw_value.trim())?;
        if parameters.insert(name, value).is_some() {
            return Err("duplicate RTSP authentication parameter".to_owned());
        }
    }
    Ok(parameters)
}

fn parse_auth_parameter_value(value: &str) -> Result<String, String> {
    if !value.starts_with('"') {
        if value.is_empty() || value.contains('"') {
            return Err("malformed RTSP authentication parameter value".to_owned());
        }
        return Ok(value.to_owned());
    }
    if value.len() < 2 || !value.ends_with('"') {
        return Err("malformed quoted RTSP authentication parameter".to_owned());
    }
    let mut parsed = String::new();
    let mut escaped = false;
    for character in value[1..value.len() - 1].chars() {
        if escaped {
            parsed.push(character);
            escaped = false;
        } else if character == '\\' {
            escaped = true;
        } else if character == '"' {
            return Err("malformed quoted RTSP authentication parameter".to_owned());
        } else {
            parsed.push(character);
        }
    }
    if escaped {
        return Err("malformed quoted RTSP authentication parameter".to_owned());
    }
    Ok(parsed)
}

#[cfg(test)]
mod tests {
    use std::collections::HashMap;
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::sync::{Arc, Mutex};
    use std::thread;
    use std::time::{Duration, Instant};

    use super::{
        Authentication, BACKCHANNEL_REQUIRE, RtspClient, parse_authentication, sanitize_rtsp_uri,
    };

    #[derive(Clone, Debug)]
    struct CapturedRequest {
        method: String,
        headers: HashMap<String, String>,
    }

    #[test]
    fn rejects_zero_port_before_attempting_a_socket_connection() {
        let error = match RtspClient::connect("127.0.0.1", 0, "", "", Duration::from_secs(1)) {
            Ok(_) => panic!("port zero unexpectedly connected"),
            Err(error) => error,
        };
        assert!(error.contains("port must be between 1 and 65535"));
    }

    #[test]
    fn strips_uri_credentials_and_fragments_before_any_request_or_digest() {
        assert_eq!(
            sanitize_rtsp_uri("rtsp://user:pass@camera/live#secret").unwrap(),
            "rtsp://camera/live"
        );
        assert!(
            parse_authentication("Digest realm=\"camera\", nonce=\"abc\", qop=\"auth-int\"")
                .is_err()
        );
        assert!(parse_authentication("Bearer realm=\"camera\"").is_err());
    }

    #[test]
    fn parses_quoted_digest_commas_and_selects_auth_from_the_qop_list() {
        let authentication = parse_authentication(
            "Digest realm=\"cam,era\", nonce=\"abc,def\", algorithm=MD5, \
             qop=\"auth-int,AUTH\", opaque=\"left,right\"",
        )
        .unwrap();

        let Authentication::Digest(challenge) = authentication else {
            panic!("expected digest authentication");
        };
        assert_eq!(challenge.realm, "cam,era");
        assert_eq!(challenge.nonce, "abc,def");
        assert_eq!(challenge.qop.as_deref(), Some("auth"));
        assert_eq!(challenge.opaque.as_deref(), Some("left,right"));
    }

    #[test]
    fn rejects_unsupported_digest_algorithms_and_qop_modes() {
        for challenge in [
            "Digest realm=\"camera\", nonce=\"abc\", algorithm=MD5-sess",
            "Digest realm=\"camera\", nonce=\"abc\", algorithm=SHA-256",
            "Digest realm=\"camera\", nonce=\"abc\", qop=\"auth-int\"",
        ] {
            assert!(parse_authentication(challenge).is_err(), "{challenge}");
        }
        assert!(
            parse_authentication("Digest realm=\"camera\", nonce=\"abc\", algorithm=MD5").is_ok()
        );
        assert!(parse_authentication("Digest realm=\"camera\", nonce=\"abc\"").is_ok());
    }

    #[test]
    fn computes_the_rfc2617_md5_auth_response_deterministically() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let mut client = RtspClient::connect(
            "127.0.0.1",
            port,
            "Mufasa",
            "Circle Of Life",
            Duration::from_secs(1),
        )
        .unwrap();
        let (server_stream, _) = listener.accept().unwrap();
        let mut authentication = parse_authentication(
            "Digest realm=\"testrealm@host.com\", \
             nonce=\"dcd98b7102dd2f0e8b11d0f600bfb0c093\", \
             algorithm=MD5, qop=\"auth-int,auth\"",
        )
        .unwrap();
        let Authentication::Digest(challenge) = &mut authentication else {
            panic!("expected digest authentication");
        };
        challenge.cnonce = "0a4f113b".to_owned();
        client.authentication = Some(authentication);

        let authorization = client.authorization("GET", "/dir/index.html").unwrap();

        assert!(authorization.contains("qop=auth"));
        assert!(authorization.contains("nc=00000001"));
        assert!(authorization.contains("cnonce=\"0a4f113b\""));
        assert!(authorization.contains("response=\"6629fae49393a05397450978507c4ef1\""));

        client.authentication = Some(
            parse_authentication(
                "Digest realm=\"testrealm@host.com\", \
                 nonce=\"dcd98b7102dd2f0e8b11d0f600bfb0c093\", algorithm=MD5",
            )
            .unwrap(),
        );
        let authorization = client.authorization("GET", "/dir/index.html").unwrap();
        assert!(authorization.contains("response=\"670fd8c2df070c60b045671b8b24ff02\""));
        assert!(!authorization.contains("qop="));
        assert!(!authorization.contains("nc="));
        assert!(!authorization.contains("cnonce="));
        drop(server_stream);
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
    fn bounds_interleaved_drain_work_when_the_peer_writes_continuously() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            stream
                .set_write_timeout(Some(Duration::from_millis(50)))
                .unwrap();
            let frame = [0x24, 0, 0, 4, 0xaa, 0xbb, 0xcc, 0xdd];
            let deadline = Instant::now() + Duration::from_millis(500);
            while Instant::now() < deadline && stream.write_all(&frame).is_ok() {}
        });

        let mut client =
            RtspClient::connect("127.0.0.1", port, "", "", Duration::from_secs(1)).unwrap();
        thread::sleep(Duration::from_millis(20));
        let started = Instant::now();
        let drained = client.drain_interleaved().unwrap();
        let elapsed = started.elapsed();
        drop(client);
        server.join().unwrap();

        assert!(drained > 0);
        assert!(elapsed < Duration::from_millis(200), "{elapsed:?}");
    }

    #[test]
    fn limits_buffered_interleaved_bytes_processed_by_one_drain_step() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let mut client =
            RtspClient::connect("127.0.0.1", port, "", "", Duration::from_secs(1)).unwrap();
        let (server_stream, _) = listener.accept().unwrap();
        let frame = [0x24, 0, 0, 4, 0xaa, 0xbb, 0xcc, 0xdd];
        let frame_count = 40_000;
        client.receive_buffer = frame.repeat(frame_count);

        let mut budget = super::MAX_RTSP_DRAIN_BYTES_PER_CALL;
        let drained = client.discard_complete_interleaved_bounded(
            &mut budget,
            Instant::now() + super::MAX_RTSP_DRAIN_DURATION,
        );

        assert!(drained > 0);
        assert!(drained < frame_count);
        assert!(!client.receive_buffer.is_empty());
        drop(server_stream);
    }

    #[test]
    fn closes_the_connection_when_the_interleaved_receive_buffer_exceeds_its_cap() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let mut client =
            RtspClient::connect("127.0.0.1", port, "", "", Duration::from_secs(1)).unwrap();
        let (mut server_stream, _) = listener.accept().unwrap();
        server_stream
            .set_read_timeout(Some(Duration::from_millis(250)))
            .unwrap();
        client.receive_buffer = vec![b'R'; super::MAX_RTSP_RECEIVE_BUFFER_BYTES + 1];

        let error = client.drain_interleaved().unwrap_err();
        let second = client.options("rtsp://camera/live").unwrap_err();
        let mut request = [0u8; 1];
        let peer_saw_eof = matches!(server_stream.read(&mut request), Ok(0));

        assert!(error.contains("receive buffer"));
        assert!(second.contains("unusable"));
        assert!(peer_saw_eof);
    }

    #[test]
    fn rejects_invalid_negative_and_conflicting_content_lengths() {
        for content_length in [
            "Content-Length: invalid\r\n".to_owned(),
            "Content-Length: -1\r\n".to_owned(),
            "Content-Length: 1\r\nContent-Length: 0\r\n".to_owned(),
        ] {
            let listener = TcpListener::bind("127.0.0.1:0").unwrap();
            let port = listener.local_addr().unwrap().port();
            let server = thread::spawn(move || {
                let (mut stream, _) = listener.accept().unwrap();
                let request = read_rtsp_request(&mut stream);
                let cseq = request_cseq(&request);
                write!(
                    stream,
                    "RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\n{content_length}\r\n"
                )
                .unwrap();
            });

            let mut client =
                RtspClient::connect("127.0.0.1", port, "", "", Duration::from_secs(1)).unwrap();
            let error = client.options("rtsp://camera/live").unwrap_err();
            server.join().unwrap();

            assert!(error.contains("Content-Length"), "{error}");
        }
    }

    #[test]
    fn rejects_missing_or_mismatched_response_cseq() {
        for response_cseq in [None, Some(99)] {
            let listener = TcpListener::bind("127.0.0.1:0").unwrap();
            let port = listener.local_addr().unwrap().port();
            let server = thread::spawn(move || {
                let (mut stream, _) = listener.accept().unwrap();
                let _ = read_rtsp_request(&mut stream);
                let cseq =
                    response_cseq.map_or_else(String::new, |value| format!("CSeq: {value}\r\n"));
                write!(stream, "RTSP/1.0 200 OK\r\n{cseq}Content-Length: 0\r\n\r\n").unwrap();
            });

            let mut client =
                RtspClient::connect("127.0.0.1", port, "", "", Duration::from_secs(1)).unwrap();
            let error = client.options("rtsp://camera/live").unwrap_err();
            server.join().unwrap();

            assert!(error.contains("CSeq"), "{error}");
        }
    }

    #[test]
    fn closes_the_connection_after_a_response_framing_error() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let request = read_rtsp_request(&mut stream);
            let cseq = request_cseq(&request);
            write!(
                stream,
                "RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nContent-Length: invalid\r\n\r\n"
            )
            .unwrap();
            stream
                .set_read_timeout(Some(Duration::from_millis(250)))
                .unwrap();
            let mut next_request = [0u8; 1024];
            matches!(stream.read(&mut next_request), Ok(0))
        });

        let mut client =
            RtspClient::connect("127.0.0.1", port, "", "", Duration::from_secs(1)).unwrap();
        assert!(client.options("rtsp://camera/live").is_err());
        let second = client.options("rtsp://camera/live").unwrap_err();
        let peer_saw_eof = server.join().unwrap();

        assert!(second.contains("unusable"));
        assert!(peer_saw_eof);
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

    fn read_rtsp_request(stream: &mut impl Read) -> String {
        let mut request = Vec::new();
        let mut byte = [0u8; 1];
        while !request.ends_with(b"\r\n\r\n") {
            stream.read_exact(&mut byte).unwrap();
            request.push(byte[0]);
        }
        String::from_utf8(request).unwrap()
    }

    fn request_cseq(request: &str) -> u32 {
        request
            .lines()
            .find_map(|line| line.strip_prefix("CSeq: "))
            .unwrap()
            .trim()
            .parse()
            .unwrap()
    }
}
