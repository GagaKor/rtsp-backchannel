use std::thread;
use std::time::{Duration, Instant};

use crate::audio::G711Variant;
use crate::onvif::OnvifDevice;
use crate::rtp::{PacingState, RtpPacketizer, interleave};
use crate::rtsp::RtspClient;
use crate::sdp::{find_backchannel_audio, parse_sdp, pick_g711_codec};

pub const SAMPLE_RATE: u64 = 8000;
pub const PACKET_MS: u64 = 40;
const SAMPLES_PER_PACKET: usize = (SAMPLE_RATE * PACKET_MS / 1000) as usize;

pub fn resolve_track_uri(base_uri: &str, content_base: Option<&str>, control: &str) -> String {
    if control.starts_with("rtsp://") {
        return control.to_owned();
    }
    if control == "*" {
        return base_uri.to_owned();
    }
    if let Some(content_base) = content_base {
        return format!(
            "{}/{}",
            content_base.trim_end_matches('/'),
            control.trim_start_matches('/')
        );
    }
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
}

pub fn rtsp_endpoint(uri: &str) -> Result<(String, u16), String> {
    let parsed = url::Url::parse(uri).map_err(|error| format!("invalid RTSP URI: {error}"))?;
    if parsed.scheme() != "rtsp" {
        return Err("ONVIF returned a non-RTSP stream URI".to_owned());
    }
    let host = parsed
        .host_str()
        .ok_or("ONVIF RTSP URI has no host")?
        .to_owned();
    Ok((host, parsed.port().unwrap_or(554)))
}

pub struct BackchannelSession {
    rtsp: RtspClient,
    stream_uri: String,
    packetizer: RtpPacketizer,
    keepalive_interval: Duration,
    next_keepalive: Instant,
    closed: bool,
    pub variant: G711Variant,
    pub payload_type: u8,
    pub rtp_channel: u8,
}

impl BackchannelSession {
    pub fn open(host: &str, user: &str, password: &str) -> Result<Self, String> {
        let mut device = OnvifDevice::new(host, user, password)?;
        device.connect()?;
        let profile = device
            .profile_tokens()?
            .into_iter()
            .next()
            .ok_or("ONVIF returned no media profile")?;
        let stream_uri = device.stream_uri(&profile)?;
        let (rtsp_host, rtsp_port) = rtsp_endpoint(&stream_uri)?;
        let mut rtsp = RtspClient::connect(
            &rtsp_host,
            rtsp_port,
            user,
            password,
            Duration::from_secs(8),
        )?;

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
            let send_track = find_backchannel_audio(&tracks).unwrap();
            let send_control = send_track
                .control
                .clone()
                .ok_or("backchannel track has no control URI")?;
            let codec = pick_g711_codec(send_track)
                .ok_or("backchannel track offers no 8kHz G.711 codec")?;
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
            variant: codec.variant,
            payload_type: codec.payload_type,
            rtp_channel,
        })
    }

    pub fn send(&mut self, g711: &[u8]) -> Result<usize, String> {
        let start = Instant::now();
        let mut pacing = PacingState::new(0);
        let mut sent = 0usize;
        for chunk in g711.chunks(SAMPLES_PER_PACKET) {
            sleep_until(start, pacing.deadline_ns());
            self.rtsp.drain_interleaved()?;
            if Instant::now() >= self.next_keepalive {
                let response = self.rtsp.keep_alive(&self.stream_uri)?;
                require_success("RTSP keepalive", response.status, &response.status_line)?;
                self.next_keepalive = Instant::now() + self.keepalive_interval;
            }

            let actual_ns = elapsed_ns(start);
            let duration_ns = (chunk.len() as u64 * 1_000_000_000) / SAMPLE_RATE;
            pacing.register_send(actual_ns, duration_ns);
            let rtp = self.packetizer.build(chunk, chunk.len() as u32);
            let frame = interleave(self.rtp_channel, &rtp);
            self.rtsp.send_interleaved(&frame)?;
            sent += 1;
        }
        if sent > 0 {
            sleep_until(start, pacing.deadline_ns());
        }
        Ok(sent)
    }

    pub fn close(&mut self) -> Result<(), String> {
        if self.closed {
            return Ok(());
        }
        self.closed = true;
        self.rtsp.teardown(&self.stream_uri)
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
    use super::{resolve_track_uri, rtsp_endpoint};

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
}
