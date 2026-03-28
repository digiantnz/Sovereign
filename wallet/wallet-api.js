'use strict';

/**
 * sov-wallet — Safe Transaction Service proxy + wallet watcher service.
 *
 * Endpoints:
 *   GET  /health                  Liveness
 *   GET  /safe/nonce              Safe nonce proxy
 *   POST /safe/propose            Safe proposal proxy
 *   GET  /safe/pending            Pending proposals proxy
 *   GET  /portfolio               Latest portfolio snapshot
 *   GET  /watchlist               Current watched addresses
 *   POST /watchlist               Add address (internal token required)
 *   DELETE /watchlist/:address    Remove address (internal token required)
 *   POST /check                   On-demand address check (internal token required)
 *
 * Background tasks (started at process init):
 *   wallet-watcher    — polls ETH (mainnet/ARB/OP) + BTC, emits payment events
 *   portfolio tracker — scheduled balance snapshots
 *   watchlist poller  — runtime MIP subscription (30s)
 */

const express   = require('express');
const rpc       = require('./wallet-rpc');
const watchlist = require('./wallet-watchlist');
const watcher   = require('./wallet-watcher');
const harnessA2A       = require('./wallet-harness-a2a');
const harnessPortfolio = require('./wallet-harness-portfolio');

const app  = express();
app.use(express.json());

const PORT           = 3001;
const SAFE_API_BASE  = process.env.SAFE_API_BASE        || 'https://safe-transaction-mainnet.safe.global/api/v1';
const INTERNAL_TOKEN = process.env.WALLET_INTERNAL_TOKEN || '';
const WALLET_CONFIG  = process.env.WALLET_CONFIG_PATH   || '/home/sovereign/governance/wallet-config.json';

// ── Internal auth middleware ───────────────────────────────────────────────────

function _requireInternalToken(req, res, next) {
  if (!INTERNAL_TOKEN) return res.status(503).json({ error: 'WALLET_INTERNAL_TOKEN not set' });
  if (req.headers['x-wallet-token'] !== INTERNAL_TOKEN)
    return res.status(401).json({ error: 'Unauthorized' });
  next();
}

// ── Startup ───────────────────────────────────────────────────────────────────

async function _startup() {
  console.log('[sov-wallet] starting up');

  // 1. Load MIP config + endpoints
  watchlist.init(({ added, removed }) => {
    console.log(`[sov-wallet] watchlist updated: +${added.length} -${removed.length}`);
  });

  await watchlist.loadConfig();
  const endpoints = await watchlist.loadNetworkEndpoints();
  rpc.configure(endpoints);

  // 2. Seed watchlist from wallet-config.json on first run
  await watchlist.seedFromConfig(WALLET_CONFIG);

  // 3. Load latest portfolio snapshot from Qdrant
  await harnessPortfolio.loadLatestSnapshot();

  // 4. Register harnesses
  watcher.registerHarness(harnessA2A.handle);
  watcher.registerHarness(harnessPortfolio.handle);

  // 5. Start background tasks
  watchlist.startPolling();
  harnessPortfolio.startScheduledSnapshots();
  await watcher.start();   // non-blocking — starts poll loops

  // 6. Initial portfolio snapshot
  harnessPortfolio.takeSnapshot().catch(e =>
    console.warn('[sov-wallet] initial snapshot failed:', e.message)
  );

  console.log('[sov-wallet] watcher active');
}

// ── Liveness ───────────────────────────────────────────────────────────────────

app.get('/health', (_req, res) => {
  res.json({ status: 'ok', role: 'sov-wallet' });
});

// ── Portfolio + watchlist ──────────────────────────────────────────────────────

app.get('/portfolio', (_req, res) => {
  const snap = harnessPortfolio.getLatestSnapshot();
  if (!snap) return res.status(503).json({ status: 'no_snapshot', message: 'Snapshot not yet available' });
  res.json({ status: 'ok', snapshot: snap });
});

app.get('/watchlist', (_req, res) => {
  res.json({ status: 'ok', addresses: watchlist.getAll() });
});

app.post('/watchlist', _requireInternalToken, async (req, res) => {
  const { value, label, chain, chains, harness, thresholds, zero_conf } = req.body;
  if (!value || !chain) return res.status(400).json({ error: 'value and chain are required' });
  try {
    const entry = await watchlist.addAddress({ value, label, chain, chains, harness, thresholds, zero_conf });
    res.json({ status: 'ok', entry });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.delete('/watchlist/:address', _requireInternalToken, async (req, res) => {
  const removed = await watchlist.removeAddress(req.params.address);
  res.json({ status: 'ok', removed });
});

app.post('/check', _requireInternalToken, async (req, res) => {
  const { address } = req.body;
  if (!address) return res.status(400).json({ error: 'address required' });
  try {
    const result = await watcher.checkNow(address);
    res.json({ status: 'ok', ...result });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// ── Safe Transaction Service proxies ──────────────────────────────────────────

app.get('/safe/nonce', async (req, res) => {
  const safe = req.query.safe;
  if (!safe) return res.status(400).json({ error: 'safe param required' });
  try {
    const r = await fetch(`${SAFE_API_BASE}/safes/${safe}/`);
    if (!r.ok) return res.status(r.status).json({ error: `Safe API ${r.status}` });
    const data = await r.json();
    res.json({ nonce: data.nonce, threshold: data.threshold, owners: data.owners });
  } catch (e) {
    res.status(502).json({ error: e.message });
  }
});

app.post('/safe/propose', async (req, res) => {
  const body = req.body;
  if (!body.safe) return res.status(400).json({ error: 'safe field required' });
  try {
    const r = await fetch(`${SAFE_API_BASE}/safes/${body.safe}/multisig-transactions/`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const text = await r.text();
    if (!r.ok) return res.status(r.status).json({ error: `Safe API ${r.status}`, body: text });
    res.json({ status: 'ok', safe_response: text });
  } catch (e) {
    res.status(502).json({ error: e.message });
  }
});

app.get('/safe/pending', async (req, res) => {
  const safe = req.query.safe;
  if (!safe) return res.status(400).json({ error: 'safe param required' });
  try {
    const r = await fetch(`${SAFE_API_BASE}/safes/${safe}/multisig-transactions/?executed=false&limit=20`);
    if (!r.ok) return res.status(r.status).json({ error: `Safe API ${r.status}` });
    const data = await r.json();
    const proposals = (data.results || []).map(tx => ({
      nonce:                  tx.nonce,
      to:                     tx.to,
      value:                  tx.value,
      data:                   tx.data,
      confirmations:          (tx.confirmations || []).length,
      confirmations_required: tx.confirmationsRequired,
      safe_tx_hash:           tx.safeTxHash,
      submission_date:        tx.submissionDate,
      origin:                 tx.origin,
    }));
    res.json({ count: proposals.length, proposals });
  } catch (e) {
    res.status(502).json({ error: e.message });
  }
});

// ── Start ──────────────────────────────────────────────────────────────────────

app.listen(PORT, '0.0.0.0', () => {
  console.log(`[sov-wallet] Safe proxy + watcher listening on port ${PORT}`);
});

_startup().catch(e => {
  console.error('[sov-wallet] startup failed:', e);
  // Non-fatal — Safe proxy endpoints remain available even if watcher fails
});
