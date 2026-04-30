"""
app/utils/email.py

Async invite email sender via Gmail SMTP.
Uses aiosmtplib — no domain verification needed, sends to anyone.
"""
from __future__ import annotations

import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import aiosmtplib

from app.core.settings import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


def _build_invite_html(invite_link: str, tenant_name: str, role: str) -> str:
    role_display = role.capitalize()

    role_badge_styles = {
        "admin": "background:#EFF6FF; color:#1D4ED8; border:1px solid #BFDBFE;",
        "agent": "background:#F1F5F9; color:#334155; border:1px solid #C8D3DF;",
    }
    badge_style = role_badge_styles.get(
        role.lower(),
        "background:#EFF6FF; color:#1D4ED8; border:1px solid #BFDBFE;",
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>You're invited to {tenant_name}</title>
  <link href="https://fonts.googleapis.com/css2?family=Sora:wght@400;500;600;700;800&display=swap" rel="stylesheet" />
</head>
<body style="margin:0; padding:0; background:#F1F5F9; font-family:'Sora',sans-serif; -webkit-font-smoothing:antialiased;">

  <table width="100%" cellpadding="0" cellspacing="0" border="0"
         style="background:#F1F5F9; padding:40px 16px;">
    <tr>
      <td align="center">

        <table width="100%" cellpadding="0" cellspacing="0" border="0"
               style="max-width:520px; background:#FFFFFF; border-radius:14px;
                      border:1.5px solid #C8D3DF;
                      box-shadow:0 4px 6px -1px rgba(0,0,0,0.12),0 2px 4px -1px rgba(0,0,0,0.08);">

          <!-- Header -->
          <tr>
            <td style="background:#2563EB; border-radius:12px 12px 0 0; padding:28px 36px;">
              <table width="100%" cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td>
                    <span style="font-size:18px; font-weight:800; color:#FFFFFF; letter-spacing:-0.3px;">
                      Unified CRM
                    </span>
                  </td>
                  <td align="right">
                    <span style="display:inline-block; width:10px; height:10px;
                                 background:rgba(255,255,255,0.35); border-radius:50%;"></span>
                    &nbsp;
                    <span style="display:inline-block; width:6px; height:6px;
                                 background:rgba(255,255,255,0.2); border-radius:50%;"></span>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Body -->
          <tr>
            <td style="padding:36px 36px 28px;">

              <h1 style="margin:0 0 8px; font-size:22px; font-weight:800;
                         color:#060D1F; letter-spacing:-0.3px; line-height:1.25;">
                You've been invited!
              </h1>

              <p style="margin:0 0 24px; font-size:14px; color:#64748B; line-height:1.6;">
                You have been invited to join
                <strong style="color:#2D3748;">{tenant_name}</strong>
                on Unified CRM.
              </p>

              <!-- Role badge -->
              <div style="margin-bottom:24px;">
                <span style="display:inline-block; padding:4px 12px;
                             border-radius:99px; font-size:12px; font-weight:600;
                             {badge_style}">
                  {role_display}
                </span>
              </div>

              <!-- Info card -->
              <div style="background:#F1F5F9; border:1.5px solid #C8D3DF;
                          border-radius:10px; padding:16px 20px; margin-bottom:28px;">
                <table width="100%" cellpadding="0" cellspacing="0" border="0">
                  <tr>
                    <td style="font-size:12px; font-weight:700; color:#64748B;
                               text-transform:uppercase; letter-spacing:0.07em; padding-bottom:6px;">
                      Workspace
                    </td>
                  </tr>
                  <tr>
                    <td style="font-size:15px; font-weight:600; color:#060D1F;">
                      {tenant_name}
                    </td>
                  </tr>
                  <tr><td style="height:12px;"></td></tr>
                  <tr>
                    <td style="font-size:12px; font-weight:700; color:#64748B;
                               text-transform:uppercase; letter-spacing:0.07em; padding-bottom:6px;">
                      Role
                    </td>
                  </tr>
                  <tr>
                    <td style="font-size:15px; font-weight:600; color:#060D1F;">
                      {role_display}
                    </td>
                  </tr>
                </table>
              </div>

              <!-- CTA Button -->
              <div style="text-align:center; margin-bottom:24px;">
                <a href="{invite_link}"
                   style="display:inline-block; padding:13px 32px;
                          background:#2563EB; color:#FFFFFF;
                          border-radius:6px; font-size:15px; font-weight:600;
                          text-decoration:none; letter-spacing:0.01em;">
                  Accept Invitation →
                </a>
              </div>

              <hr style="border:none; border-top:1.5px solid #EEF2F7; margin:0 0 20px;" />

              <!-- Expiry notice -->
              <div style="background:#FFFBEB; border:1.5px solid #FCD34D;
                          border-radius:6px; padding:12px 16px; margin-bottom:20px;">
                <p style="margin:0; font-size:12.5px; color:#B45309; line-height:1.5;">
                  ⏱ This invitation link expires in <strong>24 hours</strong>.
                  If you weren't expecting this, you can safely ignore this email.
                </p>
              </div>

              <!-- Fallback link -->
              <p style="margin:0; font-size:11.5px; color:#64748B; line-height:1.6;">
                If the button doesn't work, copy and paste this link:<br />
                <a href="{invite_link}" style="color:#2563EB; word-break:break-all; font-size:11px;">
                  {invite_link}
                </a>
              </p>

            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="background:#EEF2F7; border-radius:0 0 12px 12px;
                       padding:18px 36px; border-top:1.5px solid #C8D3DF;">
              <p style="margin:0; font-size:11.5px; color:#64748B;
                        text-align:center; line-height:1.6;">
                © 2025 Unified CRM · Sent by Betsol ·
                <span style="color:#94A3B8;">This is an automated message, please do not reply.</span>
              </p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>

</body>
</html>"""


async def send_invite_email(
    to_email: str,
    invite_link: str,
    tenant_name: str,
    role: str,
) -> None:
    """
    Send invite email via Gmail SMTP using aiosmtplib.
    Logs warning on failure — never raises.
    """
    if not settings.SMTP_USER or not settings.SMTP_PASSWORD:
        logger.warning("SMTP credentials not set — skipping invite email to %s", to_email)
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"You're invited to join {tenant_name} on Unified CRM"
    msg["From"] = f"Unified CRM <{settings.SMTP_FROM}>"
    msg["To"] = to_email
    msg.attach(MIMEText(_build_invite_html(invite_link, tenant_name, role), "html"))

    try:
        await aiosmtplib.send(
            msg,
            hostname=settings.SMTP_HOST,
            port=settings.SMTP_PORT,
            username=settings.SMTP_USER,
            password=settings.SMTP_PASSWORD,
            start_tls=True,
        )
        logger.info("Invite email sent to %s via Gmail SMTP", to_email)
    except Exception as exc:
        logger.warning("Failed to send invite email to %s: %s", to_email, exc)