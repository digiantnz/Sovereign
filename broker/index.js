const express = require('express');
const yaml = require('js-yaml');
const fs = require('fs');
const path = require('path');
const { exec, spawn } = require('child_process');
const { promisify } = require('util');
const Docker = require('dockerode');

const execAsync = promisify(exec);
const app = express();
app.use(express.json());

const docker = new Docker({ socketPath: '/var/run/docker.sock' });
const policy = yaml.load(fs.readFileSync(process.env.BROKER_POLICY, 'utf8'));

// Command execution policy (separate from container trust policy)
const COMMANDS_POLICY_PATH = process.env.COMMANDS_POLICY || '/policy/commands-policy.yaml';
const SCRIPTS_DIR = process.env.SCRIPTS_DIR || '/scripts';
let cmdPolicy = {};
try {
  cmdPolicy = yaml.load(fs.readFileSync(COMMANDS_POLICY_PATH, 'utf8'));
  console.log(`Commands policy loaded: ${Object.keys(cmdPolicy.commands || {}).length} commands`);
} catch (e) {
  console.warn(`Commands policy not found at ${COMMANDS_POLICY_PATH}: ${e.message}`);
}

// Shell metacharacter pattern — any of these in a param value → HTTP 400
const SHELL_META = /[;&|`$<>(){}\[\]\\'"\n\r\x00-\x08\x0b\x0c\x0e-\x1f]/;

// Tier order for access control
const TIER_ORDER = { low: 0, medium: 1, high: 2 };

// Run a binary with args — shell: false, fixed timeout, sanitised env
function spawnRun(binary, args, timeoutMs = 15000) {
  return new Promise((resolve) => {
    let stdout = '', stderr = '';
    const proc = spawn(binary, args, {
      shell: false,
      timeout: timeoutMs,
      // Merge parent env so skill scripts can access credentials (IMAP, SMTP, WebDAV)
      // and NODE_PATH for community skill npm modules. PATH is explicitly enforced.
      env: { ...process.env, PATH: '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin' },
    });
    proc.stdout.on('data', d => { stdout += d.toString(); });
    proc.stderr.on('data', d => { stderr += d.toString(); });
    proc.on('close', code => resolve({ stdout, stderr, return_code: code ?? -1 }));
    proc.on('error', err => resolve({ stdout: '', stderr: err.message, return_code: -1 }));
  });
}

// Host filesystem is bind-mounted read-only at /hostfs
const HOSTFS = '/hostfs';
const COMPOSE_PATH = path.join(HOSTFS, '/docker/sovereign/compose.yml');

function wildcardMatch(pattern, str) {
  const regex = new RegExp('^' + pattern.replace(/\*/g, '.*') + '$');
  return regex.test(str);
}

// Determine container name from path — handles /containers/{id} and /system/logs|inspect/{id}
function containerFromPath(p) {
  let m = p.match(/\/containers\/([^/]+)/);
  if (m && m[1] !== 'json') return m[1];
  m = p.match(/\/system\/(?:logs|inspect)\/([^/]+)/);
  return m ? m[1] : null;
}

// Validate that a requested path stays within HOSTFS — prevents traversal
function validateHostPath(reqPath) {
  if (!reqPath || typeof reqPath !== 'string') return 'path parameter required';
  if (!path.isAbsolute(reqPath)) return 'path must be absolute (start with /)';
  const resolved = path.resolve(HOSTFS + reqPath);
  if (!resolved.startsWith(HOSTFS)) return 'path traversal blocked';
  return null;
}

// Run an exec inside a named container; returns stdout as string
async function execInContainer(containerName, cmd) {
  const c = docker.getContainer(containerName);
  const ex = await c.exec({ Cmd: cmd, AttachStdout: true, AttachStderr: true });
  const stream = await ex.start({ Detach: false });
  return new Promise((resolve, reject) => {
    let buf = '';
    stream.on('data', chunk => { buf += chunk.toString(); });
    stream.on('end', () => resolve(buf.replace(/[\x00-\x08\x0e-\x1f]/g, '')));
    stream.on('error', reject);
  });
}

app.get('/health', (req, res) => res.send('OK'));

// ── POST /exec/:commandName ───────────────────────────────────────────────────
// Allowlisted CLI command execution. Completely separate from Docker operations.
// Trust level is enforced per-command from commands-policy.yaml.
// Shell metacharacters in any param → 400. Command not in allowlist → 403.
// Disabled commands → 503 (infra change required, not a policy denial).
app.post('/exec/:commandName', async (req, res) => {
  const { commandName } = req.params;
  const cmds = cmdPolicy.commands || {};
  const cmd = cmds[commandName];

  if (!cmd) {
    return res.status(403).json({
      status: 'denied',
      error: `Command not in allowlist: ${commandName}`,
      allowlist: Object.keys(cmds),
    });
  }

  if (!cmd.enabled) {
    return res.status(503).json({
      status: 'disabled',
      command: commandName,
      reason: cmd.description,
    });
  }

  // Trust level check
  const requestedTrust = req.headers['x-trust-level'] || 'low';
  const cmdTier = cmd.tier || 'low';
  if ((TIER_ORDER[requestedTrust] ?? -1) < (TIER_ORDER[cmdTier] ?? 0)) {
    return res.status(403).json({
      status: 'denied',
      error: `Command ${commandName} requires trust level: ${cmdTier}`,
    });
  }

  const params = req.body?.params || {};
  const paramErrors = [];
  const validated = {};

  // Validate and coerce params against schema
  for (const [pname, pspec] of Object.entries(cmd.params || {})) {
    const raw = params[pname];

    if (raw === undefined || raw === null) {
      if (pspec.required) { paramErrors.push(`missing required param: ${pname}`); continue; }
      if (pspec.default !== undefined) validated[pname] = pspec.default;
      continue;
    }

    const strVal = String(raw);

    // Shell metacharacter guard — reject before any further processing
    if (SHELL_META.test(strVal)) {
      return res.status(400).json({
        status: 'rejected',
        error: `Shell metacharacter detected in param ${pname}`,
        param: pname,
      });
    }

    if (pspec.type === 'integer') {
      const n = parseInt(strVal, 10);
      if (isNaN(n)) { paramErrors.push(`${pname}: expected integer, got ${strVal}`); continue; }
      if (pspec.min !== undefined && n < pspec.min) { paramErrors.push(`${pname}: ${n} below minimum ${pspec.min}`); continue; }
      if (pspec.max !== undefined && n > pspec.max) { paramErrors.push(`${pname}: ${n} above maximum ${pspec.max}`); continue; }
      validated[pname] = n;
    } else {
      // String — if allowlist key is present (even empty array), value must be in it.
      // Empty allowlist = nothing approved yet → deny all.
      if (pspec.allowlist !== undefined && !pspec.allowlist.includes(strVal)) {
        return res.status(403).json({
          status: 'denied',
          error: `Param ${pname}=${JSON.stringify(strVal)} not in allowlist`,
          allowlist: pspec.allowlist,
        });
      }
      validated[pname] = strVal;
    }
  }

  if (paramErrors.length > 0) {
    return res.status(400).json({ status: 'invalid', errors: paramErrors });
  }

  // ── Execute ───────────────────────────────────────────────────────────────
  try {
    let result;

    if (cmd.binary === '__container_exec__') {
      // Runs inside a named container via Docker exec API
      const raw = await execInContainer(cmd.container, [...(cmd.fixed_args || [])]);
      result = { stdout: raw.replace(/[\x00-\x08\x0e-\x1f]/g, ''), stderr: '', return_code: 0 };

    } else if (cmd.binary === '__script__') {
      // Approved script from scripts/ directory — name already validated against allowlist
      const scriptName = validated.name;
      const scriptPath = path.resolve(SCRIPTS_DIR, scriptName);
      if (!scriptPath.startsWith(path.resolve(SCRIPTS_DIR) + path.sep)) {
        return res.status(403).json({ status: 'denied', error: 'Script path traversal blocked' });
      }
      if (!fs.existsSync(scriptPath)) {
        return res.status(404).json({ status: 'not_found', error: `Script not found: ${scriptName}` });
      }
      result = await spawnRun(scriptPath, [], 30000);

    } else {
      // Direct binary with fixed_args + param-derived args
      // If param spec has a `flag` field, emit [flag, value]; else emit bare value.
      const args = [...(cmd.fixed_args || [])];
      for (const [pname, pspec] of Object.entries(cmd.params || {})) {
        if (validated[pname] === undefined) continue;
        if (pspec.flag) {
          args.push(pspec.flag, String(validated[pname]));
        } else {
          args.push(String(validated[pname]));
        }
      }
      result = await spawnRun(cmd.binary, args);
    }

    return res.json({
      status: 'ok',
      command: commandName,
      return_code: result.return_code,
      stdout: result.stdout,
      stderr: result.stderr,
    });

  } catch (err) {
    console.error(`/exec/${commandName} error:`, err.message);
    return res.status(500).json({ status: 'error', command: commandName, error: err.message });
  }
});

app.all('/:methodPath(*)', async (req, res) => {
  const trust = req.headers['x-trust-level'] || policy.trust.default;
  const fullPath = `/${req.params.methodPath}`;
  const methodPath = `${req.method}:${fullPath}`;

  // Container allow/deny check
  const containerId = containerFromPath(fullPath);
  if (containerId) {
    const denied = !policy.manageable.allow_names.includes(containerId)
                || policy.manageable.deny_names.includes(containerId);
    if (denied) return res.status(403).send('Container denied by policy');
  }

  // Trust level check
  const allowed = (policy.trust.levels[trust]?.allow || [])
    .some(p => wildcardMatch(p, methodPath));
  if (!allowed) return res.status(403).send('Denied by trust policy');

  try {
    // ── Existing endpoints ────────────────────────────────────────────────

    // GET /containers/json — list running containers (existing)
    if (req.method === 'GET' && fullPath === '/containers/json') {
      const containers = await docker.listContainers({ all: true });
      return res.json(containers);
    }

    // GET /containers/{id}/logs — existing
    if (req.method === 'GET' && fullPath.match(/^\/containers\/[^/]+\/logs$/)) {
      const container = docker.getContainer(containerId);
      const stream = await container.logs({
        follow: false,
        stdout: req.query.stdout !== '0',
        stderr: req.query.stderr !== '0',
        timestamps: req.query.timestamps === '1',
        tail: req.query.tail || 'all',
      });
      res.set('Content-Type', 'text/plain');
      return res.send(stream);
    }

    // GET /containers/{id}/stats — existing
    if (req.method === 'GET' && fullPath.match(/^\/containers\/[^/]+\/stats$/)) {
      const container = docker.getContainer(containerId);
      const stats = await container.stats({ stream: false });
      return res.json(stats);
    }

    // POST /containers/{id}/restart — existing
    if (req.method === 'POST' && fullPath.match(/^\/containers\/[^/]+\/restart$/)) {
      const container = docker.getContainer(containerId);
      await container.restart();
      return res.json({ status: 'restarted', container: containerId });
    }

    // GET /info — existing
    if (req.method === 'GET' && fullPath === '/info') {
      const info = await docker.info();
      return res.json(info);
    }

    // GET /system/gpu — existing (nvidia-smi via ollama exec)
    if (req.method === 'GET' && fullPath === '/system/gpu') {
      const raw = await execInContainer('ollama', [
        'nvidia-smi',
        '--query-gpu=name,memory.used,memory.total,utilization.gpu,utilization.memory,temperature.gpu',
        '--format=csv,noheader,nounits',
      ]);
      const line = raw.trim().split('\n').find(l => l.trim());
      if (!line) return res.json({ error: 'no nvidia-smi output' });
      const [name, mem_used, mem_total, gpu_util, mem_util, temp] = line.split(',').map(s => s.trim());
      return res.json({
        gpu_name:        name,
        vram_used_mb:    parseInt(mem_used, 10)  || 0,
        vram_total_mb:   parseInt(mem_total, 10) || 0,
        gpu_utilization: parseInt(gpu_util, 10)  || 0,
        mem_utilization: parseInt(mem_util, 10)  || 0,
        temperature_c:   parseInt(temp, 10)      || 0,
      });
    }

    // ── New read-only examination endpoints ───────────────────────────────

    // GET /system/containers — full docker ps -a with all fields
    if (req.method === 'GET' && fullPath === '/system/containers') {
      const containers = await docker.listContainers({ all: true });
      const detail = containers.map(c => ({
        id:       c.Id.substring(0, 12),
        names:    c.Names,
        image:    c.Image,
        state:    c.State,
        status:   c.Status,
        created:  c.Created,
        ports:    c.Ports,
        networks: Object.keys(c.NetworkSettings?.Networks || {}),
        mounts:   (c.Mounts || []).map(m => ({ src: m.Source, dst: m.Destination, mode: m.Mode })),
      }));
      return res.json({ status: 'ok', count: detail.length, containers: detail });
    }

    // GET /system/logs/:container — last N lines (cleaner path than /containers/{id}/logs)
    if (req.method === 'GET' && fullPath.match(/^\/system\/logs\/[^/]+$/)) {
      const cname = fullPath.replace('/system/logs/', '');
      const container = docker.getContainer(cname);
      const tail = parseInt(req.query.tail || '100', 10);
      const stream = await container.logs({ follow: false, stdout: true, stderr: true, tail });
      res.set('Content-Type', 'text/plain');
      return res.send(stream);
    }

    // GET /system/inspect/:container — docker inspect
    if (req.method === 'GET' && fullPath.match(/^\/system\/inspect\/[^/]+$/)) {
      const cname = fullPath.replace('/system/inspect/', '');
      const container = docker.getContainer(cname);
      const info = await container.inspect();
      // Strip sensitive env vars before returning
      if (info.Config?.Env) {
        info.Config.Env = info.Config.Env.map(e => {
          const [k, ...rest] = e.split('=');
          const ku = k.toUpperCase();
          if (/PASSWORD|SECRET|TOKEN|KEY|PAT|PASS/.test(ku)) return `${k}=<REDACTED>`;
          return e;
        });
      }
      return res.json({ status: 'ok', container: cname, inspect: info });
    }

    // GET /system/compose — current compose.yml content
    if (req.method === 'GET' && fullPath === '/system/compose') {
      if (!fs.existsSync(COMPOSE_PATH)) {
        return res.status(404).json({ error: `compose.yml not found at ${COMPOSE_PATH}` });
      }
      const content = fs.readFileSync(COMPOSE_PATH, 'utf8');
      return res.json({ status: 'ok', path: '/docker/sovereign/compose.yml', content });
    }

    // GET /fs/read?path=... — read any file or list any directory on the host filesystem
    if (req.method === 'GET' && fullPath === '/fs/read') {
      const reqPath = req.query.path || '';
      const pathErr = validateHostPath(reqPath);
      if (pathErr) return res.status(400).json({ error: pathErr });

      const hostPath = path.resolve(HOSTFS + reqPath);
      if (!fs.existsSync(hostPath)) {
        return res.status(404).json({ error: `not found: ${reqPath}` });
      }

      const stat = fs.statSync(hostPath);

      if (stat.isDirectory()) {
        const entries = fs.readdirSync(hostPath, { withFileTypes: true }).map(e => ({
          name: e.name,
          type: e.isDirectory() ? 'dir' : e.isSymbolicLink() ? 'link' : 'file',
        }));
        return res.json({ status: 'ok', path: reqPath, type: 'directory', entries });
      }

      const MAX_BYTES = 5 * 1024 * 1024; // 5 MB
      if (stat.size > MAX_BYTES) {
        return res.status(413).json({
          error: `file too large (${stat.size} bytes) — max 5 MB`,
          size: stat.size,
          path: reqPath,
        });
      }

      // Serve as text; if the file is binary, return a note rather than raw bytes
      try {
        const content = fs.readFileSync(hostPath, 'utf8');
        return res.json({ status: 'ok', path: reqPath, type: 'file', size: stat.size, content });
      } catch (e) {
        return res.json({ status: 'ok', path: reqPath, type: 'file', size: stat.size, binary: true,
          note: 'file appears to be binary — text decode failed' });
      }
    }

    // GET /system/hardware — nvidia-smi + df + memory + cpu
    if (req.method === 'GET' && fullPath === '/system/hardware') {
      // GPU via ollama exec
      let gpu = {};
      try {
        const raw = await execInContainer('ollama', [
          'nvidia-smi',
          '--query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu',
          '--format=csv,noheader,nounits',
        ]);
        const line = raw.trim().split('\n').find(l => l.trim());
        if (line) {
          const [name, memUsed, memTotal, gpuUtil, temp] = line.split(',').map(s => s.trim());
          gpu = { gpu_name: name, vram_used_mb: parseInt(memUsed)||0, vram_total_mb: parseInt(memTotal)||0,
                  gpu_util_pct: parseInt(gpuUtil)||0, temp_c: parseInt(temp)||0 };
        }
      } catch (e) { gpu = { error: e.message }; }

      // Disk — df -h (broker container has host bind mounts visible)
      let disk = '';
      try { const r = await execAsync('df -h', { timeout: 5000 }); disk = r.stdout; }
      catch (e) { disk = e.stdout || e.message; }

      // Memory — /proc/meminfo (shared with host kernel)
      let memory = {};
      try {
        const raw = fs.readFileSync('/proc/meminfo', 'utf8');
        const kb = key => { const m = raw.match(new RegExp(`${key}:\\s+(\\d+)`)); return m ? parseInt(m[1]) : 0; };
        memory = {
          total_mb:     Math.round(kb('MemTotal') / 1024),
          free_mb:      Math.round(kb('MemFree') / 1024),
          available_mb: Math.round(kb('MemAvailable') / 1024),
          cached_mb:    Math.round(kb('Cached') / 1024),
          swap_total_mb: Math.round(kb('SwapTotal') / 1024),
          swap_free_mb:  Math.round(kb('SwapFree') / 1024),
        };
      } catch (e) { memory = { error: e.message }; }

      // CPU — /proc/cpuinfo (shared with host kernel)
      let cpu = {};
      try {
        const raw = fs.readFileSync('/proc/cpuinfo', 'utf8');
        const blocks = raw.split('\n\n').filter(b => b.trim());
        const model = (raw.match(/model name\s*:\s*(.+)/) || [])[1]?.trim() || 'unknown';
        const mhz   = (raw.match(/cpu MHz\s*:\s*(.+)/)   || [])[1]?.trim() || 'unknown';
        cpu = { model, cores: blocks.length, mhz };
      } catch (e) { cpu = { error: e.message }; }

      return res.json({ status: 'ok', gpu, disk, memory, cpu });
    }

    // GET /system/processes — ps aux (exec in ollama for broader process visibility)
    if (req.method === 'GET' && fullPath === '/system/processes') {
      let processes = '';
      try {
        processes = await execInContainer('ollama', ['ps', 'aux']);
        processes = processes.trim();
      } catch (e) {
        // Fallback: run in broker container
        try {
          const r = await execAsync('ps aux', { timeout: 5000 });
          processes = r.stdout.trim();
        } catch (e2) { processes = `error: ${e2.message}`; }
      }
      return res.json({ status: 'ok', processes });
    }

    return res.status(404).send('Endpoint not implemented');

  } catch (err) {
    console.error('Broker error:', err.message);
    res.status(500).send(err.message);
  }
});

app.listen(8088, () => console.log('Broker listening on 8088'));
