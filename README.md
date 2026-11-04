<p align="center">
  <img src="docs/png/meritz-gqt.png" alt="meritz GQT" width="360" />
</p>

## Quick Start

Shared research server. Choose your own ports.

```bash
/home/base/longshort/longshort_platform/longshort start --port 50017 --ui-port 50016
```

Open `http://<host>:50016`. Your settings, modules, builds, and UI cache live in `~/longshort_workspace/`.

```bash
cd /home/base/longshort/longshort_platform
./longshort start
./longshort restart
./longshort stop
```

## Example

Paste into the editor and run:

```xml
<data expr="returns" delay="1"/>
<tsmean days="20"/>
<zscore/>
<neutralize group="sector"/>
```

## Shared Setup

Administrator, once per server:

```bash
cd /home/base/longshort/longshort_platform
./longshort init
```

Installs C++ libraries in `../dependencies/` and prepares the shared frontend packages.

## Layout

```text
/home/base/longshort/
├── longshort_platform/    # Shared source
├── dependencies/          # Shared C++ libraries
├── alpha_pool/            # Approved alphas
├── meta_pool/             # Meta strategies
└── portfolio/             # Portfolio definitions

~/longshort_workspace/     # Per-user settings, modules, builds, and results
```
