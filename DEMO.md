# Fathom demo script

Two and a half minutes. Record the browser and, if you can, a corner of `docker ps` so the containers appearing and disappearing are visible.

The ledger clock is **4 Nov 2026**. "Last quarter" means 1 Jul 2026 through 30 Sep 2026. "Last month" means October 2026. "This year" means 2026.

## Before you hit record

1. `docker compose up -d --build` on the Vultr VM, with `LLM_API_KEY` set (see the README).
2. Open the public URL. The header should say `Inference · <model>` and `Sandbox · docker`.
3. Have a terminal on the VM with `watch -n1 'docker ps --filter label=fathom.role=sandbox'` if you want the containment shot in the same frame.

If the key is empty, the header says `Fallback analyst`. The sandbox work is still real, and the self-correction still happens. For the submission video, use Vultr Serverless Inference.

## Script

**0:00 — the problem.**  
"Analysts still wait on someone who can write SQL. Fathom is that analyst, and you can watch it work. It runs on a Vultr VM. The model is Vultr Serverless Inference. Every query and every Python check runs in a container that is destroyed when the step finishes."

**0:20 — ask the question.**  
Click **Harbor SaaS**, then the suggested question: "Which customers churned last quarter and why?"  
Stay on the trace. Call out the steps as they appear:

- It reads `sqlite_master` and samples live rows.
- **Containment, in the same timeline.** The analyst emits `DROP TABLE accounts`. The step is marked **Blocked**: the guard refused it before a sandbox existed. The next step is Python that tries to read a host-only file, `LLM_API_KEY`, and `1.1.1.1`. That container has no key, cannot see the host file, has no network, and is removed when the step ends. Leave this on screen for a few seconds. It is the containment moment.
- The first analysis SQL uses `canceled_at`. The sandbox returns "no such column". The column is `ended_at`.
- The retry joins every product event. The sanity check flags join fanout, and a Python snippet in the sandbox counts rows per account.
- The corrected query aggregates by cancel reason. That is the query behind the chart.

**1:15 — the answer.**  
Read the sentence with the account count and the MRR figure. Point at the bar chart and the table. Open the dashboard and hit **Refresh**. Say that refresh re-runs the saved SQL in a new sandbox, it does not replay a cached screenshot.

**1:40 — a second ledger, faster.**  
Switch to **PesaLedger** (fictional, not Safaricom) and ask where withdrawals are failing and what fees came in last month. The first query uses `amount`. The retry looks for status `FAILED`. The stored status is `failed`. The chart is counties, and the answer is fee revenue in shillings.

**2:05 — containment, again, on purpose.**  
The timeline already showed the block. If you want a second beat, click **Containment drill** and say what each line means:

- `DROP TABLE` is refused by the guard and never enters the sandbox.
- The infinite loop is killed by the time limit and the container is removed (`--rm`).
- The host marker file is not readable, the write does not appear on the host, and the network is `none`, so the socket to 1.1.1.1 fails.

**2:25 — close.**  
"Read-only by default. Writes need an explicit confirm, and DDL stays blocked. Credentials for a Vultr Managed Database stay on the VM; the sandbox only sees a SQLite copy. The SQL that produced the answer is on screen."

## Pitch bullets

- Analysts lose days translating a question into SQL, then don't trust the number. Fathom shows the SQL and the failed attempts.
- The agent does the work: schema, SQL, Python, a sanity check, a fix, a chart, and a dashboard you can refresh.
- Containment is the product, not a footnote. Each step is a fresh container with no network, a read-only root, dropped capabilities, and a memory cap. The drill is the proof.
- It fits a finance or success team on Monday: churn, revenue, and a payments ledger, with a path to the customer's own Postgres via a read-only snapshot.
- Built for this hackathon's stack: Vultr Cloud Compute as the control plane, Vultr Serverless Inference for the model, optional Vultr Managed Database as the source.

## What not to claim

- The fallback analyst (empty `LLM_API_KEY`) is a scripted policy. Do not call it the Vultr model. The SQL, Python, errors, and charts are still executed.
- PesaLedger is synthetic. It is not M-Pesa and not a Safaricom integration.
- Subprocess sandbox mode is for unit tests. The video should be Docker mode.
