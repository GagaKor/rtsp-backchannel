use std::path::{Path, PathBuf};
use std::sync::mpsc::{self, RecvTimeoutError};
use std::thread;
use std::time::Duration;

use anyhow::{Context, Result, anyhow};

use crate::audio::{AudioCodec, AudioFrame, CodecPreference, G711Variant, transcode_file};
use crate::backchannel::BackchannelSession;

#[derive(Clone, Debug, PartialEq)]
pub struct PlaybackConfig {
    pub host: String,
    pub user: String,
    pub password: String,
    pub file: PathBuf,
    pub volume: f64,
}

#[derive(Clone, Debug, PartialEq)]
pub struct PlaybackResult {
    pub codec: AudioCodec,
    pub variant: Option<G711Variant>,
    pub sample_rate: u64,
    pub channels: u16,
    pub payload_type: u8,
    pub rtp_channel: u8,
    pub encoded_bytes: usize,
    pub packets_sent: usize,
    pub duration_seconds: f64,
}

pub fn play_file(config: &PlaybackConfig) -> Result<PlaybackResult> {
    play_file_with_codec(config, CodecPreference::Auto)
}

pub fn play_file_with_codec(
    config: &PlaybackConfig,
    preference: CodecPreference,
) -> Result<PlaybackResult> {
    play_with(
        config,
        preference,
        |path, codec, sample_rate, channels, volume| {
            transcode_file(path, codec, sample_rate, channels, volume)
        },
        BackchannelSession::open_with_codec,
    )
}

trait PlaybackSession {
    fn codec(&self) -> AudioCodec;
    fn variant(&self) -> Option<G711Variant>;
    fn clock_rate(&self) -> u32;
    fn channels(&self) -> u16;
    fn payload_type(&self) -> u8;
    fn rtp_channel(&self) -> u8;
    fn keepalive_wait(&self) -> Duration;
    fn keep_alive(&mut self) -> Result<(), String>;
    fn send_frames(&mut self, frames: &[AudioFrame]) -> Result<usize, String>;
    fn close(&mut self) -> Result<(), String>;
}

impl PlaybackSession for BackchannelSession {
    fn codec(&self) -> AudioCodec {
        self.codec
    }

    fn variant(&self) -> Option<G711Variant> {
        self.variant
    }

    fn clock_rate(&self) -> u32 {
        self.clock_rate
    }

    fn channels(&self) -> u16 {
        self.channels
    }

    fn payload_type(&self) -> u8 {
        self.payload_type
    }

    fn rtp_channel(&self) -> u8 {
        self.rtp_channel
    }

    fn keepalive_wait(&self) -> Duration {
        BackchannelSession::keepalive_wait(self)
    }

    fn keep_alive(&mut self) -> Result<(), String> {
        BackchannelSession::keep_alive(self)
    }

    fn send_frames(&mut self, frames: &[AudioFrame]) -> Result<usize, String> {
        BackchannelSession::send_frames(self, frames)
    }

    fn close(&mut self) -> Result<(), String> {
        BackchannelSession::close(self)
    }
}

fn transcode_with_keepalive<E, S>(session: &mut S, transcode: E) -> Result<Vec<AudioFrame>, String>
where
    E: FnOnce() -> Result<Vec<AudioFrame>, String> + Send,
    S: PlaybackSession,
{
    let (encoded, keepalive_error) = thread::scope(|scope| {
        let (finished, completion) = mpsc::sync_channel(1);
        let encoder = scope.spawn(move || {
            let result = transcode();
            let _ = finished.send(());
            result
        });
        let keepalive_error = loop {
            match completion.recv_timeout(session.keepalive_wait()) {
                Ok(()) | Err(RecvTimeoutError::Disconnected) => break None,
                Err(RecvTimeoutError::Timeout) => {
                    if let Err(error) = session.keep_alive() {
                        break Some(error);
                    }
                }
            }
        };
        let encoded = encoder
            .join()
            .unwrap_or_else(|_| Err("audio encoder thread panicked".to_owned()));
        (encoded, keepalive_error)
    });
    match (encoded, keepalive_error) {
        (Ok(frames), None) => Ok(frames),
        (Err(error), None) => Err(error),
        (Ok(_), Some(keepalive)) => Err(keepalive),
        (Err(error), Some(keepalive)) => {
            Err(format!("{error}; RTSP keepalive also failed: {keepalive}"))
        }
    }
}

fn play_with<E, O, S>(
    config: &PlaybackConfig,
    preference: CodecPreference,
    transcode: E,
    open: O,
) -> Result<PlaybackResult>
where
    E: FnOnce(&Path, AudioCodec, u32, u16, f64) -> Result<Vec<AudioFrame>, String> + Send,
    O: FnOnce(&str, &str, &str, CodecPreference) -> Result<S, String>,
    S: PlaybackSession,
{
    if !config.volume.is_finite() || !(0.0..=1.0).contains(&config.volume) {
        return Err(anyhow!("volume must be finite and between 0 and 1"));
    }

    let mut session = open(&config.host, &config.user, &config.password, preference)
        .map_err(|error| anyhow!(error))
        .context("failed to open RTSP backchannel")?;
    let codec = session.codec();
    let clock_rate = session.clock_rate();
    let channels = session.channels();
    let variant = session.variant();
    let payload_type = session.payload_type();
    let rtp_channel = session.rtp_channel();

    let playback = (|| -> Result<(usize, usize, f64), String> {
        let frames = transcode_with_keepalive(&mut session, move || {
            transcode(&config.file, codec, clock_rate, channels, config.volume)
        })?;
        let encoded_bytes = frames.iter().map(|frame| frame.payload.len()).sum();
        let packets_sent = session.send_frames(&frames)?;
        let duration_seconds = frames
            .iter()
            .map(|frame| f64::from(frame.samples) / f64::from(clock_rate))
            .sum();
        Ok((encoded_bytes, packets_sent, duration_seconds))
    })();
    let cleanup = session.close();
    let (encoded_bytes, packets_sent, duration_seconds) = match (playback, cleanup) {
        (Ok(result), Ok(())) => result,
        (Err(error), Ok(())) => return Err(anyhow!(error)),
        (Ok(_), Err(cleanup)) => return Err(anyhow!(cleanup)),
        (Err(error), Err(cleanup)) => {
            return Err(anyhow!("{error}; RTSP cleanup also failed: {cleanup}"));
        }
    };

    Ok(PlaybackResult {
        codec,
        variant,
        sample_rate: u64::from(clock_rate),
        channels,
        payload_type,
        rtp_channel,
        encoded_bytes,
        packets_sent,
        duration_seconds,
    })
}

#[cfg(test)]
mod tests {
    use std::cell::{Cell, RefCell};
    use std::path::PathBuf;
    use std::rc::Rc;
    use std::thread;
    use std::time::Duration;

    use crate::audio::{AudioCodec, AudioFrame, CodecPreference, G711Variant};

    use super::{PlaybackConfig, PlaybackSession, play_with, transcode_with_keepalive};

    struct FakeSession {
        codec: AudioCodec,
        variant: Option<G711Variant>,
        sent: Rc<RefCell<Vec<u8>>>,
        closed: Rc<Cell<usize>>,
        keepalives: Rc<Cell<usize>>,
        keepalive_interval: Duration,
        keepalive_result: Result<(), String>,
        send_result: Result<usize, String>,
    }

    impl PlaybackSession for FakeSession {
        fn codec(&self) -> AudioCodec {
            self.codec
        }

        fn variant(&self) -> Option<G711Variant> {
            self.variant
        }

        fn clock_rate(&self) -> u32 {
            8000
        }

        fn channels(&self) -> u16 {
            1
        }

        fn payload_type(&self) -> u8 {
            8
        }

        fn rtp_channel(&self) -> u8 {
            10
        }

        fn keepalive_wait(&self) -> Duration {
            self.keepalive_interval
        }

        fn keep_alive(&mut self) -> Result<(), String> {
            self.keepalives.set(self.keepalives.get() + 1);
            self.keepalive_result.clone()
        }

        fn send_frames(&mut self, frames: &[AudioFrame]) -> Result<usize, String> {
            for frame in frames {
                self.sent.borrow_mut().extend_from_slice(&frame.payload);
            }
            self.send_result.clone()
        }

        fn close(&mut self) -> Result<(), String> {
            self.closed.set(self.closed.get() + 1);
            Ok(())
        }
    }

    fn config() -> PlaybackConfig {
        PlaybackConfig {
            host: "camera.local".to_owned(),
            user: "admin".to_owned(),
            password: "secret".to_owned(),
            file: PathBuf::from("event.mp3"),
            volume: 0.05,
        }
    }

    #[test]
    fn returns_negotiated_metadata_and_playback_counts() {
        let sent = Rc::new(RefCell::new(Vec::new()));
        let closed = Rc::new(Cell::new(0));
        let result = play_with(
            &config(),
            CodecPreference::Auto,
            |path, _codec, _sample_rate, _channels, _volume| {
                assert_eq!(path.to_string_lossy(), "event.mp3");
                Ok(vec![AudioFrame {
                    payload: vec![0; 640],
                    samples: 640,
                    marker: true,
                }])
            },
            {
                let sent = Rc::clone(&sent);
                let closed = Rc::clone(&closed);
                move |host, user, password, _preference| {
                    assert_eq!((host, user, password), ("camera.local", "admin", "secret"));
                    Ok(FakeSession {
                        codec: AudioCodec::Pcma,
                        variant: Some(G711Variant::Pcma),
                        sent,
                        closed,
                        keepalives: Rc::new(Cell::new(0)),
                        keepalive_interval: Duration::from_secs(60),
                        keepalive_result: Ok(()),
                        send_result: Ok(2),
                    })
                }
            },
        )
        .unwrap();

        assert_eq!(result.variant, Some(G711Variant::Pcma));
        assert_eq!(result.codec, AudioCodec::Pcma);
        assert_eq!(result.sample_rate, 8000);
        assert_eq!(result.payload_type, 8);
        assert_eq!(result.rtp_channel, 10);
        assert_eq!(result.encoded_bytes, 640);
        assert_eq!(result.packets_sent, 2);
        assert_eq!(result.duration_seconds, 0.08);
        assert_eq!(sent.borrow().len(), 640);
        assert_eq!(closed.get(), 1);
    }

    #[test]
    fn omits_g711_variant_for_g726_playback() {
        let result = play_with(
            &config(),
            CodecPreference::G72632,
            |_, codec, sample_rate, channels, _| {
                assert_eq!(codec, AudioCodec::G72632);
                assert_eq!(sample_rate, 8000);
                assert_eq!(channels, 1);
                Ok(vec![AudioFrame {
                    payload: vec![0; 160],
                    samples: 320,
                    marker: false,
                }])
            },
            |_, _, _, preference| {
                assert_eq!(preference, CodecPreference::G72632);
                Ok(FakeSession {
                    codec: AudioCodec::G72632,
                    variant: None,
                    sent: Rc::new(RefCell::new(Vec::new())),
                    closed: Rc::new(Cell::new(0)),
                    keepalives: Rc::new(Cell::new(0)),
                    keepalive_interval: Duration::from_secs(60),
                    keepalive_result: Ok(()),
                    send_result: Ok(1),
                })
            },
        )
        .unwrap();

        assert_eq!(result.codec, AudioCodec::G72632);
        assert_eq!(result.variant, None);
    }

    #[test]
    fn keeps_the_rtsp_session_alive_while_transcoding() {
        let keepalives = Rc::new(Cell::new(0));
        let mut session = FakeSession {
            codec: AudioCodec::Pcma,
            variant: Some(G711Variant::Pcma),
            sent: Rc::new(RefCell::new(Vec::new())),
            closed: Rc::new(Cell::new(0)),
            keepalives: Rc::clone(&keepalives),
            keepalive_interval: Duration::from_millis(5),
            keepalive_result: Ok(()),
            send_result: Ok(1),
        };

        let frames = transcode_with_keepalive(&mut session, || {
            thread::sleep(Duration::from_millis(30));
            Ok(vec![AudioFrame {
                payload: vec![0; 320],
                samples: 320,
                marker: false,
            }])
        })
        .unwrap();

        assert_eq!(frames.len(), 1);
        assert!(keepalives.get() >= 2);
    }

    #[test]
    fn combines_encoder_and_keepalive_failures_and_closes_the_session() {
        let closed = Rc::new(Cell::new(0));
        let error = play_with(
            &config(),
            CodecPreference::Auto,
            |_, _, _, _, _| {
                thread::sleep(Duration::from_millis(10));
                Err("encoder failed".to_owned())
            },
            {
                let closed = Rc::clone(&closed);
                move |_, _, _, _| {
                    Ok(FakeSession {
                        codec: AudioCodec::Pcma,
                        variant: Some(G711Variant::Pcma),
                        sent: Rc::new(RefCell::new(Vec::new())),
                        closed,
                        keepalives: Rc::new(Cell::new(0)),
                        keepalive_interval: Duration::from_millis(1),
                        keepalive_result: Err("keepalive failed".to_owned()),
                        send_result: Ok(0),
                    })
                }
            },
        )
        .unwrap_err();

        assert!(error.to_string().contains("encoder failed"));
        assert!(error.to_string().contains("keepalive failed"));
        assert_eq!(closed.get(), 1);
    }

    #[test]
    fn closes_the_session_when_sending_fails() {
        let closed = Rc::new(Cell::new(0));
        let error = play_with(
            &config(),
            CodecPreference::Auto,
            |_, _, _, _, _| {
                Ok(vec![AudioFrame {
                    payload: vec![0; 320],
                    samples: 320,
                    marker: true,
                }])
            },
            {
                let closed = Rc::clone(&closed);
                move |_, _, _, _| {
                    Ok(FakeSession {
                        codec: AudioCodec::Pcma,
                        variant: Some(G711Variant::Pcma),
                        sent: Rc::new(RefCell::new(Vec::new())),
                        closed,
                        keepalives: Rc::new(Cell::new(0)),
                        keepalive_interval: Duration::from_secs(60),
                        keepalive_result: Ok(()),
                        send_result: Err("send failed".to_owned()),
                    })
                }
            },
        )
        .unwrap_err();

        assert!(error.to_string().contains("send failed"));
        assert_eq!(closed.get(), 1);
    }
}
