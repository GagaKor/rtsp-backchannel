pub struct RtpPacketizer {
    payload_type: u8,
    ssrc: u32,
    sequence: u16,
    timestamp: u32,
    first_packet: bool,
}

impl RtpPacketizer {
    pub fn new_random(payload_type: u8) -> Self {
        Self::with_identity(payload_type, rand::random(), rand::random(), rand::random())
    }

    pub fn with_identity(payload_type: u8, ssrc: u32, sequence: u16, timestamp: u32) -> Self {
        Self {
            payload_type,
            ssrc,
            sequence,
            timestamp,
            first_packet: true,
        }
    }

    pub fn build(&mut self, payload: &[u8], samples: u32) -> Vec<u8> {
        let mut packet = Vec::with_capacity(12 + payload.len());
        packet.push(0x80);
        packet.push((if self.first_packet { 0x80 } else { 0 }) | (self.payload_type & 0x7f));
        packet.extend_from_slice(&self.sequence.to_be_bytes());
        packet.extend_from_slice(&self.timestamp.to_be_bytes());
        packet.extend_from_slice(&self.ssrc.to_be_bytes());
        packet.extend_from_slice(payload);

        self.sequence = self.sequence.wrapping_add(1);
        self.timestamp = self.timestamp.wrapping_add(samples);
        self.first_packet = false;
        packet
    }
}

pub fn interleave(channel: u8, rtp: &[u8]) -> Vec<u8> {
    let length = u16::try_from(rtp.len()).expect("RTP packet exceeds RTSP interleaved limit");
    let mut frame = Vec::with_capacity(4 + rtp.len());
    frame.extend_from_slice(&[0x24, channel]);
    frame.extend_from_slice(&length.to_be_bytes());
    frame.extend_from_slice(rtp);
    frame
}

pub struct PacingState {
    deadline_ns: u64,
    sent: bool,
}

impl PacingState {
    pub fn new(start_ns: u64) -> Self {
        Self {
            deadline_ns: start_ns,
            sent: false,
        }
    }

    pub fn deadline_ns(&self) -> u64 {
        self.deadline_ns
    }

    pub fn register_send(&mut self, actual_ns: u64, duration_ns: u64) -> bool {
        let rebased = self.sent && actual_ns.saturating_sub(self.deadline_ns) >= duration_ns;
        if rebased {
            self.deadline_ns = actual_ns;
        }
        self.deadline_ns = self.deadline_ns.saturating_add(duration_ns);
        self.sent = true;
        rebased
    }
}

#[cfg(test)]
mod tests {
    use super::{PacingState, RtpPacketizer, interleave};

    #[test]
    fn builds_sender_owned_rtp_with_40ms_timestamp_steps() {
        let mut packetizer = RtpPacketizer::with_identity(8, 0x11223344, 0x5566, 0x778899aa);

        let first = packetizer.build(&vec![0xd5; 320], 320);
        let second = packetizer.build(&vec![0xd5; 320], 320);

        assert_eq!(first[1], 0x88);
        assert_eq!(u16::from_be_bytes([first[2], first[3]]), 0x5566);
        assert_eq!(
            u32::from_be_bytes(first[4..8].try_into().unwrap()),
            0x778899aa
        );
        assert_eq!(
            u32::from_be_bytes(first[8..12].try_into().unwrap()),
            0x11223344
        );
        assert_eq!(second[1], 0x08);
        assert_eq!(u16::from_be_bytes([second[2], second[3]]), 0x5567);
        assert_eq!(
            u32::from_be_bytes(second[4..8].try_into().unwrap()),
            0x77889aea
        );
        assert_eq!(interleave(6, &first)[..4], [0x24, 6, 0x01, 0x4c]);
    }

    #[test]
    fn rebases_after_one_full_packet_interval_of_lateness() {
        let mut pacing = PacingState::new(0);

        assert!(!pacing.register_send(0, 40_000_000));
        assert_eq!(pacing.deadline_ns(), 40_000_000);
        assert!(pacing.register_send(85_000_000, 40_000_000));
        assert_eq!(pacing.deadline_ns(), 125_000_000);
        assert!(!pacing.register_send(125_000_000, 40_000_000));
        assert_eq!(pacing.deadline_ns(), 165_000_000);
    }
}
