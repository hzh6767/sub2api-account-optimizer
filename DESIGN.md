# Design

## Boundaries

The optimizer reads PostgreSQL telemetry in a read-only transaction. Every write goes through
one mutation choke point and the official Sub2API administrator API. It never updates PostgreSQL
rows directly.

Target groups come from `OPTIMIZER_GROUP_IDS` (default example: `7,59`); account IDs are never
hard-coded. Deleted and non-OpenAI accounts are excluded. An account found in more than one target
group is reported and receives no scheduling proposal because `priority` and `load_factor` are
global account fields. It is also excluded from active probing so one physical account cannot
contribute multiple failures in a round.

A dry-run creates an immutable `deployment-baseline.json`. The first non-dry-run cycle separately
captures `activation-baseline.json` before any mutation. Rollback uses only that activation
baseline. Newly discovered target accounts are appended before their first possible mutation;
existing entries are never rewritten. In Sub2API `0.1.141`, `load_factor=0` is the documented API
encoding that clears the stored value back to SQL `NULL`.

## Ranking

Each model is scored independently:

```text
score = P50_TTFT + 0.25 * P90_TTFT
        + failure_rate * 15000
        + timeout_rate * 20000
```

TTFT values are capped at 60 seconds. Real traffic has 70% weight and targeted probes have 30%
when both sources exist. Per-model scores are normalized against the model's group median before
being combined, so a fast model is not directly compared with a slow model. An account with fewer
than three valid samples cannot rank first and defaults to scheduling tier 3.

Targeted-test candidates are restricted to the configured ordinary-text allowlist, intersected
with each account's supported models, then ordered by the mounted Sub2API `model_pricing.json`.
Every eligible candidate must have valid input and output pricing or probing is blocked. Price is
used only to choose the cheapest safe probe model, never to rank customer traffic.

Target tiers are `1/15`, `2/12`, `3/8`, and `4/5`; load factor is always capped at the account's
real concurrency. A write requires two identical hourly rankings, at least 15% score movement, a
six-hour cooldown, and moves no more than one tier. Accepted writes are persisted individually so
a later timeout cannot discard an earlier account's cooldown state. Unknown HTTP outcomes are
reconciled against the next read-only database snapshot before another change is considered.

## Health State

One failure cannot disable an account. General failures require three consecutive probes spanning
at least 30 minutes. A 401 or 403 requires two confirmations at least five minutes apart. A 429 is
temporary and never creates a permanent disable decision; a supplied reset time suppresses probes
until that time.

Only an explicit successful administrator API response creates optimizer ownership of a disabled
account. Accounts disabled by an administrator are never auto-recovered. Confirmed optimizer-owned
accounts recover after two successful probes, enter tier 4, and remain on probation until two more
successful probes complete.

## Built-in Scheduler Interaction

The Sub2API advanced scheduler performs real-time selection using priority, load, queue length,
error-rate EWMA, and TTFT EWMA. Previous-response and session affinity remain separate sticky
layers. The external optimizer provides health probes and slow baseline adjustments; it does not
attempt request-by-request scheduling.

In Sub2API `0.1.141`, only the enable flag is database-backed. Score weights and sticky escape
thresholds are startup configuration. Weight changes therefore require a backed-up configuration
change, Sub2API restart, health verification, and file rollback.

## Compatibility And Limitations

The included patch is based on and tested against Sub2API `0.1.141`, commit
`7cb98e5bdca776d643d284aa2f4ce7151308819e`. It adds `mode: optimizer` to the targeted account test
endpoint. This mode sends a one-character prompt, caps output at one token, stops after the first
valid text delta, avoids permanent account-error and successful-test recovery side effects, and
preserves the existing temporary 429 limiter. Do not enable active probes unless the capability
handshake succeeds on the running server.

Historical usage rows do not persist a tool-call marker in this version. Image, video,
non-streaming, and missing-TTFT rows are excluded exactly; historical tool-only requests cannot be
excluded exactly without a schema or metrics change.

HTTP `Retry-After` and common reset headers are parsed and audited. The optimizer does not write
directly to Sub2API's temporary limiter state.
