import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  writeInterleavedFrame,
  type FrameWritable,
  type InterleavedWriteLabels,
} from './interleavedWrite.ts';

const LABELS: InterleavedWriteLabels = {
  failed: 'test write failed',
  closed: 'test socket closed',
  timedOut: 'test write timeout after',
};

/**
 * `accepts` decides what `write()` returns: true means the socket took the
 * frame outright, false means it buffered and will emit 'drain' later.
 */
class FakeSocket implements FrameWritable {
  readonly written: Buffer[] = [];
  accepts = true;
  throwOnWrite: Error | undefined;
  /** Emit 'error' synchronously from inside write(), then return false. */
  errorDuringWrite: Error | undefined;
  /** Counts off() calls, so a double cleanup is observable. */
  offCalls = 0;
  private handlers = new Map<string, Array<(arg?: unknown) => void>>();

  write(chunk: Buffer): boolean {
    if (this.throwOnWrite) throw this.throwOnWrite;
    this.written.push(chunk);
    if (this.errorDuringWrite) {
      this.emit('error', this.errorDuringWrite);
      return false;
    }
    return this.accepts;
  }
  once(event: string, handler: (arg?: unknown) => void): unknown {
    const list = this.handlers.get(event) ?? [];
    list.push(handler);
    this.handlers.set(event, list);
    return this;
  }
  off(event: string, handler: (arg?: unknown) => void): unknown {
    this.offCalls += 1;
    this.handlers.set(
      event,
      (this.handlers.get(event) ?? []).filter((candidate) => candidate !== handler),
    );
    return this;
  }
  emit(event: string, arg?: unknown): void {
    for (const handler of [...(this.handlers.get(event) ?? [])]) handler(arg);
  }
  /** Every listener the helper is still holding, across all events. */
  get listenerCount(): number {
    let total = 0;
    for (const list of this.handlers.values()) total += list.length;
    return total;
  }
}

const FRAME = Buffer.from([0x24, 0x00, 0x00, 0x04, 1, 2, 3, 4]);

test('resolves without waiting when the socket takes the frame outright', async () => {
  // A write the socket accepts never emits 'drain', so awaiting one would
  // hang until the timeout for every well-behaved packet.
  const socket = new FakeSocket();
  await writeInterleavedFrame(socket, FRAME, 10, LABELS, () => {
    assert.fail('abort must not run on the fast path');
  });
  assert.deepEqual(socket.written, [FRAME]);
  assert.equal(socket.listenerCount, 0, 'listeners are removed on success');
});

test('resolves on drain when the write backs up', async () => {
  const socket = new FakeSocket();
  socket.accepts = false;
  const pending = writeInterleavedFrame(socket, FRAME, 1000, LABELS, () => {
    assert.fail('abort must not run when drain arrives first');
  });
  socket.emit('drain');
  await pending;
  assert.equal(socket.listenerCount, 0);
});

test('rejects with the transport label when the socket errors', async () => {
  const socket = new FakeSocket();
  socket.accepts = false;
  const pending = writeInterleavedFrame(socket, FRAME, 1000, LABELS, () => {});
  socket.emit('error', new Error('ECONNRESET'));
  await assert.rejects(pending, (error: unknown) => {
    assert.ok(error instanceof Error);
    assert.equal(error.message, 'test write failed: ECONNRESET');
    assert.equal((error.cause as Error).message, 'ECONNRESET');
    return true;
  });
  assert.equal(socket.listenerCount, 0);
});

test('rejects when the socket closes mid-write', async () => {
  const socket = new FakeSocket();
  socket.accepts = false;
  const pending = writeInterleavedFrame(socket, FRAME, 1000, LABELS, () => {});
  socket.emit('close');
  await assert.rejects(pending, { message: 'test socket closed' });
  assert.equal(socket.listenerCount, 0);
});

test('rejects and aborts the transport when the write never drains', async () => {
  const socket = new FakeSocket();
  socket.accepts = false;
  let aborted = 0;
  await assert.rejects(
    writeInterleavedFrame(socket, FRAME, 5, LABELS, () => { aborted += 1; }),
    { message: 'test write timeout after 5 ms' },
  );
  assert.equal(aborted, 1, 'the timeout disposes of the connection exactly once');
  assert.equal(socket.listenerCount, 0);
});

test('a throwing write rejects without arming a timeout', async () => {
  // The synchronous throw already settles the promise; a timer armed after it
  // would keep the process alive and fire against a rejected promise.
  const socket = new FakeSocket();
  socket.throwOnWrite = new Error('EPIPE');
  let aborted = false;
  await assert.rejects(
    writeInterleavedFrame(socket, FRAME, 5, LABELS, () => { aborted = true; }),
    { message: 'test write failed: EPIPE' },
  );
  assert.equal(socket.listenerCount, 0);
  // Outlast the 5 ms timeout: nothing may fire after the rejection.
  await new Promise((resolve) => setTimeout(resolve, 25));
  assert.equal(aborted, false, 'no timer survived the synchronous failure');
});

test('a non-Error thrown by the socket is still reported', async () => {
  const socket = new FakeSocket();
  socket.throwOnWrite = 'EPIPE' as unknown as Error;
  await assert.rejects(
    writeInterleavedFrame(socket, FRAME, 5, LABELS, () => {}),
    { message: 'test write failed: EPIPE' },
  );
});

test('settles once even when several outcomes race', async () => {
  const socket = new FakeSocket();
  socket.accepts = false;
  const pending = writeInterleavedFrame(socket, FRAME, 1000, LABELS, () => {});
  socket.emit('drain');
  // Both would reject a still-pending write; neither may override the resolve.
  socket.emit('error', new Error('late'));
  socket.emit('close');
  await pending;
});

test('an error emitted synchronously from write() arms no timeout', async () => {
  // write() can reject the frame by emitting 'error' inline and returning
  // false. The promise is already settled by the time control returns here, so
  // arming the timer would leave it running past the rejection and then call
  // abort() on a connection the caller has already given up on.
  const socket = new FakeSocket();
  socket.errorDuringWrite = new Error('EPIPE');
  let aborted = false;
  await assert.rejects(
    writeInterleavedFrame(socket, FRAME, 5, LABELS, () => { aborted = true; }),
    { message: 'test write failed: EPIPE' },
  );
  await new Promise((resolve) => setTimeout(resolve, 25));
  assert.equal(aborted, false, 'no timer was armed after the inline error');
});

test('tears its listeners down exactly once however many outcomes arrive', async () => {
  // Three listeners are registered, so one cleanup is three off() calls. A
  // second settle would run cleanup again -- invisible to the promise, which
  // ignores a repeat settle, but it is the guard that keeps finish() from
  // acting twice on a connection that is already done with.
  const socket = new FakeSocket();
  socket.accepts = false;
  const pending = writeInterleavedFrame(socket, FRAME, 1000, LABELS, () => {});
  socket.emit('drain');
  socket.emit('error', new Error('late'));
  socket.emit('close');
  socket.emit('drain');
  await pending;
  assert.equal(socket.offCalls, 3, 'cleanup ran once, for its three listeners');
});
