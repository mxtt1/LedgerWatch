# LedgerWatch

LedgerWatch is a synthetic bank-side Banking-as-a-Service assurance prototype. It demonstrates these controls:

1. real-time many-to-one matching between customer-level middleware events and the bank's independent transaction feed;
2. a bank-calculated expected customer-balance table that rolls forward only after a bank match;
3. end-of-day customer-level and aggregate reconciliation against an immutable balance batch received from the middleware provider;
4. Low / Medium / Critical risk levels for individual exceptions, each with a configurable resolution deadline; and
5. a daily risk checkpoint that decides whether to restrict the program, instead of restricting the moment an amount or count limit is crossed.

The bank is labelled **Evolve Bank & Trust** and the middleware provider **Synapse**, after the Evolve–Synapse case, and the fintech program is the fictional **Northstar Wallet**. These names are used for illustration only. All customers, accounts, transactions, balances and Synapse records are synthetic. The prototype does not connect to real banking systems, payment rails, or customer data.

## Contents

- [Hosted deployment](#hosted-deployment)
- [Local setup](#local-setup)
- [Control flow](#control-flow)
- [Data model](#data-model)
- [Scenarios and presentation](#scenarios-and-presentation)
- [Matching, exceptions and restriction](#matching-exceptions-and-restriction)
- [Limitations](#limitations)
- [Roles and screens](#roles-and-screens)
- [Project structure](#project-structure)

## Hosted deployment

The intended hosted platform is **Streamlit Community Cloud**. It deploys directly from GitHub and updates automatically when changes are pushed to the selected branch; no separate CI workflow is required.

| Setting | Value |
|---|---|
| Repository | `mxtt1/LedgerWatch` |
| Branch | `main` after the submitted version has been merged |
| Main file | `app.py` |
| Python | `3.12` |

After deployment, use the generated `*.streamlit.app` URL as the technology-demonstrator link and verify it in a signed-out or incognito browser. The **Edit** control shown to repository developers opens a GitHub Codespace; ordinary viewers cannot use it to modify this repository. The `.devcontainer` directory supports that optional Codespaces environment and is not a CI or deployment script.

Streamlit Community Cloud storage is ephemeral. The generated SQLite database can survive ordinary reruns but may be recreated after a reboot or redeployment. This is acceptable for the deterministic demonstrator because each scenario can be rebuilt from its tracked CSV fixtures. The hosted app should be treated as a single-presenter demonstration: all visitors share one SQLite state, and concurrent use is not supported.

## Local setup

Python 3.12 is recommended. From PowerShell:

```powershell
cd <path to this LedgerWatch folder>
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

For subsequent runs:

```powershell
cd <path to this LedgerWatch folder>
.\.venv\Scripts\Activate.ps1
python -m streamlit run app.py
```

If PowerShell blocks activation:

```powershell
.\.venv\Scripts\python.exe -m streamlit run app.py
```

The application creates `data/ledgerwatch.db` automatically on first launch. The generated database and local `.venv` are excluded from version control.

## Control flow

```text
Fintech customer action
        |
        +--> Synapse customer event(s) -- shared reference --+
        |                                                    |
        +--> Evolve bank transaction ------------------------+--> real-time match
                                                               |
                                                               +--> update bank-calculated
                                                                    customer balances

Synapse EOD customer-balance batch --> customer + aggregate reconciliation
Real-time exceptions + EOD result ----> daily risk checkpoint --> possible restriction
```

The two real-time inputs have different purposes. The Evolve transaction is evidence that money moved in the pooled FBO account. Synapse events provide the customer attribution needed to allocate that movement among beneficial owners. LedgerWatch does not treat either input alone as a complete reconciliation.

## Data model

LedgerWatch deliberately keeps these records separate:

| Record | Source | Update timing | Purpose |
|---|---|---|---|
| Middleware transaction events | Real-time scenario CSV | During playback | Supplies the middleware's customer attribution and claimed transaction details |
| Independent bank transactions | Bank-feed CSV | At each bank `posted_at` time | Confirms what money actually moved in the pooled FBO account |
| Bank-calculated expected customer balances | LedgerWatch/SQLite | Real time, but only after an exact match; arrival may occur within the grace period | The bank's independently rolled-forward beneficial-owner expectation |
| Middleware-reported customer balances | EOD batch CSV | Once, after scenario completion | Immutable third-party customer-balance snapshot used for daily reconciliation |
| Evolve FBO account balance | Bank ledger/SQLite | When an independent bank transaction posts | Aggregate cash position at the bank |

The bank-calculated expected customer-balance table **is updated in real time**. A middleware event alone cannot change it. The corresponding independent bank transaction must have posted and matched. The `customer_balance` included in a live middleware event is displayed as evidence but never overwrites the bank-calculated table or the EOD batch.

The bank transaction is the source of truth for cash movement, while each middleware event identifies an end user associated with that movement. Multiple events may carry the same Synapse reference and correspond to one aggregate bank transaction. LedgerWatch groups events by `middleware_txn_id`, joins that value to `middleware_reference` on the bank transaction, and compares the summed event amount, direction, program, and account. On a match, every customer event in the group rolls forward its customer's expected balance. The separate events are needed because the independent bank feed does not contain end-user IDs.

The mocked EOD batch represents the customer-level balance file received from Synapse (the middleware provider). It is not a continuously updated Synapse ledger on the dashboard. Once received, its rows are immutable for that scenario run.

The live records use synthetic customer IDs such as `U001`. A production design would keep a separately controlled mapping between a stable customer UUID and the legal beneficial owner. That identity/KYC store is outside this prototype.

## Scenarios and presentation

### Customer-transfer simulator

The **Simulate a customer transfer** form on the Reconciliation page walks one transfer through the whole flow at the current scenario time:

1. **Fintech app.** A customer (U001–U005) deposits into or withdraws from their wallet, for example $50.
2. **Synapse event.** Synapse records the transfer and sends LedgerWatch a middleware event with a reference such as `SIM-0001`. No bank posting exists yet, so the event enters `PENDING_MATCH`.
3. **Evolve bank posting.** Separately, the money movement reaches the FBO account as bank transaction `BTX-SIM-0001`, carrying the same reference.
4. **LedgerWatch matching.** LedgerWatch joins the event to the bank posting and checks amount, direction and account. On a match, the customer's bank-calculated expected balance rolls forward.

The form chooses what reaches the bank: the same amount (matches), a different amount (`AMOUNT_MISMATCH`), or nothing (stays pending, then `MISSING_BANK_TRANSACTION` after the grace period). While the program is restricted, the event is `BLOCKED_BY_RESTRICTION` and the bank posting is `REJECTED_BY_RESTRICTION`. The simulator is disabled while playback is running.

### Loading and playing a scenario

1. Open **Reconciliation** from the Streamlit sidebar.
2. Choose a scenario.
3. Select **Start** for automatic playback, or use **Next event / deadline** for presenter-controlled progression. Either one loads the chosen scenario first if it is not already loaded, and **Start** replays a finished scenario from the beginning. **Load scenario** resets the chosen scenario without playing it.
4. After playback reaches `COMPLETE`, select **Receive EOD batch**.
5. Select **Run daily reconciliation**.
6. Optionally use **Advance clock 4 hours** / **Advance clock 1 day** so resolution deadlines pass, then select **Run daily risk checkpoint**.

Loading a scenario resets its transactions, exceptions, balances, batches and audit state to deterministic opening data while retaining the saved control policy and automatic-restriction toggle. It then loads that scenario's real-time feed, independent bank records and EOD batch definition. **Reset** restarts the currently loaded scenario; it does not select a different scenario or restore policy defaults.

The application does not impose a presentation timer or automatically change pages. **Next event / deadline** has no real-time limit, so the presenter can explain each state before continuing.

### Scenario outcomes

The scenarios are numbered in demo order. Each one shows a single idea.

| # | Scenario | What it shows | EOD reconciliation result |
|---|---|---|---|
| 1 | Normal operation | Both arrival orders plus many-to-one matching: one Evolve posting waits for its Synapse event, while two customer events sharing `NORM-1002` total `$500` and match one `$500` Evolve transaction. No exceptions | Evolve FBO account balance, expected total and Synapse batch all `$10,000`; all customers match |
| 2 | Risk levels | Three separate discrepancies, one per level: a `$200` amount mismatch (**Low**), a `$1,500` transfer Evolve never receives (**Medium**) and a `$6,000` transfer Evolve records in the wrong direction (**Critical**). Exposure `$7,700` sets the label to `HIGH_RISK`; nothing is restricted | Evolve FBO account balance `$4,600`, expected `$10,000`, Synapse `$17,900`; three customer mismatches and a Critical aggregate exception |
| 3 | Missed resolution deadline | One `$5,000` transfer Evolve never receives (Critical, 4 hours to resolve, due 14:05:30). The scenario clock jumps to 13:00 (1h 5m left), 14:00 (5m left) and 14:30 (**OVERDUE**). No checkpoint runs, so the program is overdue but **not** locked | Evolve FBO account balance and expected total `$10,000`, Synapse `$15,000`; one customer mismatch |
| 4 | Lockdown at the daily checkpoint | The same Critical exception left unresolved past its deadline. The scheduled **17:00 daily checkpoint** applies the missed-deadline rule and **restricts** the program, which pauses playback. Select **Resume** in the playback controls (not Management service resumption): a later `$125` transfer is `BLOCKED_BY_RESTRICTION` and Evolve's posting is `REJECTED_BY_RESTRICTION` | Evolve FBO account balance and expected total `$10,000`, Synapse `$15,000`; one customer mismatch |
| 5 | Timing difference | A `$700` event waits in `PENDING_MATCH`; the Evolve posting arrives within the five-minute grace period and rolls expected U001 to `$4,700` | All totals `$10,700`; no exception |
| 6 | Synapse feed failure | Heartbeats stop; a `STALE_FEED` exception (Medium) at the warning timeout, then a `FEED_FAILURE` exception (Critical). With automatic restriction enabled—the default—the critical failure restricts the program immediately | A `$10,000` EOD batch can still match; that does not mean the real-time feed was available or clear its exceptions |

Scenario CSV timestamps represent business time. Autoplay fast-forwards that timeline using a short fixed presentation delay. The matching grace period, feed thresholds and resolution deadlines use scenario minutes or hours, not wall-clock demo seconds. Scenarios 3 and 4 assume the default 4-hour Critical deadline; if you change it, the countdown points shift.

A `DAILY_CHECKPOINT` row in a scenario CSV runs the daily risk checkpoint at that scenario time, as a scheduled end-of-day control. It is not a Synapse message. Scenario 4 uses it; you can also run the checkpoint manually in any scenario.

## Daily reconciliation

The daily control cannot run until the EOD batch has been received. It performs both checks:

1. **Customer-level:** compare every bank-calculated expected customer balance with the corresponding Synapse-reported EOD balance.
2. **Aggregate:** compare the sum of the Synapse batch with the bank's closing FBO balance.

The screen retains the batch identifier, as-of time, record count, received time, each customer result, expected total, reported total, FBO balance, aggregate difference, and final outcome. `EXCEPTION` on a customer row means that the row failed reconciliation; it does not create a separate workflow exception for that customer. A failed batch creates or updates one `AGGREGATE_BALANCE_MISMATCH` workflow exception covering the reconciliation failure.

Transaction matching and daily reconciliation are separate controls. An aggregate total can match while customer allocations are wrong, and a valid EOD balance batch does not prove that a real-time feed remained available.

## Playback controls

- **Load scenario** — clears prior scenario state and loads the selected data.
- **Start** — begins sequential automatic replay.
- **Pause** — stops autoplay at the current position.
- **Resume** — continues paused autoplay.
- **Next event / deadline** — processes one event or advances to the next bank posting or control deadline.
- **Reset** — restores the currently loaded scenario to its opening state.

Autoplay runs on the Reconciliation page, not as a background service. Pause it or let it finish before navigating away. Program, exception, batch, and audit state remain persisted in SQLite.

## Matching, exceptions and restriction

Default prototype thresholds are editable under **Configuration & service controls**.

| Condition | Initial result | Confirmed exception |
|---|---|---|
| One or more events have no matching bank transaction | The reference group remains `PENDING_MATCH` | One `MISSING_BANK_TRANSACTION` for the group after the five-minute grace period |
| Posted bank transaction has no matching intermediary event | Two-way pending match | `MISSING_INTERMEDIARY_EVENT` after the same five-minute grace period |
| Grouped event total differs from the bank record | Waits when additional events could complete the total; otherwise immediate | One `AMOUNT_MISMATCH` for the reference group no later than grace expiry |
| Direction differs | Immediate | `DIRECTION_MISMATCH` |
| FBO account or program differs | Immediate | `ACCOUNT_MISMATCH` |
| Required event data is missing or invalid | Immediate | Record is marked `INPUT_REJECTED` and audited; no workflow exception is created |
| EOD customer or aggregate reconciliation fails | Immediate | `AGGREGATE_BALANCE_MISMATCH` |
| No heartbeat for two scenario minutes | Warning | `STALE_FEED` |
| No heartbeat for five scenario minutes | Critical feed condition | `FEED_FAILURE` |

Exceptions use exactly four statuses: `OPEN`, `INVESTIGATING`, `RESOLVED`, and `OVERRIDDEN`.

`OPEN` and `INVESTIGATING` are unresolved states: their amounts contribute to exposure and their resolution deadlines continue to run. `RESOLVED` and `OVERRIDDEN` are terminal states and no longer contribute to unresolved exposure. Reducing exposure does not automatically lift an existing restriction; the restriction remains latched until Management resumes service with a rationale.

Matching is two-way and uses the same configured grace period in both directions. All pending intermediary events with the same Synapse reference are treated as one group and compared with one bank transaction carrying that reference. A successful group match updates every affected customer's expected balance; a failed group creates one exception rather than one exception per event. Intermediary-first pending state is stored on the processed events. Bank-first pending state is derived from the posted bank transaction and the absence of corresponding events, so no separate pending-match table is required.

The matcher also handles these arrival-order edge cases:

- A bank transaction may arrive first. If an exact event group arrives before the grace period expires, it matches normally.
- If a `MISSING_INTERMEDIARY_EVENT` exception has already been created, a late exact event group updates the affected customer balances and resolves that exception.
- An event arriving after its reference has already matched is not applied to customer balances and does not change the FBO balance. It is marked `CONFIRMED_EXCEPTION`, creates an `AMOUNT_MISMATCH`, and writes `REFERENCE_REUSED_AFTER_MATCH` evidence while preserving the original match.
- An event group whose total is below the bank amount remains pending until another same-reference event completes it or the grace period expires. A total above the bank amount is an immediate mismatch.
- Partial customer allocation is not performed. Customer balances roll forward only when the complete pending group exactly equals the bank transaction and the program, account and direction also agree.

The Exceptions page links each workflow exception to its associated Synapse event group, Evolve bank transaction and relevant audit entries. Assignment and status changes are audited. The prototype assumes an analyst has performed the required review before selecting `RESOLVED`; it does not implement a separate approval workflow or evidence-upload system.

Heartbeat monitoring does not reconcile stale data. It prevents a silent or unavailable intermediary feed from being mistaken for a successfully reconciled period. Previously completed matches remain visible. A new heartbeat restores the health indicator, but any `STALE_FEED` or `FEED_FAILURE` exception remains unresolved until it is reviewed and changed to `RESOLVED` or `OVERRIDDEN`.

To avoid double-counting the same underlying discrepancy, EOD aggregate mismatch exposure is not added to transaction-exception exposure. The risk display uses the greater applicable control amount.

## Risk levels and resolution deadlines

Every exception is given a risk level when it is created. The level is shown with a colour (red Critical, amber Medium, blue Low) on the Dashboard and the Exception management page, separately from the overall program status.

| Exception | Low | Medium | Critical |
|---|---|---|---|
| Financial (amount, direction, account, missing record, aggregate) | under `$1,000` | `$1,000` to under `$5,000` | `$5,000` and above |
| `STALE_FEED` | | always | |
| `FEED_FAILURE` | | | always |

The two amount bands (Medium from, Critical from) are editable under **Configuration & service controls**; the values above are the illustrative defaults. They level **one exception by its own amount**. They are separate from the exposure controls, which use the **total** of all unresolved exceptions to set the program-wide `WARNING` / `HIGH_RISK` label and the daily checkpoint's exposure rule. A single `$5,000` exception is Critical even while total exposure is below the warning line. Changing the bands affects exceptions created afterwards; existing exceptions keep the level and deadline they were given.

Each level has a resolution deadline, editable in hours under **Configuration & service controls**. The illustrative defaults are:

| Risk level | Time allowed to resolve |
|---|---|
| Critical | 4 hours |
| Medium | 1 day |
| Low | 3 days |

The deadline is calculated and stored on the exception when it is created (`due_at`), so a later policy change does not move existing deadlines. If an aggregate mismatch grows into a higher level, its deadline can only get shorter. The deadline keeps running while the exception is `OPEN` or `INVESTIGATING`; starting an investigation does not pause it. The Exception management page shows each exception's time remaining, or how long it has been overdue, in scenario time.

## Daily risk checkpoint and restriction

Live monitoring still detects, classifies and tracks every exception in real time, and still moves the program label to `WARNING` or `HIGH_RISK` as exposure grows. It no longer restricts the program when an amount or count limit is crossed.

Restriction is decided by the daily risk checkpoint: either a scheduled `DAILY_CHECKPOINT` row in the scenario (scenario 4) or the **Run daily risk checkpoint** button on the Reconciliation page. It applies these rules and restricts the program if any one is breached:

| Rule | Default limit |
|---|---|
| Missed resolution deadline | any unresolved exception past its deadline |
| Unresolved financial exposure | `$10,000` |
| Unresolved exception count | `10` |

Each checkpoint run is stored in the `daily_checkpoints` table and written to the audit log with every rule's value, limit and outcome. If automatic restriction is turned off, the checkpoint still records which rules were breached but does not restrict.

A critical feed failure (no heartbeat for five scenario minutes) is evaluated immediately rather than waiting for the daily checkpoint, because the bank cannot monitor the program without the feed. It restricts immediately only when automatic restriction is enabled. If that toggle is off, the Critical `FEED_FAILURE` exception and audit evidence are still created without restricting the program.

Once restricted, new middleware events are `BLOCKED_BY_RESTRICTION` and their bank postings are `REJECTED_BY_RESTRICTION`; neither changes the FBO or expected balances. Management is the only prototype role that can resume service. Resumption requires a rationale and changes every unresolved `OPEN` or `INVESTIGATING` exception to `OVERRIDDEN`. Those terminal exceptions stop contributing to unresolved exposure. The action and rationale are written to the audit log.

To let deadlines pass during a demo, **Advance clock 4 hours** and **Advance clock 1 day** move scenario time forward after playback has finished. Heartbeats are assumed to continue while the clock advances, except in the feed-failure scenario.

## Recommended demonstration

1. **Dashboard.** Explain the parties: Northstar Wallet (fintech app), Synapse (middleware) and Evolve Bank & Trust (holds the FBO account). Point out the FBO balance, Evolve's expected total and the absent EOD batch.
2. **Scenario 1 · Normal operation.** Play it to completion. Show expected balances changing only after an Evolve match. Receive the EOD batch and run daily reconciliation: everything at `$10,000`.
3. **Simulate a customer transfer.** Deposit `$50` for U001 with the same amount reaching Evolve and walk through the four steps. Repeat with **A different amount** to show an exception being raised.
4. **Scenario 2 · Risk levels.** Play it, then open **Exception management**. Show the Low, Medium and Critical exceptions side by side, their colours, and their different deadlines (3 days, 1 day, 4 hours). Note the program is `HIGH_RISK` but not restricted.
5. **Investigation.** Assign the Critical exception and move it to `INVESTIGATING`. Point out the deadline keeps running.
6. **Scenario 3 · Missed resolution deadline.** Use **Next event / deadline** and stop at each clock jump to show the time remaining fall to 1h 5m, 5m and then OVERDUE. The program is still not locked, because restriction is only decided at the daily checkpoint.
7. **Scenario 4 · Lockdown.** Select **Start**. Playback pauses at 17:00 when the checkpoint restricts the program. Show the restriction banner and the checkpoint rules. Select **Resume** in the playback controls to show the `$125` transfer blocked and Evolve's posting rejected; this does not lift the restriction.
8. **Resumption.** On **Configuration & service controls**, switch to Management and resume service with a rationale to show the override evidence.
9. If time permits, use **Scenario 5 · Timing difference** for grace-period protection or **Scenario 6 · Synapse feed failure** for the immediate feed-failure restriction.

## Limitations

- **Simulated inputs.** The scenario feeds (`data/scenarios`), bank postings (`data/bank_transactions.csv`) and EOD batches (`data/batches`) are synthetic CSVs. The customer-transfer simulator creates the Synapse event and the bank posting itself. Nothing connects to a real bank, payment rail or Synapse system.
- **Configurable policies.** Grace period, heartbeat timeouts, risk-level amount bands, resolution deadlines per level, warning and high-risk labels, checkpoint exposure and count limits, aggregate tolerance and automatic restriction are all editable and audited. Feed exceptions are levelled by type, not by a configurable band.
- **Scenario time.** All deadlines run on the scenario clock, not the wall clock. The clock only moves through playback, **Next event / deadline** and the **Advance clock** buttons.
- **Checkpoint timing.** The daily checkpoint runs when a scenario reaches a `DAILY_CHECKPOINT` row or when a user selects it. There is no real scheduler.
- **One program.** The prototype monitors one fintech program and one FBO account.
- **Not authentication.** The acting-role selector records who did what; it is not a login or a complete authorization system.
- **Grouped-reference completeness.** Many-to-one matching assumes that an exact event total represents the complete group. A production integration should also require an expected event count, sequence/checksum, settlement manifest or explicit group-complete marker.
- **Simplified payment lifecycle.** Returns, reversals, chargebacks, fees, partial corrections, revised events and mixed-direction settlement groups are not modelled. A production system would require a controlled adjustment workflow.
- **Prototype storage.** SQLite is suitable for a single-presenter demonstration, not concurrent production processing. Streamlit Community Cloud visitors share one database, and hosted filesystem state may be reset.
- **Audit assurance.** The application writes an audit trail, but SQLite is not tamper-evident or write-once storage. Production evidence would need stronger access controls, retention and integrity protection.
- **Data and integration security.** The prototype uses synthetic records and does not implement source authentication, encryption/key management, idempotency controls, KYC/AML screening, privacy workflows or a customer UUID-to-identity store.
- **Numerical precision.** Demo amounts use SQLite/Python floating-point values. Production financial calculations should use integer minor units or fixed-precision decimals.
- **Verification scope.** This repository is a technology demonstrator and does not include a production assurance, load, security or automated regression suite.

## Roles and screens

### Roles in this prototype

The acting-role selector contains exactly three roles:

- **Operations** represents the operator running scenarios and reviewing transaction or feed activity.
- **Risk / Compliance** represents the reviewer assessing exposure, exceptions, and control evidence.
- **Management** is the only role allowed to resume a restricted program. Resumption requires a rationale and changes all unresolved exceptions to `OVERRIDDEN`.

Exceptions are assigned to named synthetic staff members—Maya Chen (Reconciliation Analyst), Jordan Lee (Operations), Priya Shah (Risk / Compliance), or Elena Ruiz (Management)—so ownership identifies one accountable person rather than an entire function. These names are illustrative and do not represent real employees. There is no separate Auditor role in the current UI. Apart from Management-only service resumption, the acting-role selector primarily records who performed an action in the audit trail; it is not authentication or a complete authorization system.

### Screens

- **Dashboard** — FBO balance, real-time bank-calculated expected balances, latest Synapse batch state, bank transaction feed, blocked evidence, feed health, risk metrics, exceptions by risk level, and the last daily checkpoint.
- **Reconciliation** — scenario playback, customer-transfer simulator, real-time matching, pending/blocked evidence, expected balances, bank transactions, EOD batch receipt, customer comparison, aggregate reconciliation, clock controls and the daily risk checkpoint.
- **Exception management** — risk-level summary, filters, ownership, linked Synapse events and Evolve record, exception-specific evidence log, resolution deadlines with time remaining, resolution, and full audit history.
- **Configuration & service controls** — editable thresholds, automatic restriction, audit trail, and Management-only resumption.

## Resetting

Use **Load scenario** or **Reset** in the interface. To reset the complete demo database to the normal scenario from PowerShell:

```powershell
python -m ledgerwatch.seed
```

## Project structure

```text
LedgerWatch/
|-- app.py                         Dashboard
|-- pages/                         Reconciliation, Exceptions, Configuration
|-- ledgerwatch/                   Database and business-rule modules
|-- data/scenarios/                Six synthetic real-time Synapse feeds
|-- data/batches/                  Six mocked immutable Synapse EOD balance batches
|-- data/bank_transactions.csv     Scenario-specific independent bank feed
|-- data/ledgerwatch.db            Generated runtime SQLite state
|-- requirements.txt               Runtime dependencies
|-- .streamlit/config.toml         Application theme
|-- .devcontainer/                 Optional GitHub Codespaces environment
|-- .venv/                         Local environment; excluded from version control
```

This prototype demonstrates a possible control design; it does not assert that a particular workflow alone satisfies any final law, regulation, or supervisory requirement.
