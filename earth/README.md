# God's Eye Earth widget

The widget is an in-app PyQt6 WebEngine view. MapLibre GL JS and its CSS are
bundled under `web/vendor`; the page never opens an external browser or loads
its JavaScript from a CDN. Qt WebChannel carries page requests back to the
native widget, and the `earth_control` action can open the map or plot a route.
Cross-origin JSON requests are fetched asynchronously by a small, host-allowlisted
Qt bridge, avoiding Chromium's opaque `file://` origin/CORS failures.

The interactive map uses public, keyless data endpoints for OpenStreetMap
tiles, Open-Meteo geocoding/weather, Nominatim reverse geocoding, Wikidata
administrative capitals, OSRM road routes, AWS elevation tiles, and Esri
imagery. These services currently do not require an API key, but are internet
services with their own availability,
attribution, usage policies, and terms; the project cannot guarantee that
third-party services will remain free or available forever. Weather and route
travel times are estimates, not emergency information or live schedules.

Qt WebEngine is configured to use Chromium's SwiftShader backend so the globe
can render on CPU-only machines. An internet connection is required for current
map, weather, and route data. If WebEngine is missing, A.U.R.A. keeps running
and displays its local fallback globe instead.
