# Repository Guidelines

## Project Structure & Module Organization
- `LapCounter.html`: Single-page app that renders the lap counter via `<canvas>` and connects to a WebSocket for live updates.
- `images/`: Static assets used by the README and docs.
- `updatelap.sh`: Deploys `Lap*html` to a local CrossMgr installation (Linux path).
- `scplap.sh`: Copies `Lap*html` to a Windows CrossMgr path and syncs files from a remote host. Update user/host as needed.

## Build, Test, and Development Commands
- Local preview (static): `python3 -m http.server 8000` then open `http://localhost:8000/LapCounter.html`.
- CrossMgr WebSocket: The page computes `ws://<host>:<port+2>/` from the current page port. When served on `:8000`, CrossMgr is expected on `:8002`. Without CrossMgr, the UI loads but shows no live data.
- Deploy to CrossMgr (Linux): `./updatelap.sh` (adjust path if your Python/CrossMgr install differs).
- Remote sync (Windows/remote): `./scplap.sh` (edit user/host and target paths before use).

## Coding Style & Naming Conventions
- Indentation: 4 spaces; no tabs.
- JavaScript: semicolons required; `camelCase` for variables/functions; constants like `MaxLabels` in PascalCase/UpperCamel as used.
- HTML/CSS: keep inline CSS minimal; prefer readability over cleverness.
- Filenames: primary artifact is `LapCounter.html`; auxiliary images/scripts live under `images/` and repo root.

## Testing Guidelines
- Manual checks: open in a browser, resize window (portrait/landscape), click to open the layout modal; verify Standard/Right/Minimal/Timer Only modes and color/invert options.
- Live data: run CrossMgr so WebSocket `refresh` messages populate labels/colors; watch the console for connection logs.
- Visuals: verify legibility on common resolutions (e.g., 2560×1440, 1280×720). Include screenshots in PRs when UI changes.

## Commit & Pull Request Guidelines
- Commits: concise, imperative subject (e.g., `fix wsurl`, `improve layout scaling`). Group related edits.
- PRs: clear description, before/after screenshots for UI changes, steps to reproduce, and any risks. Link issues when applicable.

## Security & Configuration Tips
- Do not hardcode `wsurl`; the current code derives it from `window.location`. Only edit commented examples if you know your network layout.
- Local settings persist in `localStorage` (e.g., `color_layout`, `invert_colors`). Document new keys in the PR.

