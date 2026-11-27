// Invoked over stdin by Python PiAgent; pi owns the model/tool loop while Runner owns stage progression.
import { mkdtemp, readFile, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { createInterface } from "node:readline";
import {
  createAgentSession, createExtensionRuntime, ModelRuntime, SessionManager, SettingsManager,
} from "@earendil-works/pi-coding-agent";
import { createEvidenceTools, imageContent } from "./tools.mjs";
import { createRetryFetch } from "./retry-fetch.mjs";
import { createStructuredSubmitTool, resolveRuntimeDelivery } from "./structured-output.mjs";

let session;
let scratch;
let apiKey = "";
let input;
const log = value => process.stderr.write(JSON.stringify(value).replaceAll(apiKey || "\u0000", "[redacted]") + "\n");
try {
  input = createInterface({ input: process.stdin, crlfDelay: Infinity });
  const lines = input[Symbol.asyncIterator]();
  const request = JSON.parse((await lines.next()).value);
  const config = request.config;
  const api = config.api ?? "openai-completions";
  if (!["openai-completions", "openai-responses", "anthropic-messages"].includes(api)) throw new Error("Unsupported pi API protocol");
  if (!["user", "reviewer"].includes(request.role)) throw new Error("pi wrapper is only for User and Reviewer roles");
  apiKey = process.env[config.key_env] || (config.key_file ? (await readFile(config.key_file, "utf8")).trim() : "");
  if (!apiKey) throw new Error("Missing pi model API credentials");
  scratch = await mkdtemp(path.join(os.tmpdir(), "multi-round-pi-session-"));
  // Never load personal pi config, skills, AGENTS.md, or saved sessions; every call starts from explicit inputs.
  const runtime = await ModelRuntime.create({ authPath: path.join(scratch, "auth.json"),
    modelsPath: null, modelsStorePath: path.join(scratch, "models-cache"),
    allowModelNetwork: false, refreshOnCreate: false });
  const providerName = api === "openai-responses" ? "openai" : "task-model";
  runtime.registerProvider(providerName, {
    baseUrl: config.base_url, api, authHeader: config.auth_header ?? false, apiKey: config.key_env,
    models: [{ id: config.model, name: config.model, reasoning: config.reasoning,
      input: config.supports_images ? ["text", "image"] : ["text"],
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
      contextWindow: config.context_window, maxTokens: config.max_tokens,
      ...(config.thinking_level_map ? { thinkingLevelMap: config.thinking_level_map } : {}),
      compat: config.compat }],
  });
  await runtime.setRuntimeApiKey(providerName, apiKey);
  if (typeof request.system !== "string" || !request.system.trim()) throw new Error("Missing role system prompt");
  const resourceLoader = {
    getExtensions: () => ({ extensions: [], errors: [], runtime: createExtensionRuntime() }),
    getSkills: () => ({ skills: [], diagnostics: [] }), getPrompts: () => ({ prompts: [], diagnostics: [] }),
    getThemes: () => ({ themes: [], diagnostics: [] }), getAgentsFiles: () => ({ agentsFiles: [] }),
    // Schema constrains final structured evaluation results; User returns plain message text.
    getSystemPrompt: () => request.system,
    getSystemPromptSource: () => undefined, getAppendSystemPrompt: () => [], getAppendSystemPromptSources: () => [],
    extendResources: () => {}, reload: async () => {},
  };
  // Expose read-only workspace tools (plus offline Docker bash strictly for Reviewer when sandbox_image is configured).
  const sandbox = request.role === "reviewer" && config.sandbox_image
    ? { image: config.sandbox_image, defaultTimeoutSeconds: config.bash_timeout ?? 120 }
    : null;
  const tools = createEvidenceTools(request.workspace, request.python, config.supports_images, request.role, sandbox);
  const delivery = resolveRuntimeDelivery({
    delivery: request.delivery,
    api,
    schema: request.schema,
    config,
  });
  let submission = null;
  if (delivery.mode === "tool") {
    tools.push(
      createStructuredSubmitTool({
        name: delivery.toolName,
        label: "Submit structured result",
        description:
          "Call this tool after completing all checks to submit the final structured result; arguments must strictly match the tool Schema (pass arrays and objects as native JSON values).",
        schema: request.schema,
        confirmText: "Structured result submitted.",
        onSubmit: item => {
          submission = item;
        },
      }),
    );
  }
  const cwd = request.workspace ?? scratch;
  ({ session } = await createAgentSession({ cwd, agentDir: scratch, modelRuntime: runtime,
    model: runtime.getModel(providerName, config.model), thinkingLevel: config.thinking_level, resourceLoader,
    tools: tools.map(t => t.name), customTools: tools,
    sessionManager: SessionManager.inMemory(cwd),
    settingsManager: SettingsManager.inMemory({ compaction: config.compaction,
      // Read-only tools stay intact across HTTP retries; disable SDK/provider retries so retry budgets do not multiply.
      retry: { enabled: false, maxRetries: 0,
        baseDelayMs: 1000, maxAgentDelayMs: 30000,
        provider: { maxRetries: 0, maxRetryDelayMs: 30000, timeoutMs: config.request_timeout_ms },
      } }),
  }));
  // Single HTTP retry layer via retryFetch; SDK does not restart failed turns on top of it.
  const retryFetch = createRetryFetch({ api, maxRetries: config.api_retries ?? 2, onEvent: log });
  const stream = session.agent.streamFunction;
  session.agent.streamFunction = (model, context, options) =>
    stream(model, context, { ...options, fetch: retryFetch });
  // Delivery mode is resolved once; tool submission and response_format never enable simultaneously.
  // Raw model text is returned untouched for shared JSON recovery in Python rather than silently patched here.
  session.agent.onPayload = payload => {
    const outgoing = delivery.responseFormatEnabled ? { ...payload,
      response_format: { type: "json_schema", json_schema: { name: "role_reply", strict: true, schema: request.schema } } } : payload;
    // Log the actual outgoing token budget after any SDK context-window clamping.
    log({ type: "model_request", model: config.model, api, thinking: outgoing.thinking,
      max_tokens: outgoing.max_tokens ?? null,
      max_completion_tokens: outgoing.max_completion_tokens ?? null,
      max_output_tokens: outgoing.max_output_tokens ?? null,
      reasoning_effort: outgoing.reasoning_effort ?? outgoing.reasoning?.effort ?? null });
    return outgoing;
  };
  let turns = 0;
  let exhausted = false;
  log({ type: "session_config", role: request.role, model: config.model, api, tools: session.getActiveToolNames(),
    pi_version: "0.87.1", workspace: request.workspace,
    sandbox_image: sandbox?.image ?? null,
    retry: session.settingsManager.getRetrySettings(),
    provider_retry: session.settingsManager.getProviderRetrySettings(),
    request_retries: config.api_retries ?? 2, response_buffered: true,
    thinking_level: session.thinkingLevel, reasoning: config.reasoning,
    thinking_level_map: config.thinking_level_map, compat: config.compat,
    structured_output: config.structured_output,
    delivery_mode: delivery.mode,
    submit_tool: delivery.toolName,
    response_format_enabled: delivery.responseFormatEnabled,
    max_tokens: config.max_tokens, context_window: config.context_window,
    compaction: session.settingsManager.getCompactionSettings(session.model) });
  session.subscribe(event => {
    // Post-submission abort events do not represent new model turns.
    if (event.type === "turn_start" && !submission && ++turns > config.max_model_turns) {
      exhausted = true;
      void session.abort();
    }
    if (
      event.type === "tool_execution_end" &&
      delivery.mode === "tool" &&
      event.toolName === delivery.toolName &&
      !event.isError &&
      submission
    ) {
      void session.abort();
    }
    // Persist raw tool/message/compaction events without altering outer Runner stage or retry counts.
    if (["message_end", "tool_execution_start", "tool_execution_end",
      "compaction_start", "compaction_end", "auto_retry_start", "auto_retry_end"].includes(event.type)) log(event);
  });
  const images = [];
  for (const attachment of request.attachments ?? []) {
    if (attachment.kind !== "image") {
      throw new Error("Non-image files must be placed in the role workspace rather than ignored as attachments");
    }
    if (!config.supports_images) throw new Error("Current model does not declare image support");
    images.push(await imageContent(attachment.path));
  }
  // In tool mode, the submit tool already carries request.schema in its parameters; omit duplicate text Schema suffix.
  const prompt = request.schema && delivery.mode !== "tool"
    ? request.prompt + "\nOutput must conform to the following JSON Schema:\n" + JSON.stringify(request.schema)
    : request.prompt;
  await session.prompt(prompt, { expandPromptTemplates: false, images });
  const last = session.messages.filter(m => m.role === "assistant").at(-1);
  // Normalize native pause/truncation/context-overflow stop reasons so incomplete outputs are never treated as valid stops.
  const raw = last?.rawStopReason;
  const stopReason = raw === "max_tokens" ? "length"
    : ["pause_turn", "model_context_window_exceeded"].includes(raw) ? raw
    : last?.stopReason ?? "empty";
  const meta = { stopReason, ...(raw ? { rawStopReason: raw } : {}) };
  if (exhausted) {
    throw Object.assign(new Error("pi exceeded max_model_turns budget"), { ...meta, exhausted: true });
  }
  if (submission) {
    process.stdout.write(JSON.stringify({ text: submission.text, completion_method: submission.method }) + "\n");
  } else {
    if (stopReason !== "stop") {
      throw Object.assign(
        new Error(`pi did not complete normally: ${stopReason} ${last?.errorMessage ?? ""}`),
        meta,
      );
    }
    const rawText = session.getLastAssistantText();
    if (typeof rawText !== "string" || !rawText.trim()) {
      throw Object.assign(
        new Error("pi stopped normally without delivering visible assistant text"),
        { ...meta, stopReason: "empty_response" },
      );
    }
    // Preserve exact raw assistant text inside the JSON stdout envelope for traceable recovery records.
    process.stdout.write(JSON.stringify({ text: rawText, completion_method: "assistant_stop" }) + "\n");
  }
} catch (error) {
  log({
    type: "error",
    message: error.message,
    ...(error.stopReason ? { stop_reason: error.stopReason } : {}),
    ...(error.rawStopReason ? { raw_stop_reason: error.rawStopReason } : {}),
    ...(error.exhausted ? { exhausted: true } : {}),
  });
  process.exitCode = 1;
} finally {
  input?.close();
  session?.dispose();
  if (scratch) await rm(scratch, { recursive: true, force: true });
}
