use std::path::PathBuf;

use clap::Parser;

#[derive(Debug, Parser)]
#[command(
    name = "onvif-backchannel",
    about = "Play one audio file through an ONVIF RTSP backchannel",
    after_help = "Profile: PCMA 8kHz mono, TCP interleaved RTP, 40 ms packets, rebase pacing."
)]
pub struct Cli {
    #[arg(long, default_value = "172.168.46.56")]
    pub host: String,

    #[arg(long, default_value = "admin")]
    pub user: String,

    #[arg(
        long = "pass",
        env = "ONVIF_PASSWORD",
        hide_env_values = true,
        default_value = "CHANGEME"
    )]
    pub password: String,

    #[arg(long)]
    pub file: PathBuf,

    #[arg(long, default_value = "0.05", value_parser = parse_volume)]
    pub volume: f64,
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
    fn uses_the_validated_playback_defaults() {
        let cli = Cli::try_parse_from(["onvif-backchannel", "--file", "event.mp3"]).unwrap();

        assert_eq!(cli.host, "172.168.46.56");
        assert_eq!(cli.user, "admin");
        assert_eq!(cli.password, "CHANGEME");
        assert_eq!(cli.volume, 0.05);
        assert_eq!(cli.file.to_string_lossy(), "event.mp3");
    }

    #[test]
    fn rejects_non_finite_or_out_of_range_volume() {
        for volume in ["NaN", "-0.1", "1.1"] {
            assert!(
                Cli::try_parse_from([
                    "onvif-backchannel",
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
