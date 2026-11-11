"""Run this ONCE, interactively, as the owner: `python gmail_setup.py` from the project venv

Opens a browser for Google's consent screen and writes gmail_token.json.
Never run this as jarvS or from inside a job -- it needs your own login,
which is exactly why jarvS can't do this step for itself.

Before running it:
  1. In Google Cloud Console, create (or use) a project and enable the
     Gmail API.
  2. Create an OAuth client of type "Desktop app".
  3. Download its JSON and save it as the jarvS home directory as gmail_credentials.json.
"""
from __future__ import annotations

import os
import pathlib

import runtime

HOME = runtime.app_home()
CREDS = pathlib.Path(os.environ.get("GMAIL_CREDENTIALS_PATH", str(HOME / "gmail_credentials.json")))
TOKEN = pathlib.Path(os.environ.get("GMAIL_TOKEN_PATH", str(HOME / "gmail_token.json")))
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
]


def main() -> None:
    if not CREDS.exists():
        raise SystemExit(
            f"missing {CREDS}\n\n"
            "Create an OAuth client (type: Desktop app) for the Gmail API in "
            "Google Cloud Console, download its JSON, and save it at that path "
            "first. Set GMAIL_CREDENTIALS_PATH in .env if you'd rather use a "
            "different location."
        )
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(CREDS), SCOPES)
    creds = flow.run_local_server(port=0)
    TOKEN.write_text(creds.to_json(), encoding="utf-8")
    print(f"wrote {TOKEN}")
    print("gmail_search / gmail_summarize_unread / gmail_draft are live now.")
    print("Reminder: this integration can create Gmail drafts but will never "
         "send anything -- see tools/gmail_ops.py.")


if __name__ == "__main__":
    main()
