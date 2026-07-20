use std::path::{Path, PathBuf};

use anyhow::{Context, Result, anyhow};

use crate::audio::{G711Variant, decode_file, encode_g711};
use crate::backchannel::{BackchannelSession, SAMPLE_RATE};

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
    pub variant: G711Variant,
    pub sample_rate: u64,
    pub payload_type: u8,
    pub rtp_channel: u8,
    pub encoded_bytes: usize,
    pub packets_sent: usize,
    pub duration_seconds: f64,
}

pub fn play_file(config: &PlaybackConfig) -> Result<PlaybackResult> {
    play_with(config, decode_file, BackchannelSession::open)
}

trait PlaybackSession {
    fn variant(&self) -> G711Variant;
    fn payload_type(&self) -> u8;
    fn rtp_channel(&self) -> u8;
    fn send(&mut self, encoded: &[u8]) -> Result<usize, String>;
    fn close(&mut self) -> Result<(), String>;
}

impl PlaybackSession for BackchannelSession {
    fn variant(&self) -> G711Variant {
        self.variant
    }

    fn payload_type(&self) -> u8 {
        self.payload_type
    }

    fn rtp_channel(&self) -> u8 {
        self.rtp_channel
    }

    fn send(&mut self, encoded: &[u8]) -> Result<usize, String> {
        BackchannelSession::send(self, encoded)
    }

    fn close(&mut self) -> Result<(), String> {
        BackchannelSession::close(self)
    }
}

fn play_with<D, O, S>(config: &PlaybackConfig, decode: D, open: O) -> Result<PlaybackResult>
where
    D: FnOnce(&Path) -> Result<Vec<i16>, String>,
    O: FnOnce(&str, &str, &str) -> Result<S, String>,
    S: PlaybackSession,
{
    if !config.volume.is_finite() || !(0.0..=1.0).contains(&config.volume) {
        return Err(anyhow!("volume must be finite and between 0 and 1"));
    }

    let samples = decode(&config.file)
        .map_err(|error| anyhow!(error))
        .context("failed to decode audio file")?;
    let mut session = open(&config.host, &config.user, &config.password)
        .map_err(|error| anyhow!(error))
        .context("failed to open ONVIF backchannel")?;
    let variant = session.variant();
    let payload_type = session.payload_type();
    let rtp_channel = session.rtp_channel();

    let playback = (|| -> Result<(usize, usize), String> {
        let encoded = encode_g711(&samples, variant, config.volume)?;
        let encoded_bytes = encoded.len();
        let packets_sent = session.send(&encoded)?;
        Ok((encoded_bytes, packets_sent))
    })();
    let cleanup = session.close();
    let (encoded_bytes, packets_sent) = match (playback, cleanup) {
        (Ok(result), Ok(())) => result,
        (Err(error), Ok(())) => return Err(anyhow!(error)),
        (Ok(_), Err(cleanup)) => return Err(anyhow!(cleanup)),
        (Err(error), Err(cleanup)) => {
            return Err(anyhow!("{error}; RTSP cleanup also failed: {cleanup}"));
        }
    };

    Ok(PlaybackResult {
        variant,
        sample_rate: SAMPLE_RATE,
        payload_type,
        rtp_channel,
        encoded_bytes,
        packets_sent,
        duration_seconds: encoded_bytes as f64 / SAMPLE_RATE as f64,
    })
}

#[cfg(test)]
mod tests {
    use std::cell::{Cell, RefCell};
    use std::path::PathBuf;
    use std::rc::Rc;

    use crate::audio::G711Variant;

    use super::{PlaybackConfig, PlaybackSession, play_with};

    struct FakeSession {
        sent: Rc<RefCell<Vec<u8>>>,
        closed: Rc<Cell<usize>>,
        send_result: Result<usize, String>,
    }

    impl PlaybackSession for FakeSession {
        fn variant(&self) -> G711Variant {
            G711Variant::Pcma
        }

        fn payload_type(&self) -> u8 {
            8
        }

        fn rtp_channel(&self) -> u8 {
            10
        }

        fn send(&mut self, encoded: &[u8]) -> Result<usize, String> {
            self.sent.borrow_mut().extend_from_slice(encoded);
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
            |path| {
                assert_eq!(path.to_string_lossy(), "event.mp3");
                Ok(vec![1000; 640])
            },
            {
                let sent = Rc::clone(&sent);
                let closed = Rc::clone(&closed);
                move |host, user, password| {
                    assert_eq!((host, user, password), ("camera.local", "admin", "secret"));
                    Ok(FakeSession {
                        sent,
                        closed,
                        send_result: Ok(2),
                    })
                }
            },
        )
        .unwrap();

        assert_eq!(result.variant, G711Variant::Pcma);
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
    fn closes_the_session_when_sending_fails() {
        let closed = Rc::new(Cell::new(0));
        let error = play_with(&config(), |_| Ok(vec![1000; 320]), {
            let closed = Rc::clone(&closed);
            move |_, _, _| {
                Ok(FakeSession {
                    sent: Rc::new(RefCell::new(Vec::new())),
                    closed,
                    send_result: Err("send failed".to_owned()),
                })
            }
        })
        .unwrap_err();

        assert!(error.to_string().contains("send failed"));
        assert_eq!(closed.get(), 1);
    }
}
