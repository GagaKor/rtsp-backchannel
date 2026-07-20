use std::thread;
use std::time::{Duration, Instant};

use crate::audio::{AudioCodec, AudioFrame, CodecPreference, G711Variant, frame_g711};
use crate::onvif::OnvifDevice;
use crate::rtp::{PacingState, RtpPacketizer, interleave};
use crate::rtsp::{RtspClient, has_rtsp_scheme, sanitize_rtsp_uri};
use crate::sdp::{find_backchannel_audio, parse_sdp, pick_send_codec};

pub const SAMPLE_RATE: u64 = 8000;
pub const PACKET_MS: u64 = 40;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RtspTarget {
    pub uri: String,
    pub host: String,
    pub port: u16,
    pub user: String,
    pub password: String,
}

pub fn parse_rtsp_target(
    raw: &str,
    user_override: &str,
    password_override: &str,
) -> Result<RtspTarget, String> {
    if !has_rtsp_scheme(raw) {
        return Err("RTSP target must use the rtsp:// scheme".to_owned());
    }
    let (without_userinfo, uri_credentials) = split_raw_userinfo(raw)?;
    let uri = sanitize_rtsp_uri(&without_userinfo)?;
    let parsed = url::Url::parse(&uri).map_err(|_| "invalid RTSP target URI".to_owned())?;
    let host = parsed
        .host_str()
        .ok_or_else(|| "RTSP target URI has no host".to_owned())?
        .trim_start_matches('[')
        .trim_end_matches(']')
        .to_owned();
    let port = parsed.port().unwrap_or(554);
    if port == 0 {
        return Err("RTSP target port must be between 1 and 65535".to_owned());
    }
    let (uri_user, uri_password) = uri_credentials.unwrap_or_default();
    Ok(RtspTarget {
        uri,
        host,
        port,
        user: if user_override.is_empty() {
            uri_user
        } else {
            user_override.to_owned()
        },
        password: if password_override.is_empty() {
            uri_password
        } else {
            password_override.to_owned()
        },
    })
}

fn split_raw_userinfo(raw: &str) -> Result<(String, Option<(String, String)>), String> {
    let authority_start = 7;
    let authority_end = raw[authority_start..]
        .find(['/', '?', '#'])
        .map_or(raw.len(), |offset| authority_start + offset);
    let authority = &raw[authority_start..authority_end];
    let Some(at) = authority.rfind('@') else {
        return Ok((raw.to_owned(), None));
    };
    let userinfo = &authority[..at];
    let host = &authority[at + 1..];
    let (user, password) = userinfo.split_once(':').unwrap_or((userinfo, ""));
    let clean = format!("rtsp://{host}{}", &raw[authority_end..]);
    Ok((
        clean,
        Some((percent_decode(user)?, percent_decode(password)?)),
    ))
}

fn percent_decode(value: &str) -> Result<String, String> {
    let mut bytes = Vec::with_capacity(value.len());
    let mut chars = value.as_bytes().iter().copied();
    while let Some(byte) = chars.next() {
        if byte != b'%' {
            bytes.push(byte);
            continue;
        }
        let high = chars
            .next()
            .ok_or("invalid percent-encoded RTSP credential")?;
        let low = chars
            .next()
            .ok_or("invalid percent-encoded RTSP credential")?;
        let digit = |value: u8| match value {
            b'0'..=b'9' => Some(value - b'0'),
            b'a'..=b'f' => Some(value - b'a' + 10),
            b'A'..=b'F' => Some(value - b'A' + 10),
            _ => None,
        };
        bytes.push(
            (digit(high).ok_or("invalid percent-encoded RTSP credential")? << 4)
                | digit(low).ok_or("invalid percent-encoded RTSP credential")?,
        );
    }
    String::from_utf8(bytes).map_err(|_| "RTSP credential is not valid UTF-8".to_owned())
}

pub fn resolve_track_uri(base_uri: &str, content_base: Option<&str>, control: &str) -> String {
    if has_rtsp_scheme(control) {
        return sanitize_rtsp_uri(control).unwrap_or_else(|_| control.to_owned());
    }
    if control == "*" {
        return sanitize_rtsp_uri(base_uri).unwrap_or_else(|_| base_uri.to_owned());
    }
    let resolved = if let Some(content_base) = content_base {
        format!(
            "{}/{}",
            content_base.trim_end_matches('/'),
            control.trim_start_matches('/')
        )
    } else {
        url::Url::parse(base_uri)
            .and_then(|base| base.join(control))
            .map(String::from)
            .unwrap_or_else(|_| {
                format!(
                    "{}/{}",
                    base_uri.trim_end_matches('/'),
                    control.trim_start_matches('/')
                )
            })
    };
    sanitize_rtsp_uri(&resolved).unwrap_or(resolved)
}

pub fn rtsp_endpoint(uri: &str) -> Result<(String, u16), String> {
    let sanitized = sanitize_rtsp_uri(uri)?;
    let parsed = url::Url::parse(&sanitized).map_err(|_| "invalid RTSP URI".to_owned())?;
    if !parsed.scheme().eq_ignore_ascii_case("rtsp") {
        return Err("ONVIF returned a non-RTSP stream URI".to_owned());
    }
    let host = parsed
        .host_str()
        .ok_or("ONVIF RTSP URI has no host")?
        .trim_start_matches('[')
        .trim_end_matches(']')
        .to_owned();
    let port = parsed.port().unwrap_or(554);
    if port == 0 {
        return Err("RTSP URI port must be between 1 and 65535".to_owned());
    }
    Ok((host, port))
}

pub struct BackchannelSession {
    rtsp: RtspClient,
    stream_uri: String,
    packetizer: RtpPacketizer,
    keepalive_interval: Duration,
    next_keepalive: Instant,
    closed: bool,
    pub variant: Option<G711Variant>,
    pub codec: AudioCodec,
    pub clock_rate: u32,
    pub channels: u16,
    pub payload_type: u8,
    pub rtp_channel: u8,
}

impl BackchannelSession {
    pub fn open(host: &str, user: &str, password: &str) -> Result<Self, String> {
        Self::open_with_codec(host, user, password, CodecPreference::Auto)
    }

    pub fn open_with_codec(
        host: &str,
        user: &str,
        password: &str,
        preference: CodecPreference,
    ) -> Result<Self, String> {
        let target = if has_rtsp_scheme(host) {
            parse_rtsp_target(host, user, password)?
        } else {
            let mut device = OnvifDevice::new(host, user, password)?;
            device.connect()?;
            let profile = device
                .profile_tokens()?
                .into_iter()
                .next()
                .ok_or("ONVIF returned no media profile")?;
            parse_rtsp_target(&device.stream_uri(&profile)?, user, password)?
        };
        let mut rtsp = RtspClient::connect(
            &target.host,
            target.port,
            &target.user,
            &target.password,
            Duration::from_secs(8),
        )?;
        let stream_uri = target.uri;

        let established = (|| {
            let options = rtsp.options(&stream_uri)?;
            require_success("OPTIONS", options.status, &options.status_line)?;
            let describe = rtsp.describe(&stream_uri)?;
            require_success("DESCRIBE", describe.status, &describe.status_line)?;
            let sdp = String::from_utf8(describe.body)
                .map_err(|_| "RTSP DESCRIBE returned non-UTF-8 SDP".to_owned())?;
            let tracks = parse_sdp(&sdp);
            let send_index = tracks
                .iter()
                .position(|track| track.media == "audio" && track.direction == "sendonly")
                .ok_or("no sendonly audio backchannel track")?;
            let send_track =
                find_backchannel_audio(&tracks).ok_or("no sendonly audio backchannel track")?;
            let send_control = send_track
                .control
                .clone()
                .ok_or("backchannel track has no control URI")?;
            let codec = pick_send_codec(send_track, preference)?;
            let content_base = describe.headers.get("content-base").map(String::as_str);

            let mut requested_channel = 0u8;
            for (index, track) in tracks.iter().enumerate() {
                if index == send_index || track.direction != "recvonly" {
                    continue;
                }
                let Some(control) = &track.control else {
                    continue;
                };
                let uri = resolve_track_uri(&stream_uri, content_base, control);
                rtsp.setup(&uri, requested_channel, false)?;
                requested_channel = requested_channel
                    .checked_add(2)
                    .ok_or("too many RTSP interleaved tracks")?;
            }

            let send_uri = resolve_track_uri(&stream_uri, content_base, &send_control);
            let setup = rtsp.setup(&send_uri, requested_channel, true)?;
            let play = rtsp.play(&stream_uri)?;
            require_success("PLAY", play.status, &play.status_line)?;
            Ok::<_, String>((codec, setup.rtp_channel))
        })();

        let (codec, rtp_channel) = match established {
            Ok(value) => value,
            Err(error) => {
                let _ = rtsp.teardown(&stream_uri);
                return Err(error);
            }
        };
        let keepalive_interval = rtsp.session_timeout().div_f64(2.0);
        Ok(Self {
            rtsp,
            stream_uri,
            packetizer: RtpPacketizer::new_random(codec.payload_type),
            keepalive_interval,
            next_keepalive: Instant::now() + keepalive_interval,
            closed: false,
            variant: g711_variant(codec.codec),
            codec: codec.codec,
            clock_rate: codec.clock_rate,
            channels: codec.channels,
            payload_type: codec.payload_type,
            rtp_channel,
        })
    }

    pub fn send(&mut self, g711: &[u8]) -> Result<usize, String> {
        if !matches!(self.codec, AudioCodec::Pcma | AudioCodec::Pcmu) {
            return Err("legacy G.711 send requires a negotiated G.711 codec".to_owned());
        }
        self.send_frames(&frame_g711(g711)?)
    }

    pub fn send_frames(&mut self, frames: &[AudioFrame]) -> Result<usize, String> {
        let start = Instant::now();
        let mut pacing = PacingState::new(0);
        let mut sent = 0usize;
        for frame in frames {
            sleep_until(start, pacing.deadline_ns());
            self.rtsp.drain_interleaved()?;
            if self.keepalive_wait().is_zero() {
                self.keep_alive()?;
            }

            let actual_ns = elapsed_ns(start);
            let duration_ns =
                (u64::from(frame.samples) * 1_000_000_000) / u64::from(self.clock_rate);
            pacing.register_send(actual_ns, duration_ns);
            let rtp =
                self.packetizer
                    .build_with_marker(&frame.payload, frame.samples, frame.marker);
            let frame = interleave(self.rtp_channel, &rtp);
            self.rtsp.send_interleaved(&frame)?;
            sent += 1;
        }
        if sent > 0 {
            sleep_until(start, pacing.deadline_ns());
        }
        Ok(sent)
    }

    pub(crate) fn keepalive_wait(&self) -> Duration {
        self.next_keepalive
            .saturating_duration_since(Instant::now())
    }

    pub(crate) fn keep_alive(&mut self) -> Result<(), String> {
        self.rtsp.drain_interleaved()?;
        let response = self.rtsp.keep_alive(&self.stream_uri)?;
        require_success("RTSP keepalive", response.status, &response.status_line)?;
        self.next_keepalive = Instant::now() + self.keepalive_interval;
        Ok(())
    }

    pub fn close(&mut self) -> Result<(), String> {
        if self.closed {
            return Ok(());
        }
        self.closed = true;
        self.rtsp.teardown(&self.stream_uri)
    }
}

const fn g711_variant(codec: AudioCodec) -> Option<G711Variant> {
    match codec {
        AudioCodec::Pcma => Some(G711Variant::Pcma),
        AudioCodec::Pcmu => Some(G711Variant::Pcmu),
        AudioCodec::G72616
        | AudioCodec::G72624
        | AudioCodec::G72632
        | AudioCodec::G72640
        | AudioCodec::Aac => None,
    }
}

fn require_success(context: &str, status: u16, status_line: &str) -> Result<(), String> {
    if status == 200 {
        Ok(())
    } else {
        Err(format!("{context} failed: {status_line}"))
    }
}

fn elapsed_ns(start: Instant) -> u64 {
    u64::try_from(start.elapsed().as_nanos()).unwrap_or(u64::MAX)
}

fn sleep_until(start: Instant, deadline_ns: u64) {
    loop {
        let now_ns = elapsed_ns(start);
        if now_ns >= deadline_ns {
            return;
        }
        thread::sleep(Duration::from_nanos(deadline_ns - now_ns));
    }
}

#[cfg(test)]
mod tests {
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::thread;

    use super::{resolve_track_uri, rtsp_endpoint};

    #[test]
    fn reports_a_g711_variant_only_for_g711_codecs() {
        assert_eq!(
            super::g711_variant(crate::audio::AudioCodec::Pcma),
            Some(crate::audio::G711Variant::Pcma)
        );
        assert_eq!(
            super::g711_variant(crate::audio::AudioCodec::Pcmu),
            Some(crate::audio::G711Variant::Pcmu)
        );
        for codec in [
            crate::audio::AudioCodec::G72616,
            crate::audio::AudioCodec::G72624,
            crate::audio::AudioCodec::G72632,
            crate::audio::AudioCodec::G72640,
            crate::audio::AudioCodec::Aac,
        ] {
            assert_eq!(super::g711_variant(codec), None);
        }
    }

    #[test]
    fn resolves_relative_and_absolute_track_controls() {
        assert_eq!(
            resolve_track_uri(
                "rtsp://camera/video/livemedia?Ch=1",
                Some("rtsp://camera/video/livemedia/"),
                "trackID=5"
            ),
            "rtsp://camera/video/livemedia/trackID=5"
        );
        assert_eq!(
            resolve_track_uri("rtsp://camera/live", None, "rtsp://other/track"),
            "rtsp://other/track"
        );
        assert_eq!(
            resolve_track_uri(
                "rtsp://camera/video/livemedia?Ch=1&Streamtype=0",
                None,
                "trackID=5"
            ),
            "rtsp://camera/video/trackID=5"
        );
    }

    #[test]
    fn extracts_the_rtsp_host_and_default_or_explicit_port() {
        assert_eq!(
            rtsp_endpoint("rtsp://admin:pass@camera/live").unwrap(),
            ("camera".to_owned(), 554)
        );
        assert_eq!(
            rtsp_endpoint("rtsp://10.0.0.1:8554/live").unwrap(),
            ("10.0.0.1".to_owned(), 8554)
        );
    }

    #[test]
    fn parses_direct_targets_using_the_final_at_and_percent_decodes_credentials() {
        let target =
            super::parse_rtsp_target("rtsp://user:p%40ss@word@[::1]:8554/live#secret", "", "")
                .unwrap();
        assert_eq!(target.user, "user");
        assert_eq!(target.password, "p@ss@word");
        assert_eq!(target.host, "::1");
        assert_eq!(target.port, 8554);
        assert_eq!(target.uri, "rtsp://[::1]:8554/live");
        assert!(super::parse_rtsp_target("rtsp://camera:0/live", "", "").is_err());
    }

    #[test]
    fn explicit_non_empty_credentials_override_direct_target_fields_independently() {
        let target =
            super::parse_rtsp_target("rtsp://url-user:url-pass@camera/live", "cli-user", "")
                .unwrap();
        assert_eq!(target.user, "cli-user");
        assert_eq!(target.password, "url-pass");
    }

    #[test]
    fn strips_credentials_and_fragments_from_track_resolution() {
        assert_eq!(
            resolve_track_uri(
                "rtsp://user:pass@camera/live#fragment",
                Some("rtsp://u:p@camera/live/"),
                "trackID=5#ignored"
            ),
            "rtsp://camera/live/trackID=5"
        );

        assert_eq!(
            resolve_track_uri(
                "rtsp://camera/live",
                Some("rtsp://camera/base/"),
                "RTSP://control-user:control-pass@other/track#ignored"
            ),
            "rtsp://other/track"
        );
    }

    #[test]
    fn opens_and_tears_down_a_full_direct_rtsp_backchannel_without_onvif() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let sdp = "v=0\r\n\
                       m=audio 0 RTP/AVP 8\r\n\
                       a=sendonly\r\n\
                       a=control:trackID=5\r\n\
                       a=rtpmap:8 PCMA/8000\r\n";
            for response in [
                "RTSP/1.0 200 OK\r\nContent-Length: 0\r\n\r\n".to_owned(),
                format!(
                    "RTSP/1.0 200 OK\r\nContent-Type: application/sdp\r\nContent-Length: {}\r\n\r\n{sdp}",
                    sdp.len()
                ),
                "RTSP/1.0 200 OK\r\nSession: direct-test;timeout=60\r\nTransport: RTP/AVP/TCP;interleaved=0-1\r\nContent-Length: 0\r\n\r\n".to_owned(),
                "RTSP/1.0 200 OK\r\nContent-Length: 0\r\n\r\n".to_owned(),
            ] {
                let request = read_request(&mut stream);
                assert!(!request.contains("url-pass"));
                let cseq = request
                    .lines()
                    .find_map(|line| line.strip_prefix("CSeq: "))
                    .unwrap();
                let response = response.replacen(
                    "RTSP/1.0 200 OK\r\n",
                    &format!("RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\n"),
                    1,
                );
                stream.write_all(response.as_bytes()).unwrap();
            }
            let mut header = [0u8; 4];
            stream.read_exact(&mut header).unwrap();
            assert_eq!(header[..2], [0x24, 0]);
            let length = u16::from_be_bytes([header[2], header[3]]) as usize;
            let mut rtp = vec![0; length];
            stream.read_exact(&mut rtp).unwrap();
            assert_eq!(rtp[1] & 0x80, 0x80);
            assert_eq!(rtp[1] & 0x7f, 8);
            let teardown = read_request(&mut stream);
            assert!(teardown.starts_with("TEARDOWN rtsp://127.0.0.1:"));
            let cseq = teardown
                .lines()
                .find_map(|line| line.strip_prefix("CSeq: "))
                .unwrap();
            write!(
                stream,
                "RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nContent-Length: 0\r\n\r\n"
            )
            .unwrap();
        });

        let target = format!("rtsp://url-user:url-pass@127.0.0.1:{port}/live");
        let mut session = super::super::backchannel::BackchannelSession::open_with_codec(
            &target,
            "",
            "",
            crate::audio::CodecPreference::Pcma,
        )
        .unwrap();
        assert_eq!(session.codec, crate::audio::AudioCodec::Pcma);
        assert_eq!(session.send(&[0xaa]).unwrap(), 1);
        session.close().unwrap();
        server.join().unwrap();
    }

    fn read_request(stream: &mut impl Read) -> String {
        let mut request = Vec::new();
        let mut byte = [0u8; 1];
        while !request.ends_with(b"\r\n\r\n") {
            stream.read_exact(&mut byte).unwrap();
            request.push(byte[0]);
        }
        String::from_utf8(request).unwrap()
    }
}
