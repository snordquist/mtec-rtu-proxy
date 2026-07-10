# modbus-rtu-tcp-proxy

A tiny, dependency-free **single-master Modbus RTU-over-TCP proxy**. It lets
**several clients share one Modbus device** that speaks RTU-over-TCP and tolerates
only a *single* master — without the connection drops, RST storms, desyncs and
stale reads you get from a generic Modbus/TCP proxy.

Typical use: a cheap WiFi/serial Modbus gateway (solar inverter dongle, energy
meter, PLC bridge) that only accepts one TCP master, but you want both your
EMS/controller **and** a monitoring tool (e.g. Home Assistant) to read it.

## What it does

- **One persistent upstream connection, serialized.** All clients are multiplexed
  onto a single worker that runs exactly one Modbus transaction at a time, so the
  device never sees more than one master (RTU has no transaction id — concurrency
  would desync it).
- **Dialect bridge (RTU-over-TCP ↔ Modbus/TCP).** Each client may speak either
  RTU-over-TCP *or* Modbus/TCP (MBAP). The proxy detects the dialect per client
  and translates transparently to the device's RTU framing (optionally
  normalising the unit id via `UPSTREAM_UNIT`).
- **Desync-safe.** Correct RTU framing (replies framed by function code,
  CRC-checked). A late/duplicate reply is drained-to-resync instead of triggering
  a reconnect (reconnect churn agitates fragile devices); it reconnects only on a
  real socket death, with backoff.
- **Priority.** Configured `PRIORITY_IPS` (your controller) jump the queue and
  always read **live**, so control is never stale.
- **Write authorization.** Only `WRITE_IPS` may send FC06/FC16 writes; everyone
  else gets an illegal-function exception.
- **Optional read-through cache.** Non-priority reads can be served from a
  register cache (warmed by any live read) — *zero* extra device load. **Set
  `CACHE_TTL=0` to disable caching entirely** (every read goes live).
- **Fast-fail + self-healing.** Per-client timeout (a stalled upstream returns a
  gateway exception instead of hanging clients), a supervised worker, and mute
  diagnostics that log the lead-in on the first timeout of a stall.

```
   controller / EMS ─┐   priority, always live
                     ├─►  modbus-rtu-tcp-proxy  ──(one persistent RTU/TCP conn)──►  Modbus device
   monitoring / HA  ─┘   cache (if enabled), else live
```

> Community project. Use at your own risk.

## Configuration

All configuration is via environment variables (see [`.env.example`](.env.example)):

| Variable               | Default     | Meaning                                                              |
| ---------------------- | ----------- | ------------------------------------------------------------------- |
| `LISTEN_HOST`          | `0.0.0.0`   | Address the proxy listens on                                        |
| `LISTEN_PORT`          | `502`       | Port the proxy listens on                                           |
| `UPSTREAM_HOST`        | `127.0.0.1` | IP of the upstream Modbus device                                    |
| `UPSTREAM_PORT`        | `502`       | Modbus port of the device                                           |
| `UPSTREAM_UNIT`        | *(unset)*   | Force this unit id upstream (e.g. `252`); unset = pass client's through |
| `PRIORITY_IPS`         | *(empty)*   | Comma-separated client IPs that get priority + always-live reads    |
| `WRITE_IPS`            | *(= priority)* | IPs allowed to send FC06/FC16 writes (empty = allow all + warn)  |
| `CACHE_TTL`            | `30`        | Read-through cache lifetime (s). **`0` disables caching** (always live) |
| `TXN_TIMEOUT`          | `3.0`       | Per-transaction upstream timeout (s)                                |
| `RECONNECT_BACKOFF`    | `3.0`       | Wait before reconnecting a genuinely dead upstream (s)              |
| `CONNECT_SETTLE`       | `2.0`       | Pause after connecting before the first request (s)                 |
| `MIN_REQUEST_INTERVAL` | `0.0`       | Min seconds between upstream requests (0 = off); pace a fragile device |
| `PRIORITY_CACHE_TTL`   | `0.0`       | If >0, also serve priority reads from cache within this debounce window |
| `STATS_INTERVAL`       | `60`        | Log a one-line stats summary every N s (0 = off)                    |
| `CACHE_JITTER`         | `0.0`       | Random 0..N s back-dated per cache write to stagger block expiries  |
| `LOG_LEVEL`            | `INFO`      | Set `DEBUG` to log every request/reply with decoded values          |

No hosts, IPs or credentials are baked into the code.

> **Aliases:** the former names `DONGLE_HOST`/`DONGLE_PORT`/`DONGLE_UNIT`,
> `HERO_IPS`, `HERO_CACHE_TTL` are still accepted as backward-compatible aliases.

## Run with Docker (recommended)

```bash
cp .env.example .env      # then edit .env with your device IP + priority IP(s)
docker compose up -d
docker compose logs -f
```

## Run directly

```bash
pip install .
UPSTREAM_HOST=192.0.2.10 PRIORITY_IPS=192.0.2.20 LISTEN_PORT=502 python -m modbus_proxy
```

Point your clients at the proxy's `LISTEN_HOST:LISTEN_PORT` instead of at the
device directly.

## Tests

Unit tests cover CRC/framing and the cache; integration tests spin up a **mock
single-master RTU device** and drive clients through the proxy — validating
single-upstream behaviour, cache serving, priority ordering, write authorization,
transient-timeout/late-reply resilience, reconnect-after-socket-death and
malformed-frame handling — **before** you point it at real hardware.

```bash
pip install -e ".[test]"
pytest -v
```

## Scope / limitations

- The **upstream must speak RTU-over-TCP** (RTU frames over a raw TCP stream, with
  CRC — not Modbus/TCP with an MBAP header). Client-side, both dialects are
  accepted.
- Framing/caching cover **holding-register reads (FC03)** and **writes
  (FC06/FC16)** — the typical inverter/energy-device workload. Other function
  codes (e.g. FC04 input registers) are not specially handled.

## Example: M-TEC Energy Butler + EnergyHero

This proxy was originally built for the **M-TEC "espressif" WiFi dongle** on
*Energy Butler* hybrid inverters, which speaks RTU-over-TCP (unit 252) and accepts
only one master — so the **EnergyHero EMS** (Modbus/TCP, unit 255) and Home
Assistant (`type: rtuovertcp`, slave 252) could not both read it. Config for that
setup:

```env
UPSTREAM_HOST=<dongle-ip>      # (alias DONGLE_HOST)
UPSTREAM_UNIT=252              # normalise the Hero's unit-255 requests to 252
PRIORITY_IPS=<energyhero-ip>   # Hero reads live; it controls the battery
WRITE_IPS=<energyhero-ip>,<home-assistant-ip>
```

In Home Assistant keep `type: rtuovertcp` and `slave: 252`; only point `host`/
`port` at the proxy. Not affiliated with or endorsed by M-TEC; register maps are
reverse-engineered.

## Safety

- The proxy only *relays* Modbus; it never writes on its own.
- Validate with the test suite (and the mock device) before connecting real hardware.
- Keep your real IPs/credentials in `.env` (gitignored), never in the repo.

## License

MIT — see [LICENSE](LICENSE).
