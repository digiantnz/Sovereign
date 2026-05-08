'use strict';

/**
 * wallet-harness-lightning.js — BTCPay Lightning balance reporter.
 *
 * Queries the BTCPay store Lightning node for channel balance and settled invoices.
 * Called from wallet-harness-portfolio.js during snapshot assembly.
 *
 * Env vars (set in secrets/wallet.env):
 *   BTCPAY_URL      — http://nginx:3009/btcpay  (nginx proxy → BTCPay .local HTTPS)
 *   BTCPAY_API_KEY  — BTCPay store API key (btcpay.store.canviewlightninginvoice)
 *   BTCPAY_STORE_ID — BTCPay store ID (from store URL)
 *
 * API (store-level):
 *   GET /api/v1/stores/{storeId}/lightning/BTC/balance
 *   GET /api/v1/stores/{storeId}/lightning/BTC/invoices
 */

const log  = (...a) => console.log('[lightning]', ...a);
const warn = (...a) => console.warn('[lightning]', ...a);

const MSAT_PER_BTC = 100_000_000_000;  // 1 BTC = 10^8 sats = 10^11 msats

let _btcpayUrl  = process.env.BTCPAY_URL || '';
const _apiKey   = process.env.BTCPAY_API_KEY || '';
let _storeId    = process.env.BTCPAY_STORE_ID || '';
let _nodePubkey = '';

function configure(walletCfg) {
  if (!_btcpayUrl && walletCfg?.btcpay?.lan_url) {
    _btcpayUrl = walletCfg.btcpay.lan_url;
    log(`configured from wallet-config: ${_btcpayUrl}`);
  }
  if (!_storeId && walletCfg?.btcpay?.store_id) {
    _storeId = walletCfg.btcpay.store_id;
  }
  if (!_nodePubkey && walletCfg?.btcpay?.lightning?.node_pubkey) {
    _nodePubkey = walletCfg.btcpay.lightning.node_pubkey;
  }
}

function isConfigured() { return !!(process.env.BTCPAY_API_KEY && _btcpayUrl && _storeId); }

function _storeBase() { return `${_btcpayUrl}/api/v1/stores/${_storeId}`; }

function _msatToBtc(msat) {
  return (msat / MSAT_PER_BTC).toFixed(8);
}

function _msatToSat(msat) {
  return Math.round(msat / 1000);
}

async function fetchLightningBalance() {
  if (!_btcpayUrl) {
    warn('BTCPAY_URL not configured — Lightning balance unavailable');
    return null;
  }
  if (!_apiKey) {
    warn('BTCPAY_API_KEY not set — Lightning balance unavailable');
    return null;
  }

  try {
    const res = await fetch(`${_storeBase()}/lightning/BTC/balance`, {
      headers: {
        'Authorization': `token ${_apiKey}`,
        'Content-Type':  'application/json',
      },
      signal: AbortSignal.timeout(20_000),
    });

    if (!res.ok) {
      warn(`BTCPay Lightning balance HTTP ${res.status}`);
      return null;
    }

    const data = await res.json();
    const localMsat   = typeof data.localBalance   === 'number' ? data.localBalance   : 0;
    const remoteMsat  = typeof data.remoteBalance  === 'number' ? data.remoteBalance  : 0;
    const pendingMsat = typeof data.pendingBalance === 'number' ? data.pendingBalance : 0;

    const result = {
      local_btc:   _msatToBtc(localMsat),
      remote_btc:  _msatToBtc(remoteMsat),
      pending_btc: _msatToBtc(pendingMsat),
      local_sat:   _msatToSat(localMsat),
      pending_sat: _msatToSat(pendingMsat),
      source:      'btcpay-internal',
    };

    log(`balance: local=${result.local_btc} BTC (${result.local_sat} sat) ` +
        `remote=${result.remote_btc} BTC pending=${result.pending_btc} BTC`);
    return result;
  } catch (e) {
    warn('Lightning balance fetch failed:', e.message);
    return null;
  }
}

// ── Invoice watcher (inbound Lightning payments) ──────────────────────────────
// Polls BTCPay GET /api/v1/stores/{storeId}/lightning/BTC/invoices for settled invoices.
// Uses paymentHash as tx_hash — same seenTx dedup as on-chain watchers.

async function _pollSettledInvoices(emitFn, seenTx) {
  if (!_btcpayUrl || !_apiKey || !_storeId) return;

  const res = await fetch(
    `${_storeBase()}/lightning/BTC/invoices?pendingOnly=false&count=50`,
    {
      headers: { 'Authorization': `token ${_apiKey}`, 'Content-Type': 'application/json' },
      signal: AbortSignal.timeout(20_000),
    }
  );
  if (!res.ok) {
    warn(`invoices HTTP ${res.status}`);
    return false;
  }

  const invoices = await res.json();
  if (!Array.isArray(invoices)) return false;

  for (const inv of invoices) {
    if (inv.status !== 'settled' || !inv.paymentHash) continue;

    const key = `lightning:${inv.paymentHash.toLowerCase()}`;
    if (seenTx.has(key)) continue;

    const amountMsat = inv.amountReceived ?? inv.amount ?? 0;
    const ts = inv.paidAt
      ? new Date(inv.paidAt * 1000).toISOString()
      : new Date().toISOString();

    await emitFn({
      chain:        'lightning',
      tx_hash:      inv.paymentHash,
      from_address: '',           // sender pubkey not exposed in invoice response
      to_address:   _nodePubkey, // our Lightning node pubkey
      amount:       (amountMsat / MSAT_PER_BTC).toFixed(8),
      amount_msat:  amountMsat,
      currency:     'BTC',
      confirmations: 1,           // settled = final, no confirmations needed
      timestamp:    ts,
      block_number: null,
      label:        'Lightning Node (inbound)',
      message:      inv.description || '',
      payment_hash: inv.paymentHash,
      invoice_id:   inv.id || '',
      direction:    'inbound',
      fee_msat:     0,
    });
  }
  return true;
}

async function _invoiceLoop(emitFn, seenTx, pollMs) {
  while (true) {
    let ok;
    try {
      ok = await _pollSettledInvoices(emitFn, seenTx);
    } catch (e) {
      warn('invoice poll error:', e.message);
      ok = false;
    }
    if (ok === false) {
      _lightningState.invoice.failures++;
      const f = _lightningState.invoice.failures;
      if (f === 1 || f % 5 === 0)
        warn(`Lightning invoice watcher: BTCPay unreachable (${f} consecutive failure${f === 1 ? '' : 's'})`);
    } else if (ok === true) {
      if (_lightningState.invoice.failures > 0)
        log(`Lightning invoice watcher: BTCPay reconnected after ${_lightningState.invoice.failures} failure(s)`);
      _lightningState.invoice.failures = 0;
      _lightningState.invoice.lastOk = new Date().toISOString();
    }
    const sleepMs = pollMs * Math.min(16, Math.max(1, 2 ** _lightningState.invoice.failures));
    await new Promise(r => setTimeout(r, sleepMs));
  }
}

function startInvoiceWatcher(emitFn, seenTx, pollMs = 60_000) {
  if (!isConfigured()) {
    warn('BTCPay not configured — invoice watcher inactive');
    return;
  }
  log(`invoice watcher starting (${pollMs / 1000}s poll)`);
  _invoiceLoop(emitFn, seenTx, pollMs);
}

// ── Channel tracking ──────────────────────────────────────────────────────────
// Polls BTCPay GET /stores/{id}/lightning/BTC/channels for individual channel state.
// Emits a structured event (chain='lightning_channel') when a new channel is detected.
// On first run, populates _seenChannels without emitting — avoids re-announcing
// channels that already existed when sov-wallet started.

// Lightning watcher health state — exported for /health endpoint
const _lightningState = {
  invoice: { failures: 0, lastOk: null },
  channel: { failures: 0, lastOk: null },
};
function getLightningWatcherState() {
  return { invoice: { ..._lightningState.invoice }, channel: { ..._lightningState.channel } };
}

const _seenChannels = new Set();  // Set<channelPoint>  e.g. "txid:outputIndex"

async function fetchChannels() {
  if (!_btcpayUrl || !_apiKey || !_storeId) return [];
  const res = await fetch(`${_storeBase()}/lightning/BTC/channels`, {
    headers: { 'Authorization': `token ${_apiKey}`, 'Content-Type': 'application/json' },
    signal: AbortSignal.timeout(20_000),
  });
  if (!res.ok) throw new Error(`channels HTTP ${res.status}`);
  const data = await res.json();
  return Array.isArray(data) ? data : [];
}

async function _emitNewChannels(channels, emitFn, seenTx) {
  for (const ch of channels) {
    const fundingTx   = ch.fundingTransactionId || (ch.channelPoint || '').split(':')[0];
    const channelPoint = ch.channelPoint || (fundingTx ? `${fundingTx}:0` : null);
    if (!fundingTx || !channelPoint) continue;

    if (_seenChannels.has(channelPoint)) continue;

    // Also skip if already seen by the dedup set (e.g. restored from Qdrant on restart)
    const txKey = `lightning_channel:${fundingTx.toLowerCase()}`;
    if (seenTx.has(txKey)) {
      _seenChannels.add(channelPoint);
      continue;
    }

    const capacitySat = typeof ch.capacity      === 'number' ? ch.capacity      : 0;
    const localSat    = typeof ch.localBalance   === 'number' ? ch.localBalance   : 0;
    const remoteSat   = typeof ch.remoteBalance  === 'number' ? ch.remoteBalance  : 0;

    await emitFn({
      chain:          'lightning_channel',
      tx_hash:        fundingTx,
      from_address:   _nodePubkey,          // we funded the channel from our node
      to_address:     ch.remoteNode || '',  // remote peer pubkey
      amount:         (capacitySat / 100_000_000).toFixed(8),
      currency:       'BTC',
      confirmations:  ch.status === 'Active' ? 1 : 0,
      timestamp:      new Date().toISOString(),
      block_number:   null,
      label:          'Lightning Channel Open',
      direction:      'outbound',
      channel_point:  channelPoint,
      capacity_sat:   capacitySat,
      local_sat:      localSat,
      remote_sat:     remoteSat,
      channel_status: ch.status || 'unknown',
    });

    _seenChannels.add(channelPoint);
  }
}

async function _channelLoop(emitFn, seenTx, pollMs) {
  let firstRun = true;
  while (true) {
    let ok;
    try {
      const channels = await fetchChannels();
      if (firstRun) {
        // Populate without emitting — don't re-announce channels that pre-existed startup
        for (const ch of channels) {
          const cp = ch.channelPoint || (ch.fundingTransactionId ? `${ch.fundingTransactionId}:0` : null);
          if (cp) _seenChannels.add(cp);
        }
        log(`channel watcher: initialized with ${_seenChannels.size} existing channels`);
        firstRun = false;
      } else {
        await _emitNewChannels(channels, emitFn, seenTx);
      }
      ok = true;
    } catch (e) {
      warn('channel poll error:', e.message);
      ok = false;
      firstRun = false;
    }
    if (ok === false) {
      _lightningState.channel.failures++;
      const f = _lightningState.channel.failures;
      if (f === 1 || f % 5 === 0)
        warn(`Lightning channel watcher: BTCPay unreachable (${f} consecutive failure${f === 1 ? '' : 's'})`);
    } else {
      if (_lightningState.channel.failures > 0)
        log(`Lightning channel watcher: BTCPay reconnected after ${_lightningState.channel.failures} failure(s)`);
      _lightningState.channel.failures = 0;
      _lightningState.channel.lastOk = new Date().toISOString();
    }
    const sleepMs = pollMs * Math.min(16, Math.max(1, 2 ** _lightningState.channel.failures));
    await new Promise(r => setTimeout(r, sleepMs));
  }
}

function startChannelWatcher(emitFn, seenTx, pollMs = 300_000) {
  if (!isConfigured()) {
    warn('BTCPay not configured — channel watcher inactive');
    return;
  }
  log(`channel watcher starting (${pollMs / 1000}s poll)`);
  _channelLoop(emitFn, seenTx, pollMs);
}

async function fetchChannelsForSnapshot() {
  try {
    const channels = await fetchChannels();
    return channels.map(ch => ({
      channel_point: ch.channelPoint  || '',
      remote_pubkey: ch.remoteNode    || '',
      capacity_sat:  ch.capacity      || 0,
      local_sat:     ch.localBalance  || 0,
      remote_sat:    ch.remoteBalance || 0,
      status:        ch.status        || 'unknown',
      funding_tx:    ch.fundingTransactionId || '',
    }));
  } catch (e) {
    warn('fetchChannelsForSnapshot failed:', e.message);
    return [];
  }
}

module.exports = {
  configure, isConfigured,
  fetchLightningBalance,
  startInvoiceWatcher,
  startChannelWatcher,
  fetchChannels,
  fetchChannelsForSnapshot,
  getLightningWatcherState,
};
