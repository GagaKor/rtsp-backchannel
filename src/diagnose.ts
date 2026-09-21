/**
 * Backchannel A/B diagnosis — why a camera that plays audio from
 * `test_full_session.py` stays silent through this library.
 *
 * The two senders differ in seven observable ways. This tool drives the
 * library's own code path (RtspClient / parseSdp / pickSendCodec /
 * RtpPacketizer / sendPacedFrames) but exposes each of those seven as a knob,
 * so a single camera can be bisected: start from `--profile python` (expected
 * to work), flip one knob back to the library value, and the knob that
 * silences the speaker is the root cause.
 *
 *   node --experimental-transform-types src/diagnose.ts \
 *     --host 10.10.50.2 --user admin --pass admin --file announce.wav \
 *     --profile python
 *
 * Not covered here: the RTSP keepalive OPTIONS the library interleaves into a
 * send longer than half the session timeout (the Python script never sends
 * one), and the `macs-poc` User-Agent, which RtspClient hardcodes.
 */
import { readFileSync, writeFileSync, appendFileSync } from 'node:fs';
import { RtspClient, BACKCHANNEL_REQUIRE } from './rtsp/backchannelClient.ts';
import {
  parseSdp,
  findBackchannelAudio,
  pickSendTrack,
  type CodecPreference,
  type MediaDescription,
} from './rtsp/sdp.ts';
import {
  closeRtspSession,
  displayRtspTarget,
  parseRtspTarget,
  resolveTrackUri,
} from './backchannel.ts';
import { RtpPacketizer, interleave } from './rtp/sender.ts';
import { sendPacedFrames, systemClock } from './audio/pacing.ts';
import { fileToG711, type EncodedAudioFrame } from './audio/transcode.ts';
import { generateTonePcm, pcm16ToG711, type G711Variant } from './audio/g711.ts';
import { OnvifDevice } from './onvif/deviceClient.ts';

const SAMPLE_RATE = 8000;
const SILENCE: Record<G711Variant, number> = { PCMU: 0xff, PCMA: 0xd5 };

/** Every behaviour where this library and test_full_session.py disagree. */
interface Knobs {
  /**
   * Which non-backchannel tracks get SETUP before the audio track. `recvonly`
   * reproduces the pre-fix library, which demanded an explicit a=recvonly and
   * so opened a backchannel-only session on any camera that omits it.
   */
  setupTracks: 'recvonly' | 'all' | 'audio-only';
  /** RTP payload duration. The library packs 40 ms; the script packs 20 ms. */
  ptimeMs: number;
  /** Leading silence, covering a speaker path that opens late. */
  prerollMs: number;
  /** Trailing silence plus drain, so the camera's jitter buffer empties. */
  tailMs: number;
  /** PLAY Range: the library asks for `now-`, the script for `0.000-`. */
  range: 'now' | 'zero';
  /** TCP_NODELAY. The script sets it; a Node socket leaves Nagle on. */
  noDelay: boolean;
  /** The script repeats Require: backchannel on every SETUP. */
  requireOnAllSetups: boolean;
  /** Linear gain. Both senders now default to full scale. */
  volume: number;
}

const PROFILES: Record<'library' | 'python', Knobs> = {
  library: {
    setupTracks: 'all',
    ptimeMs: 40,
    prerollMs: 0,
    tailMs: 0,
    range: 'now',
    noDelay: false,
    requireOnAllSetups: false,
    volume: 1,
  },
  python: {
    setupTracks: 'all',
    ptimeMs: 20,
    prerollMs: 600,
    tailMs: 600,
    range: 'zero',
    noDelay: true,
    requireOnAllSetups: true,
    volume: 1,
  },
};

function arg(name: string): string | undefined {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 ? process.argv[i + 1] : undefined;
}

function required(name: string): string {
  const value = arg(name);
  if (!value) throw new Error(`missing --${name}`);
  return value;
}

let logPath = 'diagnose_log.txt';
function log(message = ''): void {
  console.log(message);
  appendFileSync(logPath, message + '\n');
}

function describeTrack(m: MediaDescription, isBackchannel: boolean): string {
  const pts = m.formats.join(',');
  const codecs = m.formats
    .map((f) => m.rtpmaps[Number(f)]?.encoding ?? '-')
    .join(',');
  return (
    `    ${m.media.padEnd(11)} pt=${pts.padEnd(7)} ${codecs.padEnd(16)}` +
    ` dir=${(m.direction ?? '(none)').padEnd(9)} control=${m.control ?? '(none)'}` +
    (isBackchannel ? '   <== backchannel' : '')
  );
}

function cutFrames(audio: Buffer, samplesPerPacket: number): EncodedAudioFrame[] {
  const frames: EncodedAudioFrame[] = [];
  for (let offset = 0; offset < audio.length; offset += samplesPerPacket) {
    const payload = audio.subarray(offset, offset + samplesPerPacket);
    frames.push({ payload, samples: payload.length });
  }
  return frames;
}

function silenceFrames(
  variant: G711Variant,
  ms: number,
  samplesPerPacket: number,
): EncodedAudioFrame[] {
  const count = Math.round(ms / ((samplesPerPacket * 1000) / SAMPLE_RATE));
  const payload = Buffer.alloc(samplesPerPacket, SILENCE[variant]);
  return Array.from({ length: Math.max(0, count) }, () => ({
    payload,
    samples: samplesPerPacket,
  }));
}

async function main(): Promise<void> {
  const profileName = (arg('profile') ?? 'library') as 'library' | 'python';
  if (!PROFILES[profileName]) throw new Error('profile must be library or python');
  const knobs: Knobs = { ...PROFILES[profileName] };

  // Individual knobs override the profile, which is what makes bisection work.
  const setupTracks = arg('setup-tracks');
  if (setupTracks) {
    if (setupTracks !== 'recvonly' && setupTracks !== 'all' && setupTracks !== 'audio-only') {
      throw new Error('setup-tracks must be recvonly, all, or audio-only');
    }
    knobs.setupTracks = setupTracks;
  }
  if (arg('ptime')) knobs.ptimeMs = Number(required('ptime'));
  if (arg('preroll-ms')) knobs.prerollMs = Number(required('preroll-ms'));
  if (arg('tail-ms')) knobs.tailMs = Number(required('tail-ms'));
  const range = arg('range');
  if (range) {
    if (range !== 'now' && range !== 'zero') throw new Error('range must be now or zero');
    knobs.range = range;
  }
  if (arg('nodelay')) knobs.noDelay = required('nodelay') === 'true';
  if (arg('require-all-setups')) {
    knobs.requireOnAllSetups = required('require-all-setups') === 'true';
  }
  if (arg('volume')) knobs.volume = Number(required('volume'));
  if (!Number.isInteger((SAMPLE_RATE * knobs.ptimeMs) / 1000)) {
    throw new Error('ptime must yield a whole number of 8 kHz samples');
  }

  logPath = arg('log') ?? logPath;
  writeFileSync(logPath, '');

  const host = required('host');
  const user = arg('user') ?? 'admin';
  const pass = arg('pass') ?? process.env.ONVIF_PASSWORD ?? '';
  const file = arg('file');
  // test_full_session.py takes raw G.711 bytes and sends them untouched.
  // ffmpeg cannot infer a container for a headerless .ulaw/.alaw file, so a
  // raw file has to bypass fileToG711 entirely — which also means no volume.
  const raw = process.argv.includes('--raw-g711');
  // Reading a camera's SDP must never be gated on being allowed to make
  // noise: a camera with a live speaker can be inspected mid-shift this way.
  const describeOnly = process.argv.includes('--describe-only');
  // SETUP and PLAY alone make no sound — only the RTP that follows does — so
  // a camera whose speaker is in service can still have its session shape
  // checked during working hours.
  const noSend = process.argv.includes('--no-send');
  const codecPreference = (arg('codec') ?? 'auto') as CodecPreference;

  log('='.repeat(72));
  log(` profile ${profileName}`);
  log(` knobs   ${JSON.stringify(knobs)}`);
  log('='.repeat(72));

  const endpoint = /^rtsp:\/\//i.test(host)
    ? parseRtspTarget(host, user, pass)
    : await (async () => {
        const device = new OnvifDevice(host, user, pass);
        await device.connect();
        const profiles = await device.getProfiles();
        if (profiles.length === 0) throw new Error('no media profiles');
        const uri = await device.getStreamUri(profiles[0].token);
        log(`[*] ONVIF stream uri: ${displayRtspTarget(uri)}`);
        return parseRtspTarget(uri, user, pass);
      })();

  const streamUri = endpoint.uri;
  log(`[*] target ${displayRtspTarget(streamUri)}`);

  const rtsp = new RtspClient(endpoint.host, endpoint.port, endpoint.user, endpoint.pass);
  await rtsp.connect();
  if (knobs.noDelay) {
    rtsp.rawSocket.setNoDelay(true);
    log('[*] TCP_NODELAY set (Nagle off), matching the Python script');
  } else {
    log('[*] TCP_NODELAY NOT set — Node leaves Nagle on, unlike the Python script');
  }

  let receivedBytes = 0;
  rtsp.rawSocket.on('data', (chunk: Buffer) => {
    receivedBytes += chunk.length;
  });

  try {
    const options = await rtsp.options(streamUri);
    log(`<<< OPTIONS ${options.statusLine}`);
    const desc = await rtsp.describe(streamUri, { backchannel: true });
    log(`<<< DESCRIBE ${desc.statusLine}`);
    if (desc.status !== 200) throw new Error(`DESCRIBE ${desc.statusLine}`);
    log('\n----- SDP -----');
    log(desc.body.trim());
    log('---------------\n');

    const sdp = parseSdp(desc.body);
    const chosen = pickSendTrack(sdp, codecPreference);
    const track = chosen?.track ?? findBackchannelAudio(sdp);
    log('[*] SDP tracks as this library parses them');
    for (const m of sdp.media) log(describeTrack(m, m === track));
    if (!track?.control) {
      log('\n[FAIL] no sendonly audio track with a control URI — the library ' +
        'would raise BackchannelUnavailableError here and fall back to VIGI.');
      return;
    }

    if (describeOnly) {
      log('\n[*] --describe-only: stopping before SETUP; no audio was sent.');
      return;
    }

    const codec = chosen?.codec;
    if (!codec) throw new Error('no supported backchannel codec offered');
    log(`\n[*] codec chosen: ${codec.name} pt=${codec.payloadType} ` +
      `clock=${codec.clockRate}`);
    const variant: G711Variant | undefined =
      codec.name === 'pcma' ? 'PCMA' : codec.name === 'pcmu' ? 'PCMU' : undefined;
    if (!variant) {
      throw new Error(
        `this diagnosis tool only sends G.711; the camera negotiated ${codec.name}`,
      );
    }

    const preFixWouldSetup = sdp.media.filter(
      (m) => m !== track && m.direction === 'recvonly' && m.control,
    );
    const toSetup =
      knobs.setupTracks === 'audio-only'
        ? []
        : sdp.media.filter(
            (m) =>
              m !== track &&
              m.control &&
              (knobs.setupTracks === 'all'
                ? m.direction !== 'sendonly' && m.direction !== 'inactive'
                : m.direction === 'recvonly'),
          );
    if (knobs.setupTracks === 'all' && toSetup.length !== preFixWouldSetup.length) {
      log(
        `\n[!!] this SDP omits a=recvonly: the pre-fix library would SETUP ` +
          `${preFixWouldSetup.length} companion track(s), this run SETUPs ` +
          `${toSetup.length}. That gap is the bug fixed in backchannel.ts.`,
      );
    }

    let session = '';
    let channel = 0;
    for (const m of toSetup) {
      const uri = resolveTrackUri(streamUri, desc.headers['content-base'], m.control!);
      const result = await rtsp.setup(uri, {
        rtpChannel: channel,
        backchannel: knobs.requireOnAllSetups,
      });
      session = result.session;
      log(`[OK] SETUP ${m.media.padEnd(11)} -> ch ${result.rtpChannel}-${result.rtpChannel + 1}` +
        `  session=${session}`);
      channel = Math.max(channel, result.rtpChannel) + 2;
    }

    const trackUri = resolveTrackUri(streamUri, desc.headers['content-base'], track.control);
    const backchannelSetup = await rtsp.setup(trackUri, {
      rtpChannel: channel,
      backchannel: true,
    });
    session = backchannelSetup.session;
    const rtpChannel = backchannelSetup.rtpChannel;
    log(`[OK] SETUP backchannel -> ch ${rtpChannel}-${rtpChannel + 1}  session=${session}`);

    const play = await rtsp.request('PLAY', streamUri, {
      Session: session,
      Range: knobs.range === 'now' ? 'npt=now-' : 'npt=0.000-',
      Require: BACKCHANNEL_REQUIRE,
    });
    log(`<<< PLAY ${play.statusLine}` +
      (play.headers['rtp-info'] ? `\n    RTP-Info: ${play.headers['rtp-info']}` : ''));
    if (play.status !== 200) throw new Error(`PLAY ${play.statusLine}`);

    if (noSend) {
      log('\n[*] --no-send: session established, no RTP audio was sent.');
      return;
    }

    let audio: Buffer;
    if (file && raw) {
      audio = readFileSync(file);
      log(`[*] --raw-g711: sending ${file} byte for byte; --volume is ignored`);
    } else if (file) {
      audio = await fileToG711(file, variant, knobs.volume);
    } else {
      audio = pcm16ToG711(generateTonePcm(1000, 3000, SAMPLE_RATE, 0.9), variant, knobs.volume);
    }
    log(`\n[*] audio: ${file ?? '1 kHz tone 3 s'} -> ${audio.length} bytes ` +
      `${variant} (${(audio.length / SAMPLE_RATE).toFixed(2)} s, ` +
      `volume ${file && raw ? 'unchanged (raw)' : knobs.volume})`);

    const samplesPerPacket = (SAMPLE_RATE * knobs.ptimeMs) / 1000;
    const frames = [
      ...silenceFrames(variant, knobs.prerollMs, samplesPerPacket),
      ...cutFrames(audio, samplesPerPacket),
      ...silenceFrames(variant, knobs.tailMs, samplesPerPacket),
    ];
    const packetizer = new RtpPacketizer({
      payloadType: codec.payloadType,
      clockRate: codec.clockRate,
    });
    log(`[*] sending ${frames.length} packets of ${samplesPerPacket} B ` +
      `(${knobs.ptimeMs} ms) on channel ${rtpChannel}, pt ${codec.payloadType}`);

    const bytesBefore = receivedBytes;
    const startedAt = performance.now();
    const sent = await sendPacedFrames(
      frames,
      codec.clockRate,
      async (payload, samples) => {
        await rtsp.sendInterleaved(
          interleave(rtpChannel, packetizer.build(payload, samples)),
        );
      },
      systemClock,
    );
    const elapsed = (performance.now() - startedAt) / 1000;
    log(`[OK] sent ${sent} packets in ${elapsed.toFixed(2)} s ` +
      `(ideal ${((frames.length * knobs.ptimeMs) / 1000).toFixed(2)} s)`);

    // The Python script drains for a second before TEARDOWN. Without it a
    // camera can be cut off mid-playback, so measure that window too.
    await new Promise((resolve) => setTimeout(resolve, knobs.tailMs > 0 ? 1000 : 0));
    log(`[*] bytes received from camera during send: ${receivedBytes - bytesBefore} ` +
      `(total ${receivedBytes}) — non-zero means the session stayed alive`);
  } finally {
    await closeRtspSession(rtsp, streamUri);
    log(`\n[*] log written to ${logPath}`);
  }
}

main().catch((error) => {
  log(`\n[FAIL] ${error instanceof Error ? error.stack ?? error.message : String(error)}`);
  process.exitCode = 1;
});
