use std::collections::HashMap;

use crate::audio::{AudioCodec, CodecPreference, G711Variant, aac_channel_count};

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RtpMap {
    pub encoding: String,
    pub clock_rate: u32,
    pub channels: Option<u16>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct MediaTrack {
    pub media: String,
    pub formats: Vec<u8>,
    pub direction: String,
    pub control: Option<String>,
    pub rtp_maps: HashMap<u8, RtpMap>,
    pub fmtp: HashMap<u8, HashMap<String, String>>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct G711Codec {
    pub variant: G711Variant,
    pub payload_type: u8,
    pub clock_rate: u32,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SendCodec {
    pub codec: AudioCodec,
    pub payload_type: u8,
    pub clock_rate: u32,
    pub channels: u16,
    pub aac: Option<AacFormat>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct AacFormat {
    pub audio_object_type: u8,
    pub sample_rate: u32,
    pub channel_configuration: u8,
    pub channels: u16,
    pub config: Vec<u8>,
    pub size_length: u8,
    pub index_length: u8,
    pub index_delta_length: u8,
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
                fmtp: HashMap::new(),
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
                    channels: format.next().and_then(|value| value.parse::<u16>().ok()),
                },
            );
        } else if let Some(mapping) = line.strip_prefix("a=fmtp:") {
            let Some((payload, parameters)) = mapping.split_once(char::is_whitespace) else {
                continue;
            };
            let Ok(payload_type) = payload.parse::<u8>() else {
                continue;
            };
            let parameters = parameters
                .split(';')
                .filter_map(|parameter| {
                    let (name, value) = parameter.trim().split_once('=')?;
                    Some((name.trim().to_ascii_lowercase(), value.trim().to_owned()))
                })
                .collect();
            track.fmtp.insert(payload_type, parameters);
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
                channels: Some(1),
            }),
            8 => Some(RtpMap {
                encoding: "PCMA".to_owned(),
                clock_rate: 8000,
                channels: Some(1),
            }),
            _ => None,
        })
}

pub fn pick_g711_codec(track: &MediaTrack) -> Option<G711Codec> {
    for (encoding, variant) in [("PCMA", G711Variant::Pcma), ("PCMU", G711Variant::Pcmu)] {
        for payload_type in &track.formats {
            if *payload_type > 127 {
                continue;
            }
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

pub fn pick_send_codec(
    track: &MediaTrack,
    preference: CodecPreference,
) -> Result<SendCodec, String> {
    if let Some(payload_type) = track
        .formats
        .iter()
        .find(|payload_type| **payload_type > 127)
    {
        return Err(format!(
            "RTP payload type {payload_type} exceeds the seven-bit maximum 127"
        ));
    }
    let preferences: &[(CodecPreference, &str, AudioCodec)] = match preference {
        CodecPreference::Auto => &[
            (CodecPreference::Pcma, "PCMA", AudioCodec::Pcma),
            (CodecPreference::Pcmu, "PCMU", AudioCodec::Pcmu),
            (CodecPreference::G72632, "G726-32", AudioCodec::G72632),
            (CodecPreference::G72624, "G726-24", AudioCodec::G72624),
            (CodecPreference::G72616, "G726-16", AudioCodec::G72616),
            (CodecPreference::G72640, "G726-40", AudioCodec::G72640),
            (CodecPreference::Aac, "MPEG4-GENERIC", AudioCodec::Aac),
        ],
        CodecPreference::Pcma => &[(CodecPreference::Pcma, "PCMA", AudioCodec::Pcma)],
        CodecPreference::Pcmu => &[(CodecPreference::Pcmu, "PCMU", AudioCodec::Pcmu)],
        CodecPreference::G72616 => &[(CodecPreference::G72616, "G726-16", AudioCodec::G72616)],
        CodecPreference::G72624 => &[(CodecPreference::G72624, "G726-24", AudioCodec::G72624)],
        CodecPreference::G72632 => &[(CodecPreference::G72632, "G726-32", AudioCodec::G72632)],
        CodecPreference::G72640 => &[(CodecPreference::G72640, "G726-40", AudioCodec::G72640)],
        CodecPreference::Aac => &[(CodecPreference::Aac, "MPEG4-GENERIC", AudioCodec::Aac)],
    };
    for (_, encoding, codec) in preferences {
        for payload_type in &track.formats {
            let Some(mapping) = rtp_map(track, *payload_type) else {
                continue;
            };
            if mapping.encoding == *encoding
                && (*codec == AudioCodec::Aac
                    || (mapping.clock_rate == 8000 && mapping.channels.unwrap_or(1) == 1))
            {
                let aac = (*codec == AudioCodec::Aac)
                    .then(|| parse_aac_format(track, *payload_type, &mapping))
                    .transpose()?;
                let channels = aac
                    .as_ref()
                    .map_or_else(|| mapping.channels.unwrap_or(1), |aac| aac.channels);
                return Ok(SendCodec {
                    codec: *codec,
                    payload_type: *payload_type,
                    clock_rate: mapping.clock_rate,
                    channels,
                    aac,
                });
            }
        }
    }
    if matches!(preference, CodecPreference::Auto | CodecPreference::Aac)
        && track.formats.iter().any(|payload_type| {
            rtp_map(track, *payload_type).is_some_and(|mapping| mapping.encoding == "MP4A-LATM")
        })
    {
        return Err(
            "MP4A-LATM is recognized but unsupported; AAC requires MPEG4-GENERIC/AAC-hbr"
                .to_owned(),
        );
    }
    Err(format!(
        "backchannel track does not offer requested codec {preference:?}"
    ))
}

fn parse_aac_format(
    track: &MediaTrack,
    payload_type: u8,
    mapping: &RtpMap,
) -> Result<AacFormat, String> {
    let parameters = track
        .fmtp
        .get(&payload_type)
        .ok_or_else(|| format!("MPEG4-GENERIC payload {payload_type} has no fmtp parameters"))?;
    let mode = parameters
        .get("mode")
        .ok_or_else(|| format!("MPEG4-GENERIC payload {payload_type} has no mode"))?;
    if !mode.eq_ignore_ascii_case("AAC-hbr") {
        return Err(format!(
            "MPEG4-GENERIC payload {payload_type} uses unsupported mode {mode}; expected AAC-hbr"
        ));
    }
    if parameters.get("streamtype").map(String::as_str) != Some("5") {
        return Err(format!(
            "MPEG4-GENERIC payload {payload_type} must use streamtype=5"
        ));
    }
    let size_length = required_fmtp_u8(parameters, payload_type, "sizelength")?;
    let index_length = required_fmtp_u8(parameters, payload_type, "indexlength")?;
    let index_delta_length = required_fmtp_u8(parameters, payload_type, "indexdeltalength")?;
    if (size_length, index_length, index_delta_length) != (13, 3, 3) {
        return Err(format!(
            "MPEG4-GENERIC payload {payload_type} requires sizeLength=13, indexLength=3, and indexDeltaLength=3"
        ));
    }
    if parameters
        .get("constantduration")
        .is_some_and(|value| value != "1024")
    {
        return Err(format!(
            "MPEG4-GENERIC payload {payload_type} has unsupported constantDuration"
        ));
    }
    let config_text = parameters
        .get("config")
        .ok_or_else(|| format!("MPEG4-GENERIC payload {payload_type} has no config"))?;
    let config = decode_hex(config_text).map_err(|error| {
        format!("invalid MPEG4-GENERIC config for payload {payload_type}: {error}")
    })?;
    let (audio_object_type, sample_rate, channel_configuration) =
        parse_audio_specific_config(&config)?;
    if audio_object_type != 2 {
        return Err(format!(
            "MPEG4-GENERIC payload {payload_type} is audio object type {audio_object_type}; only AAC-LC (2) is supported"
        ));
    }
    if sample_rate != mapping.clock_rate {
        return Err(format!(
            "MPEG4-GENERIC config sample rate {sample_rate} does not match rtpmap clock {}",
            mapping.clock_rate
        ));
    }
    let channels = aac_channel_count(channel_configuration)?;
    let rtpmap_channels = mapping.channels.unwrap_or(1);
    if channels != rtpmap_channels {
        return Err(format!(
            "MPEG4-GENERIC config channel count {channels} does not match rtpmap channel count {rtpmap_channels}"
        ));
    }
    Ok(AacFormat {
        audio_object_type,
        sample_rate,
        channel_configuration,
        channels,
        config,
        size_length,
        index_length,
        index_delta_length,
    })
}

fn required_fmtp_u8(
    parameters: &HashMap<String, String>,
    payload_type: u8,
    name: &str,
) -> Result<u8, String> {
    parameters
        .get(name)
        .ok_or_else(|| format!("MPEG4-GENERIC payload {payload_type} has no {name}"))?
        .parse()
        .map_err(|_| format!("MPEG4-GENERIC payload {payload_type} has invalid {name}"))
}

fn decode_hex(value: &str) -> Result<Vec<u8>, String> {
    if value.is_empty() || value.len() % 2 != 0 {
        return Err("config must contain an even number of hexadecimal digits".to_owned());
    }
    if !value.is_ascii() {
        return Err("config contains a non-ASCII hexadecimal digit".to_owned());
    }
    value
        .as_bytes()
        .chunks_exact(2)
        .map(|pair| {
            let nibble = |digit| match digit {
                b'0'..=b'9' => Some(digit - b'0'),
                b'a'..=b'f' => Some(digit - b'a' + 10),
                b'A'..=b'F' => Some(digit - b'A' + 10),
                _ => None,
            };
            let high =
                nibble(pair[0]).ok_or_else(|| "config contains a non-hex digit".to_owned())?;
            let low =
                nibble(pair[1]).ok_or_else(|| "config contains a non-hex digit".to_owned())?;
            Ok((high << 4) | low)
        })
        .collect()
}

fn parse_audio_specific_config(config: &[u8]) -> Result<(u8, u32, u8), String> {
    let mut bit = 0usize;
    let audio_object_type = read_bits(config, &mut bit, 5)? as u8;
    if audio_object_type == 31 {
        return Err("extended AAC audio object types are unsupported".to_owned());
    }
    let frequency_index = read_bits(config, &mut bit, 4)? as usize;
    let sample_rate = if frequency_index == 15 {
        read_bits(config, &mut bit, 24)?
    } else {
        const SAMPLE_RATES: [u32; 13] = [
            96_000, 88_200, 64_000, 48_000, 44_100, 32_000, 24_000, 22_050, 16_000, 12_000, 11_025,
            8_000, 7_350,
        ];
        *SAMPLE_RATES
            .get(frequency_index)
            .ok_or_else(|| "AAC config uses a reserved sample-rate index".to_owned())?
    };
    let channel_configuration = read_bits(config, &mut bit, 4)? as u8;
    let frame_length_flag = read_bits(config, &mut bit, 1)?;
    let depends_on_core_coder = read_bits(config, &mut bit, 1)?;
    let extension_flag = read_bits(config, &mut bit, 1)?;
    if frame_length_flag != 0 || depends_on_core_coder != 0 || extension_flag != 0 {
        return Err("AAC GASpecificConfig must use 1024-sample AAC-LC frames".to_owned());
    }
    Ok((audio_object_type, sample_rate, channel_configuration))
}

fn read_bits(data: &[u8], offset: &mut usize, count: usize) -> Result<u32, String> {
    if data.len().saturating_mul(8).saturating_sub(*offset) < count {
        return Err("AAC config is truncated".to_owned());
    }
    let mut value = 0u32;
    for _ in 0..count {
        value = (value << 1) | u32::from((data[*offset / 8] >> (7 - *offset % 8)) & 1);
        *offset += 1;
    }
    Ok(value)
}

#[cfg(test)]
mod tests {
    use std::path::Path;

    use crate::audio::{AudioCodec, CodecPreference, G711Variant, ffmpeg_encode_args};

    use super::{find_backchannel_audio, parse_sdp, pick_g711_codec, pick_send_codec};

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

    #[test]
    fn parses_case_insensitive_fmtp_parameters_for_each_payload_type() {
        let parsed = parse_sdp(
            "v=0\r\n\
             m=audio 0 RTP/AVP 121\r\n\
             a=sendonly\r\n\
             a=rtpmap:121 MPEG4-GENERIC/8000/1\r\n\
             a=fmtp:121 StreamType=5; mode=AAC-hbr; CONFIG=1588; SizeLength=13; IndexLength=3; IndexDeltaLength=3\r\n",
        );

        let track = find_backchannel_audio(&parsed).unwrap();
        let format = &track.fmtp[&121];
        assert_eq!(format["streamtype"], "5");
        assert_eq!(format["mode"], "AAC-hbr");
        assert_eq!(format["config"], "1588");
        assert_eq!(format["sizelength"], "13");
        assert_eq!(format["indexlength"], "3");
        assert_eq!(format["indexdeltalength"], "3");
        assert_eq!(track.rtp_maps[&121].channels, Some(1));
    }

    #[test]
    fn auto_prefers_pcma_before_other_supported_codecs() {
        let parsed = parse_sdp(
            "v=0\r\n\
             m=audio 0 RTP/AVP 121 97 0 8 96\r\n\
             a=sendonly\r\n\
             a=rtpmap:121 MPEG4-GENERIC/8000/1\r\n\
             a=fmtp:121 streamtype=5; mode=AAC-hbr; config=1588; sizelength=13; indexlength=3; indexdeltalength=3\r\n\
             a=rtpmap:97 G726-40/8000\r\n\
             a=rtpmap:96 G726-24/8000\r\n",
        );
        let track = find_backchannel_audio(&parsed).unwrap();

        let codec = pick_send_codec(track, CodecPreference::Auto).unwrap();

        assert_eq!(codec.codec, AudioCodec::Pcma);
        assert_eq!(codec.payload_type, 8);
        assert_eq!(codec.clock_rate, 8000);
    }

    #[test]
    fn auto_uses_the_shared_g726_priority_and_explicit_preferences_match_exactly() {
        let parsed = parse_sdp(
            "v=0\r\n\
             m=audio 0 RTP/AVP 103 102 101 100\r\n\
             a=sendonly\r\n\
             a=rtpmap:103 G726-40/8000\r\n\
             a=rtpmap:102 G726-16/8000\r\n\
             a=rtpmap:101 G726-24/8000\r\n\
             a=rtpmap:100 G726-32/8000\r\n",
        );
        let track = find_backchannel_audio(&parsed).unwrap();

        let automatic = pick_send_codec(track, CodecPreference::Auto).unwrap();
        assert_eq!(automatic.codec, AudioCodec::G72632);
        assert_eq!(automatic.payload_type, 100);

        for (preference, codec, payload_type) in [
            (CodecPreference::G72632, AudioCodec::G72632, 100),
            (CodecPreference::G72624, AudioCodec::G72624, 101),
            (CodecPreference::G72616, AudioCodec::G72616, 102),
            (CodecPreference::G72640, AudioCodec::G72640, 103),
        ] {
            let selected = pick_send_codec(track, preference).unwrap();
            assert_eq!(selected.codec, codec);
            assert_eq!(selected.payload_type, payload_type);
            assert_eq!(selected.clock_rate, 8000);
        }
    }

    #[test]
    fn selects_mpeg4_generic_aac_hbr_and_parses_its_audio_specific_config() {
        let parsed = parse_sdp(
            "v=0\r\n\
             m=audio 0 RTP/AVP 121\r\n\
             a=sendonly\r\n\
             a=rtpmap:121 MPEG4-GENERIC/8000/1\r\n\
             a=fmtp:121 streamtype=5; mode=AAC-hbr; config=1588; sizelength=13; indexlength=3; indexdeltalength=3\r\n",
        );
        let track = find_backchannel_audio(&parsed).unwrap();

        let codec = pick_send_codec(track, CodecPreference::Aac).unwrap();

        assert_eq!(codec.codec, AudioCodec::Aac);
        assert_eq!(codec.payload_type, 121);
        assert_eq!(codec.clock_rate, 8000);
        assert_eq!(codec.channels, 1);
        let aac = codec.aac.unwrap();
        assert_eq!(aac.audio_object_type, 2);
        assert_eq!(aac.sample_rate, 8000);
        assert_eq!(aac.channel_configuration, 1);
        assert_eq!(aac.config, [0x15, 0x88]);
        assert_eq!(aac.size_length, 13);
        assert_eq!(aac.index_length, 3);
        assert_eq!(aac.index_delta_length, 3);
    }

    #[test]
    fn rejects_non_ascii_and_non_hex_aac_config_without_panicking() {
        for config in ["0é0", "15zz"] {
            let sdp = format!(
                "m=audio 0 RTP/AVP 121\r\na=sendonly\r\na=rtpmap:121 MPEG4-GENERIC/8000/1\r\na=fmtp:121 streamtype=5; mode=AAC-hbr; config={config}; sizelength=13; indexlength=3; indexdeltalength=3\r\n"
            );
            let parsed = parse_sdp(&sdp);

            let error = pick_send_codec(
                find_backchannel_audio(&parsed).unwrap(),
                CodecPreference::Aac,
            )
            .unwrap_err();

            assert!(error.contains("config"));
        }
    }

    #[test]
    fn rejects_rtp_payload_types_above_seven_bits() {
        let parsed = parse_sdp(
            "m=audio 0 RTP/AVP 200\r\n\
             a=sendonly\r\n\
             a=rtpmap:200 PCMA/8000/1\r\n",
        );

        let error = pick_send_codec(
            find_backchannel_audio(&parsed).unwrap(),
            CodecPreference::Pcma,
        )
        .unwrap_err();

        assert!(error.contains("payload type 200"));
        assert!(error.contains("127"));
        assert!(pick_g711_codec(find_backchannel_audio(&parsed).unwrap()).is_none());
    }

    #[test]
    fn maps_aac_channel_configuration_and_passes_actual_channels_to_ffmpeg() {
        for (configuration, expected_channels) in
            [(1, 1), (2, 2), (3, 3), (4, 4), (5, 5), (6, 6), (7, 8)]
        {
            let config = format!("15{:02x}", 0x80 | (configuration << 3));
            let sdp = format!(
                "m=audio 0 RTP/AVP 121\r\na=sendonly\r\na=rtpmap:121 MPEG4-GENERIC/8000/{expected_channels}\r\na=fmtp:121 streamtype=5; mode=AAC-hbr; config={config}; sizelength=13; indexlength=3; indexdeltalength=3\r\n"
            );
            let parsed = parse_sdp(&sdp);
            let codec = pick_send_codec(
                find_backchannel_audio(&parsed).unwrap(),
                CodecPreference::Aac,
            )
            .unwrap();

            assert_eq!(codec.channels, expected_channels);
            let args = ffmpeg_encode_args(
                Path::new("event.mp3"),
                AudioCodec::Aac,
                8000,
                codec.channels,
                0.05,
            )
            .unwrap()
            .into_iter()
            .map(|argument| argument.to_string_lossy().into_owned())
            .collect::<Vec<_>>();
            assert!(
                args.windows(2)
                    .any(|pair| pair == ["-ac", expected_channels.to_string().as_str()])
            );
        }
    }

    #[test]
    fn treats_omitted_aac_rtpmap_channels_as_mono_and_rejects_mismatches() {
        let mono = parse_sdp(
            "m=audio 0 RTP/AVP 121\r\n\
             a=sendonly\r\n\
             a=rtpmap:121 MPEG4-GENERIC/8000\r\n\
             a=fmtp:121 streamtype=5; mode=AAC-hbr; config=1588; sizelength=13; indexlength=3; indexdeltalength=3\r\n",
        );
        assert_eq!(
            pick_send_codec(find_backchannel_audio(&mono).unwrap(), CodecPreference::Aac)
                .unwrap()
                .channels,
            1
        );

        for (rtpmap, config) in [
            ("MPEG4-GENERIC/8000", "1590"),
            ("MPEG4-GENERIC/8000/7", "15b8"),
        ] {
            let sdp = format!(
                "m=audio 0 RTP/AVP 121\r\na=sendonly\r\na=rtpmap:121 {rtpmap}\r\na=fmtp:121 streamtype=5; mode=AAC-hbr; config={config}; sizelength=13; indexlength=3; indexdeltalength=3\r\n"
            );
            let parsed = parse_sdp(&sdp);
            let error = pick_send_codec(
                find_backchannel_audio(&parsed).unwrap(),
                CodecPreference::Aac,
            )
            .unwrap_err();
            assert!(error.contains("channel count"));
        }

        let pce = parse_sdp(
            "m=audio 0 RTP/AVP 121\r\n\
             a=sendonly\r\n\
             a=rtpmap:121 MPEG4-GENERIC/8000/1\r\n\
             a=fmtp:121 streamtype=5; mode=AAC-hbr; config=1580; sizelength=13; indexlength=3; indexdeltalength=3\r\n",
        );
        let error = pick_send_codec(find_backchannel_audio(&pce).unwrap(), CodecPreference::Aac)
            .unwrap_err();
        assert!(error.contains("program-config-element"));
    }

    #[test]
    fn rejects_non_strict_aac_streamtype_and_1024_sample_asc_flags() {
        for fmtp in [
            "mode=AAC-hbr; config=1588; sizelength=13; indexlength=3; indexdeltalength=3",
            "streamtype=4; mode=AAC-hbr; config=1588; sizelength=13; indexlength=3; indexdeltalength=3",
        ] {
            let sdp = format!(
                "m=audio 0 RTP/AVP 121\r\na=sendonly\r\na=rtpmap:121 MPEG4-GENERIC/8000/1\r\na=fmtp:121 {fmtp}\r\n"
            );
            let parsed = parse_sdp(&sdp);
            let track = find_backchannel_audio(&parsed).unwrap();
            assert!(pick_send_codec(track, CodecPreference::Aac).is_err());
        }

        let parsed = parse_sdp(
            "m=audio 0 RTP/AVP 121\r\n\
             a=sendonly\r\n\
             a=rtpmap:121 MPEG4-GENERIC/8000/1\r\n\
             a=fmtp:121 streamtype=5; mode=AAC-hbr; config=158c; sizelength=13; indexlength=3; indexdeltalength=3\r\n",
        );
        let track = find_backchannel_audio(&parsed).unwrap();
        let error = pick_send_codec(track, CodecPreference::Aac).unwrap_err();
        assert!(error.contains("1024"));
    }

    #[test]
    fn recognizes_mp4a_latm_but_rejects_it_with_a_specific_error() {
        let parsed = parse_sdp(
            "v=0\r\n\
             m=audio 0 RTP/AVP 122\r\n\
             a=sendonly\r\n\
             a=rtpmap:122 MP4A-LATM/8000/1\r\n\
             a=fmtp:122 profile-level-id=1; object=2; cpresent=0; config=40002b10\r\n",
        );
        let track = find_backchannel_audio(&parsed).unwrap();

        let error = pick_send_codec(track, CodecPreference::Aac).unwrap_err();

        assert!(error.contains("MP4A-LATM"));
        assert!(error.contains("unsupported"));
        assert!(error.contains("MPEG4-GENERIC"));
    }
}
