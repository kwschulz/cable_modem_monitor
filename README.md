# Cable Modem Monitor

[![GitHub Release](https://img.shields.io/github/v/release/solentlabs/cable_modem_monitor?include_prereleases)](https://github.com/solentlabs/cable_modem_monitor/releases)
[![HACS installs](https://img.shields.io/badge/dynamic/json?color=41BDF5&logo=home-assistant&label=HACS&suffix=%20installs&cacheSeconds=15600&url=https://analytics.home-assistant.io/custom_integrations.json&query=$.cable_modem_monitor.total)](https://analytics.home-assistant.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Supported Modems](https://img.shields.io/badge/Supported%20Modems-View%20Catalog-blue.svg)](https://pypi.org/project/solentlabs-cable-modem-monitor-catalog/)

Monitor your cable modem's signal quality, power levels, and error rates from Home Assistant. Track connection health, identify line issues before they cause outages, and build automations that alert you when something looks off.

## What you get

- **Per-channel signal quality.** Power (dBmV), SNR, and frequency for every downstream and upstream channel.
- **Error tracking.** Corrected and uncorrected error counts, plus per-minute error rates.
- **Connection health.** Status, uptime, last boot time, and reboot detection from counter resets.
- **Health probes.** Ping and HTTP latency on a separate cadence from the full data poll.
- **Remote restart.** Reboot the modem from a Home Assistant button.
- **Local-only.** No cloud, no telemetry. Credentials stored in Home Assistant's encrypted storage.

## Dashboard

![Cable Modem Health Dashboard](https://raw.githubusercontent.com/solentlabs/cable_modem_monitor/main/images/dashboard-screenshot-dark.png)

[More screenshots on GitHub](https://github.com/solentlabs/cable_modem_monitor#see-it-in-action)

## Supported modems

Modems from ARRIS, Compal, Hitron, Motorola, Netgear, SerComm, Technicolor, and Virgin Media. Compatibility varies by firmware and ISP customization.

Check the [catalog of supported modems on PyPI](https://pypi.org/project/solentlabs-cable-modem-monitor-catalog/) before installing. Every supported model is listed with DOCSIS version and verification status.

Modem not listed? [File a request](https://github.com/solentlabs/cable_modem_monitor/blob/main/docs/MODEM_REQUEST.md). The guide walks through capturing the data needed to add support.

## Setup

1. Install via HACS (you're here).
2. Restart Home Assistant.
3. Settings → Devices & Services → Add Integration → "Cable Modem Monitor".
4. Pick your manufacturer and model, enter your modem's IP (usually `192.168.100.1`) and credentials if required.

## Privacy and security

All processing happens on your Home Assistant instance. The integration reads from the modem's local web interface; the only write action is a user-invoked restart button. Every push is scanned by GitHub CodeQL.

## Using the integration

- [Sensor reference](https://github.com/solentlabs/cable_modem_monitor#available-sensors) — full list of entities, naming patterns, and what each value means
- [Dashboard and automation examples](https://github.com/solentlabs/cable_modem_monitor/blob/main/docs/EXAMPLES.md) — ready-to-use Lovelace YAML and automation templates
- [Troubleshooting guide](https://github.com/solentlabs/cable_modem_monitor/blob/main/docs/TROUBLESHOOTING.md) — connection problems, missing sensors, duplicate entities
- [Changelog](https://github.com/solentlabs/cable_modem_monitor/blob/main/CHANGELOG.md) — what changed in each release

## Contributing and modem support

- [Modem request guide](https://github.com/solentlabs/cable_modem_monitor/blob/main/docs/MODEM_REQUEST.md) — how to capture the data needed to add your modem
- [Contributing guide](https://github.com/solentlabs/cable_modem_monitor/blob/main/CONTRIBUTING.md) — workflow, code style, and PR guidelines
- [Getting started for developers](https://github.com/solentlabs/cable_modem_monitor/blob/main/docs/setup/GETTING_STARTED.md) — environment setup, running tests, first commit

## License

MIT
