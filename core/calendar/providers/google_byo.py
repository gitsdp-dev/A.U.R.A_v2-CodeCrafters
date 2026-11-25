"""Optional Google Calendar BYO-OAuth provider (disabled unless user supplies credentials.json)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from core.calendar.models import CalendarError, Event, get_user_data_dir
from core.calendar.providers.base import CalendarProvider, ProviderHealth
from core.calendar.recurrence import expand_events
from core.calendar.secrets import SecretStore
from core.calendar.store import CalendarStore

GOOGLE_CALENDAR_SCOPE = ["https://www.googleapis.com/auth/calendar.readonly"]


def is_google_byo_available() -> bool:
    """Only enabled when optional Google libs are installed AND user supplied credentials.json."""
    creds_file = get_user_data_dir() / "google_credentials.json"
    if not creds_file.is_file():
        return False
    try:
        import google_auth_oauthlib  # noqa: F401
        import googleapiclient  # noqa: F401

        return True
    except ImportError:
        return False


class GoogleByoProvider(CalendarProvider):
    """Advanced opt-in Google Calendar provider using the user's own OAuth client."""

    def __init__(self, store: CalendarStore, secrets: SecretStore, calendar_id: str) -> None:
        if not is_google_byo_available():
            raise CalendarError("Google BYO OAuth is not configured.")
        self._store = store
        self._secrets = secrets
        self.calendar_id = calendar_id
        self.read_only = True

    def list_events(self, range_start: datetime, range_end: datetime) -> list[Event]:
        non_rec = self._store.query_non_recurring_in_range(
            range_start, range_end, calendar_id=self.calendar_id, enabled_only=False
        )
        masters = self._store.list_recurring_masters(
            calendar_id=self.calendar_id, enabled_only=False
        )
        return sorted(non_rec + expand_events(masters, range_start, range_end), key=lambda e: e.start)

    def create_event(self, event: Event) -> Event:
        raise CalendarError("Google BYO provider is configured read-only.")

    def update_event(self, event: Event) -> Event:
        raise CalendarError("Google BYO provider is configured read-only.")

    def delete_event(self, event_id: str) -> bool:
        raise CalendarError("Google BYO provider is configured read-only.")

    def refresh(self) -> ProviderHealth:
        return ProviderHealth(ok=False, offline=True, message="Run OAuth flow from settings first.")
