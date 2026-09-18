"""
outlook_auth.py — one-time helper to mint an Outlook refresh token that can SEND.

The existing OUTLOOK_REFRESH_TOKEN was consented for Mail.Read only, so it
cannot send. Run this once on your Mac:

    cd ~/pm-agent && source venv/bin/activate && python outlook_auth.py

It prints a code and a URL. Sign in as james@miami-coastline.com, approve the
permissions, and it will:
  1. print the new refresh token, and
  2. save it to the app_secrets table so Railway picks it up with no redeploy.

Prerequisites in the Azure app registration (portal.azure.com -> App
registrations -> your app):
  - API permissions -> Microsoft Graph -> Delegated: Mail.ReadWrite, Mail.Send,
    Files.ReadWrite, Calendars.ReadWrite
    (Mail.Read and offline_access should already be there)
  - Authentication -> Advanced settings -> "Allow public client flows" = Yes
"""
import os
import msal
from dotenv import load_dotenv

from outlook_mail import AUTH_SCOPES, REFRESH_TOKEN_KEY, set_secret

load_dotenv()

CLIENT_ID = os.getenv("AZURE_CLIENT_ID")
TENANT_ID = os.getenv("AZURE_TENANT_ID")


def main():
    if not CLIENT_ID or not TENANT_ID:
        raise SystemExit("AZURE_CLIENT_ID / AZURE_TENANT_ID missing from .env")

    app = msal.PublicClientApplication(
        CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{TENANT_ID}",
    )

    flow = app.initiate_device_flow(scopes=AUTH_SCOPES)
    if "user_code" not in flow:
        raise SystemExit(
            "Could not start device flow: "
            f"{flow.get('error')} — {flow.get('error_description')}\n"
            "Most likely 'Allow public client flows' is still set to No in the "
            "Azure app registration."
        )

    print("\n" + "=" * 60)
    print(flow["message"])
    print("=" * 60 + "\n")
    print("Waiting for you to finish signing in...")

    result = app.acquire_token_by_device_flow(flow)

    if "refresh_token" not in result:
        raise SystemExit(
            "Sign-in did not return a refresh token: "
            f"{result.get('error')} — {result.get('error_description')}"
        )

    token = result["refresh_token"]
    print("\nGranted scopes:", result.get("scope"))

    try:
        set_secret(REFRESH_TOKEN_KEY, token)
        print("\nSaved to the app_secrets table. Railway will use it immediately.")
    except Exception as e:
        print(f"\nCould not save to the database ({e}).")
        print("Set this as OUTLOOK_REFRESH_TOKEN in Railway instead:\n")
        print(token)
        return

    print("\nDone. Rowan can now read and reply to email, file to OneDrive and use your calendar.")


if __name__ == "__main__":
    main()
