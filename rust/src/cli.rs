use std::ffi::OsString;
use std::net::Ipv4Addr;
use std::path::PathBuf;

use clap::Parser;

#[derive(Debug, Parser)]
#[command(
    name = "rtsp-backchannel",
    about = "Play one audio file through an ONVIF RTSP backchannel",
    after_help = "Commands: rtsp-backchannel discover; rtsp-backchannel streams\n\
                  Profile: PCMA 8kHz mono, TCP interleaved RTP, 40 ms packets, rebase pacing."
)]
pub struct Cli {
    #[arg(long)]
    pub host: String,

    #[arg(long, default_value = "admin")]
    pub user: String,

    #[arg(long = "pass", env = "ONVIF_PASSWORD", hide_env_values = true)]
    pub password: String,

    #[arg(long)]
    pub file: PathBuf,

    #[arg(long, default_value = "0.05", value_parser = parse_volume)]
    pub volume: f64,
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
    pub interfaces: Vec<Ipv4Addr>,
}

#[derive(Debug, Parser)]
#[command(
    name = "rtsp-backchannel streams",
    about = "Resolve every ONVIF media profile RTSP URI"
)]
pub struct StreamsCli {
    #[arg(long)]
    pub host: String,

    #[arg(long, default_value = "admin")]
    pub user: String,

    #[arg(long = "pass", env = "ONVIF_PASSWORD", hide_env_values = true)]
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

    use super::Cli;

    #[test]
    fn requires_camera_host_and_password() {
        let command = Cli::command();
        for id in ["host", "password"] {
            let argument = command
                .get_arguments()
                .find(|argument| argument.get_id() == id)
                .unwrap();
            assert!(argument.is_required_set());
        }
    }

    #[test]
    fn uses_only_non_sensitive_playback_defaults() {
        let cli = Cli::try_parse_from([
            "rtsp-backchannel",
            "--host",
            "camera",
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
