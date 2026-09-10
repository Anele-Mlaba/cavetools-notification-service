"""Shared internal notification service.

Single Lambda used by multiple apps (cavetools, topselect, wellmed, madlite)
to send transactional emails through the Zoho Mail API instead of each app
maintaining its own email integration.
"""

import base64
import html
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone

import requests

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

ZOHO_TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"
ZOHO_SEND_MAIL_URL_TEMPLATE = "https://mail.zoho.com/api/accounts/{account_id}/messages"
ZOHO_UPLOAD_ATTACHMENT_URL_TEMPLATE = "https://mail.zoho.com/api/accounts/{account_id}/messages/attachments"
REQUEST_TIMEOUT_SECONDS = 10

# Allowed CORS origins
ALLOWED_ORIGINS = [
    "https://cavetools.co.za",
    "http://lifestyleclub.co.za",
    "https://sttps.co.za",
    "https://d248irxbraom5z.cloudfront.net",
    "https://wellmed.org.za",
]

# Cached across warm Lambda invocations so we don't request a new token on
# every call. Holds no long-lived secret, only a short-lived access token.
_token_cache = {"access_token": None, "expires_at": 0}


class NotificationError(Exception):
    """Raised for any expected failure that should map to a JSON error response."""

    def __init__(self, message, status_code):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Request parsing / validation
# ---------------------------------------------------------------------------

def parse_request_body(event):
    """Extract and JSON-decode the API Gateway proxy event body."""
    raw_body = event.get("body")
    if raw_body is None:
        raise NotificationError("Request body is required", 400)

    if event.get("isBase64Encoded"):
        raw_body = base64.b64decode(raw_body).decode("utf-8")

    try:
        body = json.loads(raw_body)
    except (json.JSONDecodeError, TypeError):
        raise NotificationError("Request body must be valid JSON", 400)

    if not isinstance(body, dict):
        raise NotificationError("Request body must be a JSON object", 400)

    return body


def validate_required_fields(body, fields):
    """Ensure each of `fields` is present in `body` and non-empty."""
    for field in fields:
        value = body.get(field)
        if value is None or value == "":
            raise NotificationError(f"Missing required field: {field}", 400)


def validate_new_lead_data(data):
    required = ["name", "email", "phone", "businessType", "message"]
    missing = [field for field in required if not data.get(field)]
    if missing:
        raise NotificationError(
            f"Missing required data field(s): {', '.join(missing)}", 400
        )


def validate_booking_confirmation_data(data):
    required = ["email", "companyName", "message"]
    missing = [field for field in required if not data.get(field)]
    if missing:
        raise NotificationError(
            f"Missing required data field(s): {', '.join(missing)}", 400
        )

    event = data.get("event")
    if not isinstance(event, dict):
        raise NotificationError("Missing required data field: event", 400)

    required_event_fields = ["title", "startTime", "endTime"]
    missing_event = [field for field in required_event_fields if not event.get(field)]
    if missing_event:
        raise NotificationError(
            f"Missing required event field(s): {', '.join(missing_event)}", 400
        )

    start, end = parse_event_times(event["startTime"], event["endTime"])
    if end <= start:
        raise NotificationError("Event 'endTime' must be after 'startTime'", 400)


def parse_event_times(start_time_raw, end_time_raw):
    """Parse ISO 8601 event start/end times, requiring an explicit UTC offset."""
    try:
        start = datetime.fromisoformat(str(start_time_raw))
        end = datetime.fromisoformat(str(end_time_raw))
    except ValueError:
        raise NotificationError(
            "Event 'startTime' and 'endTime' must be valid ISO 8601 datetimes", 400
        )

    if start.tzinfo is None or end.tzinfo is None:
        raise NotificationError(
            "Event 'startTime' and 'endTime' must include a UTC offset (e.g. 2026-08-10T10:00:00+02:00)", 400
        )

    return start, end


# ---------------------------------------------------------------------------
# App config
# ---------------------------------------------------------------------------

def load_app_config():
    """Load the appName -> app config map from the APP_CONFIG_JSON env var."""
    raw_config = os.environ.get("APP_CONFIG_JSON")
    if not raw_config:
        logger.error("APP_CONFIG_JSON environment variable is not set")
        raise NotificationError("Internal server error", 500)

    try:
        return json.loads(raw_config)
    except json.JSONDecodeError:
        logger.error("APP_CONFIG_JSON environment variable is not valid JSON")
        raise NotificationError("Internal server error", 500)


def get_app_entry(app_config, app_name):
    entry = app_config.get(app_name)
    if not entry:
        raise NotificationError("Unsupported appName", 400)
    return entry


def resolve_recipient(app_entry, recipient_type, data):
    """Resolve the recipient email address.

    `recipientType: "customer"` sends to the arbitrary customer address supplied
    in the request body (`data.email`), since customers aren't pre-registered in
    the app config. Any other recipientType is resolved from the app config using
    `{recipientType}Email`, as before.
    """
    if recipient_type == "customer":
        recipient_email = data.get("email")
        if not recipient_email:
            raise NotificationError("Missing required data field: email", 400)
        return recipient_email

    recipient_key = f"{recipient_type}Email"
    recipient_email = app_entry.get(recipient_key)
    if not recipient_email:
        raise NotificationError("Unsupported recipientType", 400)
    return recipient_email


# ---------------------------------------------------------------------------
# Email templates
# ---------------------------------------------------------------------------

def sanitize_html(value):
    """Escape a value so it is safe to embed inside an HTML email body."""
    return html.escape(str(value), quote=True)


def build_new_lead_email(app_display_name, data):
    """Build the subject, HTML body, and plain-text body for a new_lead notification."""
    name_raw = str(data.get("name", ""))
    email_raw = str(data.get("email", ""))
    phone_raw = str(data.get("phone", ""))
    business_type_raw = str(data.get("businessType", ""))
    message_raw = str(data.get("message", ""))

    name = sanitize_html(name_raw)
    email = sanitize_html(email_raw)
    phone = sanitize_html(phone_raw)
    business_type = sanitize_html(business_type_raw)
    message_html = sanitize_html(message_raw).replace("\n", "<br>")
    app_display_name_safe = sanitize_html(app_display_name)

    subject = f"New enquiry from {name_raw} via {app_display_name}"

    html_body = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{sanitize_html(subject)}</title>
</head>
<body style="margin:0; padding:0; background-color:#f4f5f7; font-family:'Segoe UI', Arial, sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#f4f5f7; padding:24px 0;">
    <tr>
      <td align="center">
        <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="background-color:#ffffff; border-radius:8px; overflow:hidden; box-shadow:0 1px 3px rgba(0,0,0,0.1); max-width:600px;">
          <tr>
            <td style="background-color:#111827; padding:24px 32px;">
              <span style="color:#ffffff; font-size:18px; font-weight:600; letter-spacing:0.3px;">{app_display_name_safe}</span>
            </td>
          </tr>
          <tr>
            <td style="padding:32px;">
              <h1 style="margin:0 0 8px 0; font-size:20px; color:#111827; font-family:'Segoe UI', Arial, sans-serif;">New Enquiry Received</h1>
              <p style="margin:0 0 24px 0; font-size:14px; color:#6b7280; line-height:1.5;">A new lead has come in through {app_display_name_safe}. Details are below.</p>
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                <tr>
                  <td style="padding:10px 0; border-bottom:1px solid #e5e7eb; font-size:13px; color:#6b7280; width:130px; vertical-align:top;">Name</td>
                  <td style="padding:10px 0; border-bottom:1px solid #e5e7eb; font-size:14px; color:#111827; vertical-align:top;">{name}</td>
                </tr>
                <tr>
                  <td style="padding:10px 0; border-bottom:1px solid #e5e7eb; font-size:13px; color:#6b7280; vertical-align:top;">Email</td>
                  <td style="padding:10px 0; border-bottom:1px solid #e5e7eb; font-size:14px; vertical-align:top;"><a href="mailto:{email}" style="color:#2563eb; text-decoration:none;">{email}</a></td>
                </tr>
                <tr>
                  <td style="padding:10px 0; border-bottom:1px solid #e5e7eb; font-size:13px; color:#6b7280; vertical-align:top;">Phone</td>
                  <td style="padding:10px 0; border-bottom:1px solid #e5e7eb; font-size:14px; color:#111827; vertical-align:top;">{phone}</td>
                </tr>
                <tr>
                  <td style="padding:10px 0; font-size:13px; color:#6b7280; vertical-align:top;">Business Type</td>
                  <td style="padding:10px 0; font-size:14px; color:#111827; vertical-align:top;">{business_type}</td>
                </tr>
              </table>
              <div style="margin-top:20px;">
                <p style="margin:0 0 6px 0; font-size:13px; color:#6b7280;">Message</p>
                <div style="background-color:#f9fafb; border:1px solid #e5e7eb; border-radius:6px; padding:16px; font-size:14px; color:#111827; line-height:1.5;">{message_html}</div>
              </div>
            </td>
          </tr>
          <tr>
            <td style="background-color:#f9fafb; padding:16px 32px; border-top:1px solid #e5e7eb;">
              <p style="margin:0; font-size:12px; color:#9ca3af; line-height:1.5;">This is an automated notification sent by the {app_display_name_safe} notification service. Please do not reply to this email.</p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""

    text_body = (
        f"New Enquiry Received - {app_display_name}\n\n"
        f"Name: {name_raw}\n"
        f"Email: {email_raw}\n"
        f"Phone: {phone_raw}\n"
        f"Business Type: {business_type_raw}\n\n"
        f"Message:\n{message_raw}\n\n"
        "---\n"
        f"This is an automated notification sent by the {app_display_name} notification service."
    )

    return subject, html_body, text_body


def format_event_time_range(event):
    """Render an event's start/end time as a human-readable string, e.g.
    'Monday, 10 August 2026, 10:00 - 10:30 (UTC+02:00)'."""
    start, end = parse_event_times(event["startTime"], event["endTime"])
    offset = start.strftime("%z")
    offset_display = f"UTC{offset[:3]}:{offset[3:]}" if offset else "UTC"
    date_part = start.strftime("%A, %d %B %Y")
    time_part = f"{start.strftime('%H:%M')} - {end.strftime('%H:%M')}"
    return f"{date_part}, {time_part} ({offset_display})"


def _prepare_booking_event_fields(data):
    """Extract and sanitize the fields shared by booking_confirmation and
    booking_reminder emails (event details, customer name, message)."""
    event = data["event"]
    name_raw = str(data.get("name") or "there")
    message_raw = str(data.get("message", ""))
    title_raw = str(event.get("title", ""))
    location_raw = str(event.get("location", ""))
    description_raw = str(event.get("description", ""))
    time_range_raw = format_event_time_range(event)

    name = sanitize_html(name_raw)
    message_html = sanitize_html(message_raw).replace("\n", "<br>")
    title = sanitize_html(title_raw)
    location = sanitize_html(location_raw)
    description_html = sanitize_html(description_raw).replace("\n", "<br>")
    time_range = sanitize_html(time_range_raw)

    optional_rows = ""
    if location_raw:
        optional_rows += f"""
                <tr>
                  <td style="padding:10px 0; border-bottom:1px solid #e5e7eb; font-size:13px; color:#6b7280; vertical-align:top;">Location</td>
                  <td style="padding:10px 0; border-bottom:1px solid #e5e7eb; font-size:14px; color:#111827; vertical-align:top;">{location}</td>
                </tr>"""
    if description_raw:
        optional_rows += f"""
                <tr>
                  <td style="padding:10px 0; font-size:13px; color:#6b7280; vertical-align:top;">Details</td>
                  <td style="padding:10px 0; font-size:14px; color:#111827; vertical-align:top;">{description_html}</td>
                </tr>"""

    return {
        "name_raw": name_raw,
        "message_raw": message_raw,
        "title_raw": title_raw,
        "location_raw": location_raw,
        "description_raw": description_raw,
        "time_range_raw": time_range_raw,
        "name": name,
        "message_html": message_html,
        "title": title,
        "time_range": time_range,
        "optional_rows": optional_rows,
    }


def build_booking_confirmation_email(company_name, data):
    """Build the subject, HTML body, and plain-text body for a booking_confirmation notification."""
    f = _prepare_booking_event_fields(data)
    name_raw, message_raw, title_raw = f["name_raw"], f["message_raw"], f["title_raw"]
    location_raw, description_raw, time_range_raw = f["location_raw"], f["description_raw"], f["time_range_raw"]
    name, message_html, title, time_range = f["name"], f["message_html"], f["title"], f["time_range"]
    optional_rows = f["optional_rows"]

    company_name_safe = sanitize_html(company_name)

    subject = f"Booking confirmed: {title_raw}"

    html_body = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{sanitize_html(subject)}</title>
</head>
<body style="margin:0; padding:0; background-color:#f4f5f7; font-family:'Segoe UI', Arial, sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#f4f5f7; padding:24px 0;">
    <tr>
      <td align="center">
        <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="background-color:#ffffff; border-radius:8px; overflow:hidden; box-shadow:0 1px 3px rgba(0,0,0,0.1); max-width:600px;">
          <tr>
            <td style="background-color:#111827; padding:24px 32px;">
              <span style="color:#ffffff; font-size:18px; font-weight:600; letter-spacing:0.3px;">{company_name_safe}</span>
            </td>
          </tr>
          <tr>
            <td style="padding:32px;">
              <p style="margin:0 0 16px 0; font-size:12px; color:#9ca3af; text-transform:uppercase; letter-spacing:0.5px;">Sent on behalf of {company_name_safe}</p>
              <h1 style="margin:0 0 8px 0; font-size:20px; color:#111827; font-family:'Segoe UI', Arial, sans-serif;">Booking Confirmed</h1>
              <p style="margin:0 0 24px 0; font-size:14px; color:#6b7280; line-height:1.5;">Hi {name}, your booking with {company_name_safe} is confirmed. A calendar invite is attached to this email.</p>
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                <tr>
                  <td style="padding:10px 0; border-bottom:1px solid #e5e7eb; font-size:13px; color:#6b7280; width:130px; vertical-align:top;">Event</td>
                  <td style="padding:10px 0; border-bottom:1px solid #e5e7eb; font-size:14px; color:#111827; vertical-align:top;">{title}</td>
                </tr>
                <tr>
                  <td style="padding:10px 0; border-bottom:1px solid #e5e7eb; font-size:13px; color:#6b7280; vertical-align:top;">When</td>
                  <td style="padding:10px 0; border-bottom:1px solid #e5e7eb; font-size:14px; color:#111827; vertical-align:top;">{time_range}</td>
                </tr>{optional_rows}
              </table>
              <div style="margin-top:20px;">
                <p style="margin:0 0 6px 0; font-size:13px; color:#6b7280;">Message</p>
                <div style="background-color:#f9fafb; border:1px solid #e5e7eb; border-radius:6px; padding:16px; font-size:14px; color:#111827; line-height:1.5;">{message_html}</div>
              </div>
            </td>
          </tr>
          <tr>
            <td style="background-color:#f9fafb; padding:16px 32px; border-top:1px solid #e5e7eb;">
              <p style="margin:0; font-size:12px; color:#9ca3af; line-height:1.5;">This is an automated booking confirmation sent on behalf of {company_name_safe}. Please do not reply to this email.</p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""

    text_body = (
        f"Booking Confirmed - {company_name}\n\n"
        f"Hi {name_raw}, your booking with {company_name} is confirmed. A calendar invite is attached.\n\n"
        f"Event: {title_raw}\n"
        f"When: {time_range_raw}\n"
        + (f"Location: {location_raw}\n" if location_raw else "")
        + (f"Details: {description_raw}\n" if description_raw else "")
        + f"\nMessage:\n{message_raw}\n\n"
        "---\n"
        f"This is an automated booking confirmation sent on behalf of {company_name}."
    )

    return subject, html_body, text_body


def build_booking_reminder_email(company_name, data):
    """Build the subject, HTML body, and plain-text body for a booking_reminder notification.

    Shares the same required `data`/`event` fields and the same calendar-invite
    handling as booking_confirmation (see `_prepare_booking_event_fields` and
    the `notification_type in ("booking_confirmation", "booking_reminder")`
    checks in `build_email_template`/`lambda_handler`), but uses reminder
    wording instead of confirmation wording since the appointment was already
    confirmed previously.
    """
    f = _prepare_booking_event_fields(data)
    name_raw, message_raw, title_raw = f["name_raw"], f["message_raw"], f["title_raw"]
    location_raw, description_raw, time_range_raw = f["location_raw"], f["description_raw"], f["time_range_raw"]
    name, message_html, title, time_range = f["name"], f["message_html"], f["title"], f["time_range"]
    optional_rows = f["optional_rows"]

    company_name_safe = sanitize_html(company_name)

    subject = f"Reminder: your {company_name} appointment is coming up"

    html_body = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{sanitize_html(subject)}</title>
</head>
<body style="margin:0; padding:0; background-color:#f4f5f7; font-family:'Segoe UI', Arial, sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#f4f5f7; padding:24px 0;">
    <tr>
      <td align="center">
        <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="background-color:#ffffff; border-radius:8px; overflow:hidden; box-shadow:0 1px 3px rgba(0,0,0,0.1); max-width:600px;">
          <tr>
            <td style="background-color:#111827; padding:24px 32px;">
              <span style="color:#ffffff; font-size:18px; font-weight:600; letter-spacing:0.3px;">{company_name_safe}</span>
            </td>
          </tr>
          <tr>
            <td style="padding:32px;">
              <p style="margin:0 0 16px 0; font-size:12px; color:#9ca3af; text-transform:uppercase; letter-spacing:0.5px;">Sent on behalf of {company_name_safe}</p>
              <h1 style="margin:0 0 8px 0; font-size:20px; color:#111827; font-family:'Segoe UI', Arial, sans-serif;">Upcoming Appointment Reminder</h1>
              <p style="margin:0 0 24px 0; font-size:14px; color:#6b7280; line-height:1.5;">Hi {name}, this is a friendly reminder about your upcoming appointment with {company_name_safe}. A calendar invite is attached to this email.</p>
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                <tr>
                  <td style="padding:10px 0; border-bottom:1px solid #e5e7eb; font-size:13px; color:#6b7280; width:130px; vertical-align:top;">Event</td>
                  <td style="padding:10px 0; border-bottom:1px solid #e5e7eb; font-size:14px; color:#111827; vertical-align:top;">{title}</td>
                </tr>
                <tr>
                  <td style="padding:10px 0; border-bottom:1px solid #e5e7eb; font-size:13px; color:#6b7280; vertical-align:top;">When</td>
                  <td style="padding:10px 0; border-bottom:1px solid #e5e7eb; font-size:14px; color:#111827; vertical-align:top;">{time_range}</td>
                </tr>{optional_rows}
              </table>
              <div style="margin-top:20px;">
                <p style="margin:0 0 6px 0; font-size:13px; color:#6b7280;">Message</p>
                <div style="background-color:#f9fafb; border:1px solid #e5e7eb; border-radius:6px; padding:16px; font-size:14px; color:#111827; line-height:1.5;">{message_html}</div>
              </div>
            </td>
          </tr>
          <tr>
            <td style="background-color:#f9fafb; padding:16px 32px; border-top:1px solid #e5e7eb;">
              <p style="margin:0; font-size:12px; color:#9ca3af; line-height:1.5;">This is an automated appointment reminder sent on behalf of {company_name_safe}. Please do not reply to this email.</p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""

    text_body = (
        f"Appointment Reminder - {company_name}\n\n"
        f"Hi {name_raw}, this is a friendly reminder about your upcoming appointment with {company_name}. A calendar invite is attached.\n\n"
        f"Event: {title_raw}\n"
        f"When: {time_range_raw}\n"
        + (f"Location: {location_raw}\n" if location_raw else "")
        + (f"Details: {description_raw}\n" if description_raw else "")
        + f"\nMessage:\n{message_raw}\n\n"
        "---\n"
        f"This is an automated appointment reminder sent on behalf of {company_name}."
    )

    return subject, html_body, text_body


TEMPLATE_BUILDERS = {
    "new_lead": build_new_lead_email,
    "booking_confirmation": build_booking_confirmation_email,
    "booking_reminder": build_booking_reminder_email,
}


def build_email_template(notification_type, app_display_name, data):
    builder = TEMPLATE_BUILDERS.get(notification_type)
    if not builder:
        raise NotificationError("Unsupported notificationType", 400)

    if notification_type == "new_lead":
        validate_new_lead_data(data)
    elif notification_type in ("booking_confirmation", "booking_reminder"):
        validate_booking_confirmation_data(data)
        app_display_name = data["companyName"]

    return builder(app_display_name, data)


# ---------------------------------------------------------------------------
# Calendar invites
# ---------------------------------------------------------------------------

def ics_escape(value):
    """Escape text per RFC 5545 §3.3.11 so it is safe inside an ICS text field."""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace(",", "\\,")
        .replace(";", "\\;")
    )


def ics_param_value(value):
    """Format a value for an ICS parameter (e.g. CN=). Per RFC 5545 §3.2, parameter
    values containing a comma, semicolon, or colon must be wrapped in double quotes
    rather than backslash-escaped (backslash-escaping only applies to property values)."""
    value = str(value).replace('"', "'")
    if any(ch in value for ch in (",", ";", ":")):
        return f'"{value}"'
    return value


def format_ics_datetime(dt):
    """Format a timezone-aware datetime as a UTC ICS DATE-TIME value (e.g. 20260810T080000Z)."""
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def fold_ics_line(line):
    """Fold a content line at 75 octets per RFC 5545 §3.1, continuation lines
    starting with a single space."""
    if len(line.encode("utf-8")) <= 75:
        return line
    folded, rest = line[:75], line[75:]
    while rest:
        chunk, rest = rest[:74], rest[74:]
        folded += "\r\n " + chunk
    return folded


def build_calendar_invite(company_name, organizer_email, attendee_email, attendee_name, event):
    """Build an RFC 5545 iCalendar (.ics) meeting request for the given event.

    Using METHOD:REQUEST makes Google Calendar, Outlook, and Apple Mail render
    this as an actual invite (with Accept/Decline) rather than a plain file.
    """
    start, end = parse_event_times(event["startTime"], event["endTime"])

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//cavetools-notification-service//booking-confirmation//EN",
        "METHOD:REQUEST",
        "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT",
        f"UID:{uuid.uuid4()}@notification-service.cavetools.co.za",
        f"DTSTAMP:{format_ics_datetime(datetime.now(timezone.utc))}",
        f"DTSTART:{format_ics_datetime(start)}",
        f"DTEND:{format_ics_datetime(end)}",
        f"SUMMARY:{ics_escape(event.get('title', ''))}",
        f"ORGANIZER;CN={ics_param_value(company_name)}:mailto:{organizer_email}",
        f"ATTENDEE;CN={ics_param_value(attendee_name)};ROLE=REQ-PARTICIPANT;RSVP=TRUE:mailto:{attendee_email}",
        "STATUS:CONFIRMED",
        "SEQUENCE:0",
    ]
    if event.get("location"):
        lines.append(f"LOCATION:{ics_escape(event['location'])}")
    if event.get("description"):
        lines.append(f"DESCRIPTION:{ics_escape(event['description'])}")
    lines += ["END:VEVENT", "END:VCALENDAR"]

    return "\r\n".join(fold_ics_line(line) for line in lines).encode("utf-8") + b"\r\n"


# ---------------------------------------------------------------------------
# Zoho Mail
# ---------------------------------------------------------------------------

def get_zoho_access_token():
    """Get (and cache) a Zoho access token by exchanging the refresh token.

    The access token is cached in-memory for the lifetime of the warm Lambda
    execution environment and transparently refreshed once it is within 60
    seconds of expiring, so no user interaction is ever required.
    """
    now = time.time()
    if _token_cache["access_token"] and _token_cache["expires_at"] - 60 > now:
        return _token_cache["access_token"]

    client_id = os.environ.get("ZOHO_CLIENT_ID")
    client_secret = os.environ.get("ZOHO_CLIENT_SECRET")
    refresh_token = os.environ.get("ZOHO_REFRESH_TOKEN")
    if not all([client_id, client_secret, refresh_token]):
        logger.error("Zoho OAuth environment variables are not fully configured")
        raise NotificationError("Internal server error", 500)

    payload = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    }

    try:
        response = requests.post(ZOHO_TOKEN_URL, data=payload, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
    except requests.RequestException as exc:
        status_code = getattr(exc.response, "status_code", "unknown")
        logger.error("Failed to obtain Zoho access token (status=%s)", status_code)
        raise NotificationError("Failed to authenticate with email service", 502)

    token_data = response.json()
    access_token = token_data.get("access_token")
    expires_in = token_data.get("expires_in", 3600)
    if not access_token:
        logger.error(
            "Zoho token response did not contain an access_token (error=%s)",
            token_data.get("error", "unknown"),
        )
        raise NotificationError("Failed to authenticate with email service", 502)

    _token_cache["access_token"] = access_token
    _token_cache["expires_at"] = now + expires_in
    logger.info("Obtained new Zoho access token, expires_in=%s", expires_in)
    return access_token


def upload_zoho_attachment(access_token, account_id, filename, content_bytes):
    """Upload a file to Zoho Mail so it can be referenced as an email attachment.

    Zoho's raw-upload endpoint requires the request itself to be sent as
    application/octet-stream regardless of the file's real type - it infers the
    attachment's content type from the `fileName` extension instead.

    Returns the {storeName, attachmentPath, attachmentName} descriptor the send
    API expects in its `attachments` array.
    """
    url = ZOHO_UPLOAD_ATTACHMENT_URL_TEMPLATE.format(account_id=account_id)
    headers = {
        "Authorization": f"Zoho-oauthtoken {access_token}",
        "Content-Type": "application/octet-stream",
    }
    params = {"fileName": filename, "isInline": "false"}

    try:
        response = requests.post(
            url, headers=headers, params=params, data=content_bytes, timeout=REQUEST_TIMEOUT_SECONDS
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        status_code = getattr(exc.response, "status_code", "unknown")
        logger.error("Zoho Mail attachment upload failed (status=%s)", status_code)
        raise NotificationError("Failed to attach calendar invite to notification email", 502)

    attachment_data = response.json().get("data")
    if not isinstance(attachment_data, dict) or not all(
        attachment_data.get(key) for key in ("storeName", "attachmentPath", "attachmentName")
    ):
        logger.error("Zoho Mail attachment upload response was missing expected fields")
        raise NotificationError("Failed to attach calendar invite to notification email", 502)

    return {
        "storeName": attachment_data["storeName"],
        "attachmentPath": attachment_data["attachmentPath"],
        "attachmentName": attachment_data["attachmentName"],
    }


def send_email_via_zoho(access_token, account_id, from_address, recipient_email, subject, html_body, attachments=None):
    """Send an email via the Zoho Mail API, optionally with pre-uploaded attachments."""
    url = ZOHO_SEND_MAIL_URL_TEMPLATE.format(account_id=account_id)
    payload = {
        "fromAddress": from_address,
        "toAddress": recipient_email,
        "subject": subject,
        "content": html_body,
    }
    if attachments:
        payload["attachments"] = attachments

    headers = {
        "Authorization": f"Zoho-oauthtoken {access_token}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
    except requests.RequestException as exc:
        status_code = getattr(exc.response, "status_code", "unknown")
        logger.error("Zoho Mail send request failed (status=%s)", status_code)
        raise NotificationError("Failed to send notification email", 502)


# ---------------------------------------------------------------------------
# Response helper
# ---------------------------------------------------------------------------

def build_response(status_code, success, message, origin=None):
    headers = {
        "Content-Type": "application/json",
        "Access-Control-Allow-Headers": "Content-Type,Authorization",
        "Access-Control-Allow-Methods": "POST,OPTIONS"
    }
    if origin and origin in ALLOWED_ORIGINS:
        headers["Access-Control-Allow-Origin"] = origin
    return {
        "statusCode": status_code,
        "headers": headers,
        "body": json.dumps({"success": success, "message": message}),
    }


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def lambda_handler(event, context):
    origin = event.get("headers", {}).get("origin")

    if event.get("httpMethod") == "OPTIONS":
        headers = {
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "POST,OPTIONS"
        }
        if origin and origin in ALLOWED_ORIGINS:
            headers["Access-Control-Allow-Origin"] = origin
        return {
            "statusCode": 200,
            "headers": headers,
            "body": ""
        }

    try:
        body = parse_request_body(event)
        validate_required_fields(body, ["appName", "notificationType", "recipientType", "data"])

        app_name = body["appName"]
        notification_type = body["notificationType"]
        recipient_type = body["recipientType"]
        data = body["data"]

        if not isinstance(data, dict):
            raise NotificationError("Field 'data' must be an object", 400)

        logger.info(
            "Notification request received appName=%s notificationType=%s recipientType=%s",
            app_name, notification_type, recipient_type,
        )

        app_config = load_app_config()
        app_entry = get_app_entry(app_config, app_name)
        recipient_email = resolve_recipient(app_entry, recipient_type, data)
        app_display_name = app_entry.get("displayName", app_name)

        subject, html_body, _text_body = build_email_template(notification_type, app_display_name, data)

        from_address = os.environ.get("EMAIL_FROM_ADDRESS")
        account_id = os.environ.get("ZOHO_ACCOUNT_ID")
        if not from_address or not account_id:
            logger.error("EMAIL_FROM_ADDRESS or ZOHO_ACCOUNT_ID environment variable is not set")
            raise NotificationError("Internal server error", 500)

        access_token = get_zoho_access_token()

        attachments = None
        if notification_type in ("booking_confirmation", "booking_reminder"):
            invite_bytes = build_calendar_invite(
                data["companyName"], from_address, recipient_email,
                data.get("name") or recipient_email, data["event"],
            )
            uploaded_invite = upload_zoho_attachment(
                access_token, account_id, "invite.ics", invite_bytes
            )
            attachments = [uploaded_invite]

        send_email_via_zoho(
            access_token, account_id, from_address, recipient_email, subject, html_body, attachments
        )

        logger.info(
            "Notification sent appName=%s notificationType=%s recipientType=%s",
            app_name, notification_type, recipient_type,
        )
        return build_response(200, True, "Notification sent successfully", origin)

    except NotificationError as exc:
        logger.warning("Notification request failed: %s", exc.message)
        return build_response(exc.status_code, False, exc.message, origin)
    except Exception:
        logger.exception("Unexpected error while processing notification request")
        return build_response(500, False, "Internal server error", origin)
