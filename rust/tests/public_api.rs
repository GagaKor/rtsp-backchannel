use std::path::PathBuf;

use onvif_backchannel::playback::{PlaybackConfig, PlaybackResult, play_file};

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

    assert_eq!(config.host, "camera.local");
    assert_eq!(config.volume, 0.05);
}
