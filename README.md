# AirBridge

Carrier detect, record and SIP forward for an Icom IC-A210 airband radio, built on a Raspberry Pi.

When the A210 receives a carrier, AirBridge:

1. Detects it from the RSSI voltage (ADS1115 ADC), a COR/squelch line, or both.
2. Operates a relay output.
3. Records the over, including pre-roll, compressed to Opus.
4. Places a SIP call **directly to the Kenwood NXR-1800's built-in SIP interface**, with no PBX or Asterisk. It sends any DTMF the NXR needs, streams the audio as G.711, and hangs up once the over ends.

A web page shows live RSSI, carrier, relay, audio level and call state. It also lists recordings, and all settings are made there.

```
IC-A210 ──RSSI──► ADS1115 ──I²C──┐
        ──COR───────────► GPIO17 ┤
        ──line out──► K5TRA USB ─┤  Raspberry Pi ──Ethernet──► NXR-1800 (SIP IF)
                     relay ◄ GPIO27┘        │
                                         web UI :8080
```

## Hardware

| Item | Notes |
|---|---|
| Raspberry Pi 4 or 5 | Wired Ethernet to the NXR's LAN |
| ADS1115 module | VDD 3.3 V, GND, SDA→GPIO2 (pin 3), SCL→GPIO3 (pin 5), ADDR→GND (0x48) |
| K5TRA 1.1 (CM119A) | Audio from the A210's line/speaker output into the mic/line input |
| Relay module | Opto-isolated, driven from GPIO27 (pin 13). Use an interposing relay for anything serious. |
| COR (optional) | Squelch-open line to GPIO17 (pin 11). Opto-isolate it if it isn't a clean 0–3.3 V signal. |

**RSSI input protection.** The ADS1115 inputs must stay within its supply rail (3.3 V). If the A210's RSSI can exceed that, use a divider and set *Divider ratio* in Setup to match (2.0 for 10k/10k). Fit a 1k series resistor plus a 3.3 V TVS or clamp diodes. If the RSSI source is high-impedance, buffer it with an op-amp follower. Check the voltage range and the pin location in the A210 manual.

## Install (on the Pi)

```bash
git clone <repo> airbridge && cd airbridge
sudo ./install.sh
```

Then open `http://<pi-ip>:8080`, go to **Setup**, and enter the NXR's IP address and SIP details. Logs: `journalctl -u airbridge -f`.

## Setting up

1. **Thresholds.** On the *Test* tab, capture the noise floor with no signal. Then key a weak carrier and capture that, and press **Apply**. On and off thresholds are set between the two, with hysteresis.
2. **dBm readout (optional).** Inject two known levels from a signal generator and capture each one.
3. **SIP.** Press *Place test call* on the *Test* tab. It calls the NXR, sends the configured DTMF, and plays a 1 kHz tone at −10 dBFS, which should come out on air as an NXDN/DMR call.
4. **Relay.** Use *Force ON/OFF* for wiring checks, then *Auto*. *Relay follows* can be set to carrier, call or either.

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

## How timing works

- **Attack** filters blips. The carrier must hold this long before it counts.
- **Hang** keeps one over together across short fades.
- **Pre-roll.** Recordings include the full pre-roll (2 s by default). The call is sent only the *forwarded pre-roll* (600 ms plus attack time), so it doesn't add dead air.
- Audio is buffered while the call sets up, so nothing is clipped. The end-to-end delay is roughly the call setup time plus DTMF plus the forwarded pre-roll, typically 1.5–3 s.
- A new over that starts while a call is still open reuses that call.

## Bench testing without hardware

```bash
pip install -r requirements.txt
python3 tools/fake_nxr.py --port 5070 --out /tmp/nxr &                    # stand-in NXR
AIRBRIDGE_DATA=/tmp/ab python3 -m airbridge                                # then in Setup: Simulation on, Repeater IP 127.0.0.1:5070
python3 -m pytest tests
```

The fake NXR answers calls, can demand auth (`--auth`), can reject calls (`--reject 486`), can use A-law (`--codec PCMA`) or omit RFC 2833 (`--no-dtmf`). It saves the received audio as WAV and prints any DTMF digits it receives.
