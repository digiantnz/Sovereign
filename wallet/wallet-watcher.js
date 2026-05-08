'use strict';

/**
 * wallet-watcher.js — Chain-aware transaction watcher.
 *
 * Polls ETH (mainnet + ARB + OP) and BTC for new transactions on watched
 * addresses.  Emits structured, idempotent payment events to registered
 * harnesses. Business-logic agnostic — detects and emits only.
 *
 * Payment event schema:
 *   { chain, tx_hash, from_address, to_address, amount, currency,
 *     confirmations, timestamp, block_number, label }
 *
 * Idempotency: seenTx Set (in-memory) pre-loaded from Qdrant episodic on
 * startup.  New entries are written to Qdrant episodic asynchronously.
 */

const rpc       = require('./wallet-rpc');
const watchlist = require('./wallet-watchlist');
const lightning = require('./wallet-harness-lightning');

const QDRANT_URL   = process.env.QDRANT_URL || 'http://qdrant-archive:6333';
const EPISODIC_COL = 'episodic';

const log  = (...a) => console.log('[watcher]', ...a);
const warn = (...a) => console.warn('[watcher]', ...a);

// seenTx: Set<"chain:txHash"> — idempotency across events
const seenTx = new Set();

// Per-chain watcher state — updated by poll loops, exported for /health
const _chainState = {};
function _initChainState(chain) {
  if (!_chainState[chain]) _chainState[chain] = { failures: 0, lastOk: null };
}
function getChainState() {
  return Object.fromEntries(
    Object.entries(_chainState).map(([k, v]) => [k, { ...v }])
  );
}

// pendingConfs: Map<"chain:txHash", {event, seenAtBlock}> — waiting for confirmations
const pendingConfs = new Map();

// Registered harnesses: Array<(event) => Promise<void>>
const _harnesses = [];

function registerHarness(fn) { _harnesses.push(fn); }

// ── Qdrant seen_tx persistence ────────────────────────────────────────────────

function _txPointId(chain, txHash) {
  const key = `seen:${chain}:${txHash}`.toLowerCase();
  let h = 0;
  for (let i = 0; i < key.length; i++) h = (Math.imul(31, h) + key.charCodeAt(i)) | 0;
  return Math.abs(h) + 3_000_000;
}

async function loadSeenTx() {
  try {
    const cutoff = new Date(Date.now() - 7 * 24 * 60 * 60 * 1000).toISOString();
    const res = await fetch(`${QDRANT_URL}/collections/${EPISODIC_COL}/points/scroll`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        filter: { must: [{ key: 'domain', match: { value: 'wallet.seen_tx' } }] },
        limit: 10_000,
        with_payload: true,
        with_vector: false,
      }),
    });
    if (!res.ok) return;
    const data = await res.json();
    let loaded = 0;
    for (const p of (data.result?.points || [])) {
      const { chain, tx_hash, seen_at } = p.payload;
      if (seen_at && seen_at > cutoff && chain && tx_hash) {
        seenTx.add(`${chain}:${tx_hash.toLowerCase()}`);
        loaded++;
      }
    }
    log(`loaded ${loaded} seen transactions from Qdrant (last 7 days)`);
  } catch (e) {
    warn('could not load seen_tx from Qdrant:', e.message);
  }
}

async function _persistSeenTx(chain, txHash, event) {
  try {
    const point = {
      id: _txPointId(chain, txHash),
      vector: new Array(768).fill(0),
      payload: {
        type:       'episodic',
        domain:     'wallet.seen_tx',
        chain,
        tx_hash:    txHash,
        seen_at:    new Date().toISOString(),
        amount:     event.amount,
        currency:   event.currency,
        label:      event.label || '',
        to_address: event.to_address,
      },
    };
    await fetch(`${QDRANT_URL}/collections/${EPISODIC_COL}/points?wait=false`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ points: [point] }),
    });
  } catch (e) {
    warn('seen_tx persist failed:', e.message);
  }
}

// ── Event emission ─────────────────────────────────────────────────────────────

async function _emit(event) {
  const key = `${event.chain}:${event.tx_hash.toLowerCase()}`;
  if (seenTx.has(key)) return;   // idempotency gate

  seenTx.add(key);
  _persistSeenTx(event.chain, event.tx_hash, event);  // async, non-blocking

  log(`PAYMENT EVENT chain=${event.chain} tx=${event.tx_hash.slice(0, 16)}… amount=${event.amount} ${event.currency} label="${event.label}"`);

  // Fan out to all harnesses
  await Promise.allSettled(_harnesses.map(h => h(event).catch(e => warn('harness error:', e.message))));
}

function _buildEthEvent(chain, tx, matchedAddress, label, blockNumber) {
  return {
    chain,
    tx_hash:      tx.hash,
    from_address: tx.from || '',
    to_address:   tx.to   || '',
    amount:       rpc.weiToEth(tx.value || '0x0'),
    currency:     'ETH',
    confirmations: 1,
    timestamp:    new Date().toISOString(),
    block_number: blockNumber,
    label:        label || matchedAddress.slice(0, 10) + '…',
  };
}

// ── ETH chain watcher ─────────────────────────────────────────────────────────

async function _pollEthChain(chain, state) {
  const cfg      = watchlist.getConfig();
  const reqConfs = cfg.eth_confirmations_required || 1;
  const watched  = new Set(watchlist.getByChain(chain).map(e => e.value.toLowerCase()));
  const labels   = new Map(watchlist.getByChain(chain).map(e => [e.value.toLowerCase(), e.label]));

  if (!watched.size) return;

  let current;
  try {
    current = await rpc.ethBlockNumber(chain);
  } catch (e) {
    warn(`${chain} blockNumber failed:`, e.message);
    return false;
  }

  if (state.lastBlock === null) {
    state.lastBlock = current;
    log(`${chain} connected at block ${current}, watching ${watched.size} addresses`);
    return true;
  }

  // Scan new blocks (cap at 20 per cycle to avoid burst)
  const from = state.lastBlock + 1;
  const to   = Math.min(current, state.lastBlock + 20);

  for (let bn = from; bn <= to; bn++) {
    let block;
    try {
      block = await rpc.ethGetBlock(chain, bn, true);
    } catch (e) {
      warn(`${chain} getBlock(${bn}) failed:`, e.message);
      continue;
    }
    if (!block?.transactions) continue;

    for (const tx of block.transactions) {
      const from_ = (tx.from || '').toLowerCase();
      const to_   = (tx.to   || '').toLowerCase();
      const hit   = watched.has(to_) ? to_ : (watched.has(from_) ? from_ : null);
      if (!hit) continue;

      const pendingKey = `${chain}:${tx.hash.toLowerCase()}`;
      if (!pendingConfs.has(pendingKey) && !seenTx.has(pendingKey)) {
        pendingConfs.set(pendingKey, {
          event:       _buildEthEvent(chain, tx, hit, labels.get(hit), bn),
          seenAtBlock: bn,
        });
      }
    }
  }
  state.lastBlock = to;

  // Check pending confirmations
  for (const [key, { event, seenAtBlock }] of pendingConfs) {
    const confs = current - seenAtBlock + 1;
    if (confs >= reqConfs) {
      event.confirmations = confs;
      pendingConfs.delete(key);
      await _emit(event);
    }
  }
  return true;
}

// ── BTC watcher ───────────────────────────────────────────────────────────────

async function _pollBtc(state) {
  const cfg     = watchlist.getConfig();
  const watched = new Set(watchlist.getByChain('btc').map(e => e.value.toLowerCase()));
  const labels  = new Map(watchlist.getByChain('btc').map(e => [e.value.toLowerCase(), e.label]));
  if (!watched.size) return;

  let currentHeight;
  try {
    currentHeight = await rpc.btcGetBlockCount();
  } catch (e) {
    warn('BTC getblockcount failed:', e.message);
    return false;
  }

  if (state.lastBlock === null) {
    state.lastBlock = currentHeight;
    log(`BTC connected at block ${currentHeight}, watching ${watched.size} addresses`);
    return;
  }

  const from = state.lastBlock + 1;
  const to   = Math.min(currentHeight, state.lastBlock + 6);  // cap at 6 blocks/cycle

  for (let bn = from; bn <= to; bn++) {
    let hash, block;
    try {
      hash  = await rpc.btcGetBlockHash(bn);
      block = await rpc.btcGetBlock(hash);
    } catch (e) {
      warn(`BTC getblock(${bn}) failed:`, e.message);
      continue;
    }
    if (!block?.tx) continue;

    const blockTime = block.time
      ? new Date(block.time * 1000).toISOString()
      : new Date().toISOString();

    for (const tx of block.tx) {
      for (const vout of (tx.vout || [])) {
        const addr = (vout.scriptPubKey?.address || '').toLowerCase();
        if (!watched.has(addr)) continue;

        const entry    = watchlist.getByAddress(addr);
        const reqConfs = entry?.metadata?.zero_conf ? 0
          : (entry?.metadata?.btc_confirmations ?? cfg.btc_confirmations_default ?? 1);
        const confs = currentHeight - bn + 1;
        if (confs < reqConfs) continue;

        await _emit({
          chain:        'btc',
          tx_hash:      tx.txid,
          from_address: '',
          to_address:   vout.scriptPubKey.address,
          amount:       vout.value.toFixed(8),
          currency:     'BTC',
          confirmations: confs,
          timestamp:    blockTime,
          block_number: bn,
          label:        labels.get(addr) || addr.slice(0, 10) + '…',
        });
      }
    }
  }
  state.lastBlock = to;
  return true;
}

// ── Watcher loops ─────────────────────────────────────────────────────────────

async function _ethLoop(chain, pollMs) {
  const state = { lastBlock: null };
  _initChainState(chain);
  while (true) {
    let ok;
    try {
      ok = await _pollEthChain(chain, state);
    } catch (e) {
      warn(`${chain} poll error:`, e.message);
      ok = false;
    }
    if (ok === false) {
      _chainState[chain].failures++;
      const f = _chainState[chain].failures;
      if (f === 1 || f % 5 === 0)
        warn(`${chain} node unreachable (${f} consecutive failure${f === 1 ? '' : 's'})`);
    } else {
      if (_chainState[chain].failures > 0)
        log(`${chain} reconnected after ${_chainState[chain].failures} failure(s)`);
      _chainState[chain].failures = 0;
      _chainState[chain].lastOk = new Date().toISOString();
    }
    const sleepMs = pollMs * Math.min(16, Math.max(1, 2 ** _chainState[chain].failures));
    await new Promise(r => setTimeout(r, sleepMs));
  }
}

async function _btcLoop(pollMs) {
  const state = { lastBlock: null };
  _initChainState('btc');
  while (true) {
    let ok;
    try {
      ok = await _pollBtc(state);
    } catch (e) {
      warn('btc poll error:', e.message);
      ok = false;
    }
    if (ok === false) {
      _chainState.btc.failures++;
      const f = _chainState.btc.failures;
      if (f === 1 || f % 5 === 0)
        warn(`BTC node unreachable (${f} consecutive failure${f === 1 ? '' : 's'})`);
    } else {
      if (_chainState.btc.failures > 0)
        log(`BTC reconnected after ${_chainState.btc.failures} failure(s)`);
      _chainState.btc.failures = 0;
      _chainState.btc.lastOk = new Date().toISOString();
    }
    const sleepMs = pollMs * Math.min(16, Math.max(1, 2 ** _chainState.btc.failures));
    await new Promise(r => setTimeout(r, sleepMs));
  }
}

async function start() {
  const cfg = watchlist.getConfig();
  const ethMs = cfg.poll_interval_eth_ms || 15_000;
  const btcMs = cfg.poll_interval_btc_ms || 60_000;

  await loadSeenTx();
  log(`starting — ETH every ${ethMs / 1000}s, BTC every ${btcMs / 1000}s`);

  // EVM chains run in parallel
  _ethLoop('eth', ethMs);
  _ethLoop('arb', ethMs * 2);   // ARB polls less frequently
  _ethLoop('op',  ethMs * 2);   // OP polls less frequently
  _btcLoop(btcMs);

  // Lightning: poll BTCPay invoices at same interval as BTC
  lightning.startInvoiceWatcher(_emit, seenTx, btcMs);

  // Lightning: poll BTCPay channel state every 5 minutes — emits on new channel open
  lightning.startChannelWatcher(_emit, seenTx, 300_000);
}

// ── On-demand check ───────────────────────────────────────────────────────────

async function checkNow(address) {
  const entry = watchlist.getByAddress(address);
  const chain = entry?.metadata?.chain || (address.startsWith('0x') ? 'eth' : 'btc');
  const label = entry?.label || address.slice(0, 10) + '…';

  if (chain === 'btc') {
    const scan = await rpc.btcScanTxOutSet([address]);
    return { chain, address, label, balance_btc: scan?.total_amount?.toFixed(8) ?? 'unavailable' };
  }

  try {
    const balance = await rpc.ethGetBalance(chain, address);
    return { chain, address, label, balance_eth: balance };
  } catch (e) {
    return { chain, address, label, error: e.message };
  }
}

module.exports = { start, registerHarness, checkNow, getChainState };
