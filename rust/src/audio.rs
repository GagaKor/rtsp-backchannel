use std::ffi::OsString;
use std::fs;
use std::io::Read;
use std::path::Path;
use std::process::{Command, ExitStatus, Stdio};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use std::time::{Duration, Instant};

const MAX_SOURCE_FILE_BYTES: u64 = 128 * 1024 * 1024;
const MAX_DECODED_BYTES: usize = 128 * 1024 * 1024;
const MAX_DIAGNOSTIC_BYTES: usize = 64 * 1024;
const FFMPEG_TIMEOUT: Duration = Duration::from_secs(120);
const PROCESS_POLL_INTERVAL: Duration = Duration::from_millis(5);
const RESAMPLE_FILTER: &str = concat!(
    "aresample=8000:resampler=swr:filter_size=32:phase_shift=10:",
    "linear_interp=1:exact_rational=1:cutoff=0.97:dither_method=none:",
    "osf=s16:ochl=mono"
);

#[derive(Debug)]
struct BoundedOutput {
    status: ExitStatus,
    stdout: Vec<u8>,
    stderr: Vec<u8>,
}

fn run_command_bounded(
    mut command: Command,
    timeout: Duration,
    max_stdout: usize,
    max_stderr: usize,
) -> Result<BoundedOutput, String> {
    command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = command
        .spawn()
        .map_err(|error| format!("failed to start ffmpeg: {error}"))?;
    let stdout = child
        .stdout
        .take()
        .ok_or("failed to capture ffmpeg stdout")?;
    let stderr = child
        .stderr
        .take()
        .ok_or("failed to capture ffmpeg stderr")?;
    let stdout_overflow = Arc::new(AtomicBool::new(false));
    let stderr_overflow = Arc::new(AtomicBool::new(false));
    let read_failed = Arc::new(AtomicBool::new(false));
    let stdout_reader = spawn_bounded_reader(
        stdout,
        max_stdout,
        Arc::clone(&stdout_overflow),
        Arc::clone(&read_failed),
        "stdout",
    );
    let stderr_reader = spawn_bounded_reader(
        stderr,
        max_stderr,
        Arc::clone(&stderr_overflow),
        Arc::clone(&read_failed),
        "stderr",
    );

    let deadline = Instant::now() + timeout;
    let process_result = loop {
        if stdout_overflow.load(Ordering::Acquire) {
            break Err(format!("ffmpeg stdout exceeds {max_stdout} byte limit"));
        }
        if stderr_overflow.load(Ordering::Acquire) {
            break Err(format!("ffmpeg stderr exceeds {max_stderr} byte limit"));
        }
        if read_failed.load(Ordering::Acquire) {
            break Err("ffmpeg pipe handling failed".to_owned());
        }
        match child.try_wait() {
            Ok(Some(status)) => break Ok(status),
            Ok(None) => {}
            Err(error) => break Err(format!("failed to wait for ffmpeg: {error}")),
        }
        let now = Instant::now();
        if now >= deadline {
            break Err(format!(
                "ffmpeg decode timed out after {} seconds",
                timeout.as_secs_f64()
            ));
        }
        thread::sleep(PROCESS_POLL_INTERVAL.min(deadline - now));
    };

    if process_result.is_err() {
        let _ = child.kill();
        let _ = child.wait();
    }
    let stdout = join_reader(stdout_reader)?;
    let stderr = join_reader(stderr_reader)?;
    if stdout_overflow.load(Ordering::Acquire) {
        return Err(format!("ffmpeg stdout exceeds {max_stdout} byte limit"));
    }
    if stderr_overflow.load(Ordering::Acquire) {
        return Err(format!("ffmpeg stderr exceeds {max_stderr} byte limit"));
    }
    Ok(BoundedOutput {
        status: process_result?,
        stdout,
        stderr,
    })
}

fn spawn_bounded_reader<R: Read + Send + 'static>(
    mut reader: R,
    limit: usize,
    overflow: Arc<AtomicBool>,
    read_failed: Arc<AtomicBool>,
    stream_name: &'static str,
) -> thread::JoinHandle<Result<Vec<u8>, String>> {
    thread::spawn(move || {
        let mut retained = Vec::new();
        let mut chunk = [0u8; 16 * 1024];
        loop {
            let read = match reader.read(&mut chunk) {
                Ok(read) => read,
                Err(error) => {
                    read_failed.store(true, Ordering::Release);
                    return Err(format!("failed to read ffmpeg {stream_name}: {error}"));
                }
            };
            if read == 0 {
                return Ok(retained);
            }
            let remaining = limit.saturating_sub(retained.len());
            retained.extend_from_slice(&chunk[..read.min(remaining)]);
            if read > remaining {
                overflow.store(true, Ordering::Release);
                return Ok(retained);
            }
        }
    })
}

fn join_reader(reader: thread::JoinHandle<Result<Vec<u8>, String>>) -> Result<Vec<u8>, String> {
    reader
        .join()
        .map_err(|_| "ffmpeg pipe reader panicked".to_owned())?
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum AudioCodec {
    Pcma,
    Pcmu,
    G72616,
    G72624,
    G72632,
    G72640,
    Aac,
}

impl AudioCodec {
    pub const fn g726_bits_per_sample(self) -> Option<usize> {
        match self {
            Self::G72616 => Some(2),
            Self::G72624 => Some(3),
            Self::G72632 => Some(4),
            Self::G72640 => Some(5),
            _ => None,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct AudioFrame {
    pub payload: Vec<u8>,
    pub samples: u32,
    pub marker: bool,
}

pub fn frame_g711(encoded: &[u8]) -> Result<Vec<AudioFrame>, String> {
    if encoded.is_empty() {
        return Err("G.711 encoding produced no audio frames".to_owned());
    }
    encoded
        .chunks(320)
        .map(|payload| {
            Ok(AudioFrame {
                payload: payload.to_vec(),
                samples: u32::try_from(payload.len())
                    .map_err(|_| "G.711 RTP timestamp step exceeds u32".to_owned())?,
                marker: false,
            })
        })
        .collect()
}

pub fn frame_g726(encoded: &[u8], codec: AudioCodec) -> Result<Vec<AudioFrame>, String> {
    let bits = codec
        .g726_bits_per_sample()
        .ok_or_else(|| format!("{codec:?} is not a G.726 codec"))?;
    if encoded.is_empty() {
        return Err("FFmpeg produced no G.726 audio frames".to_owned());
    }
    if encoded.len().saturating_mul(8) % bits != 0 {
        return Err("FFmpeg produced an incomplete G.726 code word".to_owned());
    }
    let bytes_per_packet = 320 * bits / 8;
    encoded
        .chunks(bytes_per_packet)
        .map(|payload| {
            let samples = u32::try_from(payload.len() * 8 / bits)
                .map_err(|_| "G.726 RTP timestamp step exceeds u32".to_owned())?;
            Ok(AudioFrame {
                payload: payload.to_vec(),
                samples,
                marker: false,
            })
        })
        .collect()
}

pub fn rfc3640_aac_payload(access_unit: &[u8]) -> Result<Vec<u8>, String> {
    if access_unit.len() > 0x1fff {
        return Err("AAC access unit exceeds the RFC 3640 8191-byte limit".to_owned());
    }
    let au_header = u16::try_from(access_unit.len() << 3)
        .map_err(|_| "AAC access unit header exceeds 16 bits".to_owned())?;
    let mut payload = Vec::with_capacity(4 + access_unit.len());
    payload.extend_from_slice(&16u16.to_be_bytes());
    payload.extend_from_slice(&au_header.to_be_bytes());
    payload.extend_from_slice(access_unit);
    Ok(payload)
}

pub(crate) fn aac_channel_count(channel_configuration: u8) -> Result<u16, String> {
    match channel_configuration {
        0 => Err("AAC program-config-element channel layouts are unsupported".to_owned()),
        1..=6 => Ok(u16::from(channel_configuration)),
        7 => Ok(8),
        _ => Err(format!(
            "AAC channelConfiguration {channel_configuration} is unsupported"
        )),
    }
}

pub fn parse_adts_frames(
    encoded: &[u8],
    expected_sample_rate: u32,
    expected_channels: u16,
) -> Result<Vec<AudioFrame>, String> {
    const SAMPLE_RATES: [u32; 13] = [
        96_000, 88_200, 64_000, 48_000, 44_100, 32_000, 24_000, 22_050, 16_000, 12_000, 11_025,
        8_000, 7_350,
    ];
    if encoded.is_empty() {
        return Err("FFmpeg produced no AAC frames".to_owned());
    }
    let mut frames = Vec::new();
    let mut offset = 0usize;
    while offset < encoded.len() {
        if offset + 7 > encoded.len()
            || encoded[offset] != 0xff
            || encoded[offset + 1] & 0xf6 != 0xf0
        {
            return Err(format!("invalid ADTS frame at byte {offset}"));
        }
        if encoded[offset + 1] & 0x08 != 0 {
            return Err(format!("ADTS frame at byte {offset} is MPEG-2, not MPEG-4"));
        }
        let audio_object_type = ((encoded[offset + 2] >> 6) & 0x03) + 1;
        if audio_object_type != 2 {
            return Err(format!(
                "ADTS frame at byte {offset} is not AAC-LC audio object type 2"
            ));
        }
        let frequency_index = usize::from((encoded[offset + 2] >> 2) & 0x0f);
        let sample_rate = SAMPLE_RATES
            .get(frequency_index)
            .copied()
            .ok_or_else(|| format!("ADTS frame at byte {offset} has invalid sample rate"))?;
        if sample_rate != expected_sample_rate {
            return Err(format!(
                "ADTS sample rate {sample_rate} does not match negotiated rate {expected_sample_rate}"
            ));
        }
        let channel_configuration =
            ((encoded[offset + 2] & 0x01) << 2) | (encoded[offset + 3] >> 6);
        let channels = aac_channel_count(channel_configuration)?;
        if channels != expected_channels {
            return Err(format!(
                "ADTS channel count {channels} does not match negotiated count {expected_channels}"
            ));
        }
        if encoded[offset + 6] & 0x03 != 0 {
            return Err("ADTS frames with multiple raw data blocks are unsupported".to_owned());
        }
        let frame_length = (usize::from(encoded[offset + 3] & 0x03) << 11)
            | (usize::from(encoded[offset + 4]) << 3)
            | usize::from(encoded[offset + 5] >> 5);
        let header_length = if encoded[offset + 1] & 1 == 1 { 7 } else { 9 };
        if frame_length < header_length || offset + frame_length > encoded.len() {
            return Err(format!("truncated ADTS frame at byte {offset}"));
        }
        let access_unit = &encoded[offset + header_length..offset + frame_length];
        frames.push(AudioFrame {
            payload: rfc3640_aac_payload(access_unit)?,
            samples: 1024,
            marker: true,
        });
        offset += frame_length;
    }
    Ok(frames)
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub enum CodecPreference {
    #[default]
    Auto,
    Pcma,
    Pcmu,
    G72616,
    G72624,
    G72632,
    G72640,
    Aac,
}

impl CodecPreference {
    pub const VALUES: &'static str = "auto|pcma|pcmu|g726-16|g726-24|g726-32|g726-40|aac";

    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Auto => "auto",
            Self::Pcma => "pcma",
            Self::Pcmu => "pcmu",
            Self::G72616 => "g726-16",
            Self::G72624 => "g726-24",
            Self::G72632 => "g726-32",
            Self::G72640 => "g726-40",
            Self::Aac => "aac",
        }
    }
}

impl std::fmt::Display for CodecPreference {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(self.as_str())
    }
}

impl std::str::FromStr for CodecPreference {
    type Err = String;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        match value.to_ascii_lowercase().as_str() {
            "auto" => Ok(Self::Auto),
            "pcma" => Ok(Self::Pcma),
            "pcmu" => Ok(Self::Pcmu),
            "g726-16" => Ok(Self::G72616),
            "g726-24" => Ok(Self::G72624),
            "g726-32" => Ok(Self::G72632),
            "g726-40" => Ok(Self::G72640),
            "aac" => Ok(Self::Aac),
            _ => Err(format!("codec must be one of {}", Self::VALUES)),
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum G711Variant {
    Pcma,
    Pcmu,
}

fn linear_to_alaw(sample: i16) -> u8 {
    let mut magnitude = i32::from(sample);
    let sign = if magnitude < 0 { 0x80 } else { 0 };
    if magnitude < 0 {
        magnitude = -magnitude;
    }
    magnitude = magnitude.min(32635);
    let compressed = if magnitude >= 256 {
        let mut exponent = 7;
        let mut mask = 0x4000;
        while magnitude & mask == 0 && exponent > 0 {
            exponent -= 1;
            mask >>= 1;
        }
        let mantissa = (magnitude >> (exponent + 3)) & 0x0f;
        (exponent << 4) | mantissa
    } else {
        magnitude >> 4
    };
    ((sign | compressed) ^ 0xd5) as u8
}

fn linear_to_mulaw(sample: i16) -> u8 {
    let mut magnitude = i32::from(sample);
    let sign = if magnitude < 0 { 0x80 } else { 0 };
    if magnitude < 0 {
        magnitude = -magnitude;
    }
    magnitude = magnitude.min(32635) + 0x84;
    let mut exponent = 7;
    let mut mask = 0x4000;
    while magnitude & mask == 0 && exponent > 0 {
        exponent -= 1;
        mask >>= 1;
    }
    let mantissa = (magnitude >> (exponent + 3)) & 0x0f;
    (!(sign | (exponent << 4) | mantissa) & 0xff) as u8
}

pub fn encode_g711(
    samples: &[i16],
    variant: G711Variant,
    volume: f64,
) -> Result<Vec<u8>, &'static str> {
    if !volume.is_finite() || !(0.0..=1.0).contains(&volume) {
        return Err("volume must be finite and between 0 and 1");
    }
    let gain_q11 = (volume * 2048.0).trunc() as i32;
    let encoder = match variant {
        G711Variant::Pcma => linear_to_alaw,
        G711Variant::Pcmu => linear_to_mulaw,
    };
    Ok(samples
        .iter()
        .map(|sample| {
            let scaled = (i32::from(*sample) * gain_q11) >> 11;
            encoder(scaled.clamp(-32768, 32767) as i16)
        })
        .collect())
}

pub fn parse_s16le(bytes: &[u8]) -> Result<Vec<i16>, &'static str> {
    if bytes.is_empty() {
        return Err("ffmpeg decode produced no audio samples");
    }
    if bytes.len() % 2 != 0 {
        return Err("ffmpeg returned an incomplete S16LE sample");
    }
    Ok(bytes
        .chunks_exact(2)
        .map(|sample| i16::from_le_bytes([sample[0], sample[1]]))
        .collect())
}

pub fn decode_args(path: &Path) -> Vec<OsString> {
    [
        OsString::from("-nostdin"),
        OsString::from("-hide_banner"),
        OsString::from("-loglevel"),
        OsString::from("error"),
        OsString::from("-i"),
        path.as_os_str().to_owned(),
        OsString::from("-map"),
        OsString::from("0:a:0"),
        OsString::from("-vn"),
        OsString::from("-sn"),
        OsString::from("-dn"),
        OsString::from("-af"),
        OsString::from(RESAMPLE_FILTER),
        OsString::from("-c:a"),
        OsString::from("pcm_s16le"),
        OsString::from("-f"),
        OsString::from("s16le"),
        OsString::from("-fs"),
        OsString::from((MAX_DECODED_BYTES + 1).to_string()),
        OsString::from("pipe:1"),
    ]
    .into()
}

pub fn ffmpeg_encode_args(
    path: &Path,
    codec: AudioCodec,
    sample_rate: u32,
    channels: u16,
    volume: f64,
) -> Result<Vec<OsString>, String> {
    if !volume.is_finite() || !(0.0..=1.0).contains(&volume) {
        return Err("volume must be finite and between 0 and 1".to_owned());
    }
    if sample_rate == 0 || channels == 0 {
        return Err("sample rate and channel count must be greater than zero".to_owned());
    }
    let mut arguments = vec![
        OsString::from("-nostdin"),
        OsString::from("-hide_banner"),
        OsString::from("-loglevel"),
        OsString::from("error"),
        OsString::from("-i"),
        path.as_os_str().to_owned(),
        OsString::from("-map"),
        OsString::from("0:a:0"),
        OsString::from("-vn"),
        OsString::from("-sn"),
        OsString::from("-dn"),
        OsString::from("-af"),
        OsString::from(format!("volume={volume}")),
        OsString::from("-ar"),
        OsString::from(sample_rate.to_string()),
        OsString::from("-ac"),
        OsString::from(channels.to_string()),
    ];
    match codec {
        AudioCodec::G72616 | AudioCodec::G72624 | AudioCodec::G72632 | AudioCodec::G72640 => {
            if sample_rate != 8000 || channels != 1 {
                return Err("G.726 requires 8000 Hz mono audio".to_owned());
            }
            let bits = codec.g726_bits_per_sample().expect("matched G.726 codec");
            arguments.extend([
                OsString::from("-c:a"),
                OsString::from("g726le"),
                OsString::from("-code_size"),
                OsString::from(bits.to_string()),
                OsString::from("-b:a"),
                OsString::from(format!("{}k", bits * 8)),
                OsString::from("-f"),
                OsString::from("g726le"),
            ]);
        }
        AudioCodec::Aac => arguments.extend([
            OsString::from("-c:a"),
            OsString::from("aac"),
            OsString::from("-profile:a"),
            OsString::from("aac_low"),
            OsString::from("-f"),
            OsString::from("adts"),
        ]),
        AudioCodec::Pcma | AudioCodec::Pcmu => {
            return Err("G.711 is encoded in process after FFmpeg decoding".to_owned());
        }
    }
    arguments.extend([
        OsString::from("-fs"),
        OsString::from((MAX_DECODED_BYTES + 1).to_string()),
        OsString::from("pipe:1"),
    ]);
    Ok(arguments)
}

pub fn decode_file(path: &Path) -> Result<Vec<i16>, String> {
    validate_source_file(path)?;
    let mut command = Command::new("ffmpeg");
    command.args(decode_args(path));
    let output = run_command_bounded(
        command,
        FFMPEG_TIMEOUT,
        MAX_DECODED_BYTES,
        MAX_DIAGNOSTIC_BYTES,
    )?;
    if !output.status.success() {
        return Err(format!(
            "ffmpeg exited {}: {}",
            output.status,
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    parse_s16le(&output.stdout).map_err(str::to_owned)
}

pub fn transcode_file(
    path: &Path,
    codec: AudioCodec,
    sample_rate: u32,
    channels: u16,
    volume: f64,
) -> Result<Vec<AudioFrame>, String> {
    match codec {
        AudioCodec::Pcma | AudioCodec::Pcmu => {
            let samples = decode_file(path)?;
            let variant = match codec {
                AudioCodec::Pcma => G711Variant::Pcma,
                AudioCodec::Pcmu => G711Variant::Pcmu,
                _ => unreachable!(),
            };
            frame_g711(&encode_g711(&samples, variant, volume).map_err(str::to_owned)?)
        }
        AudioCodec::G72616
        | AudioCodec::G72624
        | AudioCodec::G72632
        | AudioCodec::G72640
        | AudioCodec::Aac => {
            validate_source_file(path)?;
            let mut command = Command::new("ffmpeg");
            command.args(ffmpeg_encode_args(
                path,
                codec,
                sample_rate,
                channels,
                volume,
            )?);
            let output = run_command_bounded(
                command,
                FFMPEG_TIMEOUT,
                MAX_DECODED_BYTES,
                MAX_DIAGNOSTIC_BYTES,
            )?;
            if !output.status.success() {
                return Err(format!(
                    "ffmpeg exited {}: {}",
                    output.status,
                    String::from_utf8_lossy(&output.stderr).trim()
                ));
            }
            match codec {
                AudioCodec::G72616
                | AudioCodec::G72624
                | AudioCodec::G72632
                | AudioCodec::G72640 => frame_g726(&output.stdout, codec),
                AudioCodec::Aac => parse_adts_frames(&output.stdout, sample_rate, channels),
                AudioCodec::Pcma | AudioCodec::Pcmu => unreachable!(),
            }
        }
    }
}

fn validate_source_file(path: &Path) -> Result<(), String> {
    let metadata = fs::metadata(path)
        .map_err(|error| format!("cannot inspect source file {}: {error}", path.display()))?;
    if !metadata.is_file() {
        return Err(format!(
            "source path is not a regular file: {}",
            path.display()
        ));
    }
    if metadata.len() > MAX_SOURCE_FILE_BYTES {
        return Err(format!(
            "source file {} exceeds {MAX_SOURCE_FILE_BYTES} byte limit",
            path.display()
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::fs::{self, File};
    use std::path::Path;
    use std::process::Command;
    use std::time::{Duration, Instant};

    use super::{
        AudioCodec, CodecPreference, G711Variant, decode_args, encode_g711, ffmpeg_encode_args,
        frame_g726, parse_adts_frames, parse_s16le, rfc3640_aac_payload,
    };

    #[test]
    fn parses_and_formats_every_public_codec_preference() {
        for (name, expected) in [
            ("auto", CodecPreference::Auto),
            ("pcma", CodecPreference::Pcma),
            ("pcmu", CodecPreference::Pcmu),
            ("g726-16", CodecPreference::G72616),
            ("g726-24", CodecPreference::G72624),
            ("g726-32", CodecPreference::G72632),
            ("g726-40", CodecPreference::G72640),
            ("aac", CodecPreference::Aac),
        ] {
            assert_eq!(name.parse::<CodecPreference>().unwrap(), expected);
            assert_eq!(expected.to_string(), name);
        }
        let error = "opus".parse::<CodecPreference>().unwrap_err();
        assert!(error.contains("auto|pcma|pcmu|g726-16|g726-24|g726-32|g726-40|aac"));
    }

    #[test]
    fn frames_each_g726_bit_rate_into_40ms_rtp_payloads() {
        for (codec, bytes_per_packet) in [
            (AudioCodec::G72616, 80),
            (AudioCodec::G72624, 120),
            (AudioCodec::G72632, 160),
            (AudioCodec::G72640, 200),
        ] {
            let encoded = vec![0x5a; bytes_per_packet * 2];
            let frames = frame_g726(&encoded, codec).unwrap();

            assert_eq!(frames.len(), 2);
            assert_eq!(frames[0].payload.len(), bytes_per_packet);
            assert_eq!(frames[0].samples, 320);
            assert_eq!(frames[1].samples, 320);
        }
        assert!(frame_g726(&[0; 10], AudioCodec::Pcma).is_err());
        assert!(frame_g726(&[0; 1], AudioCodec::G72624).is_err());
    }

    #[test]
    fn adds_the_rfc3640_aac_hbr_access_unit_header() {
        assert_eq!(
            rfc3640_aac_payload(&[0xaa, 0xbb, 0xcc]).unwrap(),
            [0x00, 0x10, 0x00, 0x18, 0xaa, 0xbb, 0xcc]
        );
        assert!(rfc3640_aac_payload(&vec![0; 8191]).is_ok());
        assert!(
            rfc3640_aac_payload(&vec![0; 8192])
                .unwrap_err()
                .contains("8191")
        );
    }

    #[test]
    fn extracts_adts_access_units_and_assigns_1024_timestamp_samples() {
        let without_crc = [0xff, 0xf1, 0x6c, 0x40, 0x01, 0x5f, 0xfc, 0xaa, 0xbb, 0xcc];
        let with_crc = [
            0xff, 0xf0, 0x6c, 0x40, 0x01, 0x9f, 0xfc, 0x12, 0x34, 0xdd, 0xee, 0xff,
        ];
        let encoded = [without_crc.as_slice(), with_crc.as_slice()].concat();

        let frames = parse_adts_frames(&encoded, 8000, 1).unwrap();

        assert_eq!(frames.len(), 2);
        assert_eq!(frames[0].samples, 1024);
        assert!(frames.iter().all(|frame| frame.marker));
        assert_eq!(frames[0].payload, [0, 16, 0, 24, 0xaa, 0xbb, 0xcc]);
        assert_eq!(frames[1].samples, 1024);
        assert_eq!(frames[1].payload, [0, 16, 0, 24, 0xdd, 0xee, 0xff]);
        assert!(parse_adts_frames(&without_crc[..9], 8000, 1).is_err());
        assert!(parse_adts_frames(&without_crc, 16000, 1).is_err());
    }

    #[test]
    fn maps_adts_channel_configuration_seven_to_eight_channels() {
        let eight_channels = [0xff, 0xf1, 0x6d, 0xc0, 0x01, 0x5f, 0xfc, 0xaa, 0xbb, 0xcc];

        let frames = parse_adts_frames(&eight_channels, 8000, 8).unwrap();

        assert_eq!(frames.len(), 1);
        assert_eq!(frames[0].samples, 1024);
        assert!(frames[0].marker);
    }

    #[test]
    fn rejects_mpeg2_adts_when_mpeg4_generic_is_negotiated() {
        let mut mpeg2 = vec![0xff, 0xf9, 0x6c, 0x40, 0x01, 0x5f, 0xfc, 0xaa, 0xbb, 0xcc];
        assert!(parse_adts_frames(&mpeg2, 8000, 1).is_err());
        mpeg2[1] = 0xf1;
        assert!(parse_adts_frames(&mpeg2, 8000, 1).is_ok());
    }

    #[test]
    fn roundtrips_g726le_through_ffmpeg_when_available() {
        if Command::new("ffmpeg").arg("-version").output().is_err() {
            return;
        }
        let output = Command::new("ffmpeg")
            .args([
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=0.08",
                "-ar",
                "8000",
                "-ac",
                "1",
                "-c:a",
                "g726le",
                "-code_size",
                "4",
                "-f",
                "g726le",
                "pipe:1",
            ])
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        let frames = frame_g726(&output.stdout, AudioCodec::G72632).unwrap();
        assert!(!frames.is_empty());
        assert_eq!(frames.iter().map(|frame| frame.samples).sum::<u32>(), 640);
    }

    #[test]
    fn builds_ffmpeg_g726_and_aac_encoding_pipelines() {
        for (codec, code_size, bit_rate) in [
            (AudioCodec::G72616, "2", "16k"),
            (AudioCodec::G72624, "3", "24k"),
            (AudioCodec::G72632, "4", "32k"),
            (AudioCodec::G72640, "5", "40k"),
        ] {
            let args = ffmpeg_encode_args(Path::new("event.mp3"), codec, 8000, 1, 0.05)
                .unwrap()
                .into_iter()
                .map(|arg| arg.to_string_lossy().into_owned())
                .collect::<Vec<_>>();
            assert!(args.windows(2).any(|pair| pair == ["-c:a", "g726le"]));
            assert!(
                args.windows(2)
                    .any(|pair| pair == ["-code_size", code_size])
            );
            assert!(args.windows(2).any(|pair| pair == ["-b:a", bit_rate]));
            assert!(args.windows(2).any(|pair| pair == ["-f", "g726le"]));
        }

        let aac = ffmpeg_encode_args(Path::new("event.mp3"), AudioCodec::Aac, 8000, 1, 0.25)
            .unwrap()
            .into_iter()
            .map(|arg| arg.to_string_lossy().into_owned())
            .collect::<Vec<_>>();
        assert!(aac.windows(2).any(|pair| pair == ["-c:a", "aac"]));
        assert!(aac.windows(2).any(|pair| pair == ["-profile:a", "aac_low"]));
        assert!(aac.windows(2).any(|pair| pair == ["-f", "adts"]));
        assert!(aac.windows(2).any(|pair| pair == ["-ar", "8000"]));
        assert!(aac.windows(2).any(|pair| pair == ["-ac", "1"]));
        assert!(aac.windows(2).any(|pair| pair == ["-af", "volume=0.25"]));
    }

    #[test]
    fn uses_talkspurt_markers_for_g711_g726_and_au_markers_for_aac() {
        let g711 = super::frame_g711(&vec![0; 321]).unwrap();
        assert_eq!(g711.len(), 2);
        assert!(g711.iter().all(|frame| !frame.marker));
        assert_eq!(g711[0].samples, 320);
        assert_eq!(g711[1].samples, 1);

        let g726 = super::frame_g726(&[0; 160], AudioCodec::G72632).unwrap();
        assert!(g726.iter().all(|frame| !frame.marker));
    }

    #[test]
    fn applies_python_compatible_q11_volume_before_pcma_encoding() {
        let samples = [-32768, -30000, -1000, -1, 0, 1, 1000, 30000, 32767];

        let encoded = encode_g711(&samples, G711Variant::Pcma, 0.05).unwrap();

        assert_eq!(encoded, [108, 98, 86, 85, 213, 213, 214, 226, 236]);
    }

    #[test]
    fn decodes_little_endian_s16_and_rejects_partial_samples() {
        assert_eq!(
            parse_s16le(&[0x00, 0x80, 0xff, 0x7f]).unwrap(),
            [-32768, 32767]
        );
        assert_eq!(
            parse_s16le(&[0x00]).unwrap_err(),
            "ffmpeg returned an incomplete S16LE sample"
        );
        assert_eq!(
            parse_s16le(&[]).unwrap_err(),
            "ffmpeg decode produced no audio samples"
        );
    }

    #[test]
    fn rejects_non_regular_or_oversized_sources_before_ffmpeg() {
        let root = std::env::temp_dir().join(format!(
            "rtsp-backchannel-audio-test-{:016x}",
            rand::random::<u64>()
        ));
        fs::create_dir(&root).unwrap();
        let oversized = root.join("oversized.audio");
        let file = File::create(&oversized).unwrap();
        file.set_len((128 * 1024 * 1024 + 1) as u64).unwrap();

        let directory_error = super::decode_file(&root).unwrap_err();
        let oversized_error = super::decode_file(&oversized).unwrap_err();
        fs::remove_dir_all(&root).unwrap();

        assert!(directory_error.contains("not a regular file"));
        assert!(oversized_error.contains("exceeds 134217728 byte limit"));
    }

    #[cfg(unix)]
    #[test]
    fn terminates_a_decoder_that_exceeds_the_wall_clock_deadline() {
        let mut command = Command::new("sh");
        command.args(["-c", "while :; do :; done"]);
        let started = Instant::now();

        let error =
            super::run_command_bounded(command, Duration::from_millis(50), 1024, 1024).unwrap_err();

        assert!(error.contains("timed out"));
        assert!(started.elapsed() < Duration::from_secs(1));
    }

    #[cfg(unix)]
    #[test]
    fn terminates_a_decoder_that_exceeds_the_stderr_limit() {
        let mut command = Command::new("sh");
        command.args(["-c", "while :; do printf 1234567890 >&2; done"]);
        let started = Instant::now();

        let error =
            super::run_command_bounded(command, Duration::from_secs(1), 1024, 64).unwrap_err();

        assert!(error.contains("stderr exceeds 64 byte limit"));
        assert!(started.elapsed() < Duration::from_secs(1));
    }

    #[test]
    fn builds_the_python_compatible_ffmpeg_decode_pipeline() {
        let args = decode_args(Path::new("event.mp3"));
        let args: Vec<String> = args
            .iter()
            .map(|arg| arg.to_string_lossy().into_owned())
            .collect();

        assert_eq!(args[0], "-nostdin");
        assert_eq!(args[5], "event.mp3");
        assert!(args.iter().any(|arg| arg.contains("dither_method=none")));
        assert!(args.windows(2).any(|pair| pair == ["-c:a", "pcm_s16le"]));
        assert_eq!(args.last().unwrap(), "pipe:1");
    }
}
