'use strict';

/**
 * wallet-harness-portfolio.js — Investment portfolio tracker (stub).
 *
 * Subscribes to payment events on addresses tagged harness: ["portfolio"].
 * Aggregates watched address balances across ETH mainnet, ARB, Optimism, and BTC.
 * Fetches prices via CoinGecko (endpoint from MIP under wallet.pricefeed.endpoint).
 * Writes portfolio snapshots to Qdrant episodic memory on a configurable schedule.
 * Rex can return current total value in NZD and USD conversationally.
 *
 * Extension points (stubbed, not implemented):
 *   - wallet.lending   — AAVE V3 positions
 *   - wallet.yield     — Curve/Compound yield positions
 *   - wallet.staking   — ETH/SOL staking positions
 *   - wallet.swap      — DEX swap history
 *   - wallet.lightning — Lightning/BTCPay channel balances
 */

const rpc       = require('./wallet-rpc');
const watchlist = require('./wallet-watchlist');

const QDRANT_URL    = process.env.QDRANT_URL || 'http://qdrant-archive:6333';
const EPISODIC_COL  = 'episodic';
const DEFAULT_PRICE_URL = 'https://api.coingecko.com/api/v3/simple/price';

const log  = (...a) => console.log('[portfolio]', ...a);
const warn = (...a) => console.warn('[portfolio]', ...a);

let _latestSnapshot = null;
let _snapshotTimer  = null;

// ── Snapshot point ID ──────────────────────────────────────────────────────────

function _snapshotPointId() {
  // Fixed ID — overwritten on each snapshot (one active entry in episodic)
  return 4_000_001;
}

// ── Price fetch ────────────────────────────────────────────────────────────────

async function _fetchPrices() {
  const cfg      = watchlist.getConfig();
  const baseUrl  = cfg.price_feed_url || DEFAULT_PRICE_URL;
  const url      = `${baseUrl}?ids=bitcoin,ethereum&vs_currencies=nzd,usd`;
  try {
    const res = await fetch(url, { signal: AbortSignal.timeout(10_000) });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
    // { bitcoin: { nzd: 166200, usd: 100000 }, ethereum: { nzd: 3158, usd: 1900 } }
  } catch (e) {
    warn('CoinGecko price fetch failed:', e.message);
    return null;
  }
}

// ── Balance aggregation ────────────────────────────────────────────────────────

async function _aggregateBalances() {
  const balances = { eth: {}, arb: {}, op: {}, btc: {} };

  // ETH/EVM balances
  for (const chain of ['eth', 'arb', 'op']) {
    const addrs = watchlist.getByChain(chain).filter(e =>
      (e.metadata?.harness || []).includes('portfolio')
    );
    for (const entry of addrs) {
      try {
        const bal = await rpc.ethGetBalance(chain, entry.value);
        balances[chain][entry.value] = { eth: bal, label: entry.label };
      } catch (e) {
        warn(`${chain} balance for ${entry.value.slice(0, 10)}: ${e.message}`);
        balances[chain][entry.value] = { eth: null, label: entry.label, error: e.message };
      }
    }
  }

  // BTC balances via scantxoutset (no wallet import required)
  const btcAddrs = watchlist.getByChain('btc').filter(e =>
    (e.metadata?.harness || []).includes('portfolio')
  );
  if (btcAddrs.length) {
    const addresses = btcAddrs.map(e => e.value);
    const scan = await rpc.btcScanTxOutSet(addresses);
    if (scan?.unspents) {
      const totals = {};
      for (const u of scan.unspents) {
        totals[u.address] = (totals[u.address] || 0) + u.amount;
      }
      for (const entry of btcAddrs) {
        balances.btc[entry.value] = {
          btc:   (totals[entry.value] || 0).toFixed(8),
          label: entry.label,
        };
      }
    }
  }

  return balances;
}

function _calculateTotals(balances, prices) {
  if (!prices) return { usd: null, nzd: null };
  let totalUsd = 0, totalNzd = 0;

  for (const chain of ['eth', 'arb', 'op']) {
    const ethPrice = prices.ethereum || {};
    for (const entry of Object.values(balances[chain] || {})) {
      if (entry.eth && !entry.error) {
        const eth = parseFloat(entry.eth);
        totalUsd += eth * (ethPrice.usd || 0);
        totalNzd += eth * (ethPrice.nzd || 0);
      }
    }
  }

  const btcPrice = prices.bitcoin || {};
  for (const entry of Object.values(balances.btc || {})) {
    if (entry.btc) {
      const btc = parseFloat(entry.btc);
      totalUsd += btc * (btcPrice.usd || 0);
      totalNzd += btc * (btcPrice.nzd || 0);
    }
  }

  return {
    usd: totalUsd.toFixed(2),
    nzd: totalNzd.toFixed(2),
  };
}

// ── Snapshot write ─────────────────────────────────────────────────────────────

async function _writeSnapshot(snapshot) {
  try {
    const point = {
      id:     _snapshotPointId(),
      vector: new Array(768).fill(0),
      payload: {
        type:   'episodic',
        domain: 'wallet.portfolio_snapshot',
        ...snapshot,
      },
    };
    const res = await fetch(`${QDRANT_URL}/collections/${EPISODIC_COL}/points?wait=true`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ points: [point] }),
    });
    if (!res.ok) warn('snapshot write HTTP', res.status);
    else log(`snapshot written — total USD=${snapshot.totals?.usd} NZD=${snapshot.totals?.nzd}`);
  } catch (e) {
    warn('snapshot write failed:', e.message);
  }
}

// ── Public API ────────────────────────────────────────────────────────────────

async function takeSnapshot() {
  const [balances, prices] = await Promise.all([_aggregateBalances(), _fetchPrices()]);
  const totals = _calculateTotals(balances, prices);
  const snapshot = {
    timestamp: new Date().toISOString(),
    balances,
    totals,
    prices:    prices || {},
    // ── Extension point stubs ──────────────────────────────────────────────
    lending:   null,   // AAVE V3 positions — future build (wallet.lending module)
    yield:     null,   // Curve/Compound — future build (wallet.yield module)
    staking:   null,   // ETH/SOL staking — future build (wallet.staking module)
    lightning: null,   // Lightning/BTCPay channels — pending BTCPay configuration
  };
  _latestSnapshot = snapshot;
  await _writeSnapshot(snapshot);
  return snapshot;
}

function getLatestSnapshot() { return _latestSnapshot; }

async function loadLatestSnapshot() {
  try {
    const res = await fetch(
      `${QDRANT_URL}/collections/${EPISODIC_COL}/points/${_snapshotPointId()}`,
      { headers: { 'Content-Type': 'application/json' } }
    );
    if (!res.ok) return null;
    const data = await res.json();
    _latestSnapshot = data.result?.payload || null;
    return _latestSnapshot;
  } catch { return null; }
}

function startScheduledSnapshots() {
  const cfg = watchlist.getConfig();
  const intervalMs = cfg.portfolio_snapshot_interval_ms || 3_600_000;
  _snapshotTimer = setInterval(() => {
    takeSnapshot().catch(e => warn('scheduled snapshot failed:', e.message));
  }, intervalMs);
  log(`scheduled snapshots every ${intervalMs / 60000}min`);
}

// ── Payment event handler ─────────────────────────────────────────────────────

async function handle(event) {
  const entry = watchlist.getByAddress(event.to_address || event.from_address || '');
  if (!(entry?.metadata?.harness || []).includes('portfolio')) return;

  // Trigger an immediate balance refresh on confirmed inbound payment
  log(`inbound payment on portfolio address ${entry.label} — refreshing snapshot`);
  takeSnapshot().catch(e => warn('post-payment snapshot failed:', e.message));
}

module.exports = {
  handle,
  takeSnapshot,
  getLatestSnapshot,
  loadLatestSnapshot,
  startScheduledSnapshots,
};
