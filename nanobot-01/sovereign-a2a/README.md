# sovereign-a2a

Lightweight A2A JSON-RPC 3.0 message primitives for Python agents.

A single-file library that handles the A2A 3.0 wire format so your agent code never constructs raw JSON-RPC dicts. Three classes, no external dependencies, works in any Python 3.10+ environment.

---

## Who is this for?

Any agent or service that needs to communicate over [A2A (Agent-to-Agent) protocol](https://github.com/google-deepmind/a2a) using JSON-RPC 3.0 — whether you're building a skill executor, a browser agent, an orchestrator, or a gateway. The library is format-agnostic: it doesn't know or care what your agents do, only how their messages are structured.

---

## Install

```bash
pip install git+https://github.com/digiantnz/sovereign-a2a.git
```

No dependencies beyond the Python standard library.

---

## Quickstart

```python
from sovereign_a2a import A2AMessage, A2AErrorCodes, A2AResponse
```

### Sending a request

```python
msg = A2AMessage.request(
    method="email/check_unread",
    params={
        "skill":     "email",
        "operation": "check_unread",
        "payload":   {"account": "business", "limit": 10},
    },
    id="req-abc123",
    metadata={
        "context_hints": {
            "tier":           "LOW",
            "retry_strategy": "correct_payload",
        }
    },
)
```

### Returning a success response

```python
response = A2AMessage.success(
    id="req-abc123",
    result={
        "success":     True,
        "status_code": "IMAP OK",
        "data":        {"messages": [...]},
    },
    hints={"execution_path": "dsl"},
    agent_card={
        "name":        "my-agent",
        "version":     "1.0.0",
        "skills":      ["email", "calendar"],
        "capabilities": ["partial_results"],
        "trust_level": "internal_sidecar",
    },
)
```

### Returning an error response

```python
response = A2AMessage.error(
    id="req-abc123",
    code=A2AErrorCodes.ADAPTER_UNAVAILABLE,
    message="IMAP connection refused",
    data={"skill": "email", "operation": "check_unread"},
)
```

### Streaming (partial results)

```python
chunk = A2AMessage.partial(
    id="req-abc123",
    result={"chunk": [...], "index": 0},
    remaining=3,
)
```

### Parsing an incoming response

```python
body = await response.json()

if A2AResponse.is_success(body):
    data      = A2AResponse.get_result(body)   # result object
    hints     = A2AResponse.get_hints(body)    # metadata.context_hints
    agent_card = A2AResponse.get_agent_card(body)

elif A2AResponse.is_error(body):
    err     = A2AResponse.get_error(body)
    code    = err["code"]     # A2AErrorCodes constant
    message = err["message"]  # verbatim protocol error

# Distinguish A2A 3.0 from legacy flat responses
if A2AResponse.is_a2a(body):
    ...  # has "jsonrpc": "3.0"
```

---

## API reference

### `A2AMessage`

| Method | Description |
|--------|-------------|
| `request(method, params, id, metadata=None)` | Outbound request. `method` is `"skill/operation"`. `metadata` is deep-merged with defaults. |
| `success(id, result, hints=None, agent_card=None)` | Success response. `agent_card` advertises the agent's current capabilities. |
| `error(id, code, message, data=None, hints=None)` | Error response. `message` should be the verbatim protocol error — never sanitised. |
| `partial(id, result, remaining=-1)` | Partial result for streaming. `remaining=-1` means unknown total. |

### `A2AErrorCodes`

| Constant | Code | When to use |
|----------|------|-------------|
| `PARSE_ERROR` | -32700 | Malformed JSON received |
| `INVALID_REQUEST` | -32600 | Not a valid A2A request object |
| `METHOD_NOT_FOUND` | -32601 | Requested skill/operation not registered |
| `INVALID_PARAMS` | -32602 | Parameter validation failure |
| `SERVER_ERROR` | -32000 | General execution failure |
| `TIMEOUT` | -32001 | Execution exceeded the requested timeout |
| `SKILL_NOT_LOADED` | -32002 | Skill exists in registry but is not currently loaded |
| `ADAPTER_UNAVAILABLE` | -32003 | The underlying adapter or service is not reachable |

### `A2AResponse`

All methods return `None` (or `{}` for hints) when the field is absent — never raises on malformed input.

| Method | Returns |
|--------|---------|
| `is_a2a(response)` | `True` if `"jsonrpc": "3.0"` is present |
| `is_success(response)` | `True` if `result` key present and no `error` |
| `is_error(response)` | `True` if `error` key present |
| `get_result(response)` | The `result` object, or `None` |
| `get_error(response)` | The `error` object `{code, message, data}`, or `None` |
| `get_hints(response)` | `metadata.context_hints` dict (always a dict) |
| `get_agent_card(response)` | `metadata.agent_card`, or `None` |
| `get_id(response)` | The `id` field (echoed request ID), or `None` |

---

## Wire format

### Request
```json
{
  "jsonrpc": "3.0",
  "id":      "<request_id>",
  "method":  "<skill>/<operation>",
  "params": {
    "skill":     "<skill-name>",
    "operation": "<operation-name>",
    "payload":   {}
  },
  "metadata": {
    "priority":      "normal",
    "stream":        false,
    "capabilities":  [],
    "context_hints": {}
  }
}
```

### Success response
```json
{
  "jsonrpc": "3.0",
  "id":      "<request_id>",
  "result": {
    "success":     true,
    "status_code": "...",
    "data":        {}
  },
  "metadata": {
    "agent_card":    {"name": "...", "skills": [], "capabilities": [], "trust_level": "..."},
    "context_hints": {}
  }
}
```

### Error response
```json
{
  "jsonrpc": "3.0",
  "id":      "<request_id>",
  "error": {
    "code":    -32000,
    "message": "verbatim error string",
    "data":    {}
  },
  "metadata": {
    "context_hints": {}
  }
}
```

---

## Backward compatibility

A2A 3.0 is backward compatible with 2.0 clients. `A2AResponse.is_a2a()` lets you branch on format when you need to support both during a transition.

---

## Versioning

`sovereign-a2a` follows semver. The wire format is stable at v0.1.0. Breaking changes to the A2A spec will result in a major version bump.
