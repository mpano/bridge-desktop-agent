# Releasing Bridge

A release is a Bridge.app that anyone can download and open, signed by you and approved
(notarized) by Apple. Bridge apps you've shared then update themselves.

## Once: Apple setup (about 30 minutes, plus Apple's approval)

1. **Join the Apple Developer Program** at <https://developer.apple.com/programs/>
   ($99 a year). Approval can take a day or two.
2. **Create a Developer ID certificate.** In Xcode: Settings › Accounts › your team ›
   Manage Certificates › + › *Developer ID Application*. Check that it's there:

   ```bash
   security find-identity -v -p codesigning
   ```

   You should see `Developer ID Application: Your Name (TEAMID)`.
3. **Make an app-specific password** at <https://account.apple.com> › Sign-In and Security ›
   App-Specific Passwords. Name it "Bridge notary".
4. **Save it for notarytool.** It asks for the password itself; Bridge never sees it:

   ```bash
   xcrun notarytool store-credentials bridge-notary --apple-id you@example.com --team-id TEAMID
   ```

## Each release

1. Raise `VERSION` in `app/desktop/bundle.py` (for example `0.1.0` → `0.2.0`) and commit.
2. Build, sign and notarize (5–15 minutes, mostly waiting for Apple):

   ```bash
   .venv/bin/python -m app.desktop.release --notes "Health page, and drag the text panel"
   ```

   This leaves three files in `dist/release`: `Bridge-<v>.dmg`, `Bridge-<v>.zip` and
   `latest.json`.
3. Try the .dmg on your Mac. Then publish it on GitHub:

   ```bash
   .venv/bin/python -m app.desktop.release --skip-build --notes "…" --publish
   ```

Within a day, every Bridge app checks `latest.json`, downloads the .zip in the background and
says "Bridge 0.2.0 is ready". Clicking **Restart to update** swaps the app; the old one goes
to the Trash.

## What the updater checks

An update is installed only if all of these are true:

* the download's SHA-256 matches `latest.json`;
* it comes from this repository's GitHub releases over HTTPS (redirects too);
* Apple's signature is intact and made with the **same Developer ID team** as the running app;
* Gatekeeper accepts it (it's notarized);
* it's Bridge (bundle id) and the version announced.

Copies run from the code checkout (`python -m app.main`) don't update themselves; use git.
A copy built before you had a Developer ID can't verify updates: download the new .dmg once
by hand, and it updates itself from then on.
