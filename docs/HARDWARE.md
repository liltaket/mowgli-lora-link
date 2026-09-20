# Hardware

## Supported bench assembly

Each endpoint is a Seeed XIAO ESP32-S3 connected to a Wio-SX1262 B2B radio.
The two endpoints are physically identical and use the same firmware image.
The host decides whether an endpoint acts as BASE or ROBOT after USB session
establishment.

| SX1262 signal | ESP32-S3 GPIO |
| --- | ---: |
| SPI SCK | 7 |
| SPI MISO | 8 |
| SPI MOSI | 9 |
| NSS | 41 |
| DIO1 | 39 |
| RESET | 42 |
| BUSY | 40 |

The firmware configures DIO2 as the RF switch and DIO3 TCXO control at 1.8 V.
USB uses the ESP32-S3 native USB CDC/JTAG interface. A baud rate is supplied to
serial tools for API compatibility, but native USB CDC is not a UART baud-rate
limited link.

## Antenna and power rules

- Attach a suitable 868 MHz antenna to each SX1262 before any transmission.
- Do not transmit into an unconnected RF port.
- Keep the two boards on a table and away from mower power/motor wiring during
  this prototype phase.
- Re-identify the ESP32 MAC/USB serial before every flash. `/dev/cu.*` and
  `/dev/tty*` suffixes depend on host topology and may change.
- The firmware image contains no Wi-Fi credentials, GNSS configuration, or
  mower secrets.

## Bench RF profile

| Parameter | Value |
| --- | ---: |
| Frequency | 868.3 MHz |
| Bandwidth | 500 kHz |
| Spreading factor | 5 |
| Coding rate | 4/5 |
| Output power | +17 dBm |
| Preamble | 12 symbols |
| Sync word | RadioLib private LoRa sync word |
| PHY CRC | enabled |

This profile is a technical prototype, not a regulatory approval, range claim,
or production channel plan. In particular, high-rate RTCM traffic would have
far higher airtime than the sparse bring-up tests.
