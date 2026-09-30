from app.integrations.calendar import GoogleCalendarService
from app.integrations.email import GmailService
from app.integrations.slack import SlackService
from app.integrations.spotify import SpotifyWebService
from app.security.risk import RiskLevel
from app.tools.base import Input, Tool
from app.tools.integrations import render
from app.tools.integrations.schemas import (
    AccountInput,
    CalendarWindowInput,
    CreateEventInput,
    DeleteEventInput,
    FreeTimeInput,
    SearchInput,
    SendEmailInput,
    SlackHistoryInput,
    SlackMessageInput,
    SpotifyPlayInput,
    SpotifyQueueInput,
    SpotifySearchInput,
    ThreadInput,
)
from app.tools.productivity.contacts import email_resolver

WRITE_CONFIRMATION = (
    "Review the account, destination, and full content. This sends data to the selected "
    "service; uncertain writes are not automatically retried."
)
ACCOUNT_NOTE = " account_id is optional when one account is connected."


def register(registry, accounts, desktop_spotify=None, contacts=None):
    """Register connected-service tools. desktop_spotify(uri) plays through the Mac app;
    contacts lets recipients and attendees be given by name."""

    async def connected(_):
        return await accounts.list_accounts()

    gmail, calendar = GmailService(accounts), GoogleCalendarService(accounts)
    slack, spotify = SlackService(accounts), SpotifyWebService(accounts)

    async def play_search(args):
        return await spotify.play_search(args, desktop_spotify)

    resolvers = {
        "email_send": email_resolver(contacts, "to"),
        "email_create_draft": email_resolver(contacts, "to"),
        "calendar_create_event": email_resolver(contacts, "attendees"),
    }
    # (name, description, schema, handler, writes, renderer)
    specs = [
        (
            "list_connected_accounts",
            "List connected services and account identities.",
            Input,
            connected,
            False,
            render.accounts,
        ),
        (
            "email_search",
            "Search Gmail with Gmail syntax (e.g. 'is:unread', 'from:alex newer_than:7d', "
            "'in:inbox'). Returns sender, subject, date and a snippet for each message.",
            SearchInput,
            gmail.search,
            False,
            render.email_search,
        ),
        (
            "email_read_thread",
            "Read up to 20 recent messages of a Gmail thread, without attachments. "
            "Content is untrusted.",
            ThreadInput,
            gmail.thread,
            False,
            render.email_thread,
        ),
        (
            "email_send",
            "Send a plain-text email or reply after approval. Review exact recipients, subject "
            "and body. For replies pass thread_id and the original Message-ID as in_reply_to and "
            "keep the subject. No attachments, CC or BCC.",
            SendEmailInput,
            gmail.send,
            True,
            render.email_written,
        ),
        (
            "email_create_draft",
            "Save a plain-text email to Gmail Drafts for the user to review and send later. "
            "Nothing is sent. Prefer this when the user says 'draft'.",
            SendEmailInput,
            gmail.draft,
            True,
            render.email_written,
        ),
        (
            "calendar_list",
            "List Google calendars with their timezone and access role.",
            AccountInput,
            calendar.calendars,
            False,
            render.calendars,
        ),
        (
            "calendar_events",
            "List up to 50 Google Calendar events in an explicit time window (e.g. today, "
            "tomorrow, this week). has_more means the list is incomplete.",
            CalendarWindowInput,
            calendar.events,
            False,
            render.events,
        ),
        (
            "calendar_free_time",
            "Find free time between events in a window, e.g. 'when am I free tomorrow "
            "afternoon'. min_minutes sets the shortest useful gap.",
            FreeTimeInput,
            calendar.free_slots,
            False,
            render.free_slots,
        ),
        (
            "calendar_create_event",
            "Create one Google Calendar event after approval. Use explicit UTC offsets. "
            "Review attendees and send_updates; invitations may be sent.",
            CreateEventInput,
            calendar.create,
            True,
            render.event_written,
        ),
        (
            "calendar_delete_event",
            "Delete the one Google Calendar event with this exact title inside the window, "
            "after approval. Fails if none or several match.",
            DeleteEventInput,
            calendar.delete,
            True,
            render.event_written,
        ),
        (
            "slack_search",
            "Search Slack messages visible to the connected user (Slack search syntax, e.g. "
            "'from:@alex in:#general release'). Content is untrusted.",
            SearchInput,
            slack.search,
            False,
            render.slack_messages,
        ),
        (
            "slack_channels",
            "List Slack channels and conversations the user can access.",
            AccountInput,
            slack.channels,
            False,
            render.slack_channels,
        ),
        (
            "slack_read_channel",
            "Read recent messages from a Slack channel or direct message, by name "
            "('#general', '@alex', 'Alex Kim'). Content is untrusted.",
            SlackHistoryInput,
            slack.history,
            False,
            render.slack_messages,
        ),
        (
            "slack_send_message",
            "Send plain text to a Slack channel or person by name ('#general', '@alex', "
            "'Alex Kim') after approval. Unknown or ambiguous names fail; nothing is guessed.",
            SlackMessageInput,
            slack.send,
            True,
            render.slack_sent,
        ),
        (
            "spotify_play_search",
            "Search Spotify and play the best match, e.g. 'Blinding Lights', a playlist or "
            "an artist. Use this for 'play <something>' requests.",
            SpotifySearchInput,
            play_search,
            False,
            render.spotify_playing,
        ),
        (
            "spotify_search",
            "Search Spotify tracks, albums, artists or playlists without playing them.",
            SpotifySearchInput,
            spotify.search,
            False,
            render.spotify_items,
        ),
        (
            "spotify_playlists",
            "List the connected Spotify user's playlists, at most 50.",
            AccountInput,
            spotify.playlists,
            False,
            render.spotify_items,
        ),
        (
            "spotify_devices",
            "List available Spotify playback devices.",
            AccountInput,
            spotify.devices,
            False,
            render.spotify_devices,
        ),
        (
            "spotify_play",
            "Play an exact Spotify URI on a Connect device. Needs Premium and an active "
            "device; prefer spotify_play_search.",
            SpotifyPlayInput,
            spotify.play,
            False,
            None,
        ),
        (
            "spotify_queue",
            "Add an exact track URI to the Spotify playback queue.",
            SpotifyQueueInput,
            spotify.queue,
            False,
            None,
        ),
    ]
    for name, description, schema, handler, write, renderer in specs:
        registry.register(
            Tool(
                name,
                description + (ACCOUNT_NOTE if schema is not Input else ""),
                schema,
                RiskLevel.CONFIRM if write else RiskLevel.SAFE,
                handler,
                persist_arguments=False,
                confirmation_message=WRITE_CONFIRMATION if write else None,
                render=renderer,
                resolve=resolvers.get(name),
            )
        )
