'use strict';

/**
 * sov-wallet — Safe Transaction Service proxy
 *
 * Thin internet-facing proxy on browser_net.
 * sovereign-core signs Safe transactions directly via eth_account (EIP-712).
 * This service only forwards signed proposals to the Safe Transaction Service API.
 */

const express = require('express');
const app = express();
app.use(express.json());

const PORT = 3001;
const SAFE_API_BASE = process.env.SAFE_API_BASE || 'https://safe-transaction-mainnet.safe.global/api/v1';

app.get('/health', (_req, res) => {
  res.json({ status: 'ok', role: 'safe-proxy' });
});

// ── Safe Transaction Service proxies ────────────────────────────────────────

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
  const safe = body.safe;
  if (!safe) return res.status(400).json({ error: 'safe field required' });
  try {
    const r = await fetch(`${SAFE_API_BASE}/safes/${safe}/multisig-transactions/`, {
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

app.listen(PORT, '0.0.0.0', () => {
  console.log(`[sov-wallet] Safe proxy listening on port ${PORT}`);
});
