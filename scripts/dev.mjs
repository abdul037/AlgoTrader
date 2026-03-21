import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import readline from "node:readline";

const repoRoot = process.cwd();
const backendPython = path.join(repoRoot, "backend", ".venv", "bin", "python");

if (!existsSync(path.join(repoRoot, "frontend"))) {
  console.error("Missing frontend directory.");
  process.exit(1);
}

if (!existsSync(backendPython)) {
  console.error("Missing backend virtualenv at backend/.venv.");
  console.error("Run: cd backend && python3 -m venv .venv && source .venv/bin/activate && pip install -e .");
  process.exit(1);
}

const children = [];
let shuttingDown = false;

const colors = {
  frontend: "\x1b[34m",
  backend: "\x1b[32m",
  reset: "\x1b[0m",
};

const prefixStream = (name, stream) => {
  const rl = readline.createInterface({ input: stream });
  rl.on("line", (line) => {
    const color = colors[name] ?? "";
    console.log(`${color}[${name}]${colors.reset} ${line}`);
  });
};

const stopAll = (signal = "SIGTERM") => {
  if (shuttingDown) {
    return;
  }

  shuttingDown = true;
  for (const child of children) {
    if (!child.killed) {
      child.kill(signal);
    }
  }
};

const run = (name, command, args) => {
  const child = spawn(command, args, {
    cwd: repoRoot,
    env: { ...process.env, FORCE_COLOR: "1" },
    stdio: ["inherit", "pipe", "pipe"],
  });

  prefixStream(name, child.stdout);
  prefixStream(name, child.stderr);

  child.on("error", (error) => {
    console.error(`[${name}] failed to start: ${error.message}`);
    stopAll();
    process.exitCode = 1;
  });

  child.on("exit", (code, signal) => {
    if (shuttingDown) {
      return;
    }

    if (signal) {
      console.error(`[${name}] exited from signal ${signal}`);
      stopAll();
      process.exitCode = 1;
      return;
    }

    if (code && code !== 0) {
      console.error(`[${name}] exited with code ${code}`);
      stopAll();
      process.exitCode = code;
      return;
    }

    console.log(`[${name}] exited cleanly`);
    stopAll();
  });

  children.push(child);
};

process.on("SIGINT", () => stopAll("SIGINT"));
process.on("SIGTERM", () => stopAll("SIGTERM"));

run("frontend", "npm", [
  "--prefix",
  "frontend",
  "run",
  "dev",
  "--",
  "--hostname",
  "127.0.0.1",
  "--port",
  "3000",
]);

run("backend", backendPython, [
  "-m",
  "uvicorn",
  "app.main:app",
  "--app-dir",
  "backend",
  "--host",
  "127.0.0.1",
  "--port",
  "8000",
]);
