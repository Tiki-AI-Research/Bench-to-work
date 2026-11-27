// Read-only evidence tools scoped strictly to the role's visible root directory,
// plus optional offline Docker sandbox bash for Reviewer.
import { execFile } from "node:child_process";
import { readFile, readdir, realpath, stat } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createLsToolDefinition, createReadToolDefinition } from "@earendil-works/pi-coding-agent";
import { readConversation } from "./conversation.mjs";
import { createReviewerSandboxBashTool } from "./sandbox-bash.mjs";

const imageTypes = { ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp" };
const textTypes = new Set([".txt", ".log", ".md", ".csv", ".tsv", ".json", ".jsonl", ".xml", ".html", ".py", ".js", ".css", ".yaml", ".yml"]);
const textResult = (value) => ({ content: [{ type: "text", text: JSON.stringify(value) }], details: {} });
const schema = (properties, required = []) => ({ type: "object", properties, required, additionalProperties: false });
const string = { type: "string" };

export async function allowedPath(root, requested) {
  const base = await realpath(root);
  const target = await realpath(path.resolve(base, requested));
  const relative = path.relative(base, target);
  if (relative === ".." || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
    throw new Error("Path is outside this role's visible workspace directory");
  }
  return target;
}

export async function imageContent(filename) {
  const mimeType = imageTypes[path.extname(filename).toLowerCase()];
  if (!mimeType) throw new Error("Unsupported image format");
  if ((await stat(filename)).size > 10 * 1024 * 1024) throw new Error("Image exceeds 10MB read limit");
  return { type: "image", mimeType, data: (await readFile(filename)).toString("base64") };
}

function runPythonHelper(python, scriptName, payload, signal, errorPrefix) {
  const helper = fileURLToPath(new URL(`./${scriptName}`, import.meta.url));
  // Fixed read-only parser inside this repository; arguments carry data only, never executable code.
  return new Promise((resolve, reject) => {
    const child = execFile(
      python,
      ["-B", helper],
      { signal, timeout: 30000, maxBuffer: 2 * 1024 * 1024, env: { ...process.env, PYTHONUTF8: "1" } },
      (error, stdout, stderr) => error
        ? reject(new Error(`${errorPrefix}: ${stderr.trim() || error.message}`, { cause: error }))
        : resolve(JSON.parse(stdout)),
    );
    child.stdin.end(JSON.stringify(payload));
  });
}

export function createEvidenceTools(root, python, supportsImages, role, sandbox = null) {
  if (root === null) return []; // Progress review and context-mode User have no tools; formal Reviewer reads its isolated directory.
  const tools = [
    ...(role === "reviewer" ? [{
      name: "read_conversation", label: "Read conversation by index",
      description: "Read raw messages from evaluation/conversation.jsonl (0-based index; start_index and end_index are inclusive; max_messages defaults to 20, max 100). Preserves role and attachments; content_complete=false indicates a long message continues across pages, and has_more=true provides next arguments to continue. Returns verbatim dialogue only without judging rubric pass/fail.",
      parameters: schema({ start_index: { type: "integer", minimum: 0 },
        end_index: { type: "integer", minimum: 0 }, max_messages: { type: "integer", minimum: 1, maximum: 100 },
        offset: { type: "integer", minimum: 0 } }),
      async execute(_id, args) {
        const filename = await allowedPath(root, "evaluation/conversation.jsonl");
        return textResult(await readConversation(filename, args));
      },
    }] : []),
    // Reuse SDK pagination, truncation, and image encoding while restricting filesystem access and allowed formats.
    createLsToolDefinition(root, { operations: {
      exists: async filename => { await allowedPath(root, filename); return true; },
      stat: async filename => stat(await allowedPath(root, filename)),
      readdir: async filename => readdir(await allowedPath(root, filename)),
    } }),
    createReadToolDefinition(root, { operations: {
      access: async filename => {
        const allowed = await allowedPath(root, filename);
        const suffix = path.extname(allowed).toLowerCase();
        if (imageTypes[suffix]) {
          if (!supportsImages) throw new Error("Current model does not support image inputs");
        } else if (!textTypes.has(suffix)) {
          throw new Error(
            "No plain-text read tool for this format (never treat a file name as read content; " +
            "use read_excel for .xlsx, read_docx for .docx, read_pdf for .pdf, read_pptx for .pptx, " +
            "or inspect extracted observations in evaluation/verification.json via read_json when available)",
          );
        }
      },
      readFile: async filename => readFile(await allowedPath(root, filename)),
      detectImageMimeType: async filename => imageTypes[path.extname(await allowedPath(root, filename)).toLowerCase()],
    } }),
    {
      name: "read_json", label: "Read JSON by pointer",
      description: "Read a specific field from a JSON file via JSON Pointer (e.g., /observations/office_contents) to avoid flooding context. Omit pointer to list root keys; escape ~ as ~0 and / as ~1; pass an empty string pointer to read the root value. Long string fields paginate in 16,000-character chunks starting at offset=0; follow next when has_more=true.",
      parameters: schema({ path: string, pointer: string, offset: { type: "integer", minimum: 0 } }, ["path"]),
      async execute(_id, args) {
        const filename = await allowedPath(root, args.path);
        if (path.extname(filename).toLowerCase() !== ".json") throw new Error("read_json only supports .json files");
        let value = JSON.parse(await readFile(filename, "utf8"));
        if (args.pointer === undefined) {
          if (args.offset !== undefined) throw new Error("offset can only be used with a pointer targeting a string field");
          return textResult({ keys: Object.keys(value ?? {}) });
        }
        if (args.pointer !== "" && (!args.pointer.startsWith("/") || /~(?![01])/u.test(args.pointer))) {
          throw new Error("pointer must be empty or a valid JSON Pointer starting with /");
        }
        for (const token of args.pointer === "" ? [] : args.pointer.slice(1).split("/")) {
          const key = token.replaceAll("~1", "/").replaceAll("~0", "~");
          if (value === null || typeof value !== "object" || !Object.hasOwn(value, key)
              || (Array.isArray(value) && !/^(0|[1-9][0-9]*)$/u.test(key))) {
            throw new Error(`JSON Pointer path does not exist: ${args.pointer}`);
          }
          value = value[key];
        }
        // Paginate long string values directly without requiring line-based text tools or shell access.
        if (typeof value === "string") {
          const offset = args.offset ?? 0;
          if (!Number.isInteger(offset) || offset < 0 || offset > value.length) {
            throw new Error("offset must be a non-negative integer within the string length");
          }
          const part = value.slice(offset, offset + 16000);
          const hasMore = offset + part.length < value.length;
          return textResult({ pointer: args.pointer, value: part, offset, total_length: value.length,
            has_more: hasMore,
            next: hasMore ? { path: args.path, pointer: args.pointer, offset: offset + part.length } : null });
        }
        if (args.offset !== undefined) throw new Error("offset can only be used on string fields");
        const text = JSON.stringify({ pointer: args.pointer, value });
        if (text.length > 16000) {
          return textResult({ pointer: args.pointer, too_large: true,
            keys: value !== null && typeof value === "object" ? Object.keys(value).slice(0, 200) : [],
            instruction: "Field value omitted because serialized JSON exceeds 16,000 characters; choose a more specific pointer (index array elements by number, or paginate string fields with offset)." });
        }
        return { content: [{ type: "text", text }], details: {} };
      },
    },
    {
      name: "read_excel", label: "Read Excel workbook",
      description: "Read worksheet names/dimensions or cell formulas and last-saved cached values from an .xlsx workbook without recalculating or saving.",
      parameters: schema({ path: string, sheet: string, start_row: { type: "integer", minimum: 1 },
        max_rows: { type: "integer", minimum: 1, maximum: 100 } }, ["path"]),
      async execute(_id, args, signal) {
        const filename = await allowedPath(root, args.path);
        if (path.extname(filename).toLowerCase() !== ".xlsx") throw new Error("read_excel only supports .xlsx files");
        return textResult(await runPythonHelper(python, "read_excel.py", { ...args, path: filename }, signal, "Excel read failed"));
      },
    },
    {
      name: "read_docx", label: "Read Word document",
      description: "Read body paragraphs, tables (in exact document order), headers, footers, comments, and footnotes/endnotes from a .docx Word document without modifying the file. Paginates by character offset (default max_chars=16000); follow next when has_more=true.",
      parameters: schema({
        path: string,
        offset: { type: "integer", minimum: 0 },
        max_chars: { type: "integer", minimum: 1, maximum: 32000 },
      }, ["path"]),
      async execute(_id, args, signal) {
        const filename = await allowedPath(root, args.path);
        if (path.extname(filename).toLowerCase() !== ".docx") throw new Error("read_docx only supports .docx files");
        return textResult(await runPythonHelper(
          python, "read_office.py", { ...args, format: "docx", path: filename, client_path: args.path }, signal, "Word read failed",
        ));
      },
    },
    {
      name: "read_pdf", label: "Read PDF document",
      description: "Read page text from a .pdf document (1-based start_page, default max_pages=10, bounded by max_chars=16000) without modifying the file; follow next when has_more=true.",
      parameters: schema({
        path: string,
        start_page: { type: "integer", minimum: 1 },
        max_pages: { type: "integer", minimum: 1, maximum: 50 },
        offset: { type: "integer", minimum: 0 },
        max_chars: { type: "integer", minimum: 1, maximum: 32000 },
      }, ["path"]),
      async execute(_id, args, signal) {
        const filename = await allowedPath(root, args.path);
        if (path.extname(filename).toLowerCase() !== ".pdf") throw new Error("read_pdf only supports .pdf files");
        return textResult(await runPythonHelper(
          python, "read_office.py", { ...args, format: "pdf", path: filename, client_path: args.path }, signal, "PDF read failed",
        ));
      },
    },
    {
      name: "read_pptx", label: "Read PowerPoint presentation",
      description: "Read slide text, tables, and speaker notes from a .pptx presentation in slide order (1-based start_slide, default max_slides=20, bounded by max_chars=16000); follow next when has_more=true.",
      parameters: schema({
        path: string,
        start_slide: { type: "integer", minimum: 1 },
        max_slides: { type: "integer", minimum: 1, maximum: 50 },
        offset: { type: "integer", minimum: 0 },
        max_chars: { type: "integer", minimum: 1, maximum: 32000 },
      }, ["path"]),
      async execute(_id, args, signal) {
        const filename = await allowedPath(root, args.path);
        if (path.extname(filename).toLowerCase() !== ".pptx") throw new Error("read_pptx only supports .pptx files");
        return textResult(await runPythonHelper(
          python, "read_office.py", { ...args, format: "pptx", path: filename, client_path: args.path }, signal, "PPTX read failed",
        ));
      },
    },
  ];

  // Offline Docker bash is granted strictly to Reviewer when an explicit sandbox image is configured.
  if (role === "reviewer" && sandbox?.image) {
    tools.push(createReviewerSandboxBashTool(root, sandbox));
  }
  return tools;
}
