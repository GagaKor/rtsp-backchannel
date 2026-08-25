/**
 * One write path for interleaved RTP, shared by both transports that sit
 * behind `BackchannelSession`.
 *
 * `RtspClient.sendInterleaved` and the VIGI talk session each need the same
 * state machine — await drain, settle once, tear down every listener, arm a
 * timeout only if the write actually backed up — and they need it to behave
 * *identically*, because a caller comparing the two transports must not get a
 * different answer about whether a packet reached the camera depending on
 * which one it picked. Two copies could only diverge; a drain race or a leaked
 * listener fixed in one would silently persist in the other.
 */

/** The part of a socket a framed write touches. `net.Socket` satisfies it. */
export interface FrameWritable {
  write(chunk: Buffer): boolean;
  once(event: string, handler: (arg?: unknown) => void): unknown;
  off(event: string, handler: (arg?: unknown) => void): unknown;
}

/**
 * Per-transport error wording. Kept as data rather than derived from a single
 * prefix because the three messages do not share one shape, and callers
 * already depend on their exact text.
 */
export interface InterleavedWriteLabels {
  /** Rendered as `${failed}: ${cause.message}`. */
  failed: string;
  /** Used verbatim when the socket closes mid-write. */
  closed: string;
  /** Rendered as `${timedOut} ${timeoutMs} ms`. */
  timedOut: string;
}

/**
 * Writes one already-framed buffer and resolves only once the socket has
 * taken it, rejecting on error, close, or timeout. `abort` runs when the
 * timeout fires, so each transport can dispose of its own connection.
 */
export function writeInterleavedFrame(
  socket: FrameWritable,
  frame: Buffer,
  timeoutMs: number,
  labels: InterleavedWriteLabels,
  abort: () => void,
): Promise<void> {
  return new Promise((resolve, reject) => {
    let settled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const cleanup = () => {
      if (timer !== undefined) clearTimeout(timer);
      socket.off('drain', onDrain);
      socket.off('error', onError);
      socket.off('close', onClose);
    };
    const finish = (error?: Error) => {
      if (settled) return;
      settled = true;
      cleanup();
      if (error) reject(error);
      else resolve();
    };
    const onDrain = () => finish();
    const onError = (arg?: unknown) => {
      const cause = arg instanceof Error ? arg : new Error(String(arg));
      finish(new Error(`${labels.failed}: ${cause.message}`, { cause }));
    };
    const onClose = () => finish(new Error(labels.closed));

    socket.once('drain', onDrain);
    socket.once('error', onError);
    socket.once('close', onClose);
    try {
      // A write the socket accepts outright never drains, so resolve here
      // rather than waiting for an event that will not come.
      if (socket.write(frame)) {
        finish();
        return;
      }
    } catch (error) {
      onError(error);
      return;
    }
    // A synchronous 'error' during write() already settled this; arming a
    // timer now would leave it running past the rejection.
    if (settled) return;
    timer = setTimeout(() => {
      finish(new Error(`${labels.timedOut} ${timeoutMs} ms`));
      abort();
    }, timeoutMs);
  });
}
