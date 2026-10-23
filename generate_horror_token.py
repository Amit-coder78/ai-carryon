"""
generate_horror_token.py — run ONCE, locally, on your own machine.

1. Opens Google's consent screen in your browser.
2. Log in AS THE HORROR CHANNEL'S Google account (the account that owns the "AI CarryON Horror" YouTube channel).
3. Prints a base64 string and saves it to horror_token_b64.txt.
4. Paste that string into the GitHub secret  YOUTUBE_HORROR_TOKEN_B64.

Before running: put the OAuth client JSON you downloaded from Google Cloud (type: Desktop app) in this
folder. It may keep its long default name (client_secret_<id>.apps.googleusercontent.com.json), or be renamed
client_secret_horror.json / client_secrets.json.

Run:  python3 generate_horror_token.py
Keep the JSON and the token file private. Never commit them (*token*.json and *_token_b64.txt are git-ignored).
"""
import base64
import glob
import json
import pickle
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

# Same scopes as agents_horror/upload_agent.py
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]


def find_client_file():
    for name in ("client_secret_horror.json", "client_secrets.json"):
        if glob.glob(name):
            return name
    found = glob.glob("client_secret_*.apps.googleusercontent.com.json")
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        sys.exit("More than one client_secret_*.json here. Rename the Horror one to client_secret_horror.json.")
    sys.exit("No OAuth client JSON found. Put the downloaded file in this folder (or name it client_secret_horror.json).")


def main():
    path = find_client_file()
    with open(path) as f:
        data = json.load(f)
    if "installed" not in data:
        sys.exit("This OAuth client is not a 'Desktop app'. In Google Cloud create a new OAuth client of type "
                 "Desktop app and download that JSON.")
    print(f"Using {path}")
    flow = InstalledAppFlow.from_client_secrets_file(path, SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")
    b64 = base64.b64encode(pickle.dumps(creds)).decode()
    with open("horror_token_b64.txt", "w") as f:
        f.write(b64)
    print("\n" + "=" * 60)
    print("Saved horror_token_b64.txt. Paste its contents into the GitHub secret YOUTUBE_HORROR_TOKEN_B64")
    print("=" * 60)


if __name__ == "__main__":
    main()
