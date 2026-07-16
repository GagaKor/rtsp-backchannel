use anyhow::{Context, Result, anyhow};
use clap::Parser;
use onvif_backchannel_rs::audio::{G711Variant, decode_file, encode_g711};
use onvif_backchannel_rs::backchannel::{BackchannelSession, SAMPLE_RATE};
use onvif_backchannel_rs::cli::Cli;

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
    let samples = decode_file(&cli.file).map_err(|error| anyhow!(error))?;

    let mut session = BackchannelSession::open(&cli.host, &cli.user, &cli.password)
        .map_err(|error| anyhow!(error))
        .context("failed to open ONVIF backchannel")?;
    println!(
        "backchannel open: {}/{} pt={} ch={}",
        variant_name(session.variant),
        SAMPLE_RATE,
        session.payload_type,
        session.rtp_channel
    );

    let playback = (|| -> Result<usize, String> {
        let encoded = encode_g711(&samples, session.variant, cli.volume)?;
        println!(
            "transcoded: {} bytes (~{:.1}s {} 8kHz mono)",
            encoded.len(),
            encoded.len() as f64 / SAMPLE_RATE as f64,
            variant_name(session.variant)
        );
        session.send(&encoded)
    })();
    let cleanup = session.close();
    let sent = match (playback, cleanup) {
        (Ok(sent), Ok(())) => sent,
        (Err(error), Ok(())) => return Err(anyhow!(error)),
        (Ok(_), Err(cleanup)) => return Err(anyhow!(cleanup)),
        (Err(error), Err(cleanup)) => {
            return Err(anyhow!("{error}; RTSP cleanup also failed: {cleanup}"));
        }
    };
    println!("sent {sent} RTP packets");
    Ok(())
}

fn variant_name(variant: G711Variant) -> &'static str {
    match variant {
        G711Variant::Pcma => "PCMA",
        G711Variant::Pcmu => "PCMU",
    }
}
