const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const runtimePath = path.resolve(__dirname, '../../src/skill_temple/browser/replay_runtime.js');
const replay = eval(`(${fs.readFileSync(runtimePath, 'utf8')})`);

class TestHeaders {
  constructor() {
    this.values = [];
  }

  append(name, value) {
    this.values.push([name, value]);
  }
}

globalThis.Headers = TestHeaders;

function bytes(value) {
  return value instanceof Uint8Array ? value : new Uint8Array(Buffer.from(value));
}

async function runCase({
  chunks = [],
  responseControl = {},
  contentType = 'text/event-stream',
  readDelayMs = 0,
  status = 200,
  body = true,
}) {
  let index = 0;
  const cancellations = [];
  globalThis.fetch = async () => ({
    status,
    statusText: status >= 400 ? 'Error' : 'OK',
    url: 'https://fixture.test/stream',
    redirected: false,
    ok: status >= 200 && status < 300,
    headers: {
      entries: () => [['content-type', contentType]][Symbol.iterator](),
      get: (name) => name.toLowerCase() === 'content-type' ? contentType : null,
    },
    body: body ? {
      getReader: () => ({
        read: async () => {
          if (readDelayMs > 0) {
            await new Promise((resolve) => setTimeout(resolve, readDelayMs));
          }
          return index < chunks.length
            ? {done: false, value: bytes(chunks[index++])}
            : {done: true, value: undefined};
        },
        cancel: async (reason) => {
          cancellations.push(reason);
        },
      }),
    } : null,
  });
  const result = await replay({
    localFile: {
      text: JSON.stringify({
        url: 'https://fixture.test/stream',
        method: 'POST',
        headers: [],
        body: null,
        responseControl,
      }),
    },
  });
  return {result, cancellations};
}

test('SSE accepts LF, CRLF, CR-only, multiline data, and EOF without marker', async () => {
  const lf = await runCase({
    chunks: ['data: one\n\ndata: two\n\n'],
    responseControl: {responseMode: 'sse', terminalConditions: [{type: 'network_close'}]},
  });
  assert.equal(lf.result.sseEventCount, 2);
  assert.equal(lf.result.terminationReason, 'network_close');

  const crlf = await runCase({
    chunks: ['event: update\r\ndata: first\r\ndata: second\r\n\r\n'],
    responseControl: {responseMode: 'sse', terminalConditions: [{type: 'network_close'}]},
  });
  assert.equal(crlf.result.sseEventCount, 1);
  assert.equal(crlf.result.doneEventNameObserved, null);

  const crOnly = await runCase({
    chunks: ['data: first\r\rdata: last-without-separator'],
    responseControl: {responseMode: 'sse', terminalConditions: [{type: 'network_close'}]},
  });
  assert.equal(crOnly.result.sseEventCount, 2);
  assert.equal(crOnly.result.doneMarkerObserved, false);
});

test('SSE exact marker and event name terminate without fixed global contract', async () => {
  const matched = await runCase({
    chunks: ['event: complete\ndata: custom-terminal\n\n'],
    responseControl: {
      responseMode: 'sse',
      terminalConditions: [
        {type: 'exact_sse_data', value: 'custom-terminal', event_name: 'complete'},
      ],
    },
  });
  assert.equal(matched.result.terminationReason, 'done_marker');
  assert.equal(matched.result.terminalConditionMatched, 'exact_sse_data');
  assert.equal(matched.result.doneMarkerObserved, true);
  assert.equal(matched.result.doneEventNameObserved, 'complete');
  assert.deepEqual(matched.cancellations, ['done_marker']);
});

test('UTF-8 decoder preserves multibyte characters split across chunks', async () => {
  const encoded = Buffer.from('data: café\n\n', 'utf8');
  const split = encoded.indexOf(0xc3) + 1;
  const output = await runCase({
    chunks: [encoded.subarray(0, split), encoded.subarray(split)],
    responseControl: {responseMode: 'sse', terminalConditions: [{type: 'network_close'}]},
  });
  assert.equal(output.result.sseEventCount, 1);
  assert.match(output.result.bodyPreview, /café/);
});

test('NDJSON handles chunk boundaries, CRLF, parse errors, and incomplete EOF line', async () => {
  const output = await runCase({
    chunks: ['{"id":1}\r\n{"id":', '2}\nnot-json\n{"id":3}'],
    contentType: 'application/x-ndjson',
    responseControl: {responseMode: 'ndjson', terminalConditions: [{type: 'network_close'}]},
  });
  assert.equal(output.result.ndjsonRecordCount, 4);
  assert.equal(output.result.ndjsonParseErrorCount, 1);
  assert.deepEqual(
    output.result.ndjsonRecordMetadata.map((item) => item.valid),
    [true, true, false, true],
  );
  assert.equal(output.result.terminationReason, 'network_close');
});

test('raw stream records exact chunk boundaries and network close', async () => {
  const output = await runCase({
    chunks: ['abc', 'defg'],
    contentType: 'application/octet-stream',
    responseControl: {responseMode: 'raw_stream', terminalConditions: [{type: 'network_close'}]},
  });
  assert.equal(output.result.rawChunkCount, 2);
  assert.equal(output.result.bodyByteLength, 7);
  assert.deepEqual(output.result.chunkBoundaries.map((item) => item.byteLength), [3, 4]);
  assert.equal(output.result.terminalConditionMatched, 'network_close');
});

test('byte and event limits cancel the reader with auditable reasons', async () => {
  const bytesLimited = await runCase({
    chunks: [new Uint8Array(8193).fill(97)],
    contentType: 'application/octet-stream',
    responseControl: {
      responseMode: 'raw_stream',
      maxResponseBytes: 8192,
      terminalConditions: [{type: 'network_close'}],
    },
  });
  assert.equal(bytesLimited.result.bodyByteLength, 8192);
  assert.equal(bytesLimited.result.truncated, true);
  assert.equal(bytesLimited.result.terminationReason, 'max_response_bytes');
  assert.deepEqual(bytesLimited.cancellations, ['max_response_bytes']);

  const eventsLimited = await runCase({
    chunks: ['{"id":1}\n{"id":2}\n'],
    contentType: 'application/x-ndjson',
    responseControl: {
      responseMode: 'ndjson',
      maxEvents: 1,
      terminalConditions: [{type: 'network_close'}],
    },
  });
  assert.equal(eventsLimited.result.ndjsonRecordCount, 1);
  assert.equal(eventsLimited.result.terminationReason, 'max_events');
  assert.deepEqual(eventsLimited.cancellations, ['max_events']);
});

test('idle-window and text-pattern termination are independent', async () => {
  const idle = await runCase({
    chunks: ['late'],
    contentType: 'application/octet-stream',
    readDelayMs: 40,
    responseControl: {
      responseMode: 'raw_stream',
      idleWindowMs: 10,
      terminalConditions: [{type: 'idle_window', window_ms: 10}],
    },
  });
  assert.equal(idle.result.terminationReason, 'idle_window');
  assert.equal(idle.result.terminalConditionMatched, 'idle_window');
  assert.deepEqual(idle.cancellations, ['idle_window']);

  const pattern = await runCase({
    chunks: ['prefix terminal-text suffix'],
    contentType: 'application/octet-stream',
    responseControl: {
      responseMode: 'raw_stream',
      terminalConditions: [{type: 'text_pattern', value: 'terminal-text'}],
    },
  });
  assert.equal(pattern.result.terminationReason, 'text_pattern');
  assert.equal(pattern.result.terminalConditionMatched, 'text_pattern');
  assert.deepEqual(pattern.cancellations, ['text_pattern']);
});

test('ordinary and bodyless responses return structured facts', async () => {
  const ordinary = await runCase({
    chunks: ['{"ok":true}'],
    contentType: 'application/json',
    status: 500,
    responseControl: {responseMode: 'auto', terminalConditions: [{type: 'network_close'}]},
  });
  assert.equal(ordinary.result.responseMode, 'ordinary');
  assert.equal(ordinary.result.status, 500);
  assert.equal(ordinary.result.bodyByteLength, 11);

  const bodyless = await runCase({
    body: false,
    contentType: 'application/json',
    responseControl: {responseMode: 'ordinary', terminalConditions: [{type: 'network_close'}]},
  });
  assert.equal(bodyless.result.terminationReason, 'no_response_body');
  assert.equal(bodyless.result.bodyByteLength, 0);
});
