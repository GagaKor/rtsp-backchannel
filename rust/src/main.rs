use anyhow::Result;
use clap::Parser;
use onvif_backchannel::audio::G711Variant;
use onvif_backchannel::cli::Cli;
use onvif_backchannel::playback::{PlaybackConfig, play_file};

fn main() {
    if let Err(error) = run() {
        eprintln!("play error: {error:#}");
        std::process::exit(1);
    }
}

fn run() -> Result<()> {
    let cli = Cli::parse();
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
