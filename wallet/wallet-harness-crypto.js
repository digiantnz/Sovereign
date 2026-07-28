'use strict';

/**
 * wallet-harness-crypto.js — generic "tell the Director" alert harness.
 *
 * Unlike wallet-harness-a2a.js (opt-in per-address via harness: ["a2a"] tag,
 * payment-confirmation business logic) this harness fires for EVERY event the
 * watcher emits — every event it ever receives is already about a watchlist
 * address by construction (wallet-watcher.js only calls registered harnesses
 * after a watched-address match), so no address filtering is needed here.
 *
 * Sends a plain Telegram alert via the same /wallet_event endpoint as the a2a
 * harness, but WITHOUT the credit_a2a flag — /wallet_event only runs its
 * CoinGecko + a2a-browser credit-dispatch section when that flag is present,
 * so this harness gets the shared sign+store+notify plumbing without
 * triggering the Safe/invoice-crediting side effects that pipeline is for.
 */

const { randomUUID } = require('crypto');

const SOVEREIGN_URL  = process.env.SOVEREIGN_CORE_URL    || 'http://sovereign-core:8000';
const INTERNAL_TOKEN = process.env.WALLET_INTERNAL_TOKEN || '';

const log  = (...a) => console.log('[harness-crypto]', ...a);
const warn = (...a) => console.warn('[harness-crypto]', ...a);

const REQUIRED_FIELDS = [
  'chain', 'tx_hash', 'to_address', 'amount', 'currency', 'confirmations', 'timestamp',
];
const PRESENT_FIELDS = ['from_address'];

function _validate(event) {
  for (const f of REQUIRED_FIELDS) {
    if (!event[f] && event[f] !== 0) return `missing field: ${f}`;
  }
  for (const f of PRESENT_FIELDS) {
    if (event[f] === undefined || event[f] === null) return `missing field: ${f}`;
  }
  if (typeof event.amount !== 'string') return 'amount must be a string';
  if (typeof event.confirmations !== 'number') return 'confirmations must be a number';
  return null;
}

async function _emitToSovereignCore(event) {
  const requestId = randomUUID();
  const body = JSON.stringify({
    jsonrpc: '3.0',
    id:      requestId,
    method:  'wallet/transaction_detected',
    params:  {
      skill:     'wallet',
      operation: 'transaction_detected',
      payload:   event,   // no credit_a2a flag — /wallet_event skips credit dispatch
    },
  });
  const res = await fetch(`${SOVEREIGN_URL}/wallet_event`, {
    method: 'POST',
    headers: {
      'Content-Type':   'application/json',
      'X-Wallet-Token': INTERNAL_TOKEN,
    },
    body,
    signal: AbortSignal.timeout(15_000),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(`sovereign-core responded ${res.status}: ${text.slice(0, 200)}`);
  }
  const data = await res.json();
  return { request_id: requestId, response: data };
}

async function handle(event) {
  const err = _validate(event);
  if (err) {
    warn('invalid event schema:', err, JSON.stringify(event).slice(0, 200));
    return;
  }

  log(
    `${event.direction === 'outgoing' ? 'OUTGOING' : 'INCOMING'} chain=${event.chain} ` +
    `tx=${event.tx_hash.slice(0, 16)}… ${event.amount} ${event.currency} ` +
    `${event.label || event.to_address.slice(0, 10)} [${event.confirmations} conf]`
  );

  try {
    const result = await _emitToSovereignCore(event);
    log(`emitted to sovereign-core, request_id=${result.request_id}`);
  } catch (e) {
    warn('emission to sovereign-core failed:', e.message);
  }
}

module.exports = { handle };
