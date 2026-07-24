# Cable Modem Monitor for Home Assistant

<!-- Versions & Install -->
[![GitHub Release](https://img.shields.io/github/v/release/solentlabs/cable_modem_monitor?include_prereleases)](https://github.com/solentlabs/cable_modem_monitor/releases)
[![Core](https://img.shields.io/pypi/v/solentlabs-cable-modem-monitor-core?label=core)](https://pypi.org/project/solentlabs-cable-modem-monitor-core/)
[![Catalog](https://img.shields.io/pypi/v/solentlabs-cable-modem-monitor-catalog?label=catalog)](https://pypi.org/project/solentlabs-cable-modem-monitor-catalog/)
[![HACS installs](https://img.shields.io/badge/dynamic/json?color=41BDF5&logo=home-assistant&label=HACS&suffix=%20installs&cacheSeconds=15600&url=https://analytics.home-assistant.io/custom_integrations.json&query=$.cable_modem_monitor.total)](https://analytics.home-assistant.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

<!-- Build Status -->
[![GitHub Actions](https://github.com/solentlabs/cable_modem_monitor/actions/workflows/tests.yml/badge.svg)](https://github.com/solentlabs/cable_modem_monitor/actions/workflows/tests.yml)
[![CodeQL](https://github.com/solentlabs/cable_modem_monitor/actions/workflows/codeql.yml/badge.svg)](https://github.com/solentlabs/cable_modem_monitor/actions/workflows/codeql.yml)
[![Code Style: Black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

<!-- Meta -->
[![AI Assisted](https://img.shields.io/badge/AI-Claude%20Assisted-5A67D8.svg)](https://claude.ai)
[![Help Add Your Modem](https://img.shields.io/badge/Help-Add%20Your%20Modem-brightgreen.svg)](https://github.com/solentlabs/cable_modem_monitor/blob/main/docs/MODEM_REQUEST.md)

A custom Home Assistant integration that monitors cable modem signal quality, power levels, and error rates. Perfect for tracking your internet connection health and identifying potential issues before they cause problems.

<!-- markdownlint-disable MD033 -->
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/solentlabs/cable_modem_monitor/main/images/dashboard-screenshot-dark.png">
  <img src="https://raw.githubusercontent.com/solentlabs/cable_modem_monitor/main/images/dashboard-screenshot.png" alt="Cable Modem Health Dashboard" width="500">
</picture>
<!-- markdownlint-enable MD033 -->

Monitor your cable modem's signal quality, errors, and connection health in real-time.

> **⭐ If you find this integration useful, please star this repo!**
> It helps others discover the project and shows that the integration is actively used.
>
> **🤖 AI-Assisted Development**: This project uses AI-assisted development (Claude) to accelerate implementation while maintaining human oversight for architecture and community decisions.

## Quick Links

- [**Installation Guide**](#installation)
- [**Supported Modems**](#supported-modems)
- [**Troubleshooting Guide**](https://github.com/solentlabs/cable_modem_monitor/blob/main/docs/TROUBLESHOOTING.md)
- [**Contributing Guide**](https://github.com/solentlabs/cable_modem_monitor/blob/main/CONTRIBUTING.md)
- [**Development**](#development) (for contributors)

---

## Development

**New contributor?** Start with the [Getting Started Guide](https://github.com/solentlabs/cable_modem_monitor/blob/main/docs/setup/GETTING_STARTED.md) -- it covers environment setup, running tests, and your first commit in a single document.

**Returning contributor?** See the [Contributing Guide](https://github.com/solentlabs/cable_modem_monitor/blob/main/CONTRIBUTING.md) for workflow, code style, and PR guidelines.

**Architecture (v3.14):** Two runtime pip packages in `packages/` (Core + Catalog) plus a thin HA adapter in `custom_components/`. A third package — **Catalog Tools** — provides catalog authoring tools (HAR analysis, YAML generation, verification) for contributors and maintainers; it is never installed by HA. See [CONTRIBUTING.md](https://github.com/solentlabs/cable_modem_monitor/blob/main/CONTRIBUTING.md#project-architecture) for details.

---

## At a Glance

**What it monitors:**

- 📊 **Signal Quality**: Power levels, SNR, frequency for every channel
- ⚠️ **Error Tracking**: Corrected & uncorrected errors per channel
- 🔌 **Connection Health**: Status, uptime, and last boot time
- 💓 **Modem Health**: Real-time ping and HTTP latency monitoring
- 📈 **Trends**: Full historical data for analysis and graphing

**What it does:**

- 🔄 **Remote Control**: Restart your modem from Home Assistant
- 🤖 **Automation Ready**: Trigger actions on signal degradation or errors
- 🔐 **Local-Only**: All processing on your Home Assistant instance — no cloud services
- 🛡️ **Security Focused**: CodeQL scanned on every push, weekly schedule, and PR
- 🔌 **Plug & Play**: Easy UI configuration, no YAML editing needed

### See It In Action

Track your cable modem's health with comprehensive dashboards and real-time monitoring:

<!-- markdownlint-disable MD033 -->
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/solentlabs/cable_modem_monitor/main/images/downstream-power-levels-dark.png">
  <img src="https://raw.githubusercontent.com/solentlabs/cable_modem_monitor/main/images/downstream-power-levels.png" alt="Downstream Power Levels" width="500">
</picture>
<!-- markdownlint-enable MD033 --><br>
<em>Real-time power level monitoring across all downstream channels</em>

<!-- markdownlint-disable MD033 -->
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/solentlabs/cable_modem_monitor/main/images/signal-to-noise-ratio-dark.png">
  <img src="https://raw.githubusercontent.com/solentlabs/cable_modem_monitor/main/images/signal-to-noise-ratio.png" alt="Signal-to-Noise Ratio" width="500">
</picture>
<!-- markdownlint-enable MD033 --><br>
<em>SNR tracking helps identify signal quality issues before they cause problems</em>

<!-- markdownlint-disable MD033 -->
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/solentlabs/cable_modem_monitor/main/images/latency-dark.png">
  <img src="https://raw.githubusercontent.com/solentlabs/cable_modem_monitor/main/images/latency.png" alt="Modem Latency Monitoring" width="500">
</picture>
<!-- markdownlint-enable MD033 --><br>
<em>Ping and HTTP latency monitoring for real-time health assessment</em>

<!-- markdownlint-disable MD033 -->
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/solentlabs/cable_modem_monitor/main/images/corrected-errors-dark.png">
  <img src="https://raw.githubusercontent.com/solentlabs/cable_modem_monitor/main/images/corrected-errors.png" alt="Corrected Errors" width="500">
</picture>
<!-- markdownlint-enable MD033 --><br>
<em>Track corrected errors over time to spot developing line issues</em>

---

## Features

### Monitoring & Data Collection

- **Easy Setup**: Configure via Home Assistant UI - no YAML editing required
- **Comprehensive Channel Monitoring**: Tracks all downstream and upstream channels
- **Per-Channel Metrics**:
  - Power levels (dBmV)
  - Signal-to-Noise Ratio (SNR in dB)
  - Frequency (Hz)
  - Corrected/Uncorrected errors
- **Summary Sensors**: Total corrected and uncorrected errors across all channels
- **Unified Status**: Single sensor showing operational state (Operational/Degraded/Not Locked/Unresponsive)
- **System Information**: Software version, uptime, channel counts, and last boot time
- **Health Monitoring**: Real-time modem health checks with:
  - Ping latency monitoring
  - HTTP response time tracking
  - Automatic health status assessment
  - Circuit breaker pattern for reliability

### Control & Automation

- **Modem Control**: Restart your modem directly from Home Assistant
- **Automation-Friendly**: Last boot time sensor with timestamp device class for reboot detection
- **Consistent Entity Naming**: All entities use `cable_modem_` prefix for predictability
- **Historical Data**: All metrics are stored for trend analysis
- **Dashboard Ready**: Create graphs and alerts based on signal quality

### Developer Friendly

- **Extensible**: Plugin architecture makes adding new modem models easy
- **Well Tested**: comprehensive test coverage across Core, Catalog, Catalog Tools, and HA integration suites — see CI badge above for current pass status
- **Type Safe**: Full type hints, mypy and pyright validation

## Supported Modems

This integration supports modems from ARRIS, Compal, Hitron, Motorola, Netgear, SerComm, Technicolor, and Virgin Media. Compatibility varies based on firmware versions and ISP customizations.

> **[View the Supported Modems List](https://pypi.org/project/solentlabs-cable-modem-monitor-catalog/)** - Complete list with DOCSIS versions, ISP compatibility, verification status, and model timelines.

## Help Expand Modem Support

The catalog grows when contributors with hardware step up. Two paths:

- **Modem not listed?** Use [har-capture](https://github.com/solentlabs/har-capture) to record your modem's web interface, then [file a request](https://github.com/solentlabs/cable_modem_monitor/issues/new?template=modem_request.yml). See the [Modem Request Guide](https://github.com/solentlabs/cable_modem_monitor/blob/main/docs/MODEM_REQUEST.md) for the user-facing walkthrough.
- **Want to help more?** If you have AI access, you can analyze captures and propose catalog entries yourself. See [AI-Assisted Catalog Contribution](https://github.com/solentlabs/cable_modem_monitor/blob/main/CONTRIBUTING.md#ai-assisted-catalog-contribution).

**Modem listed as ⏳ Awaiting?** [Report it working](https://github.com/solentlabs/cable_modem_monitor/issues/new?template=modem_verification.yml) to help verify support.

## Installation

### HACS (Recommended)

> **Don't have HACS?** Follow the [HACS installation guide](https://www.hacs.xyz/docs/use/download/download/) first.

1. Open **HACS** from the sidebar
2. Search for **"Cable Modem Monitor"**
3. Click **Download**
4. Restart Home Assistant

### Testing a Beta Release

Beta releases install manually — there's no auto-update path on betas. Each beta is a deliberate per-version install.

1. In HACS, open **Cable Modem Monitor**.
2. Click the **⋯** menu → **Redownload**.
3. Click **Need a different version?**.
4. Pick the desired beta from the **Release** dropdown.
5. Click **Download** and restart Home Assistant.

To return to stable: repeat the steps and pick the latest stable release.

## Setup

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **Cable Modem Monitor**
3. Pick a manufacturer (or **All** to see every catalog entry)
4. Select your modem model and choose Channel Number mode (recommended — stable entity IDs across reboots) or Channel ID mode
5. Enter your modem's IP address (typically `192.168.100.1`) and credentials if required

<!-- markdownlint-disable MD033 -->
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/solentlabs/cable_modem_monitor/main/images/setup-step1-manufacturer-dark.png">
  <img src="https://raw.githubusercontent.com/solentlabs/cable_modem_monitor/main/images/setup-step1-manufacturer.png" alt="Setup step 1: pick a manufacturer" width="420">
</picture>
<!-- markdownlint-enable MD033 --><br>
<em>Step 3 — Pick a manufacturer (or All)</em>

<!-- markdownlint-disable MD033 -->
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/solentlabs/cable_modem_monitor/main/images/setup-step2-modem-dark.png">
  <img src="https://raw.githubusercontent.com/solentlabs/cable_modem_monitor/main/images/setup-step2-modem.png" alt="Setup step 2: select modem and channel naming" width="420">
</picture>
<!-- markdownlint-enable MD033 --><br>
<em>Step 4 — Select your modem and channel naming</em>

<!-- markdownlint-disable MD033 -->
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/solentlabs/cable_modem_monitor/main/images/setup-step3-connection-dark.png">
  <img src="https://raw.githubusercontent.com/solentlabs/cable_modem_monitor/main/images/setup-step3-connection.png" alt="Setup step 3: connection details" width="420">
</picture>
<!-- markdownlint-enable MD033 --><br>
<em>Step 5 — Enter connection details</em>

### Options

After setup, configure via **Settings → Devices & Services → Cable Modem Monitor → Configure**:

- **Host**: Update modem IP / URL
- **Credentials**: Update username/password if your modem requires authentication
- **Polling Interval**: How often to fetch full modem status (30 seconds – 24 hours, default: 10 minutes)
- **Health Check Interval**: How often to run lightweight reachability probes (default: 30 seconds; uses ICMP ping and TCP connect — no HTTP requests between data polls)

Modem Model and Channel Identity are install-time choices, not editable here. The model is fixed; Channel Identity (number vs ID) can be switched by removing and re-adding the integration (see [Known Limitations](#known-limitations)).

<!-- markdownlint-disable MD033 -->
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/solentlabs/cable_modem_monitor/main/images/cable-modem-settings-dark.png">
  <img src="https://raw.githubusercontent.com/solentlabs/cable_modem_monitor/main/images/cable-modem-settings.png" alt="Cable Modem Settings dialog" width="420">
</picture>
<!-- markdownlint-enable MD033 -->

---

## Available Sensors

All sensors use the `cable_modem_` prefix for consistent entity naming and easy identification. Supporting entities (system information, error totals and rates, latency, LAN statistics) sit in the device page's Diagnostic section.

**Entity Naming Pattern:**

System sensors: `sensor.cable_modem_{metric}` (e.g., `sensor.cable_modem_status`).

Channel sensor naming depends on the **Channel Identity** mode you picked at setup:

- **Channel Number mode** (recommended default — stable across reboots):
  `sensor.cable_modem_{direction}_ch_{number}_{metric}`
  - Example: `sensor.cable_modem_ds_ch_1_power`
  - Example: `sensor.cable_modem_us_ch_3_frequency`
- **Channel ID mode** (DOCSIS DCID-based):
  `sensor.cable_modem_{direction}_{type}_ch_{id}_{metric}`
  - Example: `sensor.cable_modem_ds_qam_ch_1_power`
  - Example: `sensor.cable_modem_us_atdma_ch_3_frequency`
  - DOCSIS 3.1 modems also have OFDM/OFDMA channels: `sensor.cable_modem_ds_ofdm_ch_1_power`

Channel Identity is set when you add the integration. To switch, remove and re-add it in the other mode, then call the `convert_channel_identity` service to rename existing recorder history to match.

### Modem Status

- `sensor.cable_modem_status`: Unified pass/fail status combining connection, health, and DOCSIS lock state
  - **Operational**: All good - data parsed, DOCSIS locked, reachable
  - **ICMP Blocked**: HTTP works but ping fails (check parser `supports_icmp` setting)
  - **Partial Lock**: Some downstream channels not locked
  - **Not Locked**: DOCSIS not locked to ISP
  - **Parser Error**: Modem reachable but data couldn't be parsed
  - **Unresponsive**: Can't reach modem via ICMP or TCP

### System Information

- **Modem Info**: Detected model, with manufacturer, DOCSIS version, release date, and catalog status as attributes
- `sensor.cable_modem_software_version`: Modem firmware/software version
- `sensor.cable_modem_last_boot_time`: When the modem last rebooted (timestamp device class); Home Assistant renders it as relative age ("5 days ago"), which doubles as uptime
- `sensor.cable_modem_ds_channel_count`: Number of active downstream channels
- `sensor.cable_modem_us_channel_count`: Number of active upstream channels

Firmware and hardware versions also appear on the device info card. Other system fields your modem reports (provisioned speeds, for example) become sensors automatically, and LAN interface statistics get per-interface sensors for bytes, packets, errors, and drops.

### Latency Monitoring

- `sensor.cable_modem_ping_latency`: Ping response time in milliseconds
- `sensor.cable_modem_tcp_latency`: TCP connect time in milliseconds
- `sensor.cable_modem_http_latency`: HTTP response time in milliseconds

### Summary Sensors

- `sensor.cable_modem_total_corrected_errors`: Total corrected errors across all downstream channels
- `sensor.cable_modem_total_uncorrected_errors`: Total uncorrected errors across all downstream channels
- `sensor.cable_modem_rate_corrected_errors`: Corrected errors per minute
- `sensor.cable_modem_rate_uncorrected_errors`: Uncorrected errors per minute

### Per-Channel Downstream Sensors (for each channel)

Replace `{type}` with channel type (qam, ofdm) and `X` with the channel number:

- `sensor.cable_modem_ds_{type}_ch_X_power`: Power level in dBmV
- `sensor.cable_modem_ds_{type}_ch_X_snr`: Signal-to-Noise Ratio in dB
- `sensor.cable_modem_ds_{type}_ch_X_frequency`: Channel frequency in Hz
- `sensor.cable_modem_ds_{type}_ch_X_corrected`: Corrected errors
- `sensor.cable_modem_ds_{type}_ch_X_uncorrected`: Uncorrected errors

### Per-Channel Upstream Sensors (for each channel)

Replace `{type}` with channel type (atdma, ofdma) and `X` with the channel number:

- `sensor.cable_modem_us_{type}_ch_X_power`: Transmit power level in dBmV
- `sensor.cable_modem_us_{type}_ch_X_frequency`: Channel frequency in Hz

### Controls

The integration registers three buttons under the modem device:

- **Restart Modem**: Reboots the modem remotely (requires modem credentials)
- **Update Modem Data**: Triggers an immediate poll outside the regular cadence
- **Reset Entities**: Clears entity registry entries for this modem (useful after a modem swap or schema migration)

### Services

- **`cable_modem_monitor.generate_dashboard`**: Generates a Lovelace dashboard YAML tailored to your modem's channel layout. Configurable: which graphs to include (status card, downstream/upstream power/SNR/frequency, errors, latency), `graph_hours` window (1–168), and short-title mode.
- **`cable_modem_monitor.request_refresh`**: Triggers an immediate data poll for the selected device.
- **`cable_modem_monitor.request_health_check`**: Runs an immediate health probe (ICMP / TCP / HTTP) outside the regular cadence.
- **`cable_modem_monitor.convert_channel_identity`**: Renames recorder statistics from the previous Channel Identity mode to the current one, so historical graphs survive a remove-and-re-add in the other mode. Modem must be online.
- **`cable_modem_monitor.orphaned_statistics`**: Lists recorder statistics left behind by a mode switch, channel rebonding, or a prefix change; call again with `execute: true` to purge them. See [Ghost Statistics in History](https://github.com/solentlabs/cable_modem_monitor/blob/main/docs/TROUBLESHOOTING.md#ghost-statistics-in-history).

## Understanding the Values

### Downstream Power (dBmV)

- **Ideal range**: -7 to +7 dBmV
- **Acceptable**: -15 to +15 dBmV
- **Poor**: Below -15 or above +15 dBmV

### Signal-to-Noise Ratio (dB)

- **Excellent**: Above 40 dB
- **Good**: 33-40 dB
- **Acceptable**: 25-33 dB
- **Poor**: Below 25 dB

### Upstream Power (dBmV)

- **Ideal range**: 35-50 dBmV
- **Acceptable**: 30-55 dBmV
- **Poor**: Below 30 or above 55 dBmV

### Corrected vs Uncorrected Errors

- **Corrected errors**: Normal in small amounts; modem can fix these
- **Uncorrected errors**: Indicate data loss; any sustained increase is concerning
- **Monitor trends**: Sudden increases may indicate line issues

## Examples

Ready-to-use dashboard and automation examples are available in the **[Examples Guide](https://github.com/solentlabs/cable_modem_monitor/blob/main/docs/EXAMPLES.md)**.

Includes:

- **Dashboard generator service** — auto-generates Lovelace YAML tailored to your modem's channels
- Complete manual dashboard YAML for monitoring all channels
- Automations for error alerts, SNR warnings, and auto-restart
- Last boot time display format options

## Removing the Integration

To remove a single modem:

1. Go to **Settings → Devices & Services → Cable Modem Monitor**.
2. Click the modem entry, open the **⋯** menu, and choose **Delete**.
3. Confirm. The integration logs out of the modem, removes its device and entities, and deletes the small per-entry state it stored (the channel-bond baseline). Credentials held in Home Assistant's encrypted storage are removed with the entry. No restart is required.

Recorded sensor history is retained according to your Home Assistant **recorder** settings — deleting the integration does not purge it. To clear leftover channel history, use the `orphaned_statistics` service or follow [Ghost Statistics in History](https://github.com/solentlabs/cable_modem_monitor/blob/main/docs/TROUBLESHOOTING.md#ghost-statistics-in-history).

To uninstall completely, delete every modem entry as above, then remove **Cable Modem Monitor** from **HACS** (open it, **⋯** menu → **Remove**) and restart Home Assistant.

## Known Limitations

- **Model is fixed at install time.** It identifies which modem this entry monitors. To point at a different modem, remove the integration and add it again.
- **Channel Identity (number vs ID) is chosen at setup and not changeable afterward.** To switch modes, remove and re-add the integration in the other mode, then call the `convert_channel_identity` service to carry your recorder statistics across.
- **Remote restart depends on the modem.** The Restart button appears only for modems whose firmware exposes a supported reboot path; others are monitored read-only.
- **Channel-bond change alerts track totals only.** The notification fires when the total downstream or upstream channel count changes, not on a per-type reshuffle that leaves the total unchanged (for example a DOCSIS 3.1 provisioning change that swaps one QAM channel for one OFDM channel).
- **Requires a reachable modem interface on your LAN.** Modems whose status page has been disabled by the ISP, or exposed only through the provider's cloud app, cannot be polled. See [ISP Disabled Web Interface](https://github.com/solentlabs/cable_modem_monitor/blob/main/docs/TROUBLESHOOTING.md#6-isp-disabled-web-interface).

## Troubleshooting

**📖 See the [Troubleshooting Guide](https://github.com/solentlabs/cable_modem_monitor/blob/main/docs/TROUBLESHOOTING.md)** for solutions to common issues including connection problems, missing sensors, and duplicate entities.

## Contributing

Contributions are welcome! If you have:

- Support for additional modem models
- Bug fixes
- Feature improvements

Please see the [Contributing Guide](https://github.com/solentlabs/cable_modem_monitor/blob/main/CONTRIBUTING.md) for details on how to add support for your modem, run tests, and submit changes.

## Privacy & Security

### Privacy Protection

- **100% Local**: All data stays on your Home Assistant instance — no cloud services
- **Read-Only by Default**: Only reads data from your modem; the only write action is a user-invoked restart
- **Diagnostic Logs**: Private IPs (RFC1918) and filesystem paths are scrubbed from log lines included in the diagnostics file. Modem identity (model, firmware, channel data) is included verbatim — review before sharing publicly. The diagnostics file embeds a `_review_before_sharing` checklist to help you spot anything sensitive
- **HAR Captures**: When onboarding a new modem, [har-capture](https://github.com/solentlabs/har-capture) sanitizes HAR files (passwords, tokens, MACs) before you submit them
- **Secure Credentials**: Stored in Home Assistant's encrypted storage

### Security Features

- **CodeQL Scanning**: Automated security analysis on every push, weekly schedule, and pull request.
  - **Standard CodeQL Python suite**: 100+ security queries covering OWASP Top 10 and broad CWE coverage — command injection, hardcoded credentials, SSL/TLS misuse, path traversal, XXE, and more.
  - **Custom queries**: One project-specific query at `cable-modem-monitor-ql/queries/no_timeout.ql` flags HTTP requests without explicit timeouts, which would otherwise hang the integration on unreachable modems.
- **Security Documentation**: See [CodeQL Testing Guide](https://github.com/solentlabs/cable_modem_monitor/blob/main/docs/reference/CODEQL_TESTING_GUIDE.md) for details
- **Vulnerability Reporting**: See [SECURITY.md](https://github.com/solentlabs/cable_modem_monitor/blob/main/SECURITY.md) for responsible disclosure

### Authentication Support

- HTTP Basic Authentication
- Form-based authentication
- HNAP/SOAP authentication
- No authentication (for open modems)

## License

MIT License - see LICENSE file for details

## Support

Cable Modem Monitor is maintained by one person, in the evenings, around a day job. I read every issue, and replies are best effort; a few days is normal. Fixes land as time allows.

Catalog contributions are welcome and are the fastest way to get a new modem supported. If you have AI access, you can do most of the intake yourself: see [AI-Assisted Catalog Contribution](https://github.com/solentlabs/cable_modem_monitor/blob/main/CONTRIBUTING.md#ai-assisted-catalog-contribution).

- [GitHub Issues](https://github.com/solentlabs/cable_modem_monitor/issues)
- [Home Assistant Community Forum](https://community.home-assistant.io/)

## Resources

### Project Documentation

- [Changelog](https://github.com/solentlabs/cable_modem_monitor/blob/main/CHANGELOG.md) - Version history and release notes
- [Contributing Guide](https://github.com/solentlabs/cable_modem_monitor/blob/main/CONTRIBUTING.md) - How to contribute code or add modem support
- [Troubleshooting Guide](https://github.com/solentlabs/cable_modem_monitor/blob/main/docs/TROUBLESHOOTING.md) - Common issues and solutions
- [Examples](https://github.com/solentlabs/cable_modem_monitor/blob/main/docs/EXAMPLES.md) - Dashboard and automation YAML
- [Modem Request Guide](https://github.com/solentlabs/cable_modem_monitor/blob/main/docs/MODEM_REQUEST.md) - Help add support for your modem

### External Resources

- [Home Assistant Releases](https://github.com/home-assistant/core/releases)
- [HACS Brand Repository](https://github.com/home-assistant/brands/tree/master/custom_integrations/cable_modem_monitor)

### Related Solent Labs Projects

- [har-capture](https://github.com/solentlabs/har-capture) - Zero-dependency Python library for sanitizing HAR files. Used by this integration to safely capture diagnostic data from cable modems without exposing passwords or network credentials.

## Legal & Safety

**⚠️ Disclaimer:**
This integration interacts with the **user-facing diagnostic interface** (LAN side) of your modem. It does not modify boot files, interact with the ISP side (WAN), or bypass any service limits.

Solent Labs™ is not affiliated with Arris, Motorola, Netgear, or any ISP. All product names are trademarks of their respective owners. Use this software at your own risk.
