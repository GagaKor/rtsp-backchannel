use anyhow::Result;
use rtsp_backchannel::audio::AudioCodec;
use rtsp_backchannel::backchannel::parse_rtsp_target;
use rtsp_backchannel::cli::{Cli, Invocation, parse_invocation_from};
use rtsp_backchannel::discovery::{
    CidrDiscoveryOptions, DiscoveryOptions, discover_devices, discover_devices_in_cidrs,
};
use rtsp_backchannel::onvif::{StreamUriOptions, get_stream_uris};
use rtsp_backchannel::playback::{PlaybackConfig, play_file_with_codec};
use rtsp_backchannel::rtsp::has_rtsp_scheme;
use std::time::Duration;

fn main() {
    let invocation = match parse_invocation_from(std::env::args_os()) {
        Ok(invocation) => invocation,
        Err(error) => error.exit(),
    };
    if let Err(error) = run(invocation) {
        eprintln!("error: {error:#}");
        std::process::exit(1);
    }
}

fn run(invocation: Invocation) -> Result<()> {
    match invocation {
        Invocation::Play(cli) => run_playback(cli),
        Invocation::Discover(cli) => {
            let devices = if cli.cidrs.is_empty() {
                discover_devices(&DiscoveryOptions {
                    timeout: Duration::from_millis(cli.timeout_ms),
                    interfaces: cli.interfaces,
                })
            } else {
                if !cli.interfaces.is_empty() {
                    anyhow::bail!("--interface cannot be combined with --cidr");
                }
                let mut options = CidrDiscoveryOptions::new(cli.cidrs);
                options.timeout = Duration::from_millis(cli.timeout_ms);
                options.concurrency = cli.concurrency;
                if !cli.ports.is_empty() {
                    options.ports = cli.ports;
                }
                discover_devices_in_cidrs(&options).map_err(anyhow::Error::msg)?
            };
            for device in devices {
                println!("{}", serde_json::to_string(&device)?);
            }
            Ok(())
        }
        Invocation::Streams(cli) => {
            let streams = get_stream_uris(&StreamUriOptions {
                host: cli.host,
                user: cli.user,
                password: cli.password,
                device_urls: cli.device_urls,
                timeout: Duration::from_secs(8),
            })
            .map_err(anyhow::Error::msg)?;
            for stream in streams {
                println!("{}", serde_json::to_string(&stream)?);
            }
            Ok(())
        }
    }
}

fn run_playback(cli: Cli) -> Result<()> {
    let display_host = if has_rtsp_scheme(&cli.host) {
        parse_rtsp_target(&cli.host, "", "")
            .map(|target| target.uri)
            .unwrap_or_else(|_| "rtsp://<invalid-target>".to_owned())
    } else {
        cli.host.clone()
    };
    println!(
        "# play \"{}\" -> {} speaker (backchannel)",
        cli.file.display(),
        display_host
    );
    let result = play_file_with_codec(
        &PlaybackConfig {
            host: cli.host,
            user: cli.user,
            password: cli.password,
            file: cli.file,
            volume: cli.volume,
        },
        cli.codec,
    )?;
    println!(
        "playback complete: {}/{} pt={} ch={}",
        codec_name(result.codec),
        result.sample_rate,
        result.payload_type,
        result.rtp_channel
    );
    println!(
        "encoded: {} bytes (~{:.1}s {} {}Hz {}ch)",
        result.encoded_bytes,
        result.duration_seconds,
        codec_name(result.codec),
        result.sample_rate,
        result.channels
    );
    println!("sent {} RTP packets", result.packets_sent);
    Ok(())
}

fn codec_name(codec: AudioCodec) -> &'static str {
    match codec {
        AudioCodec::Pcma => "PCMA",
        AudioCodec::Pcmu => "PCMU",
        AudioCodec::G72616 => "G726-16",
        AudioCodec::G72624 => "G726-24",
        AudioCodec::G72632 => "G726-32",
        AudioCodec::G72640 => "G726-40",
        AudioCodec::Aac => "AAC",
    }
}
