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
    if !bytes.len().is_multiple_of(2) {
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

pub fn decode_file(path: &Path) -> Result<Vec<i16>, String> {
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

#[cfg(test)]
mod tests {
    use std::fs::{self, File};
    use std::path::Path;
    use std::process::Command;
    use std::time::{Duration, Instant};

    use super::{G711Variant, decode_args, encode_g711, parse_s16le};

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
            "onvif-backchannel-audio-test-{:016x}",
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
