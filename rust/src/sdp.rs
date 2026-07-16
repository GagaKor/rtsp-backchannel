use std::collections::HashMap;

use crate::audio::G711Variant;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RtpMap {
    pub encoding: String,
    pub clock_rate: u32,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct MediaTrack {
    pub media: String,
    pub formats: Vec<u8>,
    pub direction: String,
    pub control: Option<String>,
    pub rtp_maps: HashMap<u8, RtpMap>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct G711Codec {
    pub variant: G711Variant,
    pub payload_type: u8,
    pub clock_rate: u32,
}

pub fn parse_sdp(text: &str) -> Vec<MediaTrack> {
    let mut tracks = Vec::new();
    for raw_line in text.lines() {
        let line = raw_line.trim();
        if let Some(media) = line.strip_prefix("m=") {
            let fields: Vec<_> = media.split_whitespace().collect();
            if fields.len() < 4 {
                continue;
            }
            tracks.push(MediaTrack {
                media: fields[0].to_owned(),
                formats: fields[3..]
                    .iter()
                    .filter_map(|field| field.parse().ok())
                    .collect(),
                direction: String::new(),
                control: None,
                rtp_maps: HashMap::new(),
            });
            continue;
        }
        let Some(track) = tracks.last_mut() else {
            continue;
        };
        if matches!(
            line,
            "a=sendonly" | "a=recvonly" | "a=sendrecv" | "a=inactive"
        ) {
            track.direction = line[2..].to_owned();
        } else if let Some(control) = line.strip_prefix("a=control:") {
            track.control = Some(control.trim().to_owned());
        } else if let Some(mapping) = line.strip_prefix("a=rtpmap:") {
            let Some((payload, format)) = mapping.split_once(char::is_whitespace) else {
                continue;
            };
            let mut format = format.trim().split('/');
            let (Ok(payload_type), Some(encoding), Some(clock_rate)) = (
                payload.parse::<u8>(),
                format.next(),
                format.next().and_then(|value| value.parse::<u32>().ok()),
            ) else {
                continue;
            };
            track.rtp_maps.insert(
                payload_type,
                RtpMap {
                    encoding: encoding.to_ascii_uppercase(),
                    clock_rate,
                },
            );
        }
    }
    tracks
}

pub fn find_backchannel_audio(tracks: &[MediaTrack]) -> Option<&MediaTrack> {
    tracks
        .iter()
        .find(|track| track.media == "audio" && track.direction == "sendonly")
}

fn rtp_map(track: &MediaTrack, payload_type: u8) -> Option<RtpMap> {
    track
        .rtp_maps
        .get(&payload_type)
        .cloned()
        .or_else(|| match payload_type {
            0 => Some(RtpMap {
                encoding: "PCMU".to_owned(),
                clock_rate: 8000,
            }),
            8 => Some(RtpMap {
                encoding: "PCMA".to_owned(),
                clock_rate: 8000,
            }),
            _ => None,
        })
}

pub fn pick_g711_codec(track: &MediaTrack) -> Option<G711Codec> {
    for (encoding, variant) in [("PCMA", G711Variant::Pcma), ("PCMU", G711Variant::Pcmu)] {
        for payload_type in &track.formats {
            let Some(mapping) = rtp_map(track, *payload_type) else {
                continue;
            };
            if mapping.encoding == encoding && mapping.clock_rate == 8000 {
                return Some(G711Codec {
                    variant,
                    payload_type: *payload_type,
                    clock_rate: mapping.clock_rate,
                });
            }
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use crate::audio::G711Variant;

    use super::{find_backchannel_audio, parse_sdp, pick_g711_codec};

    #[test]
    fn finds_receive_tracks_and_prefers_pcma_on_the_sendonly_track() {
        let parsed = parse_sdp(
            "v=0\r\n\
             m=video 0 RTP/AVP 96\r\n\
             a=recvonly\r\n\
             a=control:trackID=0\r\n\
             m=audio 0 RTP/AVP 0\r\n\
             a=recvonly\r\n\
             a=control:trackID=1\r\n\
             m=audio 0 RTP/AVP 0 8\r\n\
             a=sendonly\r\n\
             a=control:trackID=5\r\n\
             a=rtpmap:0 PCMU/8000\r\n\
             a=rtpmap:8 PCMA/8000\r\n",
        );

        let receive_controls: Vec<_> = parsed
            .iter()
            .filter(|track| track.direction == "recvonly")
            .map(|track| track.control.as_deref().unwrap())
            .collect();
        assert_eq!(receive_controls, ["trackID=0", "trackID=1"]);

        let track = find_backchannel_audio(&parsed).unwrap();
        let codec = pick_g711_codec(track).unwrap();
        assert_eq!(codec.variant, G711Variant::Pcma);
        assert_eq!(codec.payload_type, 8);
        assert_eq!(codec.clock_rate, 8000);
    }
}
