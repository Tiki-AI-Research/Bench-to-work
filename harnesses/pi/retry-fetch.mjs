// Bounded HTTP retry wrapper for a single model request; never restarts the Agent session or replays completed tools.
import { setTimeout as sleep } from "node:timers/promises";
import { Agent, fetch as nativeFetch, Request, Response } from "undici";

const terminalStatuses = new Set([402, 403, 404, 405, 410, 413, 415, 422, 451, 501, 505]);
const terminalMessages = [
  /\b(insufficient_quota|billing_hard_limit_reached)\b/i,
  /\b(insufficient (?:funds|balance|credits)|out of (?:budget|credits)|account (?:disabled|suspended|banned))\b/i,
  /\b(permission denied|access denied|safeguards|safety policy|policy violation)\b/i,
  /\b(context (?:window|length) exceeded|maximum context length|request (?:body|entity) too large)\b/i,
  /\b(model (?:not found|does not exist)|unknown model|unsupported (?:model|parameter|field))\b/i,
  /\b(?:invalid|unsupported) (?:max_tokens|max_completion_tokens|temperature|top_p|response_format|tool_choice)\b/i,
  /\b(invalid (?:request parameter|parameter|argument|json payload)|unknown (?:field|parameter))\b/i,
];

export function isRetryableFailure(status, message = "") {
  // HTTP 400 shares the bounded retry budget because upstream relays occasionally forward transient rejections as 400 invalid argument,
  // indistinguishable by status code or message text from genuine parameter errors.
  if (status === 400) return true;
  return !terminalStatuses.has(status) && !terminalMessages.some(pattern => pattern.test(message));
}

function inspectBody(response, bytes, api) {
  const text = bytes.toString("utf8");
  const type = response.headers.get("content-type") ?? "";
  if (response.status >= 400) return { message: text, kind: "http_error" };
  if (type.includes("text/event-stream")) {
    let hasFinishReason = false, hasMessageStop = false;
    for (const event of text.replaceAll("\r\n", "\n").split("\n\n")) {
      const data = event.split("\n").filter(line => line.startsWith("data:"))
        .map(line => line.slice(5).trimStart()).join("\n");
      if (!data) continue;
      // [DONE] only marks transport end; pi still requires finish_reason on the first choice.
      if (data.trim() === "[DONE]") break;
      let value;
      try { value = JSON.parse(data); }
      catch { return { kind: "invalid_stream_json", message: "Incomplete or invalid JSON in model response stream" }; }
      const error = value.error ??
        (value.type === "error" || /^event:\s*error\s*$/m.test(event) ? value : null);
      if (error) return { kind: "stream_error", message: JSON.stringify(error) };
      if (api === "anthropic-messages") {
        if (value.type === "message_delta" && value.delta?.stop_reason) hasFinishReason = true;
        if (value.type === "message_stop") hasMessageStop = true;
      } else if (api === "openai-responses") {
        if (value.type === "response.failed") {
          return { kind: "stream_error", message: JSON.stringify(value.response?.error ?? value) };
        }
        if (value.type === "response.completed" || value.type === "response.incomplete") hasFinishReason = true;
      } else if (value.choices?.[0]?.finish_reason) hasFinishReason = true;
    }
    if (!hasFinishReason || (api === "anthropic-messages" && !hasMessageStop))
      return { kind: "incomplete_stream", message: api === "anthropic-messages"
        ? "Anthropic stream ended without stop_reason or message_stop"
        : api === "openai-responses"
          ? "OpenAI Responses stream ended without response.completed"
          : "Model response stream ended without finish_reason" };
  } else if (type.includes("json")) {
    let value;
    try { value = JSON.parse(text); }
    catch { return { kind: "invalid_response_json", message: "Incomplete or invalid HTTP response JSON" }; }
    if (value.error) return { kind: "response_error", message: JSON.stringify(value.error) };
  }
  return null; // Model-text JSON syntax, tool arguments, and finish_reason='length' belong to protocol/incomplete layers, not HTTP retry.
}

// Use public undici.Agent API rather than Node-internal global symbols so connection isolation works across Node >=22.
function createFreshDispatcher() {
  return new Agent();
}

export function createRetryFetch({ api = "openai-completions", maxRetries, onEvent = () => {}, onFailure = async () => {},
  fetch: send = nativeFetch, baseDelayMs = 1000,
  dispatcherFactory = createFreshDispatcher }) {
  if (!Number.isInteger(maxRetries) || maxRetries < 0) throw new Error("maxRetries must be a non-negative integer");
  let requestNumber = 0;
  return async (input, init) => {
    const template = new Request(input, init);
    const signal = template.signal;
    const requestId = ++requestNumber;
    for (let attempt = 0; ; attempt++) {
      signal.throwIfAborted();
      // After a failure, the prior socket may be half-closed by the gateway; use a fresh connection pool on retries
      // so the next attempt does not send a truncated request over a stale keep-alive socket.
      const dispatcher = attempt === 0 ? null : dispatcherFactory();
      try {
        let response, failure, originalError;
        const chunks = [];
        let received = 0, progressAt = Date.now();
        try {
          response = await send(template.clone(), dispatcher ? { dispatcher } : undefined);
          if (response.body) {
            const reader = response.body.getReader();
            try {
              while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                chunks.push(Buffer.from(value)); received += value.length;
                if (Date.now() - progressAt >= 10000) {
                  onEvent({ type: "http_response_progress", request_id: requestId, attempt,
                    received_bytes: received });
                  progressAt = Date.now();
                }
              }
            } finally { reader.releaseLock(); }
          }
          failure = inspectBody(response, Buffer.concat(chunks), api);
        } catch (error) {
          if (signal.aborted || error.name === "AbortError") throw error;
          originalError = error;
          failure = { kind: "transport_error", message: error.message };
        }
        const bytes = Buffer.concat(chunks);
        if (failure) {
          const retry = attempt < maxRetries && isRetryableFailure(response?.status, failure.message);
          await onFailure({ requestId, attempt, response, bytes, failure,
            request: template.clone(), retry });
          // Record fresh_connection so post-mortem audits can verify whether retry attempts bypassed pooled sockets.
          onEvent({ type: "http_request_failure", request_id: requestId, attempt,
            status: response?.status ?? null, kind: failure.kind, message: failure.message.slice(0, 2000),
            retry, max_retries: maxRetries, received_bytes: received,
            fresh_connection: dispatcher !== null });
          if (retry) {
            await sleep(Math.min(baseDelayMs * 2 ** attempt, 30000), undefined, { signal });
            continue;
          }
          if (originalError) throw originalError;
        }
        // Buffer the complete response before handing it to the SDK so failed partial streams never pollute session messages.
        // Strip compression headers since fetch has already decoded the body bytes.
        const headers = new Headers(response.headers);
        headers.delete("content-encoding"); headers.delete("content-length");
        return new Response(bytes.length ? bytes : null,
          { status: response.status, statusText: response.statusText, headers });
      } finally {
        // Dispatcher teardown errors must not mask the primary request outcome.
        await dispatcher?.destroy().catch(() => {});
      }
    }
  };
}
