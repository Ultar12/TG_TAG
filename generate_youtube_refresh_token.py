import os

from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow

load_dotenv()

CLIENT_ID = os.environ.get("YOUTUBE_CLIENT_ID", "").strip()
CLIENT_SECRET = os.environ.get("YOUTUBE_CLIENT_SECRET", "").strip()
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

if not CLIENT_ID or not CLIENT_SECRET:
    raise SystemExit("Set YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET in .env first.")

client_config = {
    "installed": {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }
}

flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")
if not credentials.refresh_token:
    raise SystemExit("Google did not return a refresh token. Revoke the app permission and run again.")

print("\nYOUTUBE_REFRESH_TOKEN=" + credentials.refresh_token)
print("Keep this value secret. Do not commit it to GitHub.")
