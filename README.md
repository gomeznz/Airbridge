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
| Relay module | Opto-isolated, driven from GPIO27 (pin 13). Use an interposing relay for anything serious. |
| COR (optional) | Into the K5TRA COR input. A Pi GPIO (GPIO17, pin 11) is available as an alternative. |

The K5TRA's PL-detect input is not used: CTCSS isn't used in aviation.

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

## Carrier detection sources

Pick any combination in Setup → *Trigger sources*. *Combine* = **any** fires on whichever source sees a carrier first. **all** requires every selected source to agree, which is useful for rejecting false triggers.

| Source | How | Notes |
|---|---|---|
| RSSI | ADS1115 reading the A210 RSSI voltage | Best sensitivity if the A210 brings out a usable RSSI |
| COR | K5TRA COR input (squelch-open line) | Clean and simple; follows the A210 squelch setting |
| AF | The receiver audio on the K5TRA AF-IN | No extra wiring; two modes, below |

**AF noise mode** (default) is for use with the A210 squelch **open**. AirBridge measures the receiver noise above the voice band (4–7 kHz on the raw 48 kHz capture). When a carrier arrives, the AGC quiets that noise, which is how a classic noise squelch works. It also catches an unmodulated carrier and isn't fooled by loud speech. Use the *Audio* tab spectrum to check that the A210 line-out actually passes audio above 4 kHz. If it doesn't, move the band lower or use level mode.

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
