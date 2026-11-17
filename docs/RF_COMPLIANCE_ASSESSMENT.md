# RF power and duty-cycle assessment (Sweden, 868 MHz)

Date: 2026-11-17

Status: desk assessment made from calculations and the published rules. No RF
measurement was made for it. Nothing here is legal advice, a test-lab
opinion, or a declaration of conformity.

## Verdict

**Continuous operation of today's profile would not be lawful in Sweden.**
The power looks acceptable for this band, but the duty-cycle limit is far too
small for the traffic.

| Item | Today (868.3 MHz, SF5/BW500, +10 dBm) | Rule | Result |
| --- | --- | --- | --- |
| Band fit | 868.05–868.55 MHz | Band M 868.0–868.6 MHz, OBW ≤ 600 kHz | Probably OK; OBW not measured |
| e.r.p. | ≈ 10 mW, assuming a 0 dBd (2.15 dBi) antenna and no loss | ≤ 25 mW | Probably OK; not measured |
| e.r.p., `modem_lab_17dbm` | ≈ 50 mW with the same antenna | ≤ 25 mW | **Exceeds the limit** |
| Base duty cycle, live RTCM | 44.1 % ≈ 1,589 s/h | ≤ 1 % = 36 s/h | **44× over** |
| Robot duty cycle, 2 Hz telemetry | 2.0 % = 72 s/h | ≤ 1 % | **2× over** |
| Polite access (LBT/AFA) instead of duty cycle | Not implemented | EN 300 220-2 §4.6 | Not available |

Short bounded tests can still stay within the limit, because the limit counts
transmit time over one hour. A transmitter in band M has 36 s per hour. That
is about **81 s of live RTCM**, or 30 minutes of 2 Hz robot telemetry, in any
one hour, counted for each transmitter. The recorded tests stayed inside it
only if each hour held no more such runs than that. The historical run log
cannot confirm this.

## Rules that apply

Transmitting without a licence is allowed only under the conditions in the
exemption regulation PTSFS 2025:1. Its 3 kap. 1 § grants the exemption under
lagen (2022:482) om elektronisk kommunikation 3 kap. 4 §. The 863–870 MHz
rows implement Commission Decision 2006/771/EC as amended by (EU) 2025/105.
EN 300 220-2 V3.3.1 is the harmonised standard; Decision (EU) 2025/2499 lists
it in the Official Journal. Duty cycle means *the total active transmit time
in a one-hour period, as a percentage* (PTSFS 2025:1 1 kap. 2 §). It applies
per transmitter and per band. Using several bands at once is allowed, each
with its own limit (EN 300 220-2 §4.4.3.2). Frequency hopping does not
multiply the allowance: "the duty cycle applies to the entire transmission,
not at each hopping channel" (§4.4.11.2).

Non-specific short-range device bands near 868 MHz in Sweden (alarm, RFID and
network-access-point rows omitted):

| Band | Range (MHz) | Max e.r.p. | Channel access | Max OBW | Source |
| --- | --- | ---: | --- | ---: | --- |
| K | 863–865 | 25 mW | ≤ 0.1 % or polite access | 2 MHz | PTSFS §135 |
| L | 865–868 | 25 mW | ≤ 1 % or polite access | 3 MHz | PTSFS §137 |
| M | 868.0–868.6 | 25 mW | ≤ 1 % or polite access | 600 kHz | PTSFS §141 |
| N | 868.7–869.2 | 25 mW | ≤ 0.1 % or polite access | 500 kHz | PTSFS §143 |
| O | 869.4–869.65 | 500 mW | ≤ 10 % or polite access | 250 kHz | PTSFS §146 |
| **P** | **869.7–870.0** | **5 mW** | **no requirement** | **300 kHz** | **PTSFS §148** |
| Q | 869.7–870.0 | 25 mW | ≤ 1 % or polite access | 300 kHz | PTSFS §149 |

The OBW limits are from EN 300 220-2 Table 4. OBW means the 99 % occupied
bandwidth (§4.4.4.2).

Polite spectrum access (EN 300 220-2 Table 18) replaces the duty-cycle limit
but does not remove the time limit. It caps cumulative on-time at **100 s per
hour per 200 kHz** (2.8 %). On one channel that is far too little, but a
transmitter that spreads its traffic over many channels gets 100 s/h on each
(see plan B below). A single transmission may last at most 1 s (4 s for a
dialogue). A transmitter must stay off for 100 ms before using the same
frequency again.
A clear-channel assessment must listen for at least 160 µs, against an energy
threshold of `10·log10(OCW/kHz) − 117 dBm + 15 dB`. That threshold is −75 dBm
at 500 kHz. LoRa CAD detects LoRa preambles, not energy, so it does not
satisfy this by itself.

## Traffic arithmetic

All numbers come from `tools/lora_usb/rf_budget.py`. It uses the SX126x
datasheet time-on-air formula and this link's exact framing: 6 + 20 + 18
bytes of overhead per RTCM fragment and 174 data bytes per fragment.

Two checks show the model agrees with the repository's own figures. It gives
33.15 % airtime for the synthetic profile plus 2 Hz telemetry at BW500 (the
documentation says ~33 %). At the earlier BW250 profile it gives 66.31 % (the
12-minute simulation recorded ~66 %). The live mix reproduces the measured
2,139 B/s to within 0.4 %. Its split per message type is illustrative, because
no live capture is stored. Replace it with a real capture as soon as one
exists:

```bash
python3 -m tools.lora_usb.rf_budget --rtcm-recording base.mlr
```

Base airtime for each RTCM content option, with the shared channel occupancy
at 2 Hz robot telemetry in brackets. **L** = within the calculated legal
limits. **C** = within a 60 % planning ceiling for one half-duplex channel.

| RTCM content | B/s | Today, band M | P: 869.85 MHz BW250 | P: BW125 | O: 869.525 MHz BW125 |
| --- | ---: | --- | --- | --- | --- |
| live: 6× MSM7 + 6 ephemerides + 1005/1230 @1 Hz | 2,132 | 44 % ✗ (46 %) | 88 % L (92 %) | >100 % | >100 % |
| MSM7 GPS/GLO/GAL/BDS, statics @0.1 Hz | 1,568 | 29 % ✗ | 59 % L (63 %) | >100 % | >100 % |
| **MSM4 GPS/GLO/GAL/BDS**, statics @0.1 Hz | 973 | 18 % ✗ | **36 % LC (40 %)** | 72 % L | 72 % ✗ |
| **MSM4 GPS/GAL/BDS triple-band** | 828 | 15 % ✗ | **31 % LC (35 %)** | 61 % L | 61 % ✗ |
| MSM4 GPS/GAL/BDS dual-band | 608 | 12 % ✗ | 23 % LC | 47 % LC | 47 % ✗ |
| MSM4 GPS/GAL/BDS triple-band, every 2 s | 415 | 7.7 % ✗ | 15 % LC | 31 % LC | 31 % ✗ |
| MSM4 GPS/GAL/BDS dual-band, every 5 s | 122 | 2.4 % ✗ | 4.7 % LC | 9.5 % LC | 9.5 % LC |

At 1 % duty with BW500, band M carries at most about **55 B/s** of RTCM. No
useful RTK stream fits that.

## How the referenced projects handle it

- **Ve2mrx/mammotion-rtk-lora** documents the Mammotion RTK3 base in the US
  902–928 MHz band. It uses LoRa SF7/BW250 with 25 hop channels, 600 ms
  dwell, and a 15 s cycle. The payload is **MSM4** for GPS, GLONASS, Galileo
  and BeiDou, plus 1006/1033, with no ephemerides. The regulatory approach is
  US FCC §15.247 frequency hopping, which has no duty-cycle limit. That does
  not carry over to the EU, where hopping shares one duty-cycle allowance.
  The project gives no EU parameters.
- **jLynx/BrowSDR** commit `95e9d4d` adds receive-only decoders and IQ replay
  for Mammotion RTK1/2/3 recorded in New Zealand. It confirms the same
  MSM4/1006/1033 content and hop plans in the 915–928 MHz band (for example
  500 kHz spacing with 600 ms dwell). It cites only a community report that
  Luba 2 firmware hops in the European band and says it is unverified. It
  says nothing about EU power or duty cycle.

What carries over is the content diet: MSM4 instead of MSM7, no ephemerides,
and no SBAS/QZSS. That alone cuts the stream by more than half while keeping
all four constellations and every band. Their channel-access approach does not
carry over.

## MSM4 versus MSM7: constellations and bands are unchanged

The MSM level sets the resolution of each value and which optional fields are
present. It does not set which constellations or signals are sent. That is a
separate choice: one MSM message per constellation, with a cell for every
tracked satellite and signal. MSM4 therefore still carries GPS, GLONASS,
Galileo and BeiDou, each with all of its tracked bands (triple-band GPS,
Galileo and BeiDou; GLONASS L1/L2).

| Per cell or satellite | MSM4 | MSM7 |
| --- | --- | --- |
| Pseudorange | 2⁻²⁴ ms ≈ 18 mm | 2⁻²⁹ ms ≈ 0.6 mm |
| Carrier phase | 2⁻²⁹ ms ≈ 0.6 mm | 2⁻³¹ ms ≈ 0.14 mm |
| Lock-time indicator | 4 bits | 10 bits |
| C/N0 | 1 dB-Hz | 0.0625 dB-Hz |
| Phase-range rate (Doppler) | absent | present |
| Bits per satellite / per cell | 18 / 48 | 36 / 80 |

Carrier-phase noise is a few millimetres, so the 0.6 mm phase resolution does
not limit RTK. The coarser lock-time indicator gives the rover less detail
about how long a signal has been tracked continuously after a short loss.
Mammotion's bases send MSM4, according to both referenced projects.
Before relying on MSM4, verify that the actual rover accepts it on all three
bands, and compare fix time and fix hold against MSM7.

The `msm4-4c` scenario keeps every constellation and band and needs 973 B/s.
The full MSM7 equivalent, `msm7-4c`, needs 1,568 B/s, which is 63 % channel
occupancy in band P: lawful there, but above the 60 % planning ceiling.

## Ways to become lawful

### Recommended: band P plus an RTCM diet

1. Move the link to **869.85 MHz, SF5/BW250**, with conducted power set so
   that measured e.r.p. ≤ 5 mW. That is ≤ +7 dBm with a 0 dBd antenna; start
   at about +4 dBm for margin. Band P has no duty-cycle or LBT requirement,
   so neither transmitter needs airtime bookkeeping.
2. Configure the base receiver to output MSM4 for all four constellations
   (1074/1084/1094/1124) with every tracked band, and 1005 (and 1230 for
   GLONASS) every 10 s. Turn off ephemerides 1019/1020/1042/1044/1045/1046 and the
   SBAS/QZSS MSM messages 1107/1117. QZSS is not usefully visible from
   Sweden. Mammotion's own bases send MSM4.
3. The shared channel then runs at about 35–40 %, close to today's 46 %.

Link budget: halving the bandwidth gains about 3 dB of sensitivity at the
same SF, which offsets the 3 dB lower power. So BW250 at 5 mW e.r.p. has
roughly the same budget as today's BW500 at 10 mW. At +4 dBm it is about
3 dB weaker. See [range](#range) below. Band P has no airtime limit, so a
long outdoor range test at the target channel and power is itself lawful.

Risks that must be measured before relying on band P:

- **Band edges.** The band is only 300 kHz wide. Assuming 10 ppm, BW250 at
  869.85 MHz leaves about 16 kHz on each side. The 869.65–869.70 MHz alarm
  band sits directly below. Measure the 99 % OBW, the spectrum mask at the
  band edges, and frequency drift over temperature. BW125 has 79 kHz of
  margin but needs twice the airtime, which only fits the dual-band or
  reduced-rate diets.
- **e.r.p.** The SX1262 output setting, the board matching, the cable and the
  antenna gain all add up. Measure radiated power, or measure conducted power
  and use a datasheet antenna gain.
- **RTK quality.** Check fix time, fix retention, and correction age with
  MSM4 against MSM7 on the real rover.

### Other options

- **Band O (869.4–869.65 MHz, 10 %, up to 500 mW).** BW250 cannot fit
  because the OBW limit is exactly 250 kHz. BW125 carries only about 135 B/s
  within 10 %, which means the dual-band, every-5-s diet. It is useful only
  if more range is needed and correction quality can be traded away.
- **Band M with polite access on one channel.** 100 s/h per 200 kHz is
  roughly 2.8–8 %, depending on how a lab counts a 500 kHz emission. That is
  below every useful RTCM rate. Sporadic STOP traffic and telemetry at 1 Hz or
  less fit within plain 1 % in band M; 2 Hz telemetry does not.
- **Plan B: LBT + AFA across many channels**, described below.
- **Different radio.** 2.4 GHz LoRa (SX1280/LR1121) under PTSFS §186
  (25 mW e.i.r.p., no access rule) or §188 (100 mW, wideband data with
  EN 300 328 adaptivity) has no duty-cycle limit and more bandwidth. It needs
  new hardware and gives shorter range.

### Plan B: LBT + AFA across many channels in 863–868.6 MHz

Bands K, L and M allow polite access in place of the duty cycle, at
**25 mW e.r.p.**: 7 dB more than band P. The 100 s/h limit applies per
200 kHz portion, so a transmitter that rotates over several channels gets
100 s/h on each. The capacity below is the conservative case: channels are
spaced so that no two share a 200 kHz portion (K 2 MHz, L 3 MHz, M 600 kHz).
The upper bound, if a lab credits an emission for every 200 kHz of its
bandwidth, is 28 portions × 100 s = 2,800 s/h.

| RTCM | PHY | Need | Channels needed / available | Result |
| --- | --- | ---: | ---: | --- |
| live | SF5/BW500 | 1,589 s/h | 16 / 9 | only under the upper bound |
| msm7-4c | SF5/BW500 | 1,057 s/h | 11 / 9 | only under the upper bound |
| **msm4-4c** | **SF5/BW500** | **647 s/h** | **7 / 9** | **fits** |
| msm4-4c | SF5/BW250 | 1,293 s/h | 13 / 13 | fits with no spare channel |
| msm4-4c | SF5/BW125 | 2,586 s/h | 26 / 28 | fits, but 80 % of one radio |
| msm4-3c | SF5/BW500 | 549 s/h | 6 / 9 | fits |

Reproduce with `python3 -m tools.lora_usb.rf_budget --polite-access
--scenario msm4-4c`. The robot transmits only telemetry (72 s/h at 2 Hz,
BW500), so it needs a single channel.

What it would take:

1. **Energy-based CCA** of at least 160 µs before every transmission, at or
   below −75 dBm (500 kHz), −78 dBm (250 kHz) or −81 dBm (125 kHz) with a
   0 dBd antenna. The SX1262 RSSI can serve, but its accuracy must be
   calibrated and verified across channels and temperature. LoRa CAD alone
   does not qualify.
2. **A 100 ms rest per channel.** With back-to-back fragments the base must
   rotate through at least 5 channels (30 ms fragments, BW500). In the worst
   case of only short packets, up to 11.
3. **Receiver synchronisation.** The robot can listen on only one channel at
   a time, so both ends need a shared hop schedule, for example with beacons
   as in the Mammotion link. When CCA finds the scheduled channel busy, the
   base must defer or skip, and the robot can miss that data. A design that
   tolerates this needs retry or redundancy budget, which adds airtime.
4. **Shared, busy spectrum.** Band L also hosts 2 W RFID readers at 865.7,
   866.3, 866.9 and 867.5 MHz and 500 mW data networks. Channels near them
   will often be busy, which consumes the two spare channels quickly.
5. **Per-channel hourly bookkeeping** that survives restarts:
   `duty_cycle.AirtimeBudget` once per channel.
6. **Harder proof.** EN 300 220-2 §5.6 defines conformance tests for CCA
   threshold and timing. These need RF test equipment. The band-P path needs
   only power, bandwidth and emission measurements.

This is a feasible plan B if a band-P range test proves too short. It
roughly doubles to triples the firmware work and needs the MSM4 diet as well.

## Range

`python3 -m tools.lora_usb.rf_budget --range` compares link budgets. It
derives sensitivity from `−174 + 10·log10(BW) + NF + SNRlimit`, with
NF 6.5 dB (this reproduces the datasheet −124 dBm at SF7/BW125) and the
datasheet SNR limits. Both antennas are assumed 0 dBd. Range uses a
flat-ground two-ray model, base antenna 1.5 m, robot antenna 0.3 m. The
margin covers walls, vegetation and fading. The model ignores terrain and
buildings, so use it to compare plans, not to predict range.

| Case | Max path loss | Range, 10 dB margin | Range, 25 dB margin |
| --- | ---: | ---: | ---: |
| Today, band M, 10 mW, SF5/BW500 | 127.3 dB | ~575 m | ~240 m |
| Band P, 5 mW, SF5/BW250 | 127.3 dB | ~575 m | ~240 m |
| Band P, 2.5 mW (+4 dBm), SF5/BW250 | 124.3 dB | ~485 m | ~205 m |
| Band P, 5 mW, SF6/BW250 | 129.8 dB | ~665 m | ~280 m |
| LBT+AFA, 25 mW, SF5/BW500 | 131.3 dB | ~725 m | ~305 m |
| LBT+AFA, 25 mW, SF5/BW250 | 134.3 dB | ~860 m | ~360 m |
| Band O, 500 mW, SF5/BW125 (10 %) | 150.3 dB | ~2.2 km | ~910 m |

Doubling the base antenna height adds about 6 dB, about 40 % more range in
this model (`--base-height-m 3`). Neither antenna height nor receive antenna
gain counts against an e.r.p. limit, so both are free improvements. Transmit
antenna gain counts. SF6 in band P gains 2.5 dB but takes about 68 % channel
occupancy with `msm4-4c`: lawful, but above the planning ceiling.

## Open decision

Band P with `msm4-4c` is the recommended path. The open question is range: run
an outdoor band-P range test (lawful for any duration) at the target
power with base and robot at their real mounting heights. If the margin is too
small, choose between a higher base antenna, SF6, and plan B (LBT + AFA at
25 mW).

## Firmware and host changes this implies

- Add a band-P PHY profile and an upper power limit enforced at compile time
  for each band (for example `static_assert(POWER <= 7)` in band P with a
  0 dBd antenna). Rename or remove `modem_lab_17dbm` for 868.3 MHz: as built,
  it exceeds 25 mW e.r.p. with any antenna of 0 dBd or higher.
- Anything kept in a duty-cycle band (M, O, Q) needs the transmitter itself
  to enforce an airtime budget. `tools/lora_usb/duty_cycle.py` is the
  reference algorithm: a bucketed sliding hour that counts in the
  conservative direction. On restart it either assumes the full budget was
  just used or restores a snapshot. Its tests show that no one-hour window
  ever exceeds the budget across restarts. In a mutation check without
  restart handling, usage reached 2× the budget. Porting it to the firmware
  adds a "duty budget exhausted" error to the USB contract.
- Filter RTCM types at the base. Doing it in the receiver configuration is
  preferred; a host-side allowlist is the fallback.

## Remaining evidence before any compliance claim

1. Conducted and radiated power, so that e.r.p. is known.
2. 99 % OBW, band-edge mask, out-of-band and spurious emissions, and
   frequency stability, for the chosen channel.
3. Real base capture → `rf_budget --rtcm-recording` → the airtime that will
   actually be needed.
4. Rover RTK performance with the reduced RTCM content.
5. If the hardware or firmware is distributed to others, it is placed on the
   market and RED (2014/53/EU) conformity assessment applies: EN 300 220-2,
   EN 301 489 and safety. This repository cannot settle that alone.

## Sources

- [PTSFS 2025:1, 1 kap. 2 § and 3 kap. 133–149 §§](https://pts.se/contentassets/b05c8a7d01a64783aafc6a19e1b78590/ptsfs-2025-1-foreskrifter-om-undantag-fran-tillstandsplikt-for-anvandning-av-vissa-radiosandare.pdf)
- [ETSI EN 300 220-2 V3.3.1: Table 4, §4.4.3, §4.4.4, §4.4.11, §4.6, Tables 11, 17 and 18](https://www.etsi.org/deliver/etsi_en/300200_300299/30022002/03.03.01_60/en_30022002v030301p.pdf)
- [Consolidated Decision 2006/771/EC as amended by (EU) 2025/105](https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX%3A02006D0771%2801%29-20250123)
- [Decision (EU) 2025/2499, which lists EN 300 220-2 V3.3.1](https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32025D2499)
- [ERC Recommendation 70-03, Annex 1](https://docdb.cept.org/download/4635)
- [Ve2mrx/mammotion-rtk-lora](https://github.com/Ve2mrx/mammotion-rtk-lora) and
  [jLynx/BrowSDR@95e9d4d](https://github.com/jLynx/BrowSDR/commit/95e9d4d51daae277bb2e55f1ede390c9f5ed6c94)
