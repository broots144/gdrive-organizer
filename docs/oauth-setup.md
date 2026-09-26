# OAuth setup

gdrive-organizer talks to the Drive API with **your own** OAuth client. Nothing is hosted and no
third party ever sees a token. This takes about 20 minutes once (estimate).

## Create the client

1. Open the [Google Cloud console](https://console.cloud.google.com/) and create a new project
   (for example `gdrive-organizer`).
2. **APIs & Services > Library**: enable the **Google Drive API**.
3. **APIs & Services > OAuth consent screen**: user type **External**, publishing status
   **Testing**, and add your own Google account under **Test users**.
4. **APIs & Services > Credentials > Create credentials > OAuth client ID**: application type
   **Desktop app**.
5. Download the JSON and save it as `private/client_secret.json` (gitignored).

## Scopes and tokens

Each phase asks for its own least-privilege token the first time it runs. A browser window opens;
approve it with the account you added as a test user.

| Command | Scope | Can | Token file |
|---|---|---|---|
| `index-drive` | `drive.metadata.readonly` | list names, sizes, checksums; no content, no writes | `private/token_meta.json` |
| `peek` | `drive.readonly` | read content; no writes | `private/token_read.json` |
| `apply` | `drive` | move, rename, create folders, trash | `private/token_write.json` |

Token files are written with mode `0600`. The committed `.claude/settings.json` blocks an assistant
from reading them.

## What to expect

- **"Google hasn't verified this app."** Expected for a personal client in Testing mode. Choose
  **Advanced**, then continue to your app. You are the developer and the only user.
- **Tokens expire after 7 days in Testing mode.** This is Google policy for External apps left in
  Testing (fact: Google Identity documentation). When a stored token can no longer be refreshed,
  the tool opens the sign-in page again.
- **Revoking access:** remove the app at <https://myaccount.google.com/permissions> and delete the
  `private/token_*.json` files.
