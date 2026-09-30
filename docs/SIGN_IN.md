# Signing in to Bridge

Bridge has one **owner**: you. Your account protects the dashboard at
`http://localhost:8000`. Everything is stored on this Mac, in Bridge's local database.

## Create your account

1. Click the **Bridge** icon in the menu bar → right-click → **Open Dashboard**.
2. Choose **Continue with Google**, **Continue with GitHub**, or fill in name, email and a
   password (at least 10 characters).

Only someone at this Mac can create the owner account. The dashboard allows sign-up only
after it's opened from the menu bar (a single-use, 60-second link) or with the `API_TOKEN`
from `.env`. A website or another program can't claim your Bridge first.

## Ways to sign in

| Method | Setup |
| --- | --- |
| **Email and password** | Set when you sign up, or on the Account page. Stored as a salted scrypt hash. |
| **Touch ID (passkey)** | Account → **Add Touch ID passkey**. Works at `http://localhost:8000` (not `127.0.0.1`). |
| **Google** | Account → **Link Google**. Uses the Google client already in `.env`. |
| **GitHub** | Create a GitHub OAuth App, add `GITHUB_CLIENT_ID` and `GITHUB_CLIENT_SECRET`, restart Bridge, then Account → **Link GitHub**. |

Google and GitHub are used only to confirm who you are. Bridge asks for your name and
verified email, and nothing else, and keeps no Google/GitHub token.

### Google setup

The same Google OAuth client that connects Gmail and Calendar also signs you in.

- **Desktop app** client: nothing to add.
- **Web application** client: add this Authorized redirect URI:
  `http://localhost:8000/api/v1/auth/oauth/google/callback`

### GitHub setup

1. Open [github.com/settings/developers](https://github.com/settings/developers) → **New OAuth App**.
2. Homepage URL: `http://localhost:8000`.
   Authorization callback URL: `http://localhost:8000/api/v1/auth/oauth/github/callback`
3. Copy the Client ID, generate a client secret, and put both in `.env`:
   `GITHUB_CLIENT_ID=…` and `GITHUB_CLIENT_SECRET=…`. Restart Bridge.

## Sessions

- Sign-in uses a secure cookie (`HttpOnly`, `SameSite=Strict`). Page scripts can't read it,
  and other websites can't use it.
- **Keep me signed in** lasts 30 days (`AUTH_REMEMBER_DAYS`). Otherwise a session lasts 12
  hours (`AUTH_SESSION_HOURS`).
- The Account page lists where you're signed in. You can sign out one session, or
  everywhere else.
- After 5 wrong passwords, sign-in pauses for 5 minutes.

## Locked out?

Open the dashboard from the Bridge menu bar. That link always signs the owner in, because
it proves you're at this Mac. Then set a new password or add another sign-in method on the
Account page. The menu bar, voice and the API keep using `API_TOKEN` internally.
