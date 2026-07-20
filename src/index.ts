export { playFile } from './cli.ts';
export type { PlaybackDependencies, PlaybackOptions } from './cli.ts';

export {
  PACKET_MS,
  SAMPLE_RATE,
  openBackchannel,
  sendPacedFrames,
  sendPacedG711,
} from './backchannel.ts';
export type {
  BackchannelOptions,
  BackchannelSession,
  PacingClock,
} from './backchannel.ts';

export {
  aacRfc3640Payload,
  fileToG711,
  fileToRtpAudio,
  parseAdtsFrames,
} from './audio/transcode.ts';
export type {
  AdtsFrame,
  EncodedAudio,
  EncodedAudioFrame,
} from './audio/transcode.ts';
export {
  generateTonePcm,
  linearToALaw,
  linearToMuLaw,
  pcm16ToG711,
} from './audio/g711.ts';
export type { G711Variant } from './audio/g711.ts';
export type {
  AudioCodecName,
  CodecPreference,
  SendCodec,
} from './rtsp/sdp.ts';

export { discoverDevices } from './onvif/discovery.ts';
export type {
  DiscoveredDevice,
  DiscoveryOptions,
} from './onvif/discovery.ts';
export { getStreamUris } from './onvif/streams.ts';
export type {
  StreamUri,
  StreamUriOptions,
} from './onvif/streams.ts';
