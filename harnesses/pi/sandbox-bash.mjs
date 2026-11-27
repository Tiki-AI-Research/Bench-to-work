// Offline Docker bash sandbox exclusively for Reviewer evidence checks:
// mounts the host review directory read-only at /workspace and overlays /workspace/scratch read-write.
import { spawn } from "node:child_process";
import { copyFile, lstat, mkdir, mkdtemp, readdir, readFile, realpath, rm, unlink } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { createBashToolDefinition } from "@earendil-works/pi-coding-agent";

const DEFAULT_BASH_TIMEOUT_SECONDS = 120;
const MAX_BASH_TIMEOUT_SECONDS = 1800;

function isInside(base, target) {
  const relative = path.relative(base, target);
  return relative === "" || (!relative.startsWith(`..${path.sep}`) && relative !== ".." && !path.isAbsolute(relative));
}

async function assertSafeReviewTree(realRoot) {
  const stack = [realRoot];
  while (stack.length > 0) {
    const current = stack.pop();
    const entries = await readdir(current, { withFileTypes: true });
    for (const entry of entries) {
      const full = path.join(current, entry.name);
      const st = await lstat(full);
      if (st.isSymbolicLink()) {
        let resolved;
        try {
          resolved = await realpath(full);
        } catch {
          throw new Error(`Broken symlink inside review workspace is not allowed: ${path.relative(realRoot, full)}`);
        }
        if (!isInside(realRoot, resolved)) {
          throw new Error(`Symlink escapes review workspace boundary: ${path.relative(realRoot, full)}`);
        }
      } else if (st.isDirectory()) {
        stack.push(full);
      }
    }
  }
}

async function ensureScratchDirectory(realRoot) {
  const scratchDir = path.join(realRoot, "scratch");
  try {
    const st = await lstat(scratchDir);
    if (!st.isDirectory() || st.isSymbolicLink()) {
      throw new Error("review/scratch must be a regular directory, not a file or symlink");
    }
  } catch (err) {
    if (err?.code !== "ENOENT") throw err;
    await mkdir(scratchDir, { recursive: true });
  }
  return scratchDir;
}

async function forceRemoveContainer(cidFile) {
  let cid = "";
  try {
    cid = (await readFile(cidFile, "utf8")).trim();
  } catch {
    return;
  }
  if (!cid) return;
  await new Promise(resolve => {
    const killer = spawn("docker", ["rm", "-f", cid], { stdio: "ignore" });
    const timer = setTimeout(() => {
      try { killer.kill("SIGKILL"); } catch {}
      resolve();
    }, 10000);
    killer.on("close", () => { clearTimeout(timer); resolve(); });
    killer.on("error", () => { clearTimeout(timer); resolve(); });
  });
}

async function relocateOverflowFile(hostPath, scratchDir) {
  if (!hostPath) return null;
  const relName = `bash-output-${path.basename(hostPath)}`;
  const dest = path.join(scratchDir, relName);
  try {
    await copyFile(hostPath, dest);
    await unlink(hostPath).catch(() => {});
    return `scratch/${relName}`;
  } catch {
    return null;
  }
}

export function createReviewerSandboxBashTool(root, sandbox) {
  if (!sandbox?.image || typeof sandbox.image !== "string" || !sandbox.image.trim()) {
    throw new Error("Reviewer bash tool requires a non-empty Docker sandbox image");
  }
  const image = sandbox.image.trim();
  const defaultTimeout = sandbox.defaultTimeoutSeconds ?? DEFAULT_BASH_TIMEOUT_SECONDS;
  let scratchDirCache = null;

  const operations = {
    async exec(command, _cwd, { onData, signal, timeout }) {
      if (signal?.aborted) throw new Error("aborted");
      const effectiveSeconds = timeout === undefined ? defaultTimeout : timeout;
      if (!Number.isFinite(effectiveSeconds) || effectiveSeconds <= 0 || effectiveSeconds > MAX_BASH_TIMEOUT_SECONDS) {
        throw new Error(`Invalid timeout: must be between 1 and ${MAX_BASH_TIMEOUT_SECONDS} seconds`);
      }
      const realRoot = await realpath(root);
      const scratchDir = await ensureScratchDirectory(realRoot);
      scratchDirCache = scratchDir;
      await assertSafeReviewTree(realRoot);

      const cidTempDir = await mkdtemp(path.join(os.tmpdir(), "mr-rev-cid-"));
      const cidFile = path.join(cidTempDir, "container.cid");
      const uidGid = typeof process.getuid === "function" ? `${process.getuid()}:${process.getgid()}` : null;

      const args = [
        "run", "--rm", "--pull=never", "--init",
        "--cidfile", cidFile,
        "--read-only",
        "--network", "none",
        ...(uidGid ? ["--user", uidGid] : []),
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--memory", "2g",
        "--cpus", "2",
        "--pids-limit", "128",
        "--tmpfs", "/tmp:rw,nosuid,size=256m,mode=1777",
        "--env", "HOME=/workspace/scratch",
        "--env", "TMPDIR=/tmp",
        "--env", "PYTHONDONTWRITEBYTECODE=1",
        "--env", "PYTHONUTF8=1",
        "--env", "LANG=C.UTF-8",
        "--mount", `type=bind,src=${realRoot},dst=/workspace,readonly`,
        "--mount", `type=bind,src=${scratchDir},dst=/workspace/scratch`,
        "--workdir", "/workspace",
        image,
        "bash", "-c", command,
      ];

      const child = spawn("docker", args, {
        stdio: ["ignore", "pipe", "pipe"],
        detached: process.platform !== "win32",
      });
      child.stdout?.on("data", onData);
      child.stderr?.on("data", onData);

      let timedOut = false;
      let stopping = false;
      const stopContainer = async () => {
        if (stopping) return;
        stopping = true;
        try {
          if (child.pid && process.platform !== "win32") process.kill(-child.pid, "SIGKILL");
          else child.kill("SIGKILL");
        } catch {}
        await forceRemoveContainer(cidFile);
      };

      const onAbort = () => { void stopContainer(); };
      if (signal) {
        if (signal.aborted) onAbort();
        else signal.addEventListener("abort", onAbort, { once: true });
      }
      const timer = setTimeout(() => {
        timedOut = true;
        void stopContainer();
      }, effectiveSeconds * 1000);

      try {
        const exitCode = await new Promise((resolve, reject) => {
          child.once("error", reject);
          child.once("close", code => resolve(code));
        });
        if (signal?.aborted) throw new Error("aborted");
        if (timedOut) throw new Error(`timeout:${effectiveSeconds}`);
        return { exitCode: exitCode ?? 137 };
      } finally {
        clearTimeout(timer);
        if (signal) signal.removeEventListener("abort", onAbort);
        if (timedOut || signal?.aborted) await forceRemoveContainer(cidFile);
        await rm(cidTempDir, { recursive: true, force: true }).catch(() => {});
      }
    },
  };

  const base = createBashToolDefinition("/workspace", {
    operations,
    exposeSessionEnvironment: false,
  });

  return {
    ...base,
    description:
      "Execute a bash command inside an isolated offline Docker container (cwd=/workspace, network disabled). " +
      "The review workspace (/workspace, including submission/ and evaluation/) is mounted read-only; " +
      "write any helper scripts or output files ONLY under scratch/ (persisted) or /tmp. " +
      `Default timeout is ${defaultTimeout}s (max ${MAX_BASH_TIMEOUT_SECONDS}s).`,
    async execute(toolCallId, params, signal, onUpdate, ctx) {
      try {
        const result = await base.execute(toolCallId, params, signal, onUpdate, ctx);
        const origOverflow = result.details?.fullOutputPath;
        if (origOverflow && scratchDirCache) {
          const movedRel = await relocateOverflowFile(origOverflow, scratchDirCache);
          if (movedRel) {
            result.details.fullOutputPath = movedRel;
            for (const block of result.content ?? []) {
              if (block.type === "text" && typeof block.text === "string") {
                block.text = block.text.replaceAll(origOverflow, movedRel);
              }
            }
          }
        }
        return result;
      } catch (err) {
        if (err instanceof Error && scratchDirCache) {
          const match = err.message.match(/Full output: ([^\]]+\.log)\]/);
          if (match) {
            const movedRel = await relocateOverflowFile(match[1], scratchDirCache);
            if (movedRel) err.message = err.message.replaceAll(match[1], movedRel);
          }
        }
        throw err;
      }
    },
  };
}
