use anyhow::Result;
use onvif_backchannel::audio::G711Variant;
use onvif_backchannel::cli::{Cli, Invocation, parse_invocation_from};
use onvif_backchannel::discovery::{DiscoveryOptions, discover_devices};
use onvif_backchannel::onvif::{StreamUriOptions, get_stream_uris};
use onvif_backchannel::playback::{PlaybackConfig, play_file};
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
            let devices = discover_devices(&DiscoveryOptions {
                timeout: Duration::from_millis(cli.timeout_ms),
                interfaces: cli.interfaces,
            });
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
    println!(
        "# play \"{}\" -> {} speaker (backchannel)",
        cli.file.display(),
        cli.host
    );
    let result = play_file(&PlaybackConfig {
        host: cli.host,
        user: cli.user,
        password: cli.password,
        file: cli.file,
        volume: cli.volume,
    })?;
    println!(
        "playback complete: {}/{} pt={} ch={}",
        variant_name(result.variant),
        result.sample_rate,
        result.payload_type,
        result.rtp_channel
    );
    println!(
        "encoded: {} bytes (~{:.1}s {} 8kHz mono)",
        result.encoded_bytes,
        result.duration_seconds,
        variant_name(result.variant)
    );
    println!("sent {} RTP packets", result.packets_sent);
    Ok(())
}

fn variant_name(variant: G711Variant) -> &'static str {
    match variant {
        G711Variant::Pcma => "PCMA",
        G711Variant::Pcmu => "PCMU",
    }
}
