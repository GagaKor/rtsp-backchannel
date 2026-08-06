use std::ffi::{OsStr, OsString};
use std::net::Ipv4Addr;
use std::path::PathBuf;
use std::time::Duration;

use clap::Parser;

use crate::audio::CodecPreference;

const INVALID_TIMEOUT_ERROR: &str = "timeout-ms must be finite and greater than 0";
const MAX_CAPABILITY_TIMEOUT: Duration = Duration::from_secs(24 * 60 * 60);
const TIMEOUT_RANGE_ERROR: &str = "timeout-ms exceeds the 24-hour maximum";
const CAPABILITY_TERMINATOR_ERROR: &str = "capabilities does not accept an argument terminator";

#[derive(Debug, Parser)]
#[command(
    name = "rtsp-backchannel",
    about = "Play one audio file through an ONVIF or direct RTSP backchannel",
    after_help = "Commands: rtsp-backchannel discover; rtsp-backchannel streams; \
                  rtsp-backchannel capabilities\n\
                  Codec: auto|pcma|pcmu|g726-16|g726-24|g726-32|g726-40|aac; TCP interleaved RTP, real-time pacing."
)]
pub struct Cli {
    #[arg(long)]
    pub host: String,

    #[arg(long, default_value = "")]
    pub user: String,

    #[arg(
        long = "pass",
        env = "ONVIF_PASSWORD",
        hide_env_values = true,
        default_value = ""
    )]
    pub password: String,

    #[arg(long)]
    pub file: PathBuf,

    #[arg(long, default_value = "0.05", value_parser = parse_volume)]
    pub volume: f64,

    #[arg(long, default_value = "auto")]
    pub codec: CodecPreference,
}

#[derive(Debug, Parser)]
#[command(
    name = "rtsp-backchannel discover",
    about = "Discover ONVIF devices with WS-Discovery"
)]
pub struct DiscoveryCli {
    #[arg(long, default_value_t = 3000)]
    pub timeout_ms: u64,

    #[arg(long = "interface")]
    /// Local PC IPv4 address for WS-Discovery. Repeat to select multiple NICs.
    pub interfaces: Vec<Ipv4Addr>,

    #[arg(long = "cidr")]
    /// Target IPv4 address or CIDR. Repeat to search every selected target.
    pub cidrs: Vec<String>,

    #[arg(long = "port")]
    /// ONVIF Device Service port used for active discovery.
    pub ports: Vec<u16>,

    #[arg(long, default_value_t = 64)]
    /// Number of CIDR hosts scanned concurrently.
    pub concurrency: usize,
}

#[derive(Debug, Parser)]
#[command(
    name = "rtsp-backchannel streams",
    about = "Resolve every ONVIF media profile RTSP URI"
)]
pub struct StreamsCli {
    #[arg(long)]
    pub host: String,

    #[arg(long, default_value = "")]
    pub user: String,

    #[arg(
        long = "pass",
        env = "ONVIF_PASSWORD",
        hide_env_values = true,
        default_value = ""
    )]
    pub password: String,

    #[arg(long = "device-url")]
    pub device_urls: Vec<String>,
}

#[derive(Debug, Parser)]
#[command(
    name = "rtsp-backchannel capabilities",
    about = "Report read-only ONVIF camera capability evidence"
)]
pub struct CapabilitiesCli {
    #[arg(long, value_parser = parse_nonempty_text)]
    pub host: String,

    #[arg(long, value_parser = parse_nonempty_text)]
    pub user: Option<String>,

    #[arg(long = "pass", env = "ONVIF_PASSWORD", hide_env_values = true)]
    pub password: Option<String>,

    #[arg(
        long = "device-url",
        value_parser = parse_nonempty_text,
        help = "ONVIF Device Service URL (repeatable; supplied order is kept)"
    )]
    pub device_urls: Vec<String>,

    #[arg(
        long = "timeout-ms",
        value_name = "MILLISECONDS",
        value_parser = parse_positive_timeout_ms,
        allow_hyphen_values = true,
        help = "Finite positive per-request timeout in milliseconds (maximum: 24 hours)"
    )]
    pub timeout: Option<Duration>,
}

#[derive(Debug)]
pub enum Invocation {
    Play(Cli),
    Discover(DiscoveryCli),
    Streams(StreamsCli),
}

#[derive(Debug)]
pub enum ApplicationInvocation {
    Play(Cli),
    Discover(DiscoveryCli),
    Streams(StreamsCli),
    Capabilities(CapabilitiesCli),
}

pub fn parse_invocation_from<I, T>(arguments: I) -> Result<Invocation, clap::Error>
where
    I: IntoIterator<Item = T>,
    T: Into<OsString> + Clone,
{
    let arguments: Vec<OsString> = arguments.into_iter().map(Into::into).collect();
    let program = arguments
        .first()
        .cloned()
        .unwrap_or_else(|| OsString::from("rtsp-backchannel"));
    let command = arguments.get(1).and_then(|value| value.to_str());
    let delegated = |skip: usize| {
        std::iter::once(program.clone())
            .chain(arguments.iter().skip(skip).cloned())
            .collect::<Vec<_>>()
    };
    match command {
        Some("discover") => DiscoveryCli::try_parse_from(delegated(2)).map(Invocation::Discover),
        Some("streams") => StreamsCli::try_parse_from(delegated(2)).map(Invocation::Streams),
        Some("play") => Cli::try_parse_from(delegated(2)).map(Invocation::Play),
        _ => Cli::try_parse_from(arguments).map(Invocation::Play),
    }
}

pub fn parse_application_invocation_from<I, T>(
    arguments: I,
) -> Result<ApplicationInvocation, clap::Error>
where
    I: IntoIterator<Item = T>,
    T: Into<OsString> + Clone,
{
    let arguments: Vec<OsString> = arguments.into_iter().map(Into::into).collect();
    if arguments.get(1).and_then(|value| value.to_str()) == Some("capabilities") {
        normalize_capability_arguments(&arguments)
            .and_then(CapabilitiesCli::try_parse_from)
            .map(ApplicationInvocation::Capabilities)
    } else {
        parse_invocation_from(arguments).map(ApplicationInvocation::from)
    }
}

impl From<Invocation> for ApplicationInvocation {
    fn from(invocation: Invocation) -> Self {
        match invocation {
            Invocation::Play(cli) => Self::Play(cli),
            Invocation::Discover(cli) => Self::Discover(cli),
            Invocation::Streams(cli) => Self::Streams(cli),
        }
    }
}

fn exact_capability_option_name(value: &OsStr) -> Option<&'static str> {
    match value.to_str()? {
        "--host" => Some("host"),
        "--user" => Some("user"),
        "--pass" => Some("pass"),
        "--device-url" => Some("device-url"),
        "--timeout-ms" => Some("timeout-ms"),
        _ => None,
    }
}

fn attached_capability_option(value: &OsStr) -> Option<(&'static str, &str)> {
    let value = value.to_str()?;
    [
        ("host", "--host="),
        ("user", "--user="),
        ("pass", "--pass="),
        ("device-url", "--device-url="),
        ("timeout-ms", "--timeout-ms="),
    ]
    .into_iter()
    .find_map(|(name, prefix)| value.strip_prefix(prefix).map(|value| (name, value)))
}

fn is_known_capability_flag(value: &OsStr) -> bool {
    matches!(value.to_str(), Some("-h" | "--help"))
        || exact_capability_option_name(value).is_some()
        || attached_capability_option(value).is_some()
}

fn missing_capability_value(option: &str) -> clap::Error {
    clap::Error::raw(
        clap::error::ErrorKind::InvalidValue,
        format!("missing value for --{option}"),
    )
}

fn reject_excessive_capability_timeout(value: &str) -> Result<(), clap::Error> {
    if parse_timeout_ms(value).is_err_and(|error| error == TIMEOUT_RANGE_ERROR) {
        return Err(clap::Error::raw(
            clap::error::ErrorKind::InvalidValue,
            TIMEOUT_RANGE_ERROR,
        ));
    }
    Ok(())
}

fn reject_capability_terminator(arguments: &[OsString]) -> Result<(), clap::Error> {
    let Some(index) = arguments
        .iter()
        .enumerate()
        .skip(2)
        .find_map(|(index, argument)| (argument == OsStr::new("--")).then_some(index))
    else {
        return Ok(());
    };

    if let Some(option) = index
        .checked_sub(1)
        .and_then(|previous| arguments.get(previous))
        .and_then(|argument| exact_capability_option_name(argument))
    {
        return Err(missing_capability_value(option));
    }

    Err(clap::Error::raw(
        clap::error::ErrorKind::InvalidValue,
        CAPABILITY_TERMINATOR_ERROR,
    ))
}

fn normalize_capability_arguments(arguments: &[OsString]) -> Result<Vec<OsString>, clap::Error> {
    reject_capability_terminator(arguments)?;
    let mut normalized = vec![OsString::from("rtsp-backchannel capabilities")];
    let mut index = 2;
    while index < arguments.len() {
        let argument = &arguments[index];
        if let Some((option, value)) = attached_capability_option(argument) {
            if option != "pass" && value.starts_with('-') {
                return Err(missing_capability_value(option));
            }
            if option == "timeout-ms" {
                reject_excessive_capability_timeout(value)?;
            }
            normalized.push(argument.clone());
            index += 1;
            continue;
        }
        let Some(option) = exact_capability_option_name(argument) else {
            normalized.push(argument.clone());
            index += 1;
            continue;
        };
        let Some(value) = arguments.get(index + 1) else {
            return Err(missing_capability_value(option));
        };
        if option == "pass" {
            if is_known_capability_flag(value) {
                return Err(missing_capability_value(option));
            }
            if value.to_string_lossy().starts_with('-') {
                let mut attached = OsString::from("--pass=");
                attached.push(value);
                normalized.push(attached);
                index += 2;
                continue;
            }
        } else if value.to_string_lossy().starts_with('-') {
            return Err(missing_capability_value(option));
        }
        if option == "timeout-ms" {
            if let Some(value) = value.to_str() {
                reject_excessive_capability_timeout(value)?;
            }
        }
        normalized.push(argument.clone());
        index += 1;
    }
    Ok(normalized)
}

fn parse_nonempty_text(value: &str) -> Result<String, String> {
    if value.trim().is_empty() {
        return Err("value must not be empty".to_owned());
    }
    Ok(value.to_owned())
}

fn parse_positive_timeout_ms(value: &str) -> Result<Duration, String> {
    parse_timeout_ms(value).map_err(str::to_owned)
}

fn parse_timeout_ms(value: &str) -> Result<Duration, &'static str> {
    let milliseconds = value.parse::<f64>().map_err(|_| INVALID_TIMEOUT_ERROR)?;
    if !milliseconds.is_finite() || milliseconds <= 0.0 {
        return Err(INVALID_TIMEOUT_ERROR);
    }
    let timeout =
        Duration::try_from_secs_f64(milliseconds / 1000.0).map_err(|_| INVALID_TIMEOUT_ERROR)?;
    if timeout.is_zero() {
        return Err(INVALID_TIMEOUT_ERROR);
    }
    if timeout > MAX_CAPABILITY_TIMEOUT {
        return Err(TIMEOUT_RANGE_ERROR);
    }
    Ok(timeout)
}

fn parse_volume(value: &str) -> Result<f64, String> {
    let volume = value
        .parse::<f64>()
        .map_err(|_| "volume must be a number between 0 and 1".to_owned())?;
    if !volume.is_finite() || !(0.0..=1.0).contains(&volume) {
        return Err("volume must be finite and between 0 and 1".to_owned());
    }
    Ok(volume)
}

#[cfg(test)]
mod tests {
    use std::ffi::OsStr;

    use clap::{CommandFactory, Parser};

    use super::{Cli, StreamsCli};

    #[test]
    fn requires_only_camera_target_and_file_for_playback() {
        let command = Cli::command();
        for id in ["host", "file"] {
            let argument = command
                .get_arguments()
                .find(|argument| argument.get_id() == id)
                .unwrap();
            assert!(argument.is_required_set());
        }
        for id in ["user", "password"] {
            let argument = command
                .get_arguments()
                .find(|argument| argument.get_id() == id)
                .unwrap();
            assert!(!argument.is_required_set());
        }
    }

    #[test]
    fn defaults_playback_credentials_to_empty() {
        let cli = Cli::try_parse_from([
            "rtsp-backchannel",
            "--host",
            "camera",
            "--file",
            "event.mp3",
        ])
        .unwrap();

        assert_eq!(cli.user, "");
        assert_eq!(cli.password, "");
    }

    #[test]
    fn defaults_stream_credentials_to_empty() {
        let cli = StreamsCli::try_parse_from(["rtsp-backchannel", "--host", "camera"]).unwrap();

        assert_eq!(cli.user, "");
        assert_eq!(cli.password, "");
    }

    #[test]
    fn accepts_explicit_playback_credentials() {
        let cli = Cli::try_parse_from([
            "rtsp-backchannel",
            "--host",
            "camera",
            "--user",
            "admin",
            "--pass",
            "secret",
            "--file",
            "event.mp3",
        ])
        .unwrap();

        assert_eq!(cli.host, "camera");
        assert_eq!(cli.user, "admin");
        assert_eq!(cli.password, "secret");
        assert_eq!(cli.volume, 0.05);
        assert_eq!(cli.file.to_string_lossy(), "event.mp3");
    }

    #[test]
    fn parses_codec_preference_with_auto_as_the_default() {
        let default_cli = Cli::try_parse_from([
            "rtsp-backchannel",
            "--host",
            "camera",
            "--file",
            "event.mp3",
        ])
        .unwrap();
        assert_eq!(default_cli.codec, crate::audio::CodecPreference::Auto);

        let cli = Cli::try_parse_from([
            "rtsp-backchannel",
            "--host",
            "camera",
            "--file",
            "event.mp3",
            "--codec",
            "g726-32",
        ])
        .unwrap();
        assert_eq!(cli.codec, crate::audio::CodecPreference::G72632);
    }

    #[test]
    fn rejects_non_finite_or_out_of_range_volume() {
        for volume in ["NaN", "-0.1", "1.1"] {
            assert!(
                Cli::try_parse_from([
                    "rtsp-backchannel",
                    "--file",
                    "event.mp3",
                    "--volume",
                    volume,
                ])
                .is_err()
            );
        }
    }

    #[test]
    fn accepts_the_camera_password_from_a_non_argv_environment_variable() {
        let command = Cli::command();
        let password = command
            .get_arguments()
            .find(|argument| argument.get_id() == "password")
            .unwrap();

        assert_eq!(password.get_env(), Some(OsStr::new("ONVIF_PASSWORD")));
    }
}
