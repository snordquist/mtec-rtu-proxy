# mtec-rtu-proxy

A tiny, dependency-free **single-master, caching RTU-over-TCP proxy** for the
M-TEC "espressif" Modbus dongle used by *Energy Butler* hybrid inverters (and
the *EnergyHero* EMS).

It lets **more than one client** (e.g. your EMS **and** Home Assistant) read the
inverter over Modbus even though the dongle only tolerates a *single* master —
without the connection drops, RST storms and stale data you get from a generic
Modbus/TCP proxy.

> Community project, not affiliated with or endorsed by M-TEC. Use at your own
> risk. Register maps and behaviour are reverse-engineered.

## Why a special proxy?

The espressif dongle has two properties that break naive setups:

1. **It speaks Modbus RTU *framed over TCP*** (unit id + PDU + CRC16), **not**
   Modbus/TCP (MBAP header, no CRC). Generic TCP proxies that assume an MBAP
   length field mis-frame every reply and stall.
2. **It accepts only one Modbus master.** A second concurrent connection (a
   second EMS, or HA polling in parallel) causes refused connections and
   dropped links.

This proxy fixes both and adds a read-through cache:

- **Correct RTU framing** (replies framed by function code, CRC-checked).
- **Exactly one persistent upstream connection.** All clients are multiplexed
  onto a single serialized worker, so the dongle only ever sees one master.
- **No reconnect churn.** A transient per-transaction timeout *drains and keeps*
  the socket (an immediate reconnect would be refused); it reconnects only on a
  real socket death, with backoff.
- **Priority + cache.** Configured "hero" IPs (your EMS) get priority and always
  read **live**, so control is never stale. Everyone else (Home Assistant) is
  answered from the **register cache** for reads — adding *zero* load to the
  dongle — and only falls through to a live read on a cache miss. Writes are
  always forwarded live.
- **Dialect bridge.** Clients may speak either RTU-over-TCP *or* Modbus/TCP
  (MBAP). The M-TEC EnergyHero EMS speaks MBAP (and often unit id 255) while the
  dongle and Home Assistant speak RTU-over-TCP (unit 252). The proxy detects each
  client's dialect and translates transparently to the dongle's RTU framing
  (optionally normalising the unit id via `DONGLE_UNIT`).

```
   EMS / Hero  ─┐   priority, always live
                ├─►  mtec-rtu-proxy  ──(one persistent RTU/TCP conn)──►  espressif dongle
Home Assistant ─┘   served from cache
```

## Configuration

All configuration is via environment variables (see [`.env.example`](.env.example)):

| Variable            | Default     | Meaning                                                        |
| ------------------- | ----------- | -------------------------------------------------------------- |
| `LISTEN_HOST`       | `0.0.0.0`   | Address the proxy listens on                                   |
| `LISTEN_PORT`       | `502`       | Port the proxy listens on                                      |
| `DONGLE_HOST`       | `127.0.0.1` | IP of the espressif dongle                                     |
| `DONGLE_PORT`       | `502`       | Modbus port of the dongle                                      |
| `HERO_IPS`          | *(empty)*   | Comma-separated client IPs that get priority + always-live     |
| `TXN_TIMEOUT`       | `3.0`       | Per-transaction upstream timeout (s)                           |
| `CACHE_TTL`         | `30`        | How long a cached register stays servable (s)                  |
| `RECONNECT_BACKOFF` | `3.0`       | Wait before reconnecting a genuinely dead upstream (s)         |
| `CONNECT_SETTLE`    | `2.0`       | Pause after connecting before the first request (s)            |
| `DONGLE_UNIT`       | *(unset)*   | Force this Modbus unit id upstream (e.g. `252`); unset = pass through |

No hosts, IPs or credentials are baked into the code.

## Run with Docker (recommended)

```bash
cp .env.example .env      # then edit .env with your dongle IP + hero IP(s)
docker compose up -d
docker compose logs -f
```

## Run directly

```bash
pip install .
DONGLE_HOST=192.0.2.10 HERO_IPS=192.0.2.20 LISTEN_PORT=502 python -m mtec-rtu-proxy
# (or: python -m mtec_rtu_proxy)
```

Point your EMS/Hero and Home Assistant at the proxy's `LISTEN_HOST:LISTEN_PORT`
instead of at the dongle directly. In Home Assistant keep `type: rtuovertcp`
and `slave: 252`; only the `host`/`port` change.

## Tests

Unit tests cover CRC/framing and the cache; integration tests spin up a **mock
single-master RTU dongle** and drive clients through the proxy — validating
single-upstream behaviour, cache serving, hero priority, transient-timeout
resilience and malformed-frame handling — **before** you point it at real
hardware.

```bash
pip install -e ".[test]"
pytest -v
```

## Safety

- The proxy only *relays* Modbus; it never writes on its own.
- Validate with the test suite (and, if you like, against the mock dongle) before
  connecting production hardware.
- Keep your real IPs/credentials in `.env` (gitignored), never in the repo.

## License

MIT — see [LICENSE](LICENSE).
