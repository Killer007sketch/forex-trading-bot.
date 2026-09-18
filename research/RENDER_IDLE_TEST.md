# Render Free: does an outbound OKX WebSocket prevent 15-minute spin-down?

This is a **read-only infrastructure probe**, NOT either trading bot. No API keys, private endpoints, orders, or trades are submitted.

## Deploy

1. Connect Render to this repository and create **one** Blueprint from root `render.yaml` (plan is explicitly `free`). Do not create a paid instance.
2. Wait for a successful deploy. Check `<actual-service-url>/status` **once** and record `boot_id`, `boot_utc`, `trade_rows`, and `uptime_seconds`. Do not guess the URL; use Render's actual deployed URL.
3. Check Render logs for `connected` and `minute_heartbeat` with rising `trade_rows`. If no trades or persistent connection errors, fix connectivity before running the sleep experiment.
4. For **20 consecutive minutes**, do not request the app URL (including `/status`), do not configure external uptime pings, and do not set an HTTP `healthCheckPath`. Render's default TCP check is fine. Keep outbound OKX WebSocket running. Observe service events and logs via Render management UI/API instead, which do not HTTP-hit the app.
5. After 20 minutes GET `/status` **once**, recording whether it cold-started and whether `boot_id` changed. Correlate with service suspend/restart events and continuous minute logs. A boot ID change alone could also mean a platform restart.

**Interpretation:** if the service suspended despite receiving OKX data, outgoing WebSocket traffic does not suffice to keep this free app active. If the same boot remained and heartbeat logs are continuous, the service survived this 20-minute trial, but Render can still restart or suspend it later. Repeat over several cycles before relying on it.

**Limitations:** the probe intentionally does not send HTTP keepalives. No local persistence is provided. Free Render filesystem is ephemeral. The full two-bot bundle is still separate and must not be called 'deployed' on the basis of this connectivity/idle test.
