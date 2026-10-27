# LedgerWatch

LedgerWatch is a synthetic bank-side Banking-as-a-Service assurance prototype. It demonstrates three distinct controls:

1. real-time matching between middleware transaction events and the bank's independent transaction feed;
2. a bank-calculated expected customer-balance table that rolls forward only after a bank match; and
3. end-of-day customer-level and aggregate reconciliation against an immutable balance batch received from the middleware provider.

All programs, accounts, users, transactions, balances, and middleware records are synthetic. The prototype does not connect to real banking systems, payment rails, or customer data.

## Setup and run

From PowerShell:

```powershell
cd C:\Users\mattl\IS4301Projects\LedgerWatch
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

The `.venv` already exists in this workspace. For subsequent runs:

```powershell
cd C:\Users\mattl\IS4301Projects\LedgerWatch
.\.venv\Scripts\Activate.ps1
python -m streamlit run app.py
```

If PowerShell blocks activation:

```powershell
.\.venv\Scripts\python.exe -m streamlit run app.py
```

## The data model

LedgerWatch deliberately keeps these records separate:

| Record | Source | Update timing | Purpose |
|---|---|---|---|
| Middleware transaction events | Real-time scenario CSV | During playback | Claims that a customer transaction occurred; the event-reported resulting balance is informational only |
| Independent bank transactions | Bank-feed CSV | At each bank `posted_at` time | Confirms that money actually moved in the FBO account |
| Bank-calculated expected customer balances | LedgerWatch/SQLite | Real time, but only after an exact or within-grace bank match | The bank's independently rolled-forward beneficial-owner expectation |
| Middleware-reported customer balances | EOD batch CSV | Once, after scenario completion | Immutable third-party customer-balance snapshot used for daily reconciliation |
| Bank FBO balance | Bank ledger/SQLite | When an independent bank transaction posts | Aggregate cash position at the bank |

The bank-calculated expected customer-balance table **is updated in real time**. A middleware event alone cannot change it. The corresponding independent bank transaction must have posted and matched. The `customer_balance` included in a live middleware event is displayed as evidence but never overwrites the bank-calculated table or the EOD batch.

The mocked EOD batch represents the customer-level balance file received from Synapse (the middleware provider). It is not a continuously updated Synapse ledger on the dashboard. Once received, its rows are immutable for that scenario run.

## Loading and playing a scenario

1. Open **Reconciliation** from the Streamlit sidebar.
2. Choose a scenario.
3. Select **Load scenario**. Changing the dropdown alone does not load it.
4. Select **Start** for automatic playback, or use **Next event / deadline** for presenter-controlled progression.
5. After playback reaches `COMPLETE`, select **Receive EOD batch**.
6. Select **Run daily reconciliation**.

Loading a scenario resets SQLite to deterministic opening data and loads that scenario's real-time feed, independent bank records, and EOD batch definition. **Reset** restarts the currently loaded scenario; it does not select a different scenario.

The application does not impose a presentation timer or automatically change pages. **Next event / deadline** has no real-time limit, so the presenter can explain each state before continuing.

## Scenario outcomes

| Scenario | Real-time result | EOD reconciliation result |
|---|---|---|
| Normal operation | Demonstrates both arrival orders: a bank-first transaction waits for its intermediary event, while one intermediary-first event waits for its bank transaction; both match within grace | Bank FBO, expected total, and Synapse batch all equal `$10,000`; all customer rows match |
| Accumulating discrepancies | Five credits have no bank records; after grace they become exceptions and the program is restricted; a later `$125` intermediary attempt is blocked and its corresponding bank transaction is `REJECTED_BY_RESTRICTION` before posting | Bank FBO and expected total remain `$10,000`; Synapse batch totals `$20,700`; bank-minus-batch is `-$10,700`, with five customer mismatches |
| Intermediary feed failure | Heartbeats stop; stale-feed warning progresses to a critical feed-failure exception and restriction | The `$10,000` balance batch can still match the FBO and expected total; this does not clear the independent feed-failure exception |
| Timing difference | A `$700` event waits in `PENDING_MATCH`; the bank posting arrives within the five-minute grace period and rolls expected U001 to `$4,700` | Bank FBO, expected total, and Synapse batch all equal `$10,700`; no exception |

Scenario CSV timestamps represent business time. Autoplay fast-forwards that timeline using a short fixed presentation delay. The matching grace period, feed thresholds, and exception age use scenario minutes or hours—not wall-clock demo seconds.

## Daily reconciliation

The daily control cannot run until the EOD batch has been received. It performs both checks:

1. **Customer-level:** compare every bank-calculated expected customer balance with the corresponding Synapse-reported EOD balance.
2. **Aggregate:** compare the sum of the Synapse batch with the bank's closing FBO balance.

The screen retains the batch identifier, as-of time, record count, received time, each customer result, expected total, reported total, FBO balance, aggregate difference, and final outcome. A failure creates or updates an `AGGREGATE_BALANCE_MISMATCH` exception.

Transaction matching and daily reconciliation are separate controls. An aggregate total can match while customer allocations are wrong, and a valid EOD balance batch does not prove that a real-time feed remained available.

## Playback controls

- **Load scenario** — clears prior scenario state and loads the selected data.
- **Start** — begins sequential automatic replay.
- **Pause** — stops autoplay at the current position.
- **Resume** — continues paused autoplay.
- **Next event / deadline** — processes one event or advances to the next bank posting or control deadline.
- **Reset** — restores the currently loaded scenario to its opening state.

Autoplay runs on the Reconciliation page, not as a background service. Pause it or let it finish before navigating away. Program, exception, batch, and audit state remain persisted in SQLite.

## Exceptions and restriction

Default prototype thresholds are editable under **Configuration & service controls**.

| Condition | Initial result | Confirmed exception |
|---|---|---|
| No matching bank transaction | `PENDING_MATCH` | `MISSING_BANK_TRANSACTION` after the five-minute grace period |
| Posted bank transaction has no matching intermediary event | Two-way pending match | `MISSING_INTERMEDIARY_EVENT` after the same five-minute grace period |
| Amount differs from the bank record | Immediate | `AMOUNT_MISMATCH` |
| Direction differs | Immediate | `DIRECTION_MISMATCH` |
| FBO account or program differs | Immediate | `ACCOUNT_MISMATCH` |
| Required event data is missing or invalid | Immediate | `DATA_VALIDATION_ERROR` |
| EOD customer or aggregate reconciliation fails | Immediate | `AGGREGATE_BALANCE_MISMATCH` |
| No heartbeat for two scenario minutes | Warning | `STALE_FEED` |
| No heartbeat for five scenario minutes | Critical feed condition | `FEED_FAILURE` |

Exceptions have no separate severity level. They use exactly four statuses: `OPEN`, `INVESTIGATING`, `RESOLVED`, and `OVERRIDDEN`. Prioritization comes from the exception type, financial difference, age, and feed condition rather than a duplicated severity label.

Matching is two-way and uses the same configured grace period in both directions. Intermediary-first pending state is stored on the processed event. Bank-first pending state is derived from the posted bank transaction and the absence of a corresponding event, so no separate pending-match table is required.

The program becomes `RESTRICTED` when an enabled restriction rule is met, including cumulative unresolved financial exposure at or above the configured threshold, a critical feed failure, or an exception left specifically in `OPEN` status beyond the configured age. `INVESTIGATING` stops the open-age trigger, but its unresolved financial amount still contributes to exposure.

Management is the only prototype role that can resume service. Resumption requires a rationale and changes every unresolved `OPEN` or `INVESTIGATING` exception to `OVERRIDDEN`. Those terminal exceptions stop contributing to unresolved exposure. The action and rationale are written to the audit log.

To avoid double-counting the same missing money, aggregate mismatch exposure is not simply added to transaction-exception exposure. The risk display uses the greater applicable control amount.

## Recommended demonstration

1. On **Dashboard**, explain the FBO balance, real-time bank-calculated expected total, and the initially absent EOD batch.
2. Load **Normal operation** and play it to completion. Show the expected customer balances changing only after bank matches.
3. Select **Receive EOD batch**, inspect the immutable Synapse snapshot, then select **Run daily reconciliation** to show all three totals at `$10,000` and all customer rows matched.
4. Load **Accumulating discrepancies**. Use **Next event / deadline** to show an event enter `PENDING_MATCH`, then advance past its grace period.
5. Let the scenario reach restriction. Show the `$125` intermediary attempt as `BLOCKED_BY_RESTRICTION` and the corresponding bank record as `REJECTED_BY_RESTRICTION`. Neither record updates the FBO or expected customer balances.
6. Receive the discrepancy batch and run daily reconciliation. Show expected/FBO `$10,000` versus Synapse `$20,700`, the `-$10,700` aggregate difference, and five customer-level mismatches.
7. Open **Exception management** and **Audit trail** to demonstrate assignment, investigation, resolution/override evidence, and the restriction history.
8. If time permits, use **Timing difference** for grace-period protection or **Intermediary feed failure** for the independent availability control.

## Roles in this prototype

- **Reconciliation Analyst** operates playback and daily reconciliation, reviews matching evidence, and works reconciliation exceptions.
- **Operations** monitors transaction/feed operations and can work operational exceptions.
- **Risk / Compliance** reviews exposure and control evidence and may document an override request, but cannot resume the program.
- **Management** can resume a restricted program with a mandatory rationale; this overrides all unresolved exceptions.
- **Auditor** has read-only access to evidence and audit history.

The acting-role selector demonstrates authorization and audit attribution; it is not user authentication.

## Screens

- **Dashboard** — FBO balance, real-time bank-calculated expected balances, latest Synapse batch state, bank transaction feed, blocked evidence, feed health, and risk metrics.
- **Reconciliation** — scenario playback, real-time matching, pending/blocked evidence, expected balances, bank transactions, EOD batch receipt, customer comparison, and aggregate reconciliation.
- **Exception management** — filters, ownership, investigation, resolution, Management overrides, and audit history.
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
|-- data/scenarios/                Four synthetic real-time middleware feeds
|-- data/batches/                  Four mocked immutable EOD balance batches
|-- data/bank_transactions.csv     Scenario-specific independent bank feed
|-- data/ledgerwatch.db            Generated runtime SQLite state
|-- requirements.txt               Runtime dependencies
|-- .streamlit/config.toml         Application theme
|-- .venv/                         Local environment; excluded from version control
```

This prototype demonstrates a possible control design; it does not assert that a particular workflow alone satisfies any final law, regulation, or supervisory requirement.
