use std::ffi::OsString;
use std::net::Ipv4Addr;
use std::path::PathBuf;

use clap::Parser;

use crate::audio::CodecPreference;

#[derive(Debug, Parser)]
#[command(
    name = "rtsp-backchannel",
    about = "Play one audio file through an ONVIF or direct RTSP backchannel",
    after_help = "Commands: rtsp-backchannel discover; rtsp-backchannel streams\n\
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

#[derive(Debug)]
pub enum Invocation {
    Play(Cli),
    Discover(DiscoveryCli),
    Streams(StreamsCli),
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
