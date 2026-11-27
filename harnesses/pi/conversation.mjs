// Read evaluation conversation messages by 0-based index; returns verbatim text without relevance filtering or scoring.
import { createReadStream } from "node:fs";
import { createInterface } from "node:readline";

const MAX_CONTENT_CHARS = 16000;

export async function readConversation(filename, { start_index = 0, end_index, max_messages = 20, offset = 0 }) {
  if (!Number.isInteger(start_index) || start_index < 0
      || (end_index !== undefined && (!Number.isInteger(end_index) || end_index < start_index))
      || !Number.isInteger(max_messages) || max_messages < 1 || max_messages > 100
      || !Number.isInteger(offset) || offset < 0) {
    throw new Error(
      "Conversation range requires non-negative integer indices, end_index >= start_index, max_messages in 1..100, and offset >= 0",
    );
  }
  const messages = [];
  let remaining = MAX_CONTENT_CHARS;
  let contentOffset = offset;
  let next = null;
  let lastIndex = null;
  const stream = createReadStream(filename, { encoding: "utf8" });
  const lines = createInterface({ input: stream, crlfDelay: Infinity });
  try {
    // Stream line-by-line to capture last_index while collecting only the requested page regardless of total file size.
    for await (const line of lines) {
      if (!line.trim()) continue;
      const message = JSON.parse(line);
      lastIndex = message.index;
      if (next !== null || message.index < start_index
          || (end_index !== undefined && message.index > end_index)) continue;
      if (messages.length >= max_messages || remaining === 0) {
        next = { start_index: message.index, offset: 0, end_index, max_messages };
        continue;
      }
      if (typeof message.content !== "string") throw new Error("Evaluation conversation message content must be a string");
      if (contentOffset > message.content.length) throw new Error("offset exceeds current message content length");
      const content = message.content.slice(contentOffset, contentOffset + remaining);
      const complete = contentOffset + content.length === message.content.length;
      messages.push({ ...message, content, content_offset: contentOffset, content_complete: complete });
      remaining -= content.length;
      if (!complete) {
        next = { start_index: message.index, offset: contentOffset + content.length, end_index, max_messages };
      }
      contentOffset = 0;
    }
  } finally {
    lines.close();
    stream.destroy();
  }
  return {
    document: "evaluation/conversation.jsonl",
    last_index: lastIndex,
    messages,
    has_more: next !== null,
    next,
  };
}
