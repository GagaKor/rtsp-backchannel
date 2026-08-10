use std::path::{Path, PathBuf};

use rtsp_backchannel::audio::{CodecPreference, G711Variant};
use rtsp_backchannel::onvif::{
    PtzSession, PtzSessionOptions, PtzStatus, PtzVector, open_ptz_session,
};
use rtsp_backchannel::playback::{PlaybackConfig, PlaybackResult, play_file, play_file_with_codec};

#[test]
fn exposes_one_shot_file_playback_as_a_library_api() {
    let config = PlaybackConfig {
        host: "camera.local".to_owned(),
        user: "admin".to_owned(),
        password: "secret".to_owned(),
        file: PathBuf::from("event.mp3"),
        volume: 0.05,
    };
    let _play: fn(&PlaybackConfig) -> anyhow::Result<PlaybackResult> = play_file;
    let _play_with_codec: fn(&PlaybackConfig, CodecPreference) -> anyhow::Result<PlaybackResult> =
        play_file_with_codec;
    let _variant_type: fn(&PlaybackResult) -> Option<G711Variant> = |result| result.variant;

    assert_eq!(config.host, "camera.local");
    assert_eq!(config.volume, 0.05);
}

#[test]
fn exposes_the_complete_ptz_session_contract_as_a_library_api() {
    let _open: fn(&PtzSessionOptions) -> Result<PtzSession, String> = open_ptz_session;
    let options = PtzSessionOptions::new("camera.local", "operator", "example-only");
    let vector = PtzVector { x: 0.5, y: -0.5 };
    let status = PtzStatus {
        pan_tilt: Some(vector),
        zoom: Some(0.25),
        pan_tilt_move_status: Some("MOVING".to_owned()),
        zoom_move_status: Some("IDLE".to_owned()),
        utc_time: Some("2026-08-10T00:00:00Z".to_owned()),
    };

    assert_eq!(options.host, "camera.local");
    assert_eq!(status.pan_tilt, Some(vector));
}

#[test]
fn publishes_under_a_permissive_dual_license() {
    assert_eq!(env!("CARGO_PKG_LICENSE"), "MIT OR Apache-2.0");
    assert_eq!(env!("CARGO_PKG_NAME"), "rtsp-backchannel");
    assert_eq!(env!("CARGO_PKG_RUST_VERSION"), "1.86");
    assert_eq!(env!("CARGO_PKG_README"), "README.md");
    assert_eq!(
        env!("CARGO_PKG_REPOSITORY"),
        "https://github.com/GagaKor/rtsp-backchannel"
    );
    assert_eq!(
        env!("CARGO_PKG_HOMEPAGE"),
        "https://github.com/GagaKor/rtsp-backchannel"
    );
    for filename in [
        "README.md",
        "README.ko.md",
        "LICENSE",
        "LICENSE-MIT",
        "LICENSE-APACHE",
        "THIRD_PARTY_NOTICES.md",
    ] {
        assert!(
            Path::new(env!("CARGO_MANIFEST_DIR"))
                .join(filename)
                .is_file()
        );
    }

    for filename in ["README.md", "README.ko.md"] {
        let readme =
            std::fs::read_to_string(Path::new(env!("CARGO_MANIFEST_DIR")).join(filename)).unwrap();
        assert!(readme.contains("Rust"));
        assert!(readme.contains("rust/README.md"));
        assert!(readme.contains("rust/README.ko.md"));
        assert!(!readme.contains("```typescript"));
        assert!(!readme.contains("```python"));
        assert!(readme.contains("cidrs"));
        assert!(readme.contains("10.0.0.0/24"));
        assert!(readme.contains("10.128.0.10"));
        assert!(readme.contains("--cidr"));
    }
}
