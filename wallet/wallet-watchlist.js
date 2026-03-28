'use strict';

/**
 * wallet-watchlist.js — MIP-backed watch address management.
 *
 * Watch addresses live in Qdrant semantic memory under domain wallet.watchlist.
 * Non-secret config (endpoints, poll intervals, pricefeed URL) lives under
 * network.endpoints and wallet.watcher.config — never in /secrets/.
 *
 * On startup: loads watchlist + config from Qdrant, seeds from wallet-config.json
 * on first run.  Polls every 30s for runtime additions — no restart required.
 */

const QDRANT_URL = process.env.QDRANT_URL || 'http://qdrant-archive:6333';
const COLLECTION = 'semantic';
const POLL_MS    = 30_000;

const log  = (...a) => console.log('[watchlist]', ...a);
const warn = (...a) => console.warn('[watchlist]', ...a);

let _watchlist = new Map();   // address.toLowerCase() → entry
let _config    = {};          // wallet.watcher.config payload
let _onUpdate  = null;

function init(onUpdate) { _onUpdate = onUpdate; }

// ── Qdrant helpers ─────────────────────────────────────────────────────────────

async function _scroll(domain, extraFilter = []) {
  const must = [
    { key: 'domain', match: { value: domain } },
    ...extraFilter,
  ];
  const res = await fetch(`${QDRANT_URL}/collections/${COLLECTION}/points/scroll`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ filter: { must }, limit: 256, with_payload: true, with_vector: false }),
  });
  if (!res.ok) throw new Error(`Qdrant scroll HTTP ${res.status}`);
  const data = await res.json();
  return data.result?.points || [];
}

async function _upsert(point) {
  const res = await fetch(`${QDRANT_URL}/collections/${COLLECTION}/points?wait=true`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ points: [point] }),
  });
  if (!res.ok) throw new Error(`Qdrant upsert HTTP ${res.status}`);
}

async function _deletePoint(id) {
  await fetch(`${QDRANT_URL}/collections/${COLLECTION}/points/delete?wait=true`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ points: [id] }),
  });
}

function _addrId(address) {
  // Deterministic integer ID from address (offset 2_000_000 to avoid domain collisions)
  let h = 0;
  const s = ('watchlist:' + address.toLowerCase());
  for (let i = 0; i < s.length; i++) h = (Math.imul(31, h) + s.charCodeAt(i)) | 0;
  return Math.abs(h) + 2_000_000;
}

// ── Watchlist load/subscribe ───────────────────────────────────────────────────

async function loadWatchlist() {
  const points = await _scroll('wallet.watchlist');
  const updated = new Map();
  for (const p of points) {
    const addr = (p.payload.value || '').toLowerCase();
    if (addr) updated.set(addr, { ...p.payload, _point_id: p.id });
  }

  const prev = _watchlist;
  _watchlist = updated;

  if (_onUpdate) {
    const added   = [...updated.keys()].filter(a => !prev.has(a));
    const removed = [...prev.keys()].filter(a => !updated.has(a));
    if (added.length || removed.length) {
      log(`runtime update — +${added.length} added, -${removed.length} removed`);
      _onUpdate({ added, removed });
    }
  }
  return updated;
}

async function loadConfig() {
  try {
    const points = await _scroll('wallet.watcher.config');
    if (points.length) _config = points[0].payload;
    else _config = _defaultConfig();
  } catch {
    _config = _defaultConfig();
  }
  return _config;
}

function _defaultConfig() {
  return {
    poll_interval_eth_ms:       15_000,
    poll_interval_btc_ms:       60_000,
    portfolio_snapshot_interval_ms: 3_600_000,
    eth_confirmations_required: 1,
    btc_confirmations_default:  1,
    price_feed_url: 'https://api.coingecko.com/api/v3/simple/price',
  };
}

async function loadNetworkEndpoints() {
  try {
    const points = await _scroll('network.endpoints');
    const eps = {};
    for (const p of points) {
      const label = p.payload.label || '';
      const value = p.payload.value || '';
      const meta  = p.payload.metadata || {};
      if (label === 'eth-node-primary')   eps.eth = `${meta.protocol || 'http'}://${value}`;
      if (label === 'eth-node-secondary') eps.eth_secondary = `${meta.protocol || 'http'}://${value}`;
      if (label === 'btc-node-rpc')       eps.btc_url  = `${meta.protocol || 'http'}://${value}`;
      if (label === 'arb-node-rpc')       eps.arb = value;
      if (label === 'op-node-rpc')        eps.op  = value;
    }
    return eps;
  } catch (e) {
    warn('could not load network.endpoints from MIP:', e.message);
    return {};
  }
}

// ── Seed from wallet-config.json ──────────────────────────────────────────────

async function seedFromConfig(walletConfigPath) {
  await loadWatchlist();
  if (_watchlist.size > 0) {
    log(`watchlist has ${_watchlist.size} entries — skipping seed`);
    return;
  }

  let cfg;
  try {
    const fs = require('fs');
    cfg = JSON.parse(fs.readFileSync(walletConfigPath, 'utf8'));
  } catch (e) {
    warn('could not read wallet-config.json for seed:', e.message);
    return;
  }

  const now  = new Date().toISOString();
  const seeds = [];

  const safeAddr = cfg?.safe?.address;
  if (safeAddr) seeds.push({
    value:  safeAddr,
    label:  'Safe Multisig',
    chain:  'eth',
    chains: ['eth', 'arb', 'op'],
    harness: ['portfolio', 'a2a'],
    thresholds: {},
    zero_conf:  false,
  });

  const rexAddr = (cfg?.safe?.owners || []).find(o => o.type === 'sovereign-core')?.address;
  if (rexAddr) seeds.push({
    value:  rexAddr,
    label:  'Rex EOA',
    chain:  'eth',
    chains: ['eth'],
    harness: ['portfolio', 'a2a'],
    thresholds: {},
    zero_conf:  false,
  });

  const btcMultisig = cfg?.bitcoin?.multisig_address;
  if (btcMultisig) seeds.push({
    value:  btcMultisig,
    label:  'BTC Multisig',
    chain:  'btc',
    chains: ['btc'],
    harness: ['portfolio', 'a2a'],
    thresholds: {},
    zero_conf:  false,
  });

  const dirBtcAddr = cfg?.bitcoin?.director_btc_address;
  if (dirBtcAddr) seeds.push({
    value:  dirBtcAddr,
    label:  'Director BTC',
    chain:  'btc',
    chains: ['btc'],
    harness: ['portfolio'],
    thresholds: {},
    zero_conf:  false,
  });

  for (const s of seeds) {
    await addAddress({ ...s, source: 'seed', watch_since: now });
  }
  log(`seeded ${seeds.length} addresses from wallet-config.json`);
}

// ── CRUD ──────────────────────────────────────────────────────────────────────

async function addAddress(entry) {
  const addr = (entry.value || '').toLowerCase();
  if (!addr) throw new Error('address value is required');
  const now  = new Date().toISOString();
  const point = {
    id: _addrId(addr),
    vector: new Array(768).fill(0),   // zero-vector — filter queries only, no semantic search
    payload: {
      type:   'semantic',
      domain: 'wallet.watchlist',
      value:  entry.value,
      label:  entry.label || (addr.slice(0, 8) + '…'),
      metadata: {
        chain:       entry.chain,
        chains:      entry.chains || [entry.chain],
        harness:     entry.harness || ['portfolio'],
        watch_since: entry.watch_since || now,
        thresholds:  entry.thresholds  || {},
        zero_conf:   entry.zero_conf   || false,
      },
      source:      entry.source      || 'director',
      trust_level: entry.trust_level || 'high',
      added_at:    now,
    },
  };
  await _upsert(point);
  _watchlist.set(addr, { ...point.payload, _point_id: point.id });
  log(`added ${entry.label || addr} (${entry.chain})`);
  return point.payload;
}

async function removeAddress(address) {
  const addr = address.toLowerCase();
  const entry = _watchlist.get(addr);
  if (!entry) return false;
  await _deletePoint(entry._point_id || _addrId(addr));
  _watchlist.delete(addr);
  log(`removed ${addr.slice(0, 10)}…`);
  return true;
}

// ── Accessors ─────────────────────────────────────────────────────────────────

function getAll()              { return [..._watchlist.values()]; }
function getConfig()           { return _config; }
function getByAddress(address) { return _watchlist.get(address.toLowerCase()) || null; }

function getByChain(chain) {
  return [..._watchlist.values()].filter(e => {
    const chains = e.metadata?.chains || [e.metadata?.chain];
    return chains.includes(chain);
  });
}

function getByHarness(harness) {
  return [..._watchlist.values()].filter(e =>
    (e.metadata?.harness || []).includes(harness)
  );
}

function startPolling() {
  setInterval(() => {
    loadWatchlist().catch(e => warn('poll error:', e.message));
  }, POLL_MS);
  log('runtime subscription polling started (30s interval)');
}

module.exports = {
  init,
  loadWatchlist,
  loadConfig,
  loadNetworkEndpoints,
  seedFromConfig,
  addAddress,
  removeAddress,
  getAll,
  getConfig,
  getByAddress,
  getByChain,
  getByHarness,
  startPolling,
};
