# Requesting Support for Your Modem

Don't see your modem listed? Adding support starts with a HAR capture
from your modem's web interface. This guide walks you through capturing
and submitting it. From there, a maintainer or contributor builds a
parser, and you verify it works on your hardware before it ships.

This guide is for **Home Assistant users** who want to request support.
If you're comfortable with AI tools and want to help move things faster
(analyze the capture yourself, propose a catalog entry, help triage
issues), see the
[AI-assisted catalog contribution guide](../CONTRIBUTING.md#ai-assisted-catalog-contribution)
in CONTRIBUTING.md instead.

## What's collected

- Downstream/upstream channel data (frequency, power, SNR)
- Error counts (corrected/uncorrectable codewords)
- Connection status and DOCSIS lock state
- System information (firmware, uptime)

## What's not collected

WiFi settings, router configuration, device lists, account information.

---

## Step 1 — Capture

Install har-capture and run it against your modem's IP:

```bash
pip install "har-capture[full]"
har-capture 192.168.100.1 --patterns network-device
```

`--patterns network-device` selects the PII rule set for routers and
modems. Replace `192.168.100.1` with your modem's IP if it differs.
If your modem requires HTTP Basic Auth, add `--username` and
`--password` flags — see
[har-capture's CLI reference](https://github.com/solentlabs/har-capture#quick-start)
for details.

A few cable-modem-specific tips, in the order they happen during the
capture:

- **Before logging in, visit one status page directly** by typing its
  address into the address bar (for example
  `192.168.100.1/DocsisStatus.htm` — any status page you know works).
  You'll likely just see the login page again: that's the point. It
  records how your modem answers a data request without a valid
  session, which is exactly what the integration sees when its session
  expires mid-poll.
- **Log in once with a wrong password on purpose**, then log in with
  the real one. The rejected attempt teaches the integration how your
  modem refuses bad credentials — on some modems a failed login looks
  identical to a success unless you know what to compare, and this
  evidence can't be reconstructed later. (Your wrong guess is redacted
  like any other password.)
- **After the real login, let the page your modem sends you to finish
  loading** before clicking anything else. Many modems answer the login
  with a redirect, and that landing page is how the integration confirms
  the login worked. A capture that moves on before it loads cannot be
  used to test the login.
- **Visit all status pages**, and wait 3–5 seconds per page for async
  data to load. har-capture launches its own controlled chromium
  instance, so there's no need to use your regular browser's incognito
  mode — each capture starts from a clean session.
- **Click your modem's Logout link last**, before closing the browser.
  Some modems allow only one login at a time. Without the logout
  request in the capture, the integration cannot learn how to release
  the session, and may hold it while you are trying to reach the
  modem's own web page.

har-capture produces a sanitized, gzipped `.sanitized.har.gz` file —
that's the artifact to attach in Step 3.

## Step 2 — Review for PII

`har-capture` automatically redacts MAC addresses, serial numbers,
public IPs, and known credential patterns — but it's best-effort. Some
modems embed WiFi credentials in unlabeled JavaScript or proprietary
blobs the sanitizer hasn't seen. Pick one of these to verify before
sharing:

- **AI-assisted self-screen (faster)** — paste a prepared prompt into
  ChatGPT, Claude, or any AI assistant that takes file attachments.
  Full prompt and instructions:
  [docs/examples/har-pii-screen-prompt.md](examples/har-pii-screen-prompt.md).
- **Manual checklist (5 minutes)** — open the file in a text editor and
  search for a short list of patterns:
  [docs/examples/har-pii-manual-checklist.md](examples/har-pii-manual-checklist.md).

If anything sensitive remains, replace it with `***REDACTED***` in the
`.sanitized.har`, save, re-gzip (`gzip -kf -9 yourfile.sanitized.har`),
and note what you redacted in your issue so the sanitizer can be
improved for future contributors. Running
`har-capture validate yourfile.sanitized.har --patterns network-device`
afterwards confirms nothing leaked and that the `.har` and `.har.gz`
still match.

## Step 3 — Submit

Open the [Modem Request issue template](https://github.com/solentlabs/cable_modem_monitor/issues/new?template=modem_request.yml)
and:

- Fill in modem details (model, manufacturer)
- Attach your `.sanitized.har.gz`
- If you ran the AI screen, include the output block in your issue
- Note any manual redactions you made

---

## Privacy summary

| Data type | What happens |
|-----------|--------------|
| WiFi credentials | Auto-redacted; **verify before sharing** |
| MAC addresses | Auto-redacted (hashed, format `02:xx:xx:xx:xx:xx`) |
| Serial numbers | Auto-redacted (hashed, `SERIAL_*` prefix) |
| Public IPs | Auto-redacted (`192.0.2.x` TEST-NET reserved range) |
| Channel data (power, SNR) | Preserved — needed for parser |
| Firmware version | Preserved — useful for compatibility |
| Uptime | Preserved — useful for testing |

Modem IPs like `192.168.100.1` are preserved — they're standard
defaults, not personal information.

---

## Resources

- Browse [existing modem request issues](https://github.com/solentlabs/cable_modem_monitor/issues?q=label%3A%22new+modem%22)
  for examples
- See the [modem catalog](../packages/cable_modem_monitor_catalog/solentlabs/cable_modem_monitor_catalog/modems/)
  for currently supported modems
