"""Entity Registry — sequential sov_ids for foundational sovereign entities.

Foundational entities (sovereign_entity class) are distinct from system components:
  - Use entity_type field (not component_type) — never both on the same entry
  - Exist independently of Sovereign
  - Are bootstrap critical — Rex must know about them to function correctly
  - Have a durable named relationship to the sovereign root
  - Are Director-approved and manually curated

Standing Design Order 11: An entity qualifies for a sequential sov_id only if it
meets ALL four criteria above. All other entities receive UUID5 sov_ids via
sov_id_for() in component_registry. Sequential sov_ids are assigned in this file.

Sequential sov_id table (never re-assign; retired IDs must remain tombstoned):
  001  Sovereign            system root (seed_sovereign_root — not in this registry)
  002  Matt                 Director, authority_tier: absolute
  003  Sovereign Server     primary hardware host (172.16.201.25)
  004  node01               ETH staking node, eth-docker (172.16.201.14)
  005  node04               external services host, a2a-browser/whisper (172.16.201.4)
  006  Start9 Server        Bitcoin infrastructure, BTC node (172.16.201.5)
  007  Grok                 LLM API service (xAI)
  008  Anthropic            AI safety org, Claude provider
  009  DigiAnt              owner entity
  010  Internet             external egress network
  011  Matt's brother       family relationship
  012  Ethereum             managed blockchain asset
  013  Bitcoin              managed blockchain asset
  014  node02               ETH staking node, Rocket Pool (172.16.201.2)

parent_sov_id rules:
  Hardware (003–006): SOVEREIGN_ROOT_ID — physically owned by the system
  All others: None — external entities; relationship expressed via associative links

Registry order within groups: Director → hardware → services → organisations
                              → networks → people relationships → blockchains
"""

from datetime import datetime, timezone

# Inline constant — avoids circular import with component_registry.py
SOVEREIGN_ROOT_ID = "00000000-0000-0000-0000-000000000001"

_ADMISSION_DATE = "2026-04-05"

# Canonical sequential sov_id table — Director-approved, never UUID5-derived.
# Append-only. Never re-use or re-assign an ID.
ENTITY_SOV_IDS: dict[str, str] = {
    "sovereign":        SOVEREIGN_ROOT_ID,                         # 001 — seed_sovereign_root
    "matt":             "00000000-0000-0000-0000-000000000002",    # 002
    "sovereign-server": "00000000-0000-0000-0000-000000000003",    # 003
    "node01":           "00000000-0000-0000-0000-000000000004",    # 004
    "node04":           "00000000-0000-0000-0000-000000000005",    # 005
    "start9-server":    "00000000-0000-0000-0000-000000000006",    # 006
    "grok":             "00000000-0000-0000-0000-000000000007",    # 007
    "anthropic":        "00000000-0000-0000-0000-000000000008",    # 008
    "digiant":          "00000000-0000-0000-0000-000000000009",    # 009
    "internet":         "00000000-0000-0000-0000-000000000010",    # 010
    "matts-brother":    "00000000-0000-0000-0000-000000000011",    # 011
    "ethereum":         "00000000-0000-0000-0000-000000000012",    # 012
    "bitcoin":          "00000000-0000-0000-0000-000000000013",    # 013
    "node02":           "00000000-0000-0000-0000-000000000014",    # 014
}

# Entity definitions — 002 through 013 (001 is handled by seed_sovereign_root)
_ENTITIES: list[dict] = [

    # ── Director (002) ────────────────────────────────────────────────────────
    {
        "slug":                  "matt",
        "sov_id":                "00000000-0000-0000-0000-000000000002",
        "entity_type":           "person",
        "_key":                  "semantic:entity:matt",
        "name":                  "Matt",
        "role":                  "Director",
        "location":              "New Zealand",
        "sovereign_relationship": "director",
        "authority_tier":        "absolute",
        "parent_sov_id":         None,
        "status":                "active",
        "content": (
            "Matt — Director of the Sovereign AI system. Person. Authority tier: absolute. "
            "Location: New Zealand. Matt is the human operator and decision-maker for all "
            "Sovereign operations, actions, and governance changes. All HIGH-tier actions "
            "require explicit Director confirmation. The system exists to serve and augment "
            "Matt's decision-making, not to act autonomously beyond governance bounds."
        ),
    },

    # ── Hardware (003–006) — physical infrastructure owned by the system ──────
    {
        "slug":         "sovereign-server",
        "sov_id":       "00000000-0000-0000-0000-000000000003",
        "entity_type":  "hardware",
        "_key":         "semantic:entity:sovereign-server",
        "name":         "Sovereign Server",
        "role":         "primary",
        "ip":           "172.16.201.25",
        "tailscale_ip": "100.111.130.60",
        "os":           "Ubuntu 24",
        "cpu":          "Ryzen 9900X",
        "gpu":          "RTX 3060 Ti 8GB",
        "ram_gb":       32,
        "storage_nvme": "512GB",
        "storage_raid": "6TB RAID5",
        "location":     "New Zealand",
        "parent_sov_id": SOVEREIGN_ROOT_ID,
        "status":       "active",
        "content": (
            "Sovereign Server — primary hardware host for the Sovereign AI system. "
            "AMD Ryzen 9900X, 32GB RAM, RTX 3060 Ti 8GB GPU, 512GB NVMe, 6TB RAID5. "
            "LAN IP: 172.16.201.25. Tailscale: 100.111.130.60. OS: Ubuntu 24. "
            "Located in New Zealand. Hosts all core Docker containers: "
            "sovereign-core, ollama, qdrant, qdrant-archive, nanobot-01, gateway, "
            "docker-broker, nextcloud stack, sov-wallet."
        ),
    },
    {
        "slug":         "node01",
        "sov_id":       "00000000-0000-0000-0000-000000000004",
        "entity_type":  "hardware",
        "_key":         "semantic:entity:node01",
        "name":         "node01",
        "role":         "eth_staking",
        "ip":           "172.16.201.14",
        "location":     "New Zealand",
        "parent_sov_id": SOVEREIGN_ROOT_ID,
        "status":       "active",
        "content": (
            "node01 — Ethereum staking hardware node at 172.16.201.14. "
            "Runs eth-docker for Ethereum full node operation. "
            "Part of the Sovereign ETH staking infrastructure on VLAN 172.16.201.0/24."
        ),
    },
    {
        "slug":         "node04",
        "sov_id":       "00000000-0000-0000-0000-000000000005",
        "entity_type":  "hardware",
        "_key":         "semantic:entity:node04",
        "name":         "node04",
        "role":         "external_services",
        "ip":           "172.16.201.4",
        "location":     "New Zealand",
        "parent_sov_id": SOVEREIGN_ROOT_ID,
        "status":       "active",
        "content": (
            "node04 — external services hardware host at 172.16.201.4. "
            "Runs a2a-browser (web search and URL fetch, port 8001) and "
            "a2a-whisper (faster-whisper transcription, port 8003). "
            "All external internet egress from sovereign-core routes through this node."
        ),
    },
    {
        "slug":         "start9-server",
        "sov_id":       "00000000-0000-0000-0000-000000000006",
        "entity_type":  "hardware",
        "_key":         "semantic:entity:start9-server",
        "name":         "Start9 Server",
        "role":         "bitcoin_infrastructure",
        "ip":           "172.16.201.5",
        "location":     "New Zealand",
        "parent_sov_id": SOVEREIGN_ROOT_ID,
        "status":       "active",
        "content": (
            "Start9 Server — Bitcoin infrastructure node at 172.16.201.5. "
            "Hosts Bitcoin full node, Specter Desktop, and BTCPay Server. "
            "Primary BTC infrastructure for Sovereign wallet operations."
        ),
    },

    # ── Services / LLM providers (007) ───────────────────────────────────────
    {
        "slug":                  "grok",
        "sov_id":                "00000000-0000-0000-0000-000000000007",
        "entity_type":           "service",
        "_key":                  "semantic:entity:grok",
        "name":                  "Grok",
        "provider":              "xAI",
        "sovereign_relationship": "llm_provider",
        "parent_sov_id":         None,
        "status":                "active",
        "content": (
            "Grok — LLM API service by xAI. Used by Sovereign as primary external LLM "
            "for web-aware queries (current events, market data, news, real-time information). "
            "Accessed via GrokAdapter. Active model: grok-3. "
            "Default external provider in the cognitive loop routing decision."
        ),
    },

    # ── Organisations (008–009) ───────────────────────────────────────────────
    {
        "slug":                  "anthropic",
        "sov_id":                "00000000-0000-0000-0000-000000000008",
        "entity_type":           "organisation",
        "_key":                  "semantic:entity:anthropic",
        "name":                  "Anthropic",
        "sovereign_relationship": "llm_provider",
        "parent_sov_id":         None,
        "status":                "active",
        "content": (
            "Anthropic — AI safety company, creator of Claude. "
            "Provides Claude API for complex reasoning escalation "
            "(DCL — Director Confidence Level) in Sovereign's cognitive loop. "
            "Also creator of Claude Code, the tool used for Sovereign development "
            "and maintenance."
        ),
    },
    {
        "slug":                  "digiant",
        "sov_id":                "00000000-0000-0000-0000-000000000009",
        "entity_type":           "organisation",
        "_key":                  "semantic:entity:digiant",
        "name":                  "DigiAnt",
        "sovereign_relationship": "owner_entity",
        "parent_sov_id":         None,
        "status":                "active",
        "content": (
            "DigiAnt — owner entity for the Sovereign AI system. "
            "The organisational entity under which Sovereign operates and is developed."
        ),
    },

    # ── External networks (010) ───────────────────────────────────────────────
    {
        "slug":                  "internet",
        "sov_id":                "00000000-0000-0000-0000-000000000010",
        "entity_type":           "network",
        "_key":                  "semantic:entity:internet",
        "name":                  "Internet",
        "sovereign_relationship": "egress_network",
        "parent_sov_id":         None,
        "status":                "active",
        "content": (
            "The Internet — external network providing egress for Sovereign. "
            "Sovereign-core has no direct internet access. All internet egress routes "
            "through node04 services: a2a-browser (web search, URL fetch, port 8001) and "
            "a2a-whisper (transcription, port 8003). "
            "Sovereign never connects to the internet directly."
        ),
    },

    # ── People relationships (011) ────────────────────────────────────────────
    {
        "slug":                  "matts-brother",
        "sov_id":                "00000000-0000-0000-0000-000000000011",
        "entity_type":           "person",
        "_key":                  "semantic:entity:matts-brother",
        "name":                  "Matt's brother",
        "sovereign_relationship": "family",
        "parent_sov_id":         None,
        "status":                "active",
        "content": (
            "Matt's brother — family member of Director Matt. "
            "Person in Matt's personal network with a named relationship to the system "
            "via the Director."
        ),
    },

    # ── Hardware continued (014) ─────────────────────────────────────────────
    {
        "slug":         "node02",
        "sov_id":       "00000000-0000-0000-0000-000000000014",
        "entity_type":  "hardware",
        "_key":         "semantic:entity:node02",
        "name":         "node02",
        "role":         "eth_staking",
        "ip":           "172.16.201.2",
        "location":     "New Zealand",
        "parent_sov_id": SOVEREIGN_ROOT_ID,
        "status":       "active",
        "content": (
            "node02 — Ethereum staking hardware node at 172.16.201.2. "
            "Runs Rocket Pool for Ethereum validator operation. "
            "Part of the Sovereign ETH staking infrastructure on VLAN 172.16.201.0/24."
        ),
    },

    # ── Blockchains (012–013) ─────────────────────────────────────────────────
    {
        "slug":                  "ethereum",
        "sov_id":                "00000000-0000-0000-0000-000000000012",
        "entity_type":           "blockchain",
        "_key":                  "semantic:entity:ethereum",
        "name":                  "Ethereum",
        "sovereign_relationship": "managed_asset",
        "parent_sov_id":         None,
        "status":                "active",
        "content": (
            "Ethereum — managed blockchain asset. Sovereign holds ETH via EOA "
            "0x623061184E86914C07985c847773Ee8e7ac6d508 and Safe multisig "
            "0x50BF8f009ECC10DB65262c65d729152e989A9323 (2-of-3 threshold). "
            "ETH node operated on node01 via eth-docker at 172.16.201.14. "
            "WalletAdapter manages ETH operations and Safe transaction proposals."
        ),
    },
    {
        "slug":                  "bitcoin",
        "sov_id":                "00000000-0000-0000-0000-000000000013",
        "entity_type":           "blockchain",
        "_key":                  "semantic:entity:bitcoin",
        "name":                  "Bitcoin",
        "sovereign_relationship": "managed_asset",
        "parent_sov_id":         None,
        "status":                "active",
        "content": (
            "Bitcoin — managed blockchain asset. Sovereign holds BTC via BIP-32 HD wallet "
            "(xpub tracked). Bitcoin full node and Specter Desktop operated via Start9 Server "
            "at 172.16.201.5. BTCPay Server also hosted on Start9 Server. "
            "WalletAdapter manages BTC operations."
        ),
    },
]

# ── Fields carried into Qdrant payload via extra_meta ────────────────────────
_PASSTHROUGH_FIELDS = (
    "role", "ip", "tailscale_ip", "os", "cpu", "gpu",
    "ram_gb", "storage_nvme", "storage_raid", "location",
    "sovereign_relationship", "authority_tier", "provider",
)


def build_entity_seeds() -> list[dict]:
    """Return entity seed dicts compatible with seed_intent_semantic_entries().

    All entity-specific fields are placed in extra_meta so they appear as
    top-level Qdrant payload fields. entity_type (not component_type) is used
    for all entries — never both on the same entry.
    """
    seeds = []
    for entity in _ENTITIES:
        slug      = entity["slug"]
        safe_slug = slug.replace("-", "_")
        seed_id   = f"entity_seed_v1_{safe_slug}"
        key       = entity["_key"]
        content   = entity["content"]
        title     = f"{entity['name']} — {entity['entity_type']}"
        domain    = f"entity.{entity['entity_type']}"

        extra_meta: dict = {
            "sov_id":        entity["sov_id"],
            "entity_type":   entity["entity_type"],
            "name":          entity["name"],
            "parent_sov_id": entity.get("parent_sov_id"),
            "status":        entity.get("status", "active"),
            "source":        "entity_registry",   # overrides "intent_seed" default
        }
        for field in _PASSTHROUGH_FIELDS:
            if field in entity:
                extra_meta[field] = entity[field]

        seeds.append({
            "seed_id":    seed_id,
            "key":        key,
            "title":      title,
            "content":    content,
            "domain":     domain,
            "extra_meta": extra_meta,
        })
    return seeds


def build_entity_index() -> dict:
    """Build the semantic:governance:entity-registry payload dict.

    Includes sovereign root (001) plus all entities in this registry (002–013).
    Written to SEMANTIC collection by seed_entity_entries() with a real embedding.
    """
    entries: dict[str, dict] = {
        SOVEREIGN_ROOT_ID: {
            "name":           "Sovereign",
            "entity_type":    "system",
            "_key":           "semantic:entity:sovereign",
            "admission_date": _ADMISSION_DATE,
        },
    }
    for entity in _ENTITIES:
        entries[entity["sov_id"]] = {
            "name":           entity["name"],
            "entity_type":    entity["entity_type"],
            "_key":           entity["_key"],
            "admission_date": _ADMISSION_DATE,
        }

    return {
        "_key":               "semantic:governance:entity-registry",
        "type":               "semantic",
        "domain":             "system.governance",
        "title":              "Sovereign entity registry — foundational entities",
        "total":              len(entries),
        "entities":           entries,
        "admission_criteria": (
            "Exists independently of Sovereign; bootstrap critical; "
            "durable named relationship to root; Director-approved"
        ),
        "source":             "entity_registry",
        "_backfill_seed_id":  "entity_registry_v1_meta",
    }
