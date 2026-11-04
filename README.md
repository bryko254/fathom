# Fathom

Fathom is an autonomous database copilot. You connect a SQL database, upload a SQLite file or a dump, or open a bundled ledger. You ask in plain English. The agent explores the schema, writes SQL, runs it in a disposable sandbox, checks the result, and fixes the query when the sandbox says it is wrong. The answer is a table, a chart, a short explanation, and a dashboard you can refresh.

It is built for the [Vultr Agent Rush hackathon](https://lablab.ai/ai-hackathons/vultr-hackathon), Challenge 1 (Blast Radius Zero): a web agent that does real work inside a sandbox on Vultr, with the model on Vultr Serverless Inference.

Submissions close **8 Nov 2026, 22:00 UTC**.

## What the judges should see

- A production-style web app, not a chat wrapper.
- Multi-step work that is executed: schema reads, SQL, Python, then a corrected query.
- At least one visible failure. On Harbor SaaS the first query uses `canceled_at`, which does not exist. The retry joins product events and the sanity check flags the fanout. The third query is the one behind the chart.
- A containment moment inside the question timeline, not only on a separate screen. The analyst emits `DROP TABLE`. The guard blocks it before the sandbox. The next step is Python, in a fresh container, that tries to read a host-only file, the inference key, and `1.1.1.1`. The trace is marked **Blocked**, and the container is removed. The **Containment drill** button repeats the proof, including an infinite loop.
- The SQL that produced the answer, on screen.

## Architecture

The Vultr VM is the control plane. It plans and dispatches. It does not run the analyst's SQL inside the API process.

```mermaid
flowchart LR
  Browser[Browser] --> Web[Next.js on the Vultr VM]
  Web --> API[FastAPI control plane]
  API --> Guard[Read-only SQL guard]
  API --> LLM[Vultr Serverless Inference]
  Guard --> Run[Ephemeral sandbox container]
  Run --> Copy[(Session SQLite copy)]
  API --> Source[(Optional Vultr Managed Database)]
  Source --> Copy
  API --> AppDB[(Dashboards on the VM)]
```

One question, in order:

1. The browser streams server-sent events from the control plane.
2. If `LLM_API_KEY` is set, the control plane calls `LLM_BASE_URL` (Vultr Serverless Inference, OpenAI-compatible). With no key, a fallback policy chooses the next statement. Either way, the sandbox executes it.
3. The guard parses the statement with SQLGlot. Reads proceed. A single `INSERT`, `UPDATE`, or `DELETE` waits for an explicit confirmation token. `DROP`, `ALTER`, `CREATE`, `ATTACH`, `PRAGMA`, `load_extension`, and nested writes are refused.
4. The control plane starts a new container from `fathom-sandbox:local` and destroys it when the step returns (`docker run --rm`). The container has `--network none`, a read-only root, `--cap-drop ALL`, no-new-privileges, a pid limit, and a memory and CPU cap. The only data mount is a Docker volume holding the session copy, and query containers mount it read-only. SQLite is opened with `mode=ro&immutable=1`.
5. A sanity check looks at the rows the sandbox returned: zero rows, truncation, mostly-null columns, and join fanout (many rows per id). Warnings go back to the analyst, which runs a different statement.
6. Python in the same kind of container can reduce those rows. Imports other than a small allow-list are blocked unless the process is the containment drill, which sets `FATHOM_DRILL=1` and still has no host mounts and no network.
7. The answer, chart, and dashboard are built from those rows. Refresh re-runs the saved SQL in a new container.

Credentials for Postgres or MySQL never enter the sandbox. The control plane opens the connection, sets the session read-only, copies up to `SNAPSHOT_ROW_CAP` rows per table into SQLite, and the sandbox only sees that file.

The three bundled ledgers are fictional and generated with a fixed seed:

| Ledger | What it is | Suggested question |
| --- | --- | --- |
| Harbor SaaS | Subscriptions, product events, support tickets | Which customers churned last quarter and why? |
| Northline Commerce | Orders and line items, 2025 through Nov 2026 | Build me a sales dashboard for this year |
| PesaLedger | A Kenyan-style mobile-money ledger. Not Safaricom and not M-Pesa. | Where are withdrawals failing, and how much fee revenue did we collect last month? |

The ledger clock is **4 Nov 2026**, so "last quarter" is 1 Jul–30 Sep 2026 even if the VM's clock is later.

## Run it locally

Docker is required for the path this hackathon judges. Unit tests use a subprocess worker and do not need Docker.

```bash
cp .env.example .env
docker compose up --build
```

- App: http://127.0.0.1:43123
- API: http://127.0.0.1:43124/health

Leave `LLM_API_KEY` empty and the header reads **Fallback analyst**. The churn question still fails once, warns once, then returns a chart, an answer, and a dashboard. Set the key to send the same tool loop to Vultr.

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pytest
```

## What Brian does on Vultr

The deployment target is already provisioned: **Ubuntu 24.04, 1 vCPU, 2 GB RAM, Docker 29 and Compose v5**. The running stack fits that machine. The image build does not, unless the VM has a little swap, because the Next.js compile spikes above 2 GB. Add the swap once, then build.

Vultr services this app uses:

- **Cloud Compute** — the VM is the control plane. It plans, holds credentials, and dispatches work. It does not execute the analyst's SQL in the API process.
- **Serverless Inference** — every agent LLM call. OpenAI-compatible base URL `https://api.vultrinference.com/v1`. This is the default. The model id is `LLM_MODEL` (default `deepseek-v4.1-flash-1017`). No Vultr GPU is required, and none are available for this event.
- **Managed Database** (optional) — Postgres the copilot can snapshot. The password stays on the VM. The sandbox receives only a SQLite copy.

### 1. Inference key

The VM already exists. In the Vultr console, create a **Serverless Inference** API key. The base URL is `https://api.vultrinference.com/v1`. The default model is `deepseek-v4.1-flash-1017`. The live model list is at [https://api.vultrinference.com/docs](https://api.vultrinference.com/docs). If the hackathon gift code is not on the account yet, apply it under Billing. This event has no Vultr GPUs.

### 2. SSH in, add swap, get the code

```bash
ssh root@YOUR_VM_IP
docker --version
docker compose version
fallocate -l 2G /swapfile
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
git clone YOUR_REPO_URL /opt/fathom
cd /opt/fathom
cp .env.example .env
```

Edit `.env`:

```bash
LLM_BASE_URL=https://api.vultrinference.com/v1
LLM_API_KEY=the-key-from-the-vultr-console
LLM_MODEL=deepseek-v4.1-flash-1017
LLM_PROVIDER=auto
SANDBOX_MODE=docker
WEB_PORT=80
```

`WEB_PORT=80` publishes the app on port 80. Leave `API_PORT` at `43124` and do not open it publicly. The browser talks only to Next.js. Next.js proxies `/backend` to the API on `127.0.0.1` inside the same network namespace. Query containers are still separate: no network, read-only root, and they are removed after each step. `SANDBOX_MEMORY` defaults to `128m` so a query container fits beside the API and the web process on a 2 GB VM.

### 3. Start it

```bash
docker compose up -d --build
docker compose ps
curl -s http://127.0.0.1:43124/health
```

`health` should report `"sandbox": "docker"` and `"provider": "openai"`.

Open `http://YOUR_VM_IP/` (or `:43123` if you left the default port). If the cloud firewall is on, allow TCP 22 and TCP 80 (or 43123).

```bash
ufw allow OpenSSH
ufw allow 80/tcp
ufw enable
```

### 4. Optional: Vultr Managed Database

Use this when you want the copilot on a real Postgres instead of only the bundled files. The sandbox still never sees the password.

1. In the Vultr console, deploy a Managed PostgreSQL. Add the VM's public IP to its trusted sources. Turn on SSL.
2. From a `psql` session as the admin user:

```sql
CREATE ROLE fathom_ro LOGIN PASSWORD 'choose-a-long-password';
GRANT CONNECT ON DATABASE defaultdb TO fathom_ro;
GRANT USAGE ON SCHEMA public TO fathom_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO fathom_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO fathom_ro;
```

3. In the Fathom UI, paste a URL of this shape and choose **Snapshot into the sandbox**:

```text
postgresql://fathom_ro:choose-a-long-password@HOST:PORT/defaultdb?sslmode=require
```

The control plane sets `default_transaction_read_only`, copies tables (capped by `SNAPSHOT_ROW_CAP`, default 20,000 rows each), and seeds a volume. Later questions run against that copy. **Refresh** with resync is available on `POST /dashboards/{id}/refresh` with `{"resync": true}` when the session was opened from a connection string. The password stays in the control plane's session store on the VM disk. It is not returned by the API and it is not passed into the sandbox.

### 5. Record and submit

Follow [DEMO.md](DEMO.md). Submit on lablab.ai with the public GitHub repo, the public URL, the video, and slides. The Vultr explanation is this README. Sharing on Vultr Community is optional and does not replace the lablab.ai submission.

## Environment

| Variable | Purpose |
| --- | --- |
| `LLM_BASE_URL` | OpenAI-compatible base URL. Vultr is `https://api.vultrinference.com/v1`. |
| `LLM_API_KEY` | Inference key. Empty selects the fallback analyst. |
| `LLM_MODEL` | Model id. Unknown ids on Vultr are routed to the default chat model; check the response if you typed it by hand. |
| `LLM_PROVIDER` | `auto` (default), `mock`, or `openai`. |
| `SANDBOX_MODE` | `docker` on the VM. `subprocess` is for tests only and is not a containment boundary. |
| `SANDBOX_MEMORY` / `SANDBOX_CPUS` | Caps on every sandbox container. |
| `QUERY_TIMEOUT_S` | Wall clock inside the worker, plus a kill from the control plane. |
| `MAX_ROWS` | Rows returned to the analyst. Extra rows set a truncation warning. |
| `SNAPSHOT_ROW_CAP` | Rows copied per table from an external database. |
| `WEB_PORT` / `API_PORT` | Host ports. |

## Project layout

```text
frontend/     Next.js UI. Streams the trace, renders charts, saves dashboards.
backend/app   Control plane: guard, agent loop, snapshots, API.
backend/sandbox/worker.py
              The only program that runs in the sandbox image.
docker-compose.yml
DEMO.md       The two-and-a-half minute script.
```

## License

MIT. See [LICENSE](LICENSE).
