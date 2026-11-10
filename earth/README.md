# God's Eye · OSIRIS

The God's Eye panel is a PyQt6 `QWebEngineView` that loads
<https://osirisai.live> directly. It does not start Node.js, a local server, or
the system browser. Its named persistent profile, `aura_godseye`, stores cookies,
local storage, IndexedDB, permissions, and the disk cache under
`config/aura_godseye/`.

The panel uses A.U.R.A.'s black-and-orange HUD styling, keeps its web content
alive while hidden, and freezes the page while closed. Failed loads display an
in-panel retry state and retry with increasing delays; network recovery events
trigger an immediate retry. Fullscreen requests expand only within A.U.R.A.'s
window. Off-site top-level links are routed to A.U.R.A.'s existing in-app
browser. The system browser is never used.

OSIRIS needs an internet connection and WebGL2. The page also uses HTTPS/WSS,
geolocation, fullscreen, clipboard APIs, external map/tile/API services, fonts,
and embedded video providers. Geolocation and clipboard permissions are granted
only to the HTTPS OSIRIS domain; notification, media-capture, and other
permissions are denied. Third-party services and their availability, terms,
and content are controlled by their respective providers.

`AURA_GODSEYE_DEBUG=1` enables the page's developer tools (`Ctrl+Shift+I`) and
more detailed JavaScript console logging. In normal builds, context menus and
developer tools are disabled.

## Troubleshooting

- **Offline / DNS / TLS errors:** Check connectivity, DNS, system time, and
  certificate trust. Use **RETRY** or reconnect; the panel retries automatically.
- **Blank/unsupported map or WebGL2 message:** Update graphics drivers. If
  A.U.R.A.'s optional WebView2 engine is installed, it is used automatically
  when QtWebEngine cannot create a WebGL context.
- **Login or site data does not persist:** Ensure A.U.R.A. can write to
  `config/aura_godseye/`; profile data can be removed only while A.U.R.A. is
  closed if a corrupted cache needs repair.
- **Site/API features unavailable:** OSIRIS depends on remote services and
  embedded providers. Their outages, access policies, and browser support are
  outside A.U.R.A.'s control.

The legacy local globe and MapLibre assets are retained under `_legacy/` for
reference only and are not imported by the active application.
