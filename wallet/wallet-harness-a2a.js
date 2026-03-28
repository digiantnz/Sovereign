'use strict';

/**
 * wallet-harness-a2a.js — A2A payment rail harness (stub).
 *
 * Subscribes to payment events on addresses tagged harness: ["a2a"].
 * On confirmed inbound payment:
 *   1. Validates the event schema
 *   2. Logs to local audit with full payment details
 *   3. Emits a structured A2A JSON-RPC 3.0 wallet/payment_confirmed event
 *      to sovereign-core for cognitive loop processing (Telegram alert + memory)
 *
 * The overseer integration is a separate build — this stub ensures events are
 * correctly formed, emitted, and logged so the overseer can subscribe when ready.
 */

const { randomUUID } = require('crypto');
const watchlist = require('./wallet-watchlist');

const SOVEREIGN_URL   = process.env.SOVEREIGN_CORE_URL  || 'http://sovereign-core:8000';
const INTERNAL_TOKEN  = process.env.WALLET_INTERNAL_TOKEN || '';

const log  = (...a) => console.log('[harness-a2a]', ...a);
const warn = (...a) => console.warn('[harness-a2a]', ...a);

// ── Schema validation ──────────────────────────────────────────────────────────

const REQUIRED_FIELDS = [
  'chain', 'tx_hash', 'from_address', 'to_address',
  'amount', 'currency', 'confirmations', 'timestamp',
];

function _validate(event) {
  for (const f of REQUIRED_FIELDS) {
    if (!event[f] && event[f] !== 0) return `missing field: ${f}`;
  }
  if (typeof event.amount !== 'string') return 'amount must be a string';
  if (typeof event.confirmations !== 'number') return 'confirmations must be a number';
  return null;
}

// ── A2A JSON-RPC 3.0 emission ─────────────────────────────────────────────────

async function _emitToSovereignCore(event) {
  const requestId = randomUUID();
  const body = JSON.stringify({
    jsonrpc: '3.0',
    id:      requestId,
    method:  'wallet/payment_confirmed',
    params:  {
      skill:     'wallet',
      operation: 'payment_confirmed',
      payload:   event,
    },
  });
  const res = await fetch(`${SOVEREIGN_URL}/wallet_event`, {
    method: 'POST',
    headers: {
      'Content-Type':    'application/json',
      'X-Wallet-Token':  INTERNAL_TOKEN,
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

// ── Main handler ──────────────────────────────────────────────────────────────

async function handle(event) {
  const entry = watchlist.getByAddress(event.to_address || event.from_address || '');
  const harnesses = entry?.metadata?.harness || [];
  if (!harnesses.includes('a2a')) return;   // not subscribed to a2a harness

  const err = _validate(event);
  if (err) {
    warn('invalid event schema:', err, JSON.stringify(event).slice(0, 200));
    return;
  }

  // Local audit log (signed by sovereign-core when it receives the event)
  log(
    `PAYMENT chain=${event.chain} tx=${event.tx_hash.slice(0, 16)}… ` +
    `${event.amount} ${event.currency} → ${event.label || event.to_address.slice(0, 10)} ` +
    `[${event.confirmations} conf]`
  );

  // Emit to sovereign-core
  try {
    const result = await _emitToSovereignCore(event);
    log(`emitted to sovereign-core, request_id=${result.request_id}`);
  } catch (e) {
    warn('emission to sovereign-core failed:', e.message);
    // Non-fatal — event is already logged locally.  sovereign-core will
    // receive it on next restart if sov-wallet persists a retry queue (future build).
  }
}

// ── Extension point for overseer subscription (future build) ─────────────────
// When the A2A overseer is ready, register it here:
//   harness.registerOverseer(overseerClient)
// The overseer will receive the same structured event for cross-agent routing.

module.exports = { handle };
