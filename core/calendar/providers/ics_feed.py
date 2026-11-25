"""IcsFeedProvider — READ-ONLY subscription to a private HTTPS iCal (.ics) URL.

Security & robustness:
- HTTPS only (rejects http, file, etc. and blocks non-HTTPS redirects)
- SSRF protection: blocks localhost, loopback, private, link-local, reserved IPs
- Conditional GET (ETag / If-Modified-Since)
- 5 s request timeout, 5 MB max body size, 5000 max VEVENTs
- Caches last good copy in SQLite for offline reads
- Never logs the feed URL
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urlparse

import icalendar
import requests

from core.calendar.models import CalendarError, Event, ReadOnlyCalendarError, ensure_aware, get_local_tz
from core.calendar.providers.base import CalendarProvider, ProviderHealth
from core.calendar.recurrence import expand_events
from core.calendar.secrets import SecretStore
from core.calendar.store import CalendarStore

LOGGER = logging.getLogger(__name__)

MAX_ICS_BYTES = 5 * 1024 * 1024  # 5 MB
MAX_ICS_EVENTS = 5000
FEED_TIMEOUT_SECONDS = 5


def validate_feed_url(url: str) -> str:
    """Validate that an ICS feed URL is HTTPS and does not target local/private hosts."""
    cleaned = (url or "").strip()
    if not cleaned:
        raise CalendarError("Feed URL cannot be empty.")
    parsed = urlparse(cleaned)
    if parsed.scheme.lower() != "https":
        raise CalendarError("Only HTTPS calendar links are allowed.")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise CalendarError("Invalid calendar link: missing host.")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise CalendarError("Local network calendar links are not allowed.")

    # Check literal IP address first
    try:
        ip = ipaddress.ip_address(host)
        if _is_disallowed_ip(ip):
            raise CalendarError("Private or local IP addresses are not allowed for calendar links.")
        return cleaned
    except ValueError:
        pass

    # Resolve DNS and ensure no private/loopback/link-local addresses are returned
    try:
        infos = socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
    except OSError:
        # Allow creation if offline; runtime fetch will re-verify before connecting.
        return cleaned

    for info in infos:
        addr_str = info[4][0].split("%")[0]
        try:
            ip = ipaddress.ip_address(addr_str)
            if _is_disallowed_ip(ip):
                raise CalendarError("Calendar link resolves to a private or local network address.")
        except ValueError:
            continue
    return cleaned


def _is_disallowed_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def parse_ics_bytes(
    raw: bytes,
    calendar_id: str,
    source: str = "ics_feed",
    max_events: int = MAX_ICS_EVENTS,
) -> list[Event]:
    """Parse raw .ics bytes into Event objects with strict caps and defensive handling."""
    if len(raw) > MAX_ICS_BYTES:
        raise CalendarError("Calendar data exceeds the 5 MB size limit.")
    try:
        cal = icalendar.Calendar.from_ical(raw)
    except Exception as exc:
        raise CalendarError(f"Malformed ICS data: {exc}") from exc

    tz = get_local_tz()
    events: list[Event] = []
    for component in cal.walk("VEVENT"):
        if len(events) >= max_events:
            LOGGER.warning("ICS event cap (%d) reached for calendar %s.", max_events, calendar_id)
            break
        try:
            ev = _vevent_to_event(component, calendar_id, source, tz)
            if ev is not None:
                events.append(ev)
        except Exception as exc:
            LOGGER.debug("Skipping malformed VEVENT in %s: %s", calendar_id, exc)
            continue
    return events


def _vevent_to_event(component: icalendar.cal.Component, calendar_id: str, source: str, tz) -> Event | None:
    dtstart_prop = component.get("dtstart")
    if dtstart_prop is None:
        return None
    raw_start = dtstart_prop.dt
    all_day = isinstance(raw_start, date) and not isinstance(raw_start, datetime)
    if all_day:
        start_dt = datetime.combine(raw_start, time.min, tzinfo=tz)
    else:
        start_dt = ensure_aware(raw_start, tz)

    dtend_prop = component.get("dtend")
    if dtend_prop is not None:
        raw_end = dtend_prop.dt
        if isinstance(raw_end, date) and not isinstance(raw_end, datetime):
            end_dt = datetime.combine(raw_end, time.min, tzinfo=tz)
        else:
            end_dt = ensure_aware(raw_end, tz)
    else:
        dur_prop = component.get("duration")
        if dur_prop is not None:
            end_dt = start_dt + dur_prop.dt
        else:
            end_dt = start_dt + (timedelta(days=1) if all_day else timedelta(hours=1))

    uid = str(component.get("uid") or "").strip()
    title = str(component.get("summary") or "Untitled event").strip()
    location = str(component.get("location") or "").strip()
    notes = str(component.get("description") or "").strip()

    rrule_prop = component.get("rrule")
    rrule_str = ""
    if rrule_prop is not None:
        try:
            rrule_str = rrule_prop.to_ical().decode("utf-8", errors="ignore")
        except Exception:
            rrule_str = ""

    exdates: list[str] = []
    ex_prop = component.get("exdate")
    if ex_prop is not None:
        ex_list = ex_prop if isinstance(ex_prop, list) else [ex_prop]
        for ex_item in ex_list:
            for d in getattr(ex_item, "dts", []):
                dt_val = d.dt
                if isinstance(dt_val, date) and not isinstance(dt_val, datetime):
                    exdates.append(datetime.combine(dt_val, time.min, tzinfo=tz).isoformat())
                elif isinstance(dt_val, datetime):
                    exdates.append(ensure_aware(dt_val, tz).isoformat())

    lm_prop = component.get("last-modified")
    lm_dt = None
    if lm_prop is not None and isinstance(lm_prop.dt, datetime):
        lm_dt = ensure_aware(lm_prop.dt, tz)

    rec_id_prop = component.get("recurrence-id")
    rec_suffix = ""
    if rec_id_prop is not None:
        rec_suffix = f"_{rec_id_prop.dt}"

    evt_id = f"{calendar_id}:{uid or hash((title, start_dt.isoformat()))}{rec_suffix}"
    return Event(
        id=evt_id,
        calendar_id=calendar_id,
        title=title,
        start=start_dt,
        end=end_dt,
        all_day=all_day,
        location=location,
        notes=notes,
        rrule=rrule_str,
        exdates=exdates,
        reminders=[15],
        source=source,
        uid=uid or evt_id,
        last_modified=lm_dt,
    )


class IcsFeedProvider(CalendarProvider):
    """Read-only ICS feed provider backed by SQLite offline cache."""

    def __init__(
        self,
        store: CalendarStore,
        secrets: SecretStore,
        calendar_id: str,
        http_get=None,
    ) -> None:
        self._store = store
        self._secrets = secrets
        self.calendar_id = calendar_id
        self.read_only = True
        self._http_get = http_get or requests.get
        self._last_health = ProviderHealth(ok=True, message="Cached")

    def _secret_key(self) -> str:
        return f"ics_url:{self.calendar_id}"

    def list_events(self, range_start: datetime, range_end: datetime) -> list[Event]:
        non_rec = self._store.query_non_recurring_in_range(
            range_start, range_end, calendar_id=self.calendar_id, enabled_only=False
        )
        masters = self._store.list_recurring_masters(
            calendar_id=self.calendar_id, enabled_only=False
        )
        expanded = expand_events(masters, range_start, range_end)
        combined = non_rec + expanded
        combined.sort(key=lambda e: (e.start, e.title.lower()))
        return combined

    def create_event(self, event: Event) -> Event:
        raise ReadOnlyCalendarError("Subscribed ICS calendars are read-only.")

    def update_event(self, event: Event) -> Event:
        raise ReadOnlyCalendarError("Subscribed ICS calendars are read-only.")

    def delete_event(self, event_id: str) -> bool:
        raise ReadOnlyCalendarError("Subscribed ICS calendars are read-only.")

    def refresh(self) -> ProviderHealth:
        url = self._secrets.get_secret(self._secret_key())
        if not url:
            self._last_health = ProviderHealth(
                ok=False, offline=True, message="Missing feed link; showing cached events."
            )
            return self._last_health

        try:
            validate_feed_url(url)
        except CalendarError as exc:
            self._last_health = ProviderHealth(ok=False, offline=True, message=str(exc))
            return self._last_health

        meta = self._store.get_calendar_metadata(self.calendar_id)
        headers: dict[str, str] = {"User-Agent": "AURA-Calendar/1.0"}
        if meta.get("etag"):
            headers["If-None-Match"] = str(meta["etag"])
        if meta.get("last_modified"):
            headers["If-Modified-Since"] = str(meta["last_modified"])

        try:
            resp = self._http_get(
                url,
                headers=headers,
                timeout=FEED_TIMEOUT_SECONDS,
                allow_redirects=False,
                stream=True,
            )
            # Follow at most 3 HTTPS-only validated redirects
            redirects = 0
            while resp.status_code in (301, 302, 303, 307, 308) and redirects < 3:
                loc = resp.headers.get("Location", "")
                validate_feed_url(loc)
                resp.close()
                resp = self._http_get(
                    loc,
                    headers=headers,
                    timeout=FEED_TIMEOUT_SECONDS,
                    allow_redirects=False,
                    stream=True,
                )
                redirects += 1

            now_dt = datetime.now(timezone.utc)
            if resp.status_code == 304:
                meta["last_refresh"] = now_dt.isoformat()
                self._store.set_calendar_metadata(self.calendar_id, meta)
                self._last_health = ProviderHealth(
                    ok=True, message="Up to date (304)", last_refresh=now_dt
                )
                return self._last_health

            if resp.status_code != 200:
                self._last_health = ProviderHealth(
                    ok=False,
                    offline=True,
                    message=f"Feed returned HTTP {resp.status_code}; showing cached data.",
                )
                return self._last_health

            # Stream with strict size cap
            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_ICS_BYTES:
                    resp.close()
                    self._last_health = ProviderHealth(
                        ok=False,
                        offline=True,
                        message="Feed exceeded 5 MB limit; showing cached data.",
                    )
                    return self._last_health
                chunks.append(chunk)
            raw = b"".join(chunks)

            events = parse_ics_bytes(raw, calendar_id=self.calendar_id, source="ics_feed")
            self._store.replace_calendar_events(self.calendar_id, events)

            if resp.headers.get("ETag"):
                meta["etag"] = resp.headers["ETag"]
            if resp.headers.get("Last-Modified"):
                meta["last_modified"] = resp.headers["Last-Modified"]
            meta["last_refresh"] = now_dt.isoformat()
            self._store.set_calendar_metadata(self.calendar_id, meta)

            self._last_health = ProviderHealth(
                ok=True,
                message=f"Synced {len(events)} events",
                last_refresh=now_dt,
            )
            return self._last_health

        except CalendarError as exc:
            self._last_health = ProviderHealth(ok=False, offline=True, message=str(exc))
            return self._last_health
        except Exception as exc:
            LOGGER.warning("ICS feed refresh failed for %s: %s", self.calendar_id, type(exc).__name__)
            self._last_health = ProviderHealth(
                ok=False,
                offline=True,
                message="Offline or unreachable; showing cached data.",
            )
            return self._last_health

    def health(self) -> ProviderHealth:
        return self._last_health
