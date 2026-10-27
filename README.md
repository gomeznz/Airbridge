# AirBridge

Two-way bridge between an Icom IC-A210 airband radio and a Kenwood NXR-1800 repeater, over SIP, built on a Raspberry Pi with a K5TRA (CM119A) USB interface.

When the A210 receives a carrier, AirBridge:

1. Detects it from any mix of three sources: the RSSI voltage (ADS1115 ADC), the COR line (K5TRA COR input), and the receiver audio itself (AF detection).
2. Operates a relay output.
3. Records the over, including pre-roll, compressed to Opus.
4. Places a SIP call **directly to the Kenwood NXR-1800's built-in SIP interface**, with no PBX or Asterisk. It sends any DTMF the NXR needs and streams the audio as G.711. The call stays open for a reply window, then hangs up.

In the other direction, audio from the NXR keys the A210 through the K5TRA and is transmitted. That audio can arrive on an open call or on a call the NXR places to the Pi.

- **Busy-channel rule:** if the airband channel is busy when NXR audio arrives, the burst is dropped, never transmitted over traffic, and logged as *dropped*.
- **PTT** comes from the K5TRA's CM119A GPIO, keyed by VOX on the received audio, with lead and tail times and a TX time-out.
- **RX guard:** RSSI/COR are ignored while transmitting and for a short guard time afterwards, so the A210's own transmission never triggers the detector.

**AF analysis** covers both directions:
- Live RX/TX meters in dBFS, dBm and mV (600 Ω, after calibration), with peak hold.
- Alarms for low, high or clipping levels per over and burst, and for carrier with no audio.
- Tone analysis: frequency, level, SINAD and THD.
- A live 0–4 kHz spectrum.
- A TX test-tone generator for setting the A210 mic level.

A web page shows live RSSI, carrier, PTT, relay, AF levels and call state. It also lists recordings for both directions, and all settings are made there.

```
IC-A210 ──RSSI──► ADS1115 ──I²C──┐
        ──COR───────────► GPIO17 ┤
        ◄─mic/PTT── K5TRA USB ◄──┤
        ──line out──► K5TRA USB ─┤  Raspberry Pi ◄─Ethernet─► NXR-1800 (SIP IF)
                     relay ◄ GPIO27┘        │
                                         web UI :8080
```

## Hardware

| Item | Notes |
|---|---|
| Raspberry Pi 4 or 5 | Wired Ethernet to the NXR's LAN |
| ADS1115 module | VDD 3.3 V, GND, SDA→GPIO2 (pin 3), SCL→GPIO3 (pin 5), ADDR→GND (0x48) |
| K5TRA 1.1 (CM119A) | RX: A210 line/speaker out → K5TRA input. TX: K5TRA output → A210 mic/line in. PTT: CM119A GPIO (default GPIO3) → A210 PTT. COS (optional): A210 squelch → K5TRA COS input |
| 20×4 LCD (optional) | HD44780 with a PCF8574 I²C backpack, on the same I²C bus as the ADS1115 (SDA → GPIO2, SCL → GPIO3). Address 0x27 or 0x3F is auto-detected. VCC 5 V: most backpacks pull SDA/SCL up to 5 V, so remove those pull-ups or fit a level shifter (the Pi's pins are 3.3 V only). |
| Relay module | Opto-isolated, driven from GPIO27 (pin 13). Use an interposing relay for anything serious. |
| COR (optional) | Into the K5TRA COR input. A Pi GPIO (GPIO17, pin 11) is available as an alternative. |

The K5TRA's PL-detect input is not used: CTCSS isn't used in aviation.

**RSSI input protection.** The ADS1115 inputs must stay within its supply rail (3.3 V). If the A210's RSSI can exceed that, use a divider and set *Divider ratio* in Setup to match (2.0 for 10k/10k). Fit a 1k series resistor plus a 3.3 V TVS or clamp diodes. If the RSSI source is high-impedance, buffer it with an op-amp follower. Check the voltage range and the pin location in the A210 manual.

## Versions

AirBridge uses semantic versioning. The version number lives in `airbridge/__init__.py`, every release is tagged in git (`v0.7.0`, …), and [CHANGELOG.md](CHANGELOG.md) lists what changed. The running version and build are shown:
- in the web page footer and in **Access → About**;
- on the LCD for three seconds at boot;
- at `/api/version`.

Each unit keeps its own upgrade history, which appears in Access → About and in the audit log.

To install a specific release, run `git checkout v0.6.0 && sudo ./install.sh`. To go back to the latest, run `git checkout main && git pull && sudo ./install.sh`.

**Making a release:** bump `__version__`, add a CHANGELOG entry (a test checks that it's there), commit, then `git tag -a vX.Y.Z -m "..." && git push --follow-tags`.

## Install (on the Pi)

```bash
git clone <repo> airbridge && cd airbridge
sudo ./install.sh
```

Then open `http://<pi-ip>:8080`, go to **Setup**, and enter the NXR's IP address and SIP details. Logs: `journalctl -u airbridge -f`.

## Access control

The web panel requires a sign-in. Each user has one role:

| Role | Can |
|---|---|
| viewer | Status, history, audio analysis, listen to recordings |
| operator | + test calls, TX test tone, PTT release, relay override, hang up, delete recordings |
| admin | + settings, users, sessions, audit log |

**First admin.** On a fresh install, the installer prints a one-time setup code, which is also in the service log and in `/var/lib/airbridge/setup-code.txt`. Open `/setup` and create the admin account with that code. The setup page disappears once an account exists. A password set in an older version is migrated automatically to a user called `admin`, and the plain-text copy is removed.

**Managing access.** Use the **Access** tab:
- Add users, change roles, reset passwords, disable or delete accounts.
- See active sessions and sign any of them out.
- Read the audit log. It records sign-ins, failed attempts, lockouts, settings changes, test calls, TX tones, relay and PTT actions, and user changes, each with user and IP.
- Every user can change their own password there.

**Protections:**
- Passwords are stored as scrypt hashes, with a 10-character minimum.
- Sessions are server-side, signed out after 12 h idle and expiring after 30 days at most.
- Cookies are HttpOnly and SameSite=strict.
- State-changing requests need a CSRF header and a same-origin request.
- After 5 failed sign-ins in 15 minutes, both that user and that IP are locked out for the rest of the window.
- Changing a user's role, password or enabled state signs out all of their sessions.
- The last active admin can't be removed or demoted.
- Security headers include no framing and a CSP.

**Network limits.** Setup → Web → *Allowed networks* takes a list such as `192.168.1.0/24`. It restricts which addresses can reach the panel at all, and users still have to sign in. If the panel is published through Cloudflare Tunnel or a reverse proxy, tick *Behind a proxy / tunnel* so the real client IP is used for the allowlist, lockout and audit log.

**Locked out of the admin account?** From the Pi:

```bash
sudo -u airbridge AIRBRIDGE_DATA=/var/lib/airbridge /opt/airbridge/venv/bin/python -m airbridge user passwd <name>
# also: user list | user add NAME --role admin | user role NAME operator | user enable|disable NAME
```

## Setting up

1. **Thresholds.** On the *Test* tab, capture the noise floor with no signal. Then key a weak carrier and capture that, and press **Apply**. On and off thresholds are set between the two, with hysteresis.
2. **dBm readout (optional).** Inject two known levels from a signal generator and capture each one.
3. **SIP.** Press *Place test call* on the *Test* tab. It calls the NXR, sends the configured DTMF, and plays a 1 kHz tone at −10 dBFS, which should come out on air as an NXDN/DMR call.
4. **Relay.** Use *Force ON/OFF* for wiring checks, then *Auto*. *Relay follows* can be set to carrier, call, PTT, either, or any.
5. **AF calibration.** On the *Audio* tab:
   - RX: feed a known level (e.g. −10 dBm, 600 Ω) into the K5TRA input and press *Set RX*.
   - TX: key a TX test tone, measure the K5TRA output with a meter, enter the reading and press *Set TX*.
6. **TX level.** Key a TX test tone and set *Output gain* in Setup so the A210 modulates correctly. Check this on a test receiver or modulation meter.

### NXR-1800 settings to confirm in the manual

| AirBridge setting | What to find out |
|---|---|
| Number / extension to dial | What the NXR expects in the request URI, if anything |
| DTMF after answer | Whether the NXR selects the talkgroup/ID by DTMF, a fixed default or the dialled number |
| Auth username / password | Whether the NXR challenges inbound INVITEs |
| Register first | Only if the NXR refuses calls from a UA that hasn't registered with it |
| Codec / packet time | μ-law or A-law; 20 ms is the usual default |
| Max call (s) | Keep this inside the NXR's TX time-out |

AirBridge listens on UDP 5062 for SIP by default, with RTP on random even ports between 16384 and 32766. If the NXR only sends to 5060, change *Local SIP port*.

## RSSI thresholds: adaptive floor tracking

The A210 has no COR output, and an RSSI tap on its AGC line drifts with temperature, supply voltage and the site noise floor. This is why a fixed threshold, or a hardware Schmitt trigger, is hard to keep set.

In **adaptive** mode (the default), AirBridge tracks the idle floor continuously:
- The floor is the 20th percentile of the last 120 s of readings, taken only while no carrier is present, so carriers can't pull it up.
- The detector triggers at floor + *on offset* and clears at floor + *off offset*, so the thresholds move with the drift.
- Until the floor has been learned (about 5 s after start), the fixed thresholds are used.
- If RSSI stays "high" longer than *max carrier* (120 s), that isn't a real over: the floor has stepped (new interference, a supply change). The floor is re-learned and an alarm is logged, so the detector can never stick on.

The **History** tab keeps 24 h of RSSI at 1 s resolution, and it survives restarts. It shows:
- RSSI as a min/max band and mean, with the floor, the effective on/off thresholds, and shading for carrier and TX.
- The AF detector trace, when AF is in use.
- Site statistics: floor drift, idle excursion above the floor, and typical carrier lift. From these it suggests on/off offsets, which one button applies.

Leave it running for a few hours, including some real traffic, before using the suggestion. For a belt-and-braces trigger, use RSSI + AF noise with *all*.

**Tap hardware.** Buffer the AGC/RSSI tap with a rail-to-rail op-amp follower and a small RC (10–20 ms) before the ADS1115, and power the ADS1115 from a clean 3.3 V. Loading the AGC line directly can change the radio's own AGC behaviour and adds noise to the reading.

## Front panel LCD

A 20×4 I²C LCD shows the bridge state at the radio:

```
CARRIER 4.2s   23:47      IDLE / CARRIER / HANG / TX / BUSY: NXR DROPPED, plus the time
RSSI███████▏   2.35V      RSSI bar and volts (AF level if RSSI isn't used)
SIP connected 12s         call state; IN for calls from the NXR; FAIL + reason
192.168.1.60              alarms (flashing) take priority; otherwise alternates IP / today's counts
```

The Status page shows a live mirror of the display, so you can check it in simulation or remotely. The backlight can be always on, always off, or *auto*: lit while there's activity and off after a timeout. If the display is unplugged, AirBridge keeps running and reconnects every 10 s. Settings are in Setup → *Front panel LCD*.

## Path delay

AirBridge measures how far behind live the audio is delivered, every time:
- **Airband → NXR:** typically about 1.3 s for a new call, or about 2.9 s when the group is selected by DTMF. On a call that's already open, it's about 0.9 s.
- **NXR → airband:** about 0.25 s (jitter buffer, PTT lead and sound-card output).

The Status page shows the latest figures, and every recording stores its own delay. The NXR's own NXDN processing adds to both. To shorten airband → NXR, the most effective change is dialling the group as the *number* instead of sending it as DTMF. After that, lower *Forwarded pre-roll* and *Attack*.

**Delay test** (Test tab, operator or admin) measures:
- SIP ping round trip;
- call setup and DTMF time;
- the delay AirBridge adds in each direction;
- an **echo round trip**. AirBridge sends a 1 kHz tone burst and times its return to ±20 ms. This needs something on the NXDN side to echo the audio back, such as an NXDN radio set up as a parrot on the group.

NXR audio is never put on air during the test. To try it on the bench, run `tools/fake_nxr.py --echo 0.45`.

## Carrier detection sources

Pick any combination in Setup → *Trigger sources*. *Combine* = **any** fires on whichever source sees a carrier first. **all** requires every selected source to agree, which is useful for rejecting false triggers.

| Source | How | Notes |
|---|---|---|
| RSSI | ADS1115 reading the A210 RSSI voltage | Best sensitivity if the A210 brings out a usable RSSI |
| COR | K5TRA COR input (squelch-open line) | Clean and simple; follows the A210 squelch setting |
| AF | The receiver audio on the K5TRA AF-IN | No extra wiring; two modes, below |

**AF noise mode** (default) is for use with the A210 squelch **open**. AirBridge measures the receiver noise above the voice band (4–7 kHz on the raw 48 kHz capture). When a carrier arrives, the AGC quiets that noise, which is how a classic noise squelch works. It also catches an unmodulated carrier and isn't fooled by loud speech. To check that the A210 line-out actually passes audio above 4 kHz, watch the *AF noise* reading on the Status page with the squelch open: it should drop by 10 dB or more when a carrier arrives. If it barely moves, move the band lower (e.g. 3000–3800 Hz) or use level mode.

**AF level mode** is for use with the A210 squelch **set**. Any audio above the threshold counts as a carrier. It is simpler, but a carrier with no modulation is missed until someone speaks.

The *Test* tab's floor/carrier capture sets the RSSI and AF thresholds together. Like RSSI and COR, AF detection is ignored while transmitting and during the RX guard time.

## How timing works

- **Attack** filters blips. The carrier must hold this long before it counts.
- **Hang** keeps one over together across short fades.
- **Pre-roll.** Recordings include the full pre-roll (2 s by default). The call is sent only the *forwarded pre-roll* (600 ms plus attack time), so it doesn't add dead air.
- Audio is buffered while the call sets up, so nothing is clipped. The end-to-end delay is roughly the call setup time plus DTMF plus the forwarded pre-roll, typically 1.5–3 s.
- A new over that starts while a call is still open reuses that call. This includes an inbound call from the NXR.
- **Reply hold** keeps an outbound call open (10 s by default) after the last activity in either direction, so NXR users can reply.
- NXR audio keys the A210 once it exceeds the *VOX threshold*. It is held for *VOX hang*, then released. If the NXR keeps streaming silence, the PTT still releases.

## Bench testing without hardware

```bash
pip install -r requirements.txt
python3 tools/fake_nxr.py --port 5070 --out /tmp/nxr --talkback 3,3 &     # stand-in NXR that replies after 3 s
python3 tools/nxr_call_in.py --to 127.0.0.1:5062 --talk 3                    # NXR placing an inbound call
AIRBRIDGE_DATA=/tmp/ab python3 -m airbridge                                # then in Setup: Simulation on, Repeater IP 127.0.0.1:5070
python3 -m pytest tests
```

The fake NXR answers calls, can demand auth (`--auth`), can reject calls (`--reject 486`), can use A-law (`--codec PCMA`) or omit RFC 2833 (`--no-dtmf`). It saves the received audio as WAV and prints any DTMF digits it receives.
