# Changelog

All notable changes to AirBridge. Versions follow [semantic versioning](https://semver.org/);
each release is tagged in git (`v0.7.0` etc.). The version number lives in `airbridge/__init__.py`.

## [0.9.0] - 2026-10-29
### Added
- Login and setup pages now have a logo and a wallpaper. The defaults are an original AirBridge logo and an
  airfield-at-night wallpaper, both built in with no internet needed.
- Setup → Login screen branding lets you upload your own logo and wallpaper (PNG, JPEG, WebP or SVG) or restore
  the defaults. This is admin only and audited. Uploaded SVGs are served sandboxed.
- The site name is shown under the sign-in box.
### Fixed
- install.sh: `SRC: unbound variable`; git could not read the clone when run as root; upgrades now restart the
  service and the installer checks that AirBridge started.

## [0.8.0] - 2026-10-28
### Added
- Path delay is measured on every over (airband → NXR) and every NXR burst (NXR → airband). It is shown on the
  Status page and stored with each recording.
- Call setup and DTMF times are measured on every call.
- Delay test on the Test tab: SIP ping, call setup, DTMF, the delay AirBridge adds in each direction, and an
  echo round trip using a 1 kHz tone burst, when something on the NXDN side echoes it back. NXR audio is never
  transmitted on airband during the test.
- `tools/fake_nxr.py --echo SECONDS`, which simulates an NXDN parrot for bench testing.
### Changed
- Desktop layout: the status grid packs without gaps, settings are in two columns, and each recording is on one row.
- Alarm and TX events are coloured; alarms from the TX test tone are labelled as such.

## [0.7.0] - 2026-10-28
### Added
- Version tracking. The version and build are shown in the web footer, in Access → About, on the LCD
  boot splash and at `/api/version`.
- Per-unit version history. Each new build that starts is logged on the Pi and recorded in the audit log.
- `install.sh` stamps the installed version and commit (`/opt/airbridge/version.json`).
- This changelog, plus git tags for all earlier releases.

## [0.6.0] - 2026-10-27
### Added
- Front panel 20×4 I²C LCD (HD44780 on a PCF8574 backpack) with a live mirror on the Status page:
  state and clock, RSSI bar, SIP/TX state, alarms, IP address and today's counts. Backlight can be
  on, off or auto. The display reconnects if unplugged.

## [0.5.0] - 2026-10-27
### Added
- Access control: user accounts with viewer, operator and admin roles; scrypt password hashes;
  server-side sessions; login lockout; audit log; IP allowlist; CSRF protection.
- One-time setup code for the first admin; the old single password is migrated to an `admin` user.
- Access tab for users, sessions, the audit log and changing your own password.
- `python -m airbridge user ...` for password recovery from the command line.

## [0.4.0] - 2026-10-27
### Added
- Adaptive RSSI thresholds that track the drifting idle floor, with re-learning if a carrier sticks on.
- History tab: 24 h of RSSI at 1 s resolution, site statistics and suggested offsets.

## [0.3.0] - 2026-10-27
### Added
- AF carrier detection, in noise-quieting or level mode.
- RSSI, COR and AF can be combined as trigger sources (any or all).
### Changed
- COR input now defaults to the K5TRA COR input.

## [0.2.0] - 2026-10-27
### Added
- Two-way audio: NXR → A210 transmit through the K5TRA (CM119A PTT), with the busy channel rule
  (NXR audio is dropped if the airband channel is busy).
- Inbound SIP calls from the NXR, and a reply-hold window on outbound calls.
- AF analysis: calibrated RX/TX meters, level alarms, tone analysis (frequency, SINAD, THD),
  spectrum and a TX test tone.

## [0.1.0] - 2026-10-27
### Added
- First release: RSSI carrier detection (ADS1115), relay output, recording with pre-roll, direct
  SIP to the NXR-1800 (no PBX), and a web status, recordings and setup page.

[0.9.0]: https://github.com/gomeznz/Airbridge/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/gomeznz/Airbridge/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/gomeznz/Airbridge/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/gomeznz/Airbridge/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/gomeznz/Airbridge/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/gomeznz/Airbridge/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/gomeznz/Airbridge/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/gomeznz/Airbridge/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/gomeznz/Airbridge/releases/tag/v0.1.0
