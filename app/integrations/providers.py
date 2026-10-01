from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    authorization_url: str
    token_url: str
    api_base: str
    read_scopes: tuple[str, ...]
    write_scopes: tuple[str, ...]


GOOGLE = "https://www.googleapis.com/auth/"
PROVIDERS = {
    "gmail": ProviderSpec(
        "Gmail",
        "https://accounts.google.com/o/oauth2/v2/auth",
        "https://oauth2.googleapis.com/token",
        "https://gmail.googleapis.com/gmail/v1/users/me/",
        (GOOGLE + "gmail.readonly",),
        (GOOGLE + "gmail.send", GOOGLE + "gmail.compose"),
    ),
    "google_calendar": ProviderSpec(
        "Google Calendar",
        "https://accounts.google.com/o/oauth2/v2/auth",
        "https://oauth2.googleapis.com/token",
        "https://www.googleapis.com/calendar/v3/",
        (
            "openid",
            "email",
            GOOGLE + "calendar.events.readonly",
            GOOGLE + "calendar.calendarlist.readonly",
        ),
        (GOOGLE + "calendar.events",),
    ),
    "slack": ProviderSpec(
        "Slack",
        "https://slack.com/oauth/v2/authorize",
        "https://slack.com/api/oauth.v2.access",
        "https://slack.com/api/",
        (
            "search:read",
            "channels:read",
            "groups:read",
            "im:read",
            "mpim:read",
            "channels:history",
            "groups:history",
            "im:history",
            "mpim:history",
            "users:read",
            "users.profile:read",
        ),
        ("chat:write", "im:write", "users.profile:write", "dnd:write"),
    ),
    "spotify": ProviderSpec(
        "Spotify",
        "https://accounts.spotify.com/authorize",
        "https://accounts.spotify.com/api/token",
        "https://api.spotify.com/v1/",
        ("playlist-read-private", "playlist-read-collaborative", "user-read-playback-state"),
        ("user-modify-playback-state",),
    ),
}
