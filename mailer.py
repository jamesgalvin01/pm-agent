"""
mailer.py — outbound email via Resend.
"""
import os
import resend

resend.api_key = os.getenv("RESEND_API_KEY")

EMAIL_FROM = os.getenv("EMAIL_FROM", "Rowan <rowan@miami-coastline.com>")


def send_magic_link_email(to_email: str, magic_link_url: str, code: str = None) -> None:
    """Send a sign-in email: a 6-digit code (for the phone app) and a one-time link."""
    code_block = ""
    if code:
        code_block = f"""
    <p style="margin:24px 0 6px;">Signing in from the Rowan app on your phone? Type this code:</p>
    <p style="font-size:34px;font-weight:700;letter-spacing:8px;color:#1F3864;margin:0 0 24px;font-family:Menlo,Consolas,monospace;">{code}</p>
    <p>On a computer, you can use the button instead.</p>"""
    html = f"""\
<!doctype html>
<html>
  <body style="font-family: -apple-system, Helvetica, Arial, sans-serif; max-width: 560px; margin: 0 auto; padding: 24px; color: #1a1a1a;">
    <h2 style="color:#1F3864;margin-top:0;">Sign in to Rowan</h2>
    <p>This sign-in is valid for 15 minutes and can only be used once.</p>{code_block}
    <p style="margin: 28px 0;">
      <a href="{magic_link_url}"
         style="background:#1F3864;color:white;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:600;display:inline-block;">
        Sign in to Rowan
      </a>
    </p>
    <p style="font-size:13px;color:#666;">If the button doesn't work, copy and paste this URL into your browser:</p>
    <p style="font-size:13px;color:#666;word-break:break-all;">{magic_link_url}</p>
    <hr style="border:none;border-top:1px solid #eee;margin:32px 0 16px;">
    <p style="font-size:12px;color:#999;">If you didn't request this, you can safely ignore this email.</p>
  </body>
</html>
"""
    resend.Emails.send({
        "from": EMAIL_FROM,
        "to": to_email,
        "subject": f"Rowan sign-in code: {code}" if code else "Sign in to Rowan",
        "html": html,
    })
