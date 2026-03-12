**1️⃣ Design Overview**

**Core Philosophy**

NVMe (/docker) → Fast AI runtime\
RAID (/home) → Durable truth\
Broker → Control boundary\
Sovereign → Orchestration brain\
Ollama → Local cognition\
Nextcloud → Business memory

You are building:

A sovereign AI-assisted Linux & business orchestration plane

Without:

- Exposed public ports

- Direct docker.sock in AI container

- Blind external LLM calls

------------------------------------------------------------------------

**2️⃣ Filesystem & Storage Schema**

**Current Layout (Good)**

/ → 512GB NVMe\
/docker → NVMe (ephemeral AI runtime)\
/home → 6TB RAID5 SSD

------------------------------------------------------------------------

**Recommended Structure**

**NVMe (Fast, Ephemeral)**

/docker/\
sovereign/\
compose.yml\
core/\
broker/\
runtime/ ← temporary AI workspaces\
tmp/

Nothing critical stored here long-term.

------------------------------------------------------------------------

**RAID (Durable Truth)**

/home/sovereign/\
governance/\
governance.json\
docker-policy.yaml\
memory/\
MEMORY.md\
lessons.json\
audit/\
logs/\
backups/\
models/ ← optional fallback model cache

------------------------------------------------------------------------

**Ollama Model Storage**

You already use:

ollama_models:\
external: true\
name: compose_ollama_models

Ensure that volume maps to RAID, not NVMe:

docker volume inspect compose_ollama_models

If needed, bind it to:

/home/sovereign/models

Because models are large and should survive reboots.

------------------------------------------------------------------------

**3️⃣ Container Architecture Schema**

**Network Topology**

ai_net\
- ollama\
- docker-broker\
- sovereign-core\
\
business_net\
- nextcloud\
- redis\
- mariadb\
- nextcloud-rp\
- sovereign-core (dual-homed)

Sovereign connects to both networks.\
Only broker sees docker.sock.

------------------------------------------------------------------------

**Runtime Control Flow**

SSH\
↓\
Sovereign API (loopback only)\
↓\
Policy Check\
↓\
If Docker → Broker\
If Files → WebDAV\
If Mail → IMAP/SMTP\
If Reasoning → Ollama\
If Research → Grok (sanitized)

------------------------------------------------------------------------

**4️⃣ Governance-Lite Schema**

No complex DSL.

Single JSON file on RAID:

/home/sovereign/governance/governance.json

{\
\"tiers\": {\
\"LOW\": {\
\"docker_read\": true,\
\"file_read\": true,\
\"mail_read\": true\
},\
\"MID\": {\
\"docker_workflows\": \[\"restart\", \"update\"\],\
\"file_write\": true,\
\"calendar_write\": true,\
\"mail_send\": true,\
\"requires_confirmation\": true\
},\
\"HIGH\": {\
\"docker_workflows\": \[\"rebuild\", \"prune\"\],\
\"file_delete\": true,\
\"requires_double_confirmation\": true\
}\
}\
}

This is sufficient for Phase 2.

You need a **tight orchestration kernel** that:

- Enforces LOW / MID / HIGH

- Talks to broker

- Talks to Ollama

- Talks to Nextcloud (WebDAV/CalDAV)

- Talks to IMAP/SMTP

- Optionally calls Grok

- Persists durable memory to RAID

- Uses NVMe only for runtime scratch

Below is a **clean, production-appropriate Sovereign Core design**
aligned to your hardware and storage model.

------------------------------------------------------------------------

**🧠 Sovereign Core --- Design Overview**

**Design Goals**

1.  Deterministic governance enforcement

2.  No docker.sock exposure

3.  Explicit tier mapping

4.  Modular adapters

5.  Clean separation of:

    - Cognition

    - Execution

    - Persistence

    - External research

------------------------------------------------------------------------

**🔷 Architectural Model**

SSH (localhost)\
│\
┌──────────────┐\
│ Sovereign API │\
└───────┬──────┘\
│\
┌────────┼────────┐\
│ │ │\
Governance Cognition Execution\
Engine Engine Engine\
│ │ │\
│ │ ├──────── Broker Adapter (Docker)\
│ │ ├──────── WebDAV Adapter\
│ │ ├──────── CalDAV Adapter\
│ │ ├──────── IMAP Adapter\
│ │ └──────── SMTP Adapter\
│ │\
│ └──────── Ollama Adapter\
│\
└──────── Grok Adapter (Optional)

------------------------------------------------------------------------

**📂 Directory Layout (NVMe vs RAID Clean Separation)**

**On NVMe (/docker/sovereign/core/)**

core/\
app/\
main.py\
config.py\
api/\
routes.py\
governance/\
engine.py\
schema.py\
cognition/\
engine.py\
prompts.py\
execution/\
engine.py\
adapters/\
broker.py\
ollama.py\
webdav.py\
caldav.py\
imap.py\
smtp.py\
grok.py\
memory/\
session.py\
requirements.txt\
Dockerfile

This is runtime code only.

------------------------------------------------------------------------

**On RAID (/home/sovereign/)**

governance/\
governance.json\
memory/\
MEMORY.md\
lessons.json\
audit/\
logs/\
backups/

------------------------------------------------------------------------

**🧱 Core Modules (Deep Design)**

------------------------------------------------------------------------

**1️⃣ main.py --- FastAPI Kernel**

Responsibilities:

- Load governance.json from RAID

- Load environment secrets

- Initialize adapters

- Register routes

- Handle lifecycle hooks (startup/shutdown)

Skeleton:

from fastapi import FastAPI\
from app.governance.engine import GovernanceEngine\
from app.execution.engine import ExecutionEngine\
from app.cognition.engine import CognitionEngine\
\
app = FastAPI()\
\
\@app.on_event(\"startup\")\
def startup():\
app.state.gov =
GovernanceEngine(\"/home/sovereign/governance/governance.json\")\
app.state.exec = ExecutionEngine(app.state.gov)\
app.state.cog = CognitionEngine()\
\
\@app.get(\"/health\")\
def health():\
return {\"status\": \"ok\"}\
\
\@app.post(\"/query\")\
async def query(payload: dict):\
return await app.state.exec.handle_request(payload)

------------------------------------------------------------------------

**2️⃣ Governance Engine**

This is critical and simple.

Responsibilities:

- Determine tier required

- Validate action allowed

- Enforce confirmation requirement

- Enforce double confirmation for HIGH

Schema:

class GovernanceEngine:\
def \_\_init\_\_(self, path):\
self.policy = json.load(open(path))\
\
def validate(self, action_type, tier):\
if tier not in self.policy\[\"tiers\"\]:\
raise Exception(\"Invalid tier\")\
\
rules = self.policy\[\"tiers\"\]\[tier\]\
\# Check allowed actions\
return True

This stays deterministic.\
No LLM inside governance.

------------------------------------------------------------------------

**3️⃣ Cognition Engine**

Responsibilities:

- Call Ollama

- Summarize logs

- Interpret file content

- Decide when research required

Never executes actions.\
Only reasons.

class CognitionEngine:\
async def ask_local(self, prompt):\
\# call ollama\
return response

Model recommendation for 3060 Ti:

- mistral:7b-instruct-q4

- llama3:8b-q4

------------------------------------------------------------------------

**4️⃣ Execution Engine**

The real orchestrator.

Flow:

Request →\
Determine action →\
Ask Governance →\
If LOW → execute\
If MID → request confirmation\
If HIGH → double confirm\
Route to adapter

Pseudo:

class ExecutionEngine:\
\
async def handle_request(self, payload):\
action = payload\[\"action\"\]\
tier = payload.get(\"tier\", \"LOW\")\
\
self.gov.validate(action, tier)\
\
if tier == \"MID\":\
return {\"requires_confirmation\": True}\
\
if tier == \"HIGH\":\
return {\"requires_double_confirmation\": True}\
\
return await self.route(action)

------------------------------------------------------------------------

**5️⃣ Adapter Layer**

Each adapter is stateless.

------------------------------------------------------------------------

**Broker Adapter**

Never runs raw shell.\
Only calls broker API.

class BrokerAdapter:\
async def restart(self, container):\
return requests.post(\
\"http://docker-broker:8088/workflow/restart\",\
json={\"container\": container}\
)

------------------------------------------------------------------------

**Ollama Adapter**

class OllamaAdapter:\
async def generate(self, prompt):\
return requests.post(\
\"http://ollama:11434/api/generate\",\
json={\"model\":\"mistral\",\"prompt\":prompt}\
)

------------------------------------------------------------------------

**WebDAV Adapter**

class WebDAVAdapter:\
def list(self, path):\
return requests.request(\
\"PROPFIND\",\
f\"{BASE}{path}\",\
auth=(USER, PASS)\
)

------------------------------------------------------------------------

**IMAP Adapter**

class IMAPAdapter:\
def fetch_unread(self):\
mail = imaplib.IMAP4_SSL(HOST, PORT)\
mail.login(USER, PASS)

------------------------------------------------------------------------

**SMTP Adapter**

class SMTPAdapter:\
def send(self, to, subject, body):\
server = smtplib.SMTP(HOST, PORT)\
server.starttls()

------------------------------------------------------------------------

**Grok Adapter (Controlled)**

Before call:

- Remove IP addresses

- Remove hostnames

- Remove secrets

- Truncate logs

def sanitize(text):\
text = re.sub(r\'\\b\\d{1,3}(\\.\\d{1,3}){3}\\b\', \'\[IP\]\', text)\
return text\[:4000\]

------------------------------------------------------------------------

**🧠 Memory Model**

Two layers:

**1️⃣ Session Memory (NVMe)**

- Stored in /docker/sovereign/runtime/session.json

- Reset on container restart

**2️⃣ Durable Memory (RAID)**

- /home/sovereign/memory/MEMORY.md

- Updated only on explicit save

- Synced on shutdown

Shutdown hook:

\@app.on_event(\"shutdown\")\
def sync_memory():\
shutil.copy(\"/runtime/MEMORY.md\",\
\"/home/sovereign/memory/MEMORY.md\")

------------------------------------------------------------------------

**🔁 State Machine Model**

IDLE\
↓\
REQUEST_RECEIVED\
↓\
CLASSIFY_ACTION\
↓\
GOVERNANCE_CHECK\
↓\
EXECUTE_ADAPTER\
↓\
COGNITION_SUMMARY\
↓\
LOG_AUDIT\
↓\
RESPOND

Simple and predictable.

------------------------------------------------------------------------

**🔐 Security Boundaries**

  ----------------------------------
  **Component**    **Access**
  ---------------- -----------------
  docker.sock      broker only

  sovereign-core   no privileged
                   mounts

  Nextcloud        business_net only

  Ollama           ai_net only

  API              127.0.0.1 only
  ----------------------------------

------------------------------------------------------------------------

**🖥 Hardware Utilization Strategy**

**CPU (9900X)**

- Governance logic

- Adapter orchestration

- IMAP parsing

- WebDAV processing

**GPU (3060 Ti)**

- Ollama inference only

- Keep VRAM \< 7GB

- Avoid concurrent heavy model loads

------------------------------------------------------------------------

**📊 Operational Profile**

  ---------------------------------------------------
  **Task**         **GPU**                 **CPU**
  ---------------- ----------------------- ----------
  Log              GPU                     minimal
  summarization                            

  File parsing     CPU                     moderate

  Docker restart   CPU                     minimal

  CVE research     GPU (local) + Grok      minimal
                   (optional)              
  ---------------------------------------------------

**5️⃣ PHASED IMPLEMENTATION (Step-by-Step)**

You are SSH'ing from laptop.

------------------------------------------------------------------------

**🔹 PHASE 0 --- Observer AI**

Goal:

Read-only Linux + Docker + Nextcloud

------------------------------------------------------------------------

**Step 1 --- Prepare RAID structure**

ssh matt@server\
\
sudo mkdir -p /home/sovereign/{governance,memory,audit,backups}\
sudo chown -R matt:matt /home/sovereign

------------------------------------------------------------------------

**Step 2 --- Prepare NVMe workspace**

sudo mkdir -p /docker/sovereign/{core,broker,runtime,tmp}\
sudo chown -R matt:matt /docker/sovereign\
cd /docker/sovereign

------------------------------------------------------------------------

**Step 3 --- Place governance.json**

nano /home/sovereign/governance/governance.json

Paste LOW tier only initially.

**Step 4 --- Deploy Sovereign Phase 0**

Use compose file replacing openclaw with sovereign-core.

Bring up only:

docker compose up -d sovereign-core

------------------------------------------------------------------------

**Step 5 --- Validate Ollama GPU**

docker exec -it ollama nvidia-smi

Should show 3060 Ti visible.

Then:

ollama run mistral

Confirm VRAM usage \~6--7GB.

------------------------------------------------------------------------

**Phase 0 Capabilities**

- docker ps

- docker logs

- df -h

- free -m

- Read Nextcloud files via WebDAV

- Summarise MEMORY.md

No mutation allowed.

------------------------------------------------------------------------

**🔹 PHASE 1 --- Controlled Docker Workflows**

Goal:

Safe restart/update

------------------------------------------------------------------------

**Step 6 --- Add Broker Workflow Scripts**

On NVMe:

/docker/sovereign/broker/workflows/restart.sh

Example:

#!/bin/sh\
docker inspect \$1 \> /home/sovereign/backups/\$1.inspect.\$(date
+%s).json\
docker restart \$1\
sleep 3\
docker ps \| grep \$1

Make executable:

chmod +x restart.sh

------------------------------------------------------------------------

**Step 7 --- Extend governance.json**

Enable MID tier.

------------------------------------------------------------------------

**Step 8 --- Add Confirmation Prompt**

Sovereign behavior:

When MID action requested:

{\
\"action\": \"restart redis\",\
\"requires_confirmation\": true\
}

You confirm via curl or SSH CLI.

------------------------------------------------------------------------

**🔹 PHASE 2 --- Business Integration**

Now we activate:

- WebDAV write

- CalDAV

- SMTP send

- IMAP read

- HIGH destructive docker workflows

------------------------------------------------------------------------

**Step 9 --- Create Nextcloud Service Account**

Inside Nextcloud UI:

Create user:

svc-sovereign

Generate App Password.

------------------------------------------------------------------------

**Step 10 --- Configure WebDAV Adapter**

Test:

curl -u svc-sovereign:APP_PASS \\\
http://localhost:8080/remote.php/dav/files/svc-sovereign/

Confirm response.

------------------------------------------------------------------------

**Step 11 --- Calendar Test**

Create test ICS and PUT to:

/remote.php/dav/calendars/svc-sovereign/personal/test.ics

Confirm visible in UI.

------------------------------------------------------------------------

**Step 12 --- Mail Test**

openssl s_client -connect smtp.example.com:587 -starttls smtp

Confirm TLS works.

------------------------------------------------------------------------

**Step 13 --- HIGH Tier Enablement**

Add:

\"requires_double_confirmation\": true

Implement:

- volume snapshot

- rollback script

- health check validation

**6️⃣ GPU + Ollama Optimization (3060 Ti)**

Your GPU:

- 8GB VRAM

- Best suited for:

  - Mistral 7B Q4_K_M

  - Llama 3 8B Q4

  - Gemma 7B Q4

Avoid:

- 13B Q8 (will OOM)

Set:

OLLAMA_KEEP_ALIVE=30m

Load frequently used model at startup:

ollama pull mistral:instruct

------------------------------------------------------------------------

**7️⃣ Operational Lifecycle Model**

**AI Session Flow**

Start container\
Use NVMe runtime\
Modify MEMORY.md in /docker/runtime\
Before shutdown:\
sync to /home/sovereign/memory/\
Container stops\
Ephemeral wiped\
Durable persists

Implement shutdown hook:

- Copy runtime state to RAID

- Log session summary

------------------------------------------------------------------------

**🧠 Final System Schema (Clean View)**

RAID (/home)\
governance/\
memory/\
audit/\
backups/\
\
NVMe (/docker)\
runtime/\
sovereign-core\
broker\
\
AI Flow:\
SSH → Sovereign → Policy → Broker / WebDAV / IMAP / Ollama

------------------------------------------------------------------------

**🎯 What You Have After Phase 2**

- AI-assisted Linux admin

- Safe container lifecycle control

- AI file manipulation in Nextcloud

- Calendar integration

- Mail integration

- GPU-accelerated local cognition

- Clear NVMe vs RAID separation

- No docker.sock exposure to AI

- No public AI endpoint

This is production-grade without being overengineered.
