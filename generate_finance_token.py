from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]
flow = InstalledAppFlow.from_client_secrets_file("client_secret_finance.json", SCOPES)
creds = flow.run_local_server(port=0, prompt="consent")
open("finance_token.json", "w").write(creds.to_json())
print("Saved finance_token.json")
