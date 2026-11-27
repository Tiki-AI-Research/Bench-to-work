// Strict raw-argument validation and structured submit tool wrapper: intercepts type mismatches before SDK coercion.
import { Compile } from "typebox/compile";

const validatorCache = new WeakMap();

function getValidator(schema) {
  let validator = validatorCache.get(schema);
  if (!validator) {
    validator = Compile(schema);
    validatorCache.set(schema, validator);
  }
  return validator;
}

function jsonTypeOf(value) {
  if (value === null) return "null";
  if (Array.isArray(value)) return "array";
  return typeof value;
}

function valueAtPointer(root, instancePath) {
  if (!instancePath) return root;
  let current = root;
  for (const rawSegment of instancePath.slice(1).split("/")) {
    if (current === null || typeof current !== "object") return undefined;
    const segment = rawSegment.replaceAll("~1", "/").replaceAll("~0", "~");
    current = current[segment];
  }
  return current;
}

function formatPath(error) {
  const base = error.instancePath
    ? error.instancePath
        .slice(1)
        .split("/")
        .map((s) => s.replaceAll("~1", "/").replaceAll("~0", "~"))
        .join(".")
    : "";
  if (error.keyword === "required") {
    const missing = error.params?.requiredProperties;
    if (Array.isArray(missing) && missing.length === 1) {
      return base ? `${base}.${missing[0]}` : missing[0];
    }
  }
  return base || "root";
}

function formatValidationError(error, rootValue) {
  const path = formatPath(error);
  if (error.keyword === "type") {
    const expected = Array.isArray(error.params?.type)
      ? error.params.type.join(" | ")
      : String(error.params?.type ?? "unknown");
    const actual = jsonTypeOf(valueAtPointer(rootValue, error.instancePath));
    if (expected === "array" && actual === "string") {
      return `  - ${path}: ${error.message} (expected array, got string; pass a native JSON array [...] directly without serializing it in quotes)`;
    }
    if (expected === "object" && actual === "string") {
      return `  - ${path}: ${error.message} (expected object, got string; pass a native JSON object {...} directly without serializing it in quotes)`;
    }
    return `  - ${path}: ${error.message} (expected ${expected}, got ${actual}; null/string/number values are not auto-coerced)`;
  }
  if (error.keyword === "additionalProperties") {
    const extras = error.params?.additionalProperties;
    const detail = Array.isArray(extras) && extras.length ? `: ${extras.join(", ")}` : "";
    return `  - ${path}: ${error.message}${detail}`;
  }
  return `  - ${path}: ${error.message}`;
}

/**
 * Validate raw tool arguments strictly without type coercion against the provided Schema as-is.
 */
export function validateStrictParameters(
  schema,
  rawArgs,
  toolName,
  { allowTextFallback = true } = {},
) {
  const validator = getValidator(schema);
  if (validator.Check(rawArgs)) {
    return structuredClone(rawArgs);
  }
  const seen = new Set();
  const lines = [];
  for (const error of validator.Errors(rawArgs)) {
    if (
      error.keyword === "boolean" &&
      typeof error.schemaPath === "string" &&
      error.schemaPath.endsWith("/additionalProperties")
    ) {
      continue;
    }
    const line = formatValidationError(error, rawArgs);
    if (!seen.has(line)) {
      seen.add(line);
      lines.push(line);
    }
    if (lines.length >= 20) break;
  }
  const summary = lines.length ? lines.join("\n") : "  - root: arguments do not conform to Schema";
  const tail = allowTextFallback
    ? `Fix the field types or missing items above and call "${toolName}" again; if tool argument validation repeatedly fails, you may output a single valid JSON object as your final reply.`
    : `Fix the field types or missing items above and call "${toolName}" again.`;
  throw new Error(
    `Tool "${toolName}" arguments failed strict Schema validation:\n${summary}\n${tail}`,
  );
}

/**
 * Create a structured-output submission tool:
 * 1. Validate raw arguments in prepareArguments before pi-ai's coerceWithJsonSchema runs;
 * 2. Declare executionMode: "sequential" so preceding read tools finish first and the turn stops immediately upon submission;
 * 3. Consume the deep-cloned validated raw arguments in execute rather than SDK-coerced args.
 */
export function createStructuredSubmitTool({
  name,
  label,
  description,
  schema,
  onSubmit,
  confirmText = "Structured result submitted.",
}) {
  if (!name || !schema || typeof onSubmit !== "function") {
    throw new Error("createStructuredSubmitTool requires name, schema, and onSubmit");
  }
  let pending;
  return {
    name,
    executionMode: "sequential",
    label: label ?? "Submit structured result",
    description:
      description ??
      "Call this tool after completing all checks to submit the final structured result; arguments must strictly match the tool Schema.",
    parameters: schema,
    prepareArguments(rawArgs) {
      pending = undefined;
      pending = validateStrictParameters(schema, rawArgs, name);
      return rawArgs;
    },
    async execute(_id, _args) {
      const validated = pending;
      pending = undefined;
      if (validated === undefined) {
        throw new Error(`Tool "${name}" is missing validated raw arguments`);
      }
      onSubmit({
        text: JSON.stringify(validated),
        method: name,
        value: validated,
      });
      return {
        content: [{ type: "text", text: confirmText }],
        details: {},
      };
    },
  };
}

/**
 * Validate and normalize runtime delivery settings so submit tool and response_format stay strictly mutually exclusive.
 */
export function resolveRuntimeDelivery({ delivery, api, schema, config }) {
  if (delivery != null) {
    if (typeof delivery !== "object" || Array.isArray(delivery)) {
      throw new Error("delivery must be an object");
    }
    const mode = delivery.mode;
    const toolName = delivery.tool_name ?? null;
    if (!["tool", "response_format", "text"].includes(mode)) {
      throw new Error(`Unsupported delivery mode: ${mode}`);
    }
    if (mode === "tool") {
      if (!schema) throw new Error("tool delivery mode requires schema");
      if (typeof toolName !== "string" || !toolName.trim()) {
        throw new Error("tool delivery mode must declare tool_name");
      }
      return { mode: "tool", toolName, responseFormatEnabled: false };
    }
    if (mode === "response_format") {
      if (!schema) throw new Error("response_format delivery mode requires schema");
      if (api !== "openai-completions") {
        throw new Error("response_format delivery mode only supports openai-completions");
      }
      if (toolName) {
        throw new Error("response_format delivery mode cannot specify tool_name");
      }
      return { mode: "response_format", toolName: null, responseFormatEnabled: true };
    }
    if (toolName) {
      throw new Error("text delivery mode cannot specify tool_name");
    }
    return { mode: "text", toolName: null, responseFormatEnabled: false };
  }
  if (!schema) {
    return { mode: "text", toolName: null, responseFormatEnabled: false };
  }
  if (api === "anthropic-messages") {
    return { mode: "tool", toolName: "submit_review_result", responseFormatEnabled: false };
  }
  if (api === "openai-responses") {
    return config?.structured_output === false
      ? { mode: "text", toolName: null, responseFormatEnabled: false }
      : { mode: "tool", toolName: "submit_review_result", responseFormatEnabled: false };
  }
  if (config?.structured_output) {
    return { mode: "response_format", toolName: null, responseFormatEnabled: true };
  }
  return { mode: "text", toolName: null, responseFormatEnabled: false };
}
