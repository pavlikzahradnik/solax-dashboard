# SolaX Dashboard

A local monitoring app for the **SolaX X3 Hybrid G4** solar inverter.
Shows live data from the inverter (and optionally wallboxes) read over your
local network via the dongle's local HTTP API — no cloud, everything stays
at home.

> **Read-only.** The app only displays data; it does not set or control
> anything on the inverter or wallbox.

## Features

- Live energy flow overview (PV production, house, grid, battery, wallbox)
- Per-phase power and voltage (L1/L2/L3)
- PV strings shown separately (PV1 / PV2)
- Battery detail on click — SOC, power, voltage, capacity, cell voltage and
  temperature (min–max), charge/discharge today and total
- Inverter heatsink and inner temperature
- Inverter run mode (Normal, Waiting, Fault…)
- Daily chart (production / consumption / grid / SOC)
- Up to two wallboxes
- Czech and English UI

## Who it's for

SolaX **X3 Hybrid G4** inverters (type 14). Other models are detected and the
app says clearly that they're not supported — instead of showing wrong values.

The app must run on a PC on the **same network** as the inverter. Reading data
requires the **dongle password** (the dongle's serial number, found on its
label or in the SolaX app).

## Install (for users)

Download the latest `SolaXDashboard.exe` from the
[Releases](../../releases) section and run it. On first launch, enter your
inverter IP and dongle password.

> Windows SmartScreen may warn about an unknown app — *More info → Run anyway*.
> Built and tested on Windows 10 and 11.

## Run from source (for developers)

```bash
pip install flask waitress pywebview
py solax_desktop.py
```

## Build the .exe

```bash
py -m PyInstaller --onefile --noconsole --name SolaXDashboard ^
   --icon solax.ico --collect-all pywebview ^
   --collect-submodules waitress solax_desktop.py
```

## Credits

The value mapping for the local HTTP API is based on community
reverse-engineering, in particular:

- [nazar-pc/solax-local-api-docs](https://github.com/nazar-pc/solax-local-api-docs)
- [wills106/homeassistant-solax-modbus](https://github.com/wills106/homeassistant-solax-modbus)
- [PatrikTrestik/homeassistant-solax-http](https://github.com/PatrikTrestik/homeassistant-solax-http)

Thanks to everyone who figured it out.

## Disclaimer

Unofficial project, not affiliated with SolaX Power. The value mapping is
verified on a specific inverter (X3 Hybrid G4), but on other firmware or
hardware variants some edge values may differ. Use at your own risk, no
warranty.

## License

GNU GPLv3 — see [LICENSE](LICENSE). If you modify and redistribute the app,
you must also publish your source code under the same license.
