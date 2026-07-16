export { playFile } from './cli.ts';
export type { PlaybackDependencies, PlaybackOptions } from './cli.ts';

export {
  PACKET_MS,
  SAMPLE_RATE,
  openBackchannel,
  sendPacedG711,
} from './backchannel.ts';
export type {
  BackchannelSession,
  PacingClock,
} from './backchannel.ts';

export { fileToG711 } from './audio/transcode.ts';
export {
  generateTonePcm,
  linearToALaw,
  linearToMuLaw,
  pcm16ToG711,
} from './audio/g711.ts';
export type { G711Variant } from './audio/g711.ts';
