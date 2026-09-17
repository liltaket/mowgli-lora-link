# Security and EU868 design boundary

Status: design review only. No authenticated command path or compliant
continuous-radio configuration is implemented yet.

## Command security decision

CRC, LoRa's private sync word, sessions, sequence numbers, and mock STOP
idempotency do not authenticate a peer. Real mower commands therefore remain
disabled by default.

The proposed host-to-host command protection is OSCORE using an established
implementation, not custom cryptography:

- one random 256-bit master secret per paired base and robot;
- distinct fixed sender IDs;
- HKDF-SHA-256;
- AES-CCM-16-128-128 with a 128-bit authentication tag;
- monotonically increasing OSCORE sender sequence numbers with atomically
  reserved persistent counter blocks;
- standard replay-window handling and OSCORE Echo-based synchronization after
  loss of receiver state;
- an authenticated current robot boot/session target plus a short-lived
  receiver challenge for command freshness;
- authenticated ACKs bound to the exact request;
- local provisioning with `0600` key material, explicit context rotation, and
  no shared factory or repository keys.

Logical command IDs remain separate from cryptographic packet sequence
numbers. On missing keys, tag failure, replay, stale challenge, wrong target,
counter rollback, or storage failure, command handling fails closed before any
side effect. There is no radio reset-to-default-key path.

OSCORE adds bytes and changes the usable application payload. Fragmentation,
airtime, timeout, restart, power-loss, replay, delayed-command, wrong-key, and
old-context tests must pass before enabling even an allowlisted high-level
command. RTCM-only lab testing may temporarily use unauthenticated protocol v1
with explicit warnings.

Primary standards:

- [RFC 8613: OSCORE](https://www.rfc-editor.org/rfc/rfc8613)
- [RFC 9053: COSE algorithms](https://www.rfc-editor.org/rfc/rfc9053)
- [RFC 9175: OSCORE Echo and request freshness](https://www.rfc-editor.org/rfc/rfc9175)

## Current 868.3 MHz profile

The present bench profile is 868.3 MHz, 500 kHz, SF5, CR4/5, +10 dBm radio
setting, and about 33% aggregate airtime at the synthetic nominal RTCM load.
It is not approved for continuous operation.

For Sweden, a nominal 500 kHz signal centered at 868.3 MHz spans
868.05–868.55 MHz within the 868.0–868.6 MHz range in
PTSFS 2025:1 section 141: at most 25 mW ERP and at most 1% duty cycle, or
channel access that provides at least equivalent performance. The radio's
+10 dBm setting is conducted output configuration, not measured ERP.

EN 300 220-2 V3.3.1 polite access still imposes clear-channel assessment,
transmission/dialogue/off-time rules, and a 100 seconds/hour limit per 200 kHz.
LoRa CAD detects LoRa activity and is not by itself a calibrated all-signal
energy detector or compliance proof. LBT/AFA cannot make the current roughly
33% single-channel traffic unrestricted.

## Lower-power band candidate

The first continuous-capacity candidate to investigate is 869.85 MHz,
250 kHz, at no more than 5 mW measured ERP. PTSFS 2025:1 section 148 lists
869.7–870.0 MHz with a 5 mW ERP limit and without the corresponding duty-cycle
condition. At 869.85 MHz a nominal 250 kHz signal spans 869.725–869.975 MHz,
leaving only 25 kHz nominal margin at each band edge.

This is a candidate, not a configuration change or compliance claim. Before
using it, measure with the final board, antenna, cable, enclosure, frequency
tolerance, and firmware:

- maximum ERP;
- occupied bandwidth and frequency drift;
- band-edge, out-of-band, and spurious emissions;
- receiver blocking and coexistence;
- range and packet performance with final security overhead;
- full traffic and retry behavior.

For 868.3 MHz operation, firmware would additionally need a persistent
per-transmitter/sub-band airtime budget across retries and restarts, plus a
fully verified channel-access implementation if relying on that alternative.
Functional throughput and regulatory evidence must remain separate reports.

Primary regulatory sources:

- [PTSFS 2025:1, sections 141 and 148](https://www.pts.se/contentassets/b05c8a7d01a64783aafc6a19e1b78590/ptsfs-2025-1-foreskrifter-om-undantag-fran-tillstandsplikt-for-anvandning-av-vissa-radiosandare.pdf)
- [ETSI EN 300 220-2 V3.3.1](https://www.etsi.org/deliver/etsi_en/300200_300299/30022002/03.03.01_60/en_30022002v030301p.pdf)
- [EU Implementing Decision 2025/2499](https://eur-lex.europa.eu/legal-content/SV/ALL/?uri=CELEX%3A32025D2499)
- [Semtech CAD technical note](https://www.semtech.com/uploads/technology/LoRa/cad-ensuring-lora-packets.pdf)
