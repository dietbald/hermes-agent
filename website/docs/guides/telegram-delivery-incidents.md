# Telegram delivery incident runbook

Use this when Telegram polling still works but final replies do not arrive, especially for `sendMessage` timeouts or HTTP 429/FloodWait responses.

## What Hermes guarantees

Final agent replies are written to `state.db` before the first platform send. Transient failures — including ambiguous send timeouts, connection errors, Telegram 429/FloodWait, and server 5xx errors — remain retryable obligations. A supervised gateway watcher retries them with server-requested `retry_after` or capped backoff (5, 15, 30, 60, 120, then 300 seconds). They do not expire at the historical three-attempt/24-hour poison-row limit. Retry stops only when the platform acknowledges delivery or an operator explicitly cancels the obligation.

An ambiguous timeout can mean Telegram received the message but its acknowledgement was lost. Retried replies therefore carry the recovered-reply marker and may be duplicates; this is deliberate at-least-once delivery.

After three failures Hermes emits an ERROR-level `DELIVERY NEEDS ATTENTION` journal entry and appends a token-free record to:

```text
$HERMES_HOME/logs/delivery_alerts.jsonl
```

This is the visible, Telegram-independent alert path. A host monitor should alert on either a new line in that file or a non-zero health-probe exit. Do not route that alert only through Telegram.

## Inspect or cancel obligations

These commands do not print response content:

```bash
cd /home/tj/hermes/hermes-agent
.venv/bin/python scripts/delivery-obligations.py --profile atlas list
.venv/bin/python scripts/delivery-obligations.py --profile atlas cancel <obligation_id>
```

Cancellation is explicit and terminal. Use it only when the reply is no longer wanted or repeated delivery would be harmful.

## Safe first probe

The default probe is read-only. It checks the gateway unit, `getMe`, `getWebhookInfo`, recent local journal counters, and ledger backlog. It never prints bot tokens or response content.

```bash
cd /home/tj/hermes/hermes-agent
.venv/bin/python scripts/telegram-delivery-health.py atlas donna contour-gm \
  --output "$HOME/.hermes/telegram-delivery-health.json"
```

Do not call `getUpdates` while a gateway is polling. A second poller can conflict with the live bot or consume updates and makes the diagnosis worse.

A read-only result of `control_plane_healthy_send_path_untested` proves only that the Bot API control path and credentials work. It does not prove `sendMessage` works.

## Controlled send-path probe

During an active incident, after confirming the selected profiles and home channels, perform one silent probe message per bot:

```bash
cd /home/tj/hermes/hermes-agent
.venv/bin/python scripts/telegram-delivery-health.py atlas donna \
  --send-test \
  --output "$HOME/.hermes/telegram-delivery-health.json"
```

`--send-test` creates one visible message in each configured home chat but disables push notification. The script returns:

- exit 0: all tested sends succeeded;
- exit 1: the control path was indeterminate/unhealthy;
- exit 2: at least one `sendMessage` failed.

## Distinguish local flood from Telegram-side throttling

Collect all of these facts; no single Bot API response proves the origin.

1. **Local retry volume:** inspect `recent_journal.rate_limits`, `send_failures`, and `delivery_alerts` in the JSON report. A rapidly increasing local count supports self-generated pressure.
2. **Independent bot tokens:** test at least two bots. If both have successful `getMe` and both receive `sendMessage` 429 with similar `retry_after`, the result is `multi_bot_send_throttle_with_control_plane_healthy`.
3. **Process ownership:** confirm only the expected gateway units are running. Search service definitions and process command lines for unexpected gateway/relay workers. Never print token-bearing environment values.
4. **Credential reuse outside this host:** local process inspection cannot exclude a copied bot token being used on another machine. If local volume is low but throttling persists, treat external use or Telegram recipient/IP-side policy as possible until credentials and Telegram telemetry prove otherwise.
5. **Recipient scope:** if the same independent bots fail only for one chat while sends to a designated test chat work, recipient/user scope is more likely than per-bot flood. Do not send to arbitrary users for diagnosis.

Interpretation matrix:

| Evidence | Most likely interpretation |
|---|---|
| One bot 429; its journal shows sustained sends/retries | Local per-bot flood or a remote process using that token |
| Multiple independent bots: `getMe` works, `sendMessage` 429, local volume low | Telegram-side, recipient-scoped, per-IP, or external credential use; not explained by one local gateway flooding |
| `getMe` and `sendMessage` fail with network/TLS errors | Network, DNS, proxy, or Telegram reachability incident |
| `getMe` returns 401/403 | Token/auth problem; do not keep retrying as a transient throttle |
| Probe sends work but a ledger row keeps failing | Inspect its target/thread routing and permanent error; cancel only if appropriate |

## Alternate notification path

Telegram must not be the only incident channel.

- The gateway writes repeated-delivery alerts to journald and `delivery_alerts.jsonl`.
- The health probe writes deterministic JSON and exits non-zero, so systemd/cron/external monitoring can relay it through a non-Telegram path.
- On this deployment, record the incident on the Paperclip TJS issue from an authenticated Paperclip session or notify through the active operator shell/dashboard. Do not copy Paperclip credentials into a gateway profile and do not post bot tokens or message content.
- If a monitor is configured, point it at the JSON report/exit status and deliver to Paperclip, email, or another independently hosted channel. A Telegram-only `OnFailure` target defeats the purpose.

## Recovery

1. Leave the gateway running unless it is wedged; the retry watcher needs the process alive.
2. Remove the source of a confirmed local flood before forcing another send.
3. Respect Telegram `retry_after`; do not manually hammer `sendMessage`.
4. Watch the ledger list until the row becomes `delivered`, or explicitly cancel it.
5. If code/config changed, a gateway restart requires operator approval and should be performed outside the gateway process.
