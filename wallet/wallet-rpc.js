'use strict';

/**
 * wallet-rpc.js — Chain-aware RPC clients for ETH (EVM) and BTC.
 *
 * Endpoints are configured at startup from MIP (network.endpoints domain).
 * configure(endpoints) must be called before any RPC methods are used.
 * All functions are plain JSON-RPC — no ethers/web3 dependency.
 */

const log = (...a) => console.log('[rpc]', ...a);
const warn = (...a) => console.warn('[rpc]', ...a);

const EVM_CHAINS = {
  eth: { name: 'Ethereum Mainnet', currency: 'ETH', chainId: 1     },
  arb: { name: 'Arbitrum One',     currency: 'ETH', chainId: 42161 },
  op:  { name: 'Optimism',         currency: 'ETH', chainId: 10    },
};

// Defaults — overwritten by configure() from MIP
const _endpoints = {
  eth: process.env.ETH_RPC_PRIMARY   || 'http://172.16.201.15:8545',
  arb: 'https://arb1.arbitrum.io/rpc',
  op:  'https://mainnet.optimism.io',
  btc: {
    url:  process.env.BTC_RPC_URL   || 'http://172.16.201.5:8332',
    user: process.env.BTC_RPC_USER  || '',
    pass: process.env.BTC_RPC_PASS  || '',
  },
};

function configure(endpoints) {
  if (endpoints.eth)      _endpoints.eth = endpoints.eth;
  if (endpoints.eth_secondary) _endpoints.eth_secondary = endpoints.eth_secondary;
  if (endpoints.arb)      _endpoints.arb = endpoints.arb;
  if (endpoints.op)       _endpoints.op  = endpoints.op;
  if (endpoints.btc_url)  _endpoints.btc.url  = endpoints.btc_url;
  if (endpoints.btc_user) _endpoints.btc.user = endpoints.btc_user;
  if (endpoints.btc_pass) _endpoints.btc.pass = endpoints.btc_pass;
  log('endpoints configured — eth:', _endpoints.eth, 'btc:', _endpoints.btc.url);
}

// ── ETH JSON-RPC ──────────────────────────────────────────────────────────────

let _ethId = 1;

async function _ethPost(url, method, params) {
  const body = JSON.stringify({ jsonrpc: '2.0', id: _ethId++, method, params });
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body,
    signal: AbortSignal.timeout(10_000),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const data = await res.json();
  if (data.error) throw new Error(JSON.stringify(data.error));
  return data.result;
}

async function ethRpc(chain, method, params = []) {
  const primary = _endpoints[chain];
  if (!primary) throw new Error(`No endpoint for chain: ${chain}`);
  try {
    return await _ethPost(primary, method, params);
  } catch (e) {
    // Fallback to secondary for ETH mainnet only
    if (chain === 'eth' && _endpoints.eth_secondary) {
      warn(`primary unreachable (${e.message}), trying secondary`);
      return await _ethPost(_endpoints.eth_secondary, method, params);
    }
    throw e;
  }
}

async function ethBlockNumber(chain) {
  const hex = await ethRpc(chain, 'eth_blockNumber');
  return parseInt(hex, 16);
}

async function ethGetBlock(chain, blockNum, fullTxs = true) {
  const hex = '0x' + blockNum.toString(16);
  return await ethRpc(chain, 'eth_getBlockByNumber', [hex, fullTxs]);
}

async function ethGetBalance(chain, address) {
  const hex = await ethRpc(chain, 'eth_getBalance', [address.toLowerCase(), 'latest']);
  // Return as decimal ETH string with 8 decimal places
  const wei = BigInt(hex);
  const whole = wei / 10n ** 18n;
  const frac  = (wei % 10n ** 18n) * 10n ** 8n / 10n ** 18n;
  return `${whole}.${frac.toString().padStart(8, '0')}`;
}

function weiToEth(hexWei) {
  try {
    return `${(parseInt(hexWei, 16) / 1e18).toFixed(8)}`;
  } catch { return '0.00000000'; }
}

// ── BTC JSON-RPC ──────────────────────────────────────────────────────────────

let _btcId = 1;

async function btcRpc(method, params = []) {
  const { url, user, pass } = _endpoints.btc;
  if (!url) throw new Error('BTC RPC URL not configured');
  const auth = Buffer.from(`${user}:${pass}`).toString('base64');
  const res = await fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type':  'application/json',
      'Authorization': `Basic ${auth}`,
    },
    body: JSON.stringify({ jsonrpc: '1.0', id: _btcId++, method, params }),
    signal: AbortSignal.timeout(15_000),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const data = await res.json();
  if (data.error) throw new Error(JSON.stringify(data.error));
  return data.result;
}

async function btcGetBlockCount() {
  return await btcRpc('getblockcount');
}

async function btcGetBestBlockHash() {
  return await btcRpc('getbestblockhash');
}

async function btcListSinceBlock(blockhash) {
  // Returns {transactions:[...], lastblock:"..."}
  // Each tx: {address, category, amount, txid, confirmations, blockhash, blocktime, ...}
  return await btcRpc('listsinceblock', [blockhash || '', 1, false]);
}

async function btcGetReceivedByAddress(address, minConf = 0) {
  try {
    return await btcRpc('getreceivedbyaddress', [address, minConf]);
  } catch {
    return null;
  }
}

async function btcScanTxOutSet(addresses) {
  const descriptors = addresses.map(a => `addr(${a})`);
  try {
    return await btcRpc('scantxoutset', ['start', descriptors]);
  } catch {
    return null;
  }
}

module.exports = {
  EVM_CHAINS,
  configure,
  ethRpc,
  ethBlockNumber,
  ethGetBlock,
  ethGetBalance,
  weiToEth,
  btcRpc,
  btcGetBlockCount,
  btcGetBestBlockHash,
  btcListSinceBlock,
  btcGetReceivedByAddress,
  btcScanTxOutSet,
};
