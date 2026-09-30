# Email, Slack, Calendar and Spotify

Bridge works with these services at two levels:

| | Works with no setup | With a connected account |
| --- | --- | --- |
| **Email** | Opens a pre-filled Gmail or Mail.app draft for you to send; opens Gmail search | Search, read threads, send (after approval), save drafts |
| **Calendar** | — | Today's/this week's events, free time, create and delete events (after approval) |
| **Slack** | Open the Slack app | Search, read a channel or DM, send to `#channel` or `@person` (after approval) |
| **Spotify** | Play/pause/next/previous, what's playing, shuffle, repeat, volume, open search in the Mac app | Play any song, artist, album or playlist by name; list playlists and devices |

## Built-in Mac apps (no setup)

| | Try | Notes |
| --- | --- | --- |
| **Reminders** | "Remind me to call mom at 6pm" · "What reminders do I have?" · "I bought the milk, tick it off" | A due time adds an alert. Completing matches key words but refuses if several reminders match. |
| **Notes** | "Find my notes about Kigali" · "Read my Shopping note" · "Add eggs to my Shopping note" · "Make a note called Ideas: …" | Bridge only creates notes and appends to them; it never deletes or rewrites. |
| **Shortcuts** | "What shortcuts do I have?" · "Run my Focus Mode shortcut" | Each run asks for approval unless the name is in `SHORTCUTS_TRUSTED=["Focus Mode"]`. Text output is shown in the reply. |

| **Calendar** | "What's on tomorrow?" · "When am I free Friday afternoon?" · "Put Dentist on my calendar tomorrow 3–4pm" · "Delete Standup today" | Uses every calendar added to this Mac (iCloud, Google, Exchange), including repeating events. Deleting asks first. |
| **Briefing** | "Brief me" · "What's my day?" | Rest of today's events, reminders due, unread Gmail (if connected), battery and low-disk warnings. |
| **Chrome** | "What tabs do I have open?" · "Switch to my Gmail tab" · "Close all YouTube tabs" · "Close this tab" · "Summarize this page" | Closing by search asks first. Reading a page needs Chrome → View → Developer → **Allow JavaScript from Apple Events**. For the AI to summarize it, add `chrome_read_page` to `REMOTE_TOOL_RESULT_ALLOWLIST`. |
| **This Mac** | "How's my battery?" · "Dark mode on" · "Lock my screen" · "Put the Mac to sleep" | Lock sleeps the display. Sleeping the Mac asks first. For Focus/Do Not Disturb, make a Shortcut. |
| **Schedules** | "Every weekday at 8:30 brief me" · "Every Friday at 17:00 show my unread email" · "What's scheduled?" · "Stop the morning briefing" | Creating one asks first. Runs while Bridge is open; results arrive as a Mac notification. Runs more than 2 hours late (Mac asleep) are skipped. |

If you type a request in the panel and close it, a notification tells you when it's done
or needs your approval.

| **Contacts & Messages** | "Email Olivier that I miss him" · "Text Mom I'm on my way" · "What's Sam's number?" | Names come from your Mac's Contacts. The approval shows the real address or number, and exactly that is used. If a name matches several people, Bridge asks which. |
| **Memory** | "Remember that Olivier Mupenzi is my brother" · "Text my brother I'll call tonight" · "What do you remember?" · "Forget my boss" | Facts are stored on this Mac and included with each request (sent to OpenAI when it is your model). Passwords, codes and card numbers are refused. Manage them on the dashboard's Preferences page. |

| **Proactive** | "Tell me when Olivier emails me" · "Let me know when someone mentions me on Slack" · "Remind me 15 minutes before meetings" · "Send me a summary of tomorrow every weekday at 7pm" · "What are you watching?" · "Stop watching Olivier" | Watches check Gmail/Slack every 2 minutes. They never announce mail that was already there when the watch was created, and notifications show only the sender and subject. Meeting heads-ups use your Mac calendar. Everything runs on this Mac while Bridge is open; nothing is sent to the AI. Manage it on the dashboard's Preferences page. |

The first time Bridge uses Reminders, Notes, Chrome or System Events, macOS asks whether
**Bridge** may control that app; for Calendar it asks for calendar access (choose **Full
Access**). Click **OK**. If you missed the prompt, allow it in System Settings → Privacy &
Security → Automation → Bridge.

You can also type any request straight into the menu-bar panel's **Ask Bridge** box
(press Return), with no need to open the dashboard.

## Things to try

- "What are my unread emails?" · "Show emails from Alex this week" · "Read the latest thread from Alex"
- "Draft an email to alex@example.com saying I'll be 10 minutes late" · "Send it"
- "What's on my calendar tomorrow?" · "When am I free Friday afternoon for an hour?"
- "Add 'Dentist' tomorrow 3–4pm" · "Delete 'Standup' today"
- "What's new in #general?" · "Search Slack for release notes" · "Tell @alex I'm on my way"
- "Play Blinding Lights" · "Play my coding playlist" · "What's playing?" · "Skip" · "Spotify volume 30"

Anything that sends, creates or deletes asks for your approval first and shows the exact
account, destination and content. Declining cancels it.

### Privacy: what the AI model sees

With the default `REMOTE_TOOL_RESULTS=status_only`, OpenAI never receives your emails,
events, Slack messages or playlists; it only learns whether a step succeeded, and Bridge
shows the results to you locally. To let OpenAI summarize and reply to that content,
allow the reading tools, for example:

```dotenv
REMOTE_TOOL_RESULTS=allowlist
REMOTE_TOOL_RESULT_ALLOWLIST=["calendar_events","email_search"]
```

## Setup

1. Install the Keychain support once: `.venv/bin/python -m pip install -e '.[integrations]'`
2. In `.env`, set `INTEGRATIONS_ENABLED=true` and keep `INTEGRATIONS_CALLBACK_PORT=8000`.
3. Create the OAuth clients below, put their IDs in `.env`, then restart Bridge.
4. In the dashboard, open **Connections**, pick the service, tick **Allow sending…** if you
   want actions (not just reading), and choose **Prepare secure sign-in**. After signing in,
   choose **Refresh connections**.

Tokens are stored only in macOS Keychain. If you connected an account before these
features existed, reconnect it so Bridge gets the new permissions (drafts, Slack history
and direct messages).

### Gmail and Google Calendar

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project and enable
   **Gmail API** and **Google Calendar API**.
2. Configure the OAuth consent screen (External, add yourself as a test user).
3. Create credentials → **OAuth client ID** → **Desktop app**.
4. Set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`. (A desktop-app "secret" is not really
   secret, but keep `.env` private anyway.)

Connect Gmail and Google Calendar separately in Connections; they are separate grants.

### Slack

1. Create an app at [api.slack.com/apps](https://api.slack.com/apps) → **From scratch**.
2. **OAuth & Permissions**:
   - Redirect URL: `http://localhost:8000/api/v1/connections/callback/slack`
   - Turn on **PKCE** (public client).
   - **User Token Scopes** (not bot scopes): `search:read`, `channels:read`,
     `groups:read`, `im:read`, `mpim:read`, `channels:history`, `groups:history`,
     `im:history`, `mpim:history`, `users:read`, and for sending: `chat:write`, `im:write`.
3. Set `SLACK_CLIENT_ID` (no secret needed with PKCE). Your workspace may require an admin
   to approve the app.

Messages you send appear as you. Names are matched exactly: "#general" is a channel;
"@alex", "Alex Kim" or a display name is a person. Bridge refuses rather than guessing
when a name is unknown or matches several people.

### Spotify

1. Create an app at the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard)
   with **Web API** enabled.
2. Redirect URI: `http://127.0.0.1:8000/api/v1/connections/callback/spotify`
3. Set `SPOTIFY_CLIENT_ID` (PKCE, no secret).
4. While the app is in development mode, add your Spotify account under **User Management**.

"Play …" searches with your account and plays the top result in the **Spotify Mac app**,
so no Premium Connect device is needed. Playback control works without any setup.
