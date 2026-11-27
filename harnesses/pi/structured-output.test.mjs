import test from "node:test";
import assert from "node:assert/strict";
import {
  validateStrictParameters,
  resolveRuntimeDelivery,
} from "./structured-output.mjs";

const REVIEW_SCHEMA = {
  type: "object",
  properties: {
    reason: { type: "string" },
    criteria: {
      type: "array",
      items: {
        type: "object",
        properties: {
          id: { type: "string" },
          evidence: { type: "string" },
          passed: { type: "boolean" },
        },
        required: ["id", "evidence", "passed"],
        additionalProperties: false,
      },
    },
  },
  required: ["reason", "criteria"],
  additionalProperties: false,
};

test("validateStrictParameters rejects passed=null and stringified arrays while allowing valid false", () => {
  assert.throws(
    () =>
      validateStrictParameters(
        REVIEW_SCHEMA,
        { reason: "r", criteria: [{ id: "c1", evidence: "e", passed: null }] },
        "submit_review_result",
      ),
    /criteria\.0\.passed.*expected boolean, got null/,
  );
  assert.throws(
    () =>
      validateStrictParameters(
        REVIEW_SCHEMA,
        { reason: "r", criteria: '[{"id":"c1","evidence":"e","passed":false}]' },
        "submit_review_result",
      ),
    /criteria.*expected array, got string/,
  );
  const checked = validateStrictParameters(
    REVIEW_SCHEMA,
    { reason: "r", criteria: [{ id: "c1", evidence: "e", passed: false }] },
    "submit_review_result",
  );
  assert.equal(checked.criteria[0].passed, false);
});

test("resolveRuntimeDelivery enforces mutual exclusion and rejects invalid combinations", () => {
  assert.deepEqual(
    resolveRuntimeDelivery({
      delivery: { mode: "tool", tool_name: "submit_review_result" },
      api: "openai-completions",
      schema: REVIEW_SCHEMA,
      config: { structured_output: true },
    }),
    { mode: "tool", toolName: "submit_review_result", responseFormatEnabled: false },
  );
  assert.deepEqual(
    resolveRuntimeDelivery({
      delivery: { mode: "response_format", tool_name: null },
      api: "openai-completions",
      schema: REVIEW_SCHEMA,
      config: { structured_output: true },
    }),
    { mode: "response_format", toolName: null, responseFormatEnabled: true },
  );
  assert.deepEqual(
    resolveRuntimeDelivery({
      delivery: null,
      api: "openai-responses",
      schema: REVIEW_SCHEMA,
      config: { structured_output: true },
    }),
    { mode: "tool", toolName: "submit_review_result", responseFormatEnabled: false },
  );
  assert.throws(
    () =>
      resolveRuntimeDelivery({
        delivery: { mode: "response_format", tool_name: null },
        api: "openai-responses",
        schema: REVIEW_SCHEMA,
        config: { structured_output: true },
      }),
    /openai-completions/,
  );
  assert.throws(
    () =>
      resolveRuntimeDelivery({
        delivery: { mode: "response_format", tool_name: null },
        api: "anthropic-messages",
        schema: REVIEW_SCHEMA,
        config: { structured_output: true },
      }),
    /openai-completions/,
  );
});


test("in-memory session constructs official openai-responses Sol tool loop and openai-completions gpt-6-luna reasoning_effort=none", async () => {
  const { mkdtemp, writeFile, rm } = await import("node:fs/promises");
  const os = await import("node:os");
  const path = await import("node:path");
  const {
    createAgentSession,
    createExtensionRuntime,
    ModelRuntime,
    SessionManager,
    SettingsManager,
  } = await import("@earendil-works/pi-coding-agent");
  const { createEvidenceTools } = await import("./tools.mjs");
  const { createRetryFetch } = await import("./retry-fetch.mjs");
  const { createStructuredSubmitTool } = await import("./structured-output.mjs");

  const tmp = await mkdtemp(path.join(os.tmpdir(), "pi-proto-test-"));
  try {
    await writeFile(path.join(tmp, "note.txt"), "value=42\n", "utf8");
    const loader = {
      getExtensions: () => ({ extensions: [], errors: [], runtime: createExtensionRuntime() }),
      getSkills: () => ({ skills: [], diagnostics: [] }),
      getPrompts: () => ({ prompts: [], diagnostics: [] }),
      getThemes: () => ({ themes: [], diagnostics: [] }),
      getAgentsFiles: () => ({ agentsFiles: [] }),
      getSystemPrompt: () => "Inspect workspace strictly.",
      getSystemPromptSource: () => undefined,
      getAppendSystemPrompt: () => [],
      getAppendSystemPromptSources: () => [],
      extendResources: () => {},
      reload: async () => {},
    };

    // 1. Official OpenAI Responses multi-turn tool call (gpt-6.2-sol) without TCP sockets
    const rtSol = await ModelRuntime.create({
      authPath: path.join(tmp, "auth-sol.json"),
      modelsPath: null,
      modelsStorePath: path.join(tmp, "cache-sol"),
      allowModelNetwork: false,
      refreshOnCreate: false,
    });
    rtSol.registerProvider("openai", {
      baseUrl: "https://api.openai.com/v1",
      api: "openai-responses",
      authHeader: false,
      apiKey: "OPENAI_API_KEY",
      models: [
        {
          id: "gpt-6.2-sol",
          name: "gpt-6.2-sol",
          reasoning: true,
          input: ["text"],
          cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
          contextWindow: 131072,
          maxTokens: 16384,
          compat: { supportsStrictMode: true, supportsDeveloperRole: true },
        },
      ],
    });
    await rtSol.setRuntimeApiKey("openai", "mock-key");
    let solSubmitted = null;
    const solTools = [
      ...createEvidenceTools(tmp, "python3", false, "reviewer", null),
      createStructuredSubmitTool({
        name: "submit_review_result",
        label: "Submit",
        description: "Submit structured review.",
        schema: REVIEW_SCHEMA,
        confirmText: "Submitted.",
        onSubmit: item => {
          solSubmitted = item.value;
        },
      }),
    ];
    const { session: solSession } = await createAgentSession({
      cwd: tmp,
      agentDir: tmp,
      modelRuntime: rtSol,
      model: rtSol.getModel("openai", "gpt-6.2-sol"),
      thinkingLevel: "high",
      resourceLoader: loader,
      tools: solTools.map(t => t.name),
      customTools: solTools,
      sessionManager: SessionManager.inMemory(tmp),
      settingsManager: SettingsManager.inMemory({
        retry: { enabled: false, maxRetries: 0, provider: { maxRetries: 0 } },
      }),
    });
    solSession.subscribe(e => {
      if (e.type === "tool_execution_end" && e.toolName === "submit_review_result" && !e.isError && solSubmitted) {
        void solSession.abort();
      }
    });
    const solBodies = [];
    let solCalls = 0;
    const sseEv = (k, v) => `event: ${k}\ndata: ${JSON.stringify(v)}\n\n`;
    const solFetch = createRetryFetch({
      api: "openai-responses",
      maxRetries: 1,
      baseDelayMs: 0,
      fetch: async req => {
        assert.equal(req.url, "https://api.openai.com/v1/responses");
        solCalls += 1;
        const body = JSON.parse(await req.text());
        if (solCalls === 1) {
          return new Response(sseEv("response.created", { type: "response.created", response: { id: "r0" } }), {
            status: 200,
            headers: { "content-type": "text/event-stream" },
          });
        }
        solBodies.push(body);
        if (solBodies.length === 1) {
          const item = {
            type: "function_call",
            id: "fc_1",
            call_id: "call_1",
            name: "read",
            arguments: JSON.stringify({ path: "note.txt" }),
            status: "completed",
          };
          return new Response(
            sseEv("response.created", { type: "response.created", response: { id: "r1" } }) +
              sseEv("response.output_item.added", { type: "response.output_item.added", output_index: 0, item: { ...item, arguments: "" } }) +
              sseEv("response.function_call_arguments.done", { type: "response.function_call_arguments.done", output_index: 0, item_id: "fc_1", arguments: item.arguments }) +
              sseEv("response.output_item.done", { type: "response.output_item.done", output_index: 0, item }) +
              sseEv("response.completed", { type: "response.completed", response: { id: "r1", status: "completed", output: [item], usage: { input_tokens: 5, output_tokens: 5, total_tokens: 10 } } }),
            { status: 200, headers: { "content-type": "text/event-stream" } },
          );
        }
        const subArgs = JSON.stringify({ reason: "ok", criteria: [] });
        const sub = { type: "function_call", id: "fc_2", call_id: "call_2", name: "submit_review_result", arguments: subArgs, status: "completed" };
        return new Response(
          sseEv("response.created", { type: "response.created", response: { id: "r2" } }) +
            sseEv("response.output_item.added", { type: "response.output_item.added", output_index: 0, item: { ...sub, arguments: "" } }) +
            sseEv("response.function_call_arguments.done", { type: "response.function_call_arguments.done", output_index: 0, item_id: "fc_2", arguments: subArgs }) +
            sseEv("response.output_item.done", { type: "response.output_item.done", output_index: 0, item: sub }) +
            sseEv("response.completed", { type: "response.completed", response: { id: "r2", status: "completed", output: [sub], usage: { input_tokens: 8, output_tokens: 5, total_tokens: 13 } } }),
          { status: 200, headers: { "content-type": "text/event-stream" } },
        );
      },
    });
    const baseSolStream = solSession.agent.streamFunction;
    solSession.agent.streamFunction = (m, c, o) => baseSolStream(m, c, { ...o, fetch: solFetch });
    await solSession.prompt("Check note.txt");
    assert.deepEqual(solSubmitted, { reason: "ok", criteria: [] });
    assert.equal(solCalls, 3);
    assert.equal(solBodies[0].reasoning.effort, "high");
    const fcReplay = solBodies[1].input.find(x => x.type === "function_call");
    assert.equal(fcReplay.call_id, "call_1");
    assert.equal(fcReplay.id, "fc_1");

    // 2. Official Chat Completions gpt-6-luna with 7 file tools & reasoning_effort="none"
    const rtLuna = await ModelRuntime.create({
      authPath: path.join(tmp, "auth-luna.json"),
      modelsPath: null,
      modelsStorePath: path.join(tmp, "cache-luna"),
      allowModelNetwork: false,
      refreshOnCreate: false,
    });
    rtLuna.registerProvider("task-model", {
      baseUrl: "https://api.openai.com/v1",
      api: "openai-completions",
      authHeader: false,
      apiKey: "OPENAI_API_KEY",
      models: [
        {
          id: "gpt-6-luna",
          name: "gpt-6-luna",
          reasoning: true,
          input: ["text"],
          cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
          contextWindow: 131072,
          maxTokens: 16384,
          thinkingLevelMap: { off: "none" },
          compat: {
            supportsDeveloperRole: false,
            supportsStore: false,
            supportsReasoningEffort: true,
            maxTokensField: "max_completion_tokens",
          },
        },
      ],
    });
    await rtLuna.setRuntimeApiKey("task-model", "mock-key");
    const lunaTools = createEvidenceTools(tmp, "python3", false, "user", null);
    assert.equal(lunaTools.length, 7);
    const { session: lunaSession } = await createAgentSession({
      cwd: tmp,
      agentDir: tmp,
      modelRuntime: rtLuna,
      model: rtLuna.getModel("task-model", "gpt-6-luna"),
      thinkingLevel: "off",
      resourceLoader: loader,
      tools: lunaTools.map(t => t.name),
      customTools: lunaTools,
      sessionManager: SessionManager.inMemory(tmp),
      settingsManager: SettingsManager.inMemory({
        retry: { enabled: false, maxRetries: 0, provider: { maxRetries: 0 } },
      }),
    });
    let lunaBody = null;
    const lunaFetch = createRetryFetch({
      api: "openai-completions",
      maxRetries: 0,
      fetch: async req => {
        assert.equal(req.url, "https://api.openai.com/v1/chat/completions");
        lunaBody = JSON.parse(await req.text());
        return new Response(
          'data: {"id":"c1","object":"chat.completion.chunk","created":0,"model":"gpt-6-luna","choices":[{"index":0,"delta":{"content":"Looks good."},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n',
          { status: 200, headers: { "content-type": "text/event-stream" } },
        );
      },
    });
    const baseLunaStream = lunaSession.agent.streamFunction;
    lunaSession.agent.streamFunction = (m, c, o) => baseLunaStream(m, c, { ...o, fetch: lunaFetch });
    await lunaSession.prompt("Reply briefly");
    assert.equal(lunaBody.model, "gpt-6-luna");
    assert.equal(lunaBody.reasoning_effort, "none");
    assert.equal(lunaBody.tools.length, 7);
    assert.equal(lunaBody.max_completion_tokens, 16384);
  } finally {
    await rm(tmp, { recursive: true, force: true });
  }
});
