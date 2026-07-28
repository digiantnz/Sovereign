---
name: csv
version: "1.0"
description: "Parse tax-related CSV files stored in Nextcloud. Auto-detects encoding (UTF-8, UTF-16 LE), delimiter, and format (receipts, Wirex NZD Statement, EasyCrypto, Swyftx, Etherscan standard/internal). Returns normalised rows with ISO8601 dates, row counts, and skip reasons. Supports optional date range filtering."
sovereign:
  specialists:
    - business_agent
    - memory_agent
  tier_required: LOW
  adapter_deps:
    - nanobot
  checksum: 606e93580644deec6dd35c8fe1f292d34634d93b7460115f6a7a0dc1df3d428a
  operations:
    parse_tax_csv:
      tool: python3_exec
      script: scripts/csv_parse.py
      args: ["parse_tax_csv"]
      tier: LOW
      params:
        path:       {type: str, required: true,  description: "Nextcloud file path e.g. /Digiant/Tax/FY2026/receipts.csv"}
        source:     {type: str, required: false, description: "Source label for log messages (defaults to filename)"}
        start_date: {type: str, required: false, description: "ISO8601 start of date range filter (inclusive) e.g. 2025-04-01T00:00:00Z"}
        end_date:   {type: str, required: false, description: "ISO8601 end of date range filter (inclusive) e.g. 2026-03-31T23:59:59Z"}
      returns: "{status, path, format, total_rows, in_range_rows, skipped, skip_reasons, rows}"
---

# Skill: csv — Tax CSV Parser

Parses tax-related CSV files from Nextcloud. Handles encoding detection, delimiter
sniffing, format classification, and row normalisation. All column access is
case-insensitive — header capitalisation differences between CSV exporters are
handled automatically.

## Operations

### parse_tax_csv

Download a CSV from Nextcloud and return structured, normalised rows.

**Supported formats (auto-detected from column headers):**

| Format | Source | Row type |
|--------|--------|----------|
| `receipts` | Manually-maintained NZ expense spreadsheet | tax:expense |
| `wirex_nzd_statement` | Wirex NZD Statement export (semicolon, UTF-16 LE) | tax:expense + tax:crypto |
| `wirex_trade` | Legacy Wirex trade CSV | tax:expense + tax:crypto |
| `easycrypto` | EasyCrypto orders CSV | tax:crypto |
| `swyftx` | Swyftx/EasyCrypto legacy CSV | tax:crypto |
| `etherscan_standard` | Etherscan standard transaction export | tax:crypto |
| `etherscan_internal` | Etherscan internal transaction export | tax:crypto |

**Unified row schema:**
- `event_tag` — `"tax:crypto"` or `"tax:expense"`
- `date_iso` — ISO8601 UTC timestamp
- `raw_date` — original date string (for debugging)
- `format` — detected format name
- Format-specific fields (vendor, amount_nzd, asset, amount, nzd_value, tx_hash, ...)

**Notes:**
- EasyCrypto `nzd_value` is the NZD acquisition cost from the order (From amount).
- Etherscan rows return `nzd_value: ""` — caller must enrich via CoinGecko.
- Wirex NZD Statement crypto rows return `nzd_value` from the NZD amount column.
- `start_date` / `end_date` filter is applied after parsing; `total_rows` reflects
  parsed rows before filtering.
- `skip_reasons` (capped at 20) explains why rows were dropped — useful for
  debugging 0-row results.
- WEI amounts in Etherscan exports are normalised to ETH (÷ 1e18) and dust-filtered.

## Usage Notes
- `path` must be a Nextcloud-relative path, not including the WebDAV prefix
- UTF-16 LE detection is automatic — pass Wirex NZD Statement exports as-is
- EasyCrypto: one Order ID may appear on multiple rows (one per asset); reference
  includes the asset symbol to ensure uniqueness
- Large files (10 000+ rows) may take up to 30 seconds
