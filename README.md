# lapcounter
# Sun Mar 16 11:00:07 PM PDT 2025

## Overview

This a revised version of the *CrossMgr* *LapCounter.html* file.

This extends the original *LapCounter.html* file to include the following features:

- alternate ways to display the bottom row information, category, elapsed time and bell
- improved scaling of the lap counter for modern high resolution displays


## Usage

To use this file, simply copy it to the same directory as the *CrossMgr* *LapCounter.html* file.

Then, open the *LapCounter.html* file in a web browser.

See the install bat file:

- install.bat

### Standalone deployment helper

`crossmgrweb.py` supports two deployment modes.

Install mode writes the current `LapCounter.html` into a CrossMgr HTML directory:

```sh
python3 crossmgrweb.py --install /path/to/CrossMgrHtml
```

Server mode serves `LapCounter.html` directly on port `8675` and points the page at CrossMgr's websocket. If CrossMgr is on the same machine:

```sh
python3 crossmgrweb.py
```

If CrossMgr is on another machine:

```sh
python3 crossmgrweb.py --crossmgr 192.168.40.41
```

Open `http://<server-ip>:8675/LapCounter.html` in the browser. Category-layout URLs such as `http://<server-ip>:8675/LapCounterA-B.html` also work.

To launch the browser from the helper, first check the detected screen numbers:

```sh
python3 crossmgrweb.py --list-screens
```

Then launch on the desired screen:

```sh
python3 crossmgrweb.py --crossmgr 192.168.40.41 --screen 1
```

When KWin is available, screen numbers follow KWin's `Screen N` numbering; `--list-screens` shows the geometry and KWin index. Browser placement uses KDE/KWin's `Switch to Screen N` shortcut through `qdbus6`, launches Chrome in app mode with a dedicated temporary profile, moves it to the target screen, maximizes it, then sends KWin's `Window Fullscreen` shortcut. Firefox is used only as a fallback or when selected with `--browser`.

## Motivation

The original *LapCounter.html* file was not scaling well on modern high resolution displays.

By rearranging the bottom row information we can make the lap counter more larger and easier to read.

We are currently using a reasonably modern Samsung:

- Samsung 32" SD850 WQHD LED Monitor 
- 2560 x 1440
- 178 degree viewing angle
- screen size 28"x15"
- HDMI or DisplayPort

![Samsung 32" SD850 WQHD LED Monitor](./images/IMG_3759.jpg "Samsung 32\" SD850 WQHD LED Monitor")

These appear to be easily available on Facebook Marketplace for $100-$200 (March 2025, Vancouver BC, two available).

## Options
Clicking on the web page brings up a configuration dialog box.

The following options are available:
- Left
- Standard
- Right
- Category/Time only

## Other

DisplayPort 10' cable $15 Cdn
DisplayPort 20' cable $45 Cdn

N.b. most laptops have DisplayPort output, check your laptop for compatibility.

Tripod:
- Stand Screen Gator Frameworks GFW-AV-LCD-1 $100 Cdn

Cases:
- Gator Case large (medium 27-32" LCD screens) $199 Cdn
- Toribio Tripod Carrying Case 41.5" $40 


## Testing resolutions for dev tools

-720x1280 
- 480x854
