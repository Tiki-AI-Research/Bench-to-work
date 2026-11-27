import test from "node:test";
import assert from "node:assert/strict";
import { createRetryFetch } from "./retry-fetch.mjs";

const good = 'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n';
const sse = text => new Response(text, { headers: { "content-type": "text/event-stream" } });
const invoke = fetch => fetch("https://example.test/v1/chat/completions", {
  method: "POST", body: '{"messages":[{"role":"user","content":"same"}]}',
});

for (const status of [400, 401, 408, 409, 429, 500, 502, 503, 599]) {
  test(`HTTP ${status} retries within budget without mutating request body`, async () => {
    const bodies = [], statuses = [];
    const fetch = createRetryFetch({ maxRetries: 2, baseDelayMs: 0,
      onEvent: e => statuses.push(e), fetch: async req => {
        bodies.push(await req.text());
        return bodies.length < 3 ? new Response("temporary failure", { status }) : sse(good);
      } });
    assert.equal(await (await invoke(fetch)).text(), good);
    assert.equal(bodies.length, 3); assert.equal(new Set(bodies).size, 1);
    assert.equal(statuses.filter(e => e.retry).length, 2);
  });
}

for (const [status, message] of [
  [429, "insufficient_quota"], [402, "balance required"], [403, "Forbidden"],
  [404, "Not found"], [413, "too large"], [422, "invalid request"],
  [500, "safeguards refusal"],
]) {
  test(`non-retryable error stops immediately: ${status} ${message}`, async () => {
    let sent = 0;
    const fetch = createRetryFetch({ maxRetries: 2, baseDelayMs: 0, fetch: async () => {
      sent++; return new Response(message, { status });
    } });
    const response = await invoke(fetch);
    assert.equal(response.status, status); assert.equal(await response.text(), message); assert.equal(sent, 1);
  });
}

for (const message of [
  '{"error":{"message":"Request contains an invalid argument.","type":"upstream_error","code":400}}',
  "Invalid request parameter.",
  "invalid max_tokens=50000",
  "maximum context length exceeded",
]) {
  test(`HTTP 400 uses unified bounded retry budget: ${message.slice(0, 40)}`, async () => {
    const bodies = [], statuses = [];
    const fetch = createRetryFetch({ maxRetries: 2, baseDelayMs: 0,
      onEvent: e => statuses.push(e), fetch: async req => {
        bodies.push(await req.text());
        return bodies.length < 3 ? new Response(message, { status: 400 }) : sse(good);
      } });
    // Gateways may surface transient upstream rejections with identical 400 wording; retry recovers when transient.
    assert.equal(await (await invoke(fetch)).text(), good);
    assert.equal(bodies.length, 3);
    assert.equal(new Set(bodies).size, 1);
    assert.equal(statuses.filter(e => e.retry).length, 2);
  });
}

test("exhausted HTTP 400 retries return original response without fabricating success", async () => {
  let sent = 0;
  const message = '{"error":{"message":"Request contains an invalid argument.","code":400}}';
  const fetch = createRetryFetch({ maxRetries: 2, baseDelayMs: 0, fetch: async () => {
    sent++; return new Response(message, { status: 400 });
  } });
  const response = await invoke(fetch);
  assert.equal(response.status, 400);
  assert.equal(await response.text(), message);
  assert.equal(sent, 3); // Initial attempt + 2 bounded retries.
});

test("HTTP 200 mid-stream gateway error retries without exposing partial body to SDK", async () => {
  let sent = 0;
  const failures = [];
  const fetch = createRetryFetch({ maxRetries: 2, baseDelayMs: 0,
    onFailure: f => failures.push(f), fetch: async () => ++sent === 1
      ? sse('data: {"choices":[{"delta":{"content":"partial"}}]}\n\ndata: {"error":{"message":"unmarshal Gemini stream response: unexpected end of JSON input"}}\n\n')
      : sse(good) });
  assert.equal(await (await invoke(fetch)).text(), good);
  assert.equal(sent, 2); assert.equal(failures[0].failure.kind, "stream_error");
  assert.match(failures[0].bytes.toString(), /partial/);
});

for (const bad of ['data: {"choices":', 'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n']) {
  test("incomplete SSE stream retries cleanly", async () => {
    let sent = 0;
    const fetch = createRetryFetch({ maxRetries: 2, baseDelayMs: 0, fetch: async () => sse(++sent === 1 ? bad : good) });
    assert.equal(await (await invoke(fetch)).text(), good); assert.equal(sent, 2);
  });
}

test("transport and stream-read failures retry and record partial bytes received", async () => {
  let sent = 0;
  const failures = [];
  const fetch = createRetryFetch({ maxRetries: 2, baseDelayMs: 0,
    onFailure: f => failures.push(f), fetch: async () => {
      if (++sent === 1) throw new TypeError("unknown transport failure");
      if (sent === 2) {
        let read = false;
        return new Response(new ReadableStream({ pull(controller) {
          if (read) controller.error(new TypeError("read failed"));
          else { read = true; controller.enqueue(new TextEncoder().encode("partial")); }
        } }), { headers: { "content-type": "text/event-stream" } });
      }
      return sse(good);
    } });
  assert.equal(await (await invoke(fetch)).text(), good); assert.equal(sent, 3);
  assert.equal(failures[1].bytes.toString(), "partial");
});

test("exhausted stream-error retries return raw error stream without fabricating status", async () => {
  let sent = 0;
  const bad = 'data: {"error":{"message":"unmarshal failure"}}\n\n';
  const fetch = createRetryFetch({ maxRetries: 2, baseDelayMs: 0, fetch: async () => { sent++; return sse(bad); } });
  const response = await invoke(fetch);
  assert.equal(sent, 3); assert.equal(response.status, 200); assert.equal(await response.text(), bad);
});

test("malformed model JSON, bad tool args, and finish_reason=length pass through without HTTP retry", async () => {
  let sent = 0;
  const text = 'data: {"choices":[{"delta":{"content":"{broken answer"},"finish_reason":"length"}]}\n\n';
  const fetch = createRetryFetch({ maxRetries: 2, baseDelayMs: 0, fetch: async () => { sent++; return sse(text); } });
  assert.equal(await (await invoke(fetch)).text(), text); assert.equal(sent, 1);
});

test("abort and exhausted network retries rethrow the original error", async () => {
  const abort = new DOMException("cancelled", "AbortError");
  let sent = 0;
  let fetch = createRetryFetch({ maxRetries: 2, baseDelayMs: 0, fetch: async () => { sent++; throw abort; } });
  await assert.rejects(invoke(fetch), e => e === abort); assert.equal(sent, 1);
  const network = new Error("unknown network error"); sent = 0;
  fetch = createRetryFetch({ maxRetries: 2, baseDelayMs: 0, fetch: async () => { sent++; throw network; } });
  await assert.rejects(invoke(fetch), e => e === network); assert.equal(sent, 3);
});

test("thought-signature 400 and SSE event:error both recover within retry budget", async () => {
  let sent = 0;
  const fetch = createRetryFetch({ maxRetries: 2, baseDelayMs: 0, fetch: async () => {
    sent++;
    if (sent === 1) return new Response("Invalid thought signature.", { status: 400 });
    if (sent === 2) return sse('event: error\ndata: {"message":"unmarshal Gemini stream response: unexpected end of JSON input"}\n\ndata: [DONE]\n\n');
    return sse(good);
  } });
  assert.equal(await (await invoke(fetch)).text(), good); assert.equal(sent, 3);
});

test("maxRetries=0 disables retry and abort during backoff stops immediately", async () => {
  let sent = 0;
  let fetch = createRetryFetch({ maxRetries: 0, fetch: async () => { sent++; return new Response("temporary", { status: 503 }); } });
  assert.equal((await invoke(fetch)).status, 503); assert.equal(sent, 1);
  const controller = new AbortController(); sent = 0;
  fetch = createRetryFetch({ maxRetries: 2, onEvent: () => controller.abort(),
    fetch: async () => { sent++; return new Response("temporary", { status: 503 }); } });
  await assert.rejects(fetch("https://example.test", { signal: controller.signal })); assert.equal(sent, 1);
});

for (const message of ["Invalid API key", "invalid_api_key", "expired credentials"]) {
  test(`HTTP 401 credential error retries within budget: ${message}`, async () => {
    let sent = 0;
    const fetch = createRetryFetch({ maxRetries: 2, baseDelayMs: 0, fetch: async () => {
      sent++; return new Response(message, { status: 401 });
    } });
    assert.equal((await invoke(fetch)).status, 401);
    assert.equal(sent, 3);
  });
}

for (const bad of [
  'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":null}]}\n\ndata: [DONE]\n\n',
  'data: [DONE]\n\n',
  'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":null},{"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n',
  'data: [DONE]\n\ndata: {"choices":[{"finish_reason":"stop"}]}\n\n',
]) {
  test("SSE [DONE] does not substitute for finish_reason on choice 0", async () => {
    const bodies = [], failures = [];
    const fetch = createRetryFetch({ maxRetries: 2, baseDelayMs: 0,
      onFailure: e => failures.push(e), fetch: async req => {
        bodies.push(await req.text()); return sse(bodies.length === 1 ? bad : good);
      } });
    assert.equal(await (await invoke(fetch)).text(), good);
    assert.equal(bodies.length, 2); assert.equal(bodies[0], bodies[1]);
    assert.equal(failures[0].failure.kind, "incomplete_stream");
    assert.equal(failures[0].bytes.toString(), bad);
  });
}

test("retry attempts use a fresh dispatcher while initial request uses default pool", async () => {
  const calls = [], failures = [];
  const fetch = createRetryFetch({ maxRetries: 2, baseDelayMs: 0,
    onEvent: e => e.type === "http_request_failure" && failures.push(e),
    fetch: async (req, init) => {
      calls.push({ body: await req.text(), fresh: Boolean(init?.dispatcher) });
      return sse(calls.length === 1 ? "data: [DONE]\n\n" : good);
    } });
  assert.equal(await (await invoke(fetch)).text(), good);
  assert.equal(calls.length, 2);
  assert.equal(calls[0].fresh, false);
  assert.equal(calls[1].fresh, true);
  assert.equal(calls[0].body, calls[1].body);
  assert.equal(failures.length, 1);
  assert.equal(failures[0].fresh_connection, false);
});

test("falls back cleanly when dispatcherFactory returns null", async () => {
  const calls = [];
  const fetch = createRetryFetch({ maxRetries: 2, baseDelayMs: 0,
    dispatcherFactory: () => null, fetch: async (req, init) => {
      calls.push({ body: await req.text(), init });
      return sse(calls.length === 1 ? "data: [DONE]\n\n" : good);
    } });
  assert.equal(await (await invoke(fetch)).text(), good);
  assert.equal(calls.length, 2);
  assert.equal(calls[1].init, undefined);
  assert.equal(calls[0].body, calls[1].body);
});

test("openai-responses SSE requires response.completed and retries truncated or failed streams", async () => {
  const completedSse =
    'event: response.created\ndata: {"type":"response.created","response":{"id":"r1"}}\n\n' +
    'event: response.completed\ndata: {"type":"response.completed","response":{"id":"r1","status":"completed","output":[]}}\n\n';
  const truncatedSse = 'event: response.created\ndata: {"type":"response.created","response":{"id":"r1"}}\n\n';
  const failedSse = 'event: response.failed\ndata: {"type":"response.failed","response":{"error":{"message":"server_error"}}}\n\n';
  const seen = [];
  const fetch = createRetryFetch({
    api: "openai-responses",
    maxRetries: 2,
    baseDelayMs: 0,
    onFailure: e => seen.push(e.failure.kind),
    fetch: async () => sse(seen.length === 0 ? truncatedSse : seen.length === 1 ? failedSse : completedSse),
  });
  assert.equal(await (await invoke(fetch)).text(), completedSse);
  assert.deepEqual(seen, ["incomplete_stream", "stream_error"]);
});
