import os
import ssl
import time
import json
import socket
import smtplib
import logging
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import formataddr, make_msgid
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


class ConfigError(Exception):
    pass


class EmailSendError(Exception):
    pass


def env_str(name: str, default: str | None = None, required: bool = False) -> str | None:
    val = os.getenv(name, default)
    if required and (val is None or str(val).strip() == ""):
        raise ConfigError(f"Missing required environment variable: {name}")
    return val


def parse_email_list(value: str | None) -> list[str]:
    if not value:
        return []
    parts = [p.strip() for p in value.replace(";", ",").split(",")]
    return [p for p in parts if p]


def safe_truncate(text: str, limit: int = 8000) -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n[truncated]"


@dataclass
class EmailConfig:
    gmail_username: str
    gmail_app_password: str
    to_list: list[str]
    cc_list: list[str]
    bcc_list: list[str]
    from_name: str
    smtp_host: str
    smtp_port: int


def load_email_config() -> EmailConfig:
    gmail_username = env_str("GMAIL_USERNAME", required=True)
    gmail_app_password = env_str("GMAIL_APP_PASSWORD", required=True)

    to_list = parse_email_list(env_str("EMAIL_TO", required=True))
    cc_list = parse_email_list(env_str("EMAIL_CC"))
    bcc_list = parse_email_list(env_str("EMAIL_BCC"))

    if not to_list:
        raise ConfigError("EMAIL_TO was provided but no valid recipient emails were parsed")

    from_name = env_str("EMAIL_FROM_NAME", default="Weekly Market Recap") or "Weekly Market Recap"
    smtp_host = env_str("SMTP_HOST", default="smtp.gmail.com") or "smtp.gmail.com"

    smtp_port_raw = env_str("SMTP_PORT", default="465") or "465"
    try:
        smtp_port = int(smtp_port_raw)
    except ValueError:
        smtp_port = 465

    return EmailConfig(
        gmail_username=gmail_username,
        gmail_app_password=gmail_app_password,
        to_list=to_list,
        cc_list=cc_list,
        bcc_list=bcc_list,
        from_name=from_name,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
    )


def send_email(
    cfg: EmailConfig,
    subject: str,
    text_body: str,
    html_body: str | None,
    max_retries: int = 5,
) -> None:
    all_recipients = cfg.to_list + cfg.cc_list + cfg.bcc_list

    msg = MIMEMultipart("alternative")
    msg["Subject"] = (subject or "").strip()
    msg["From"] = formataddr((cfg.from_name, cfg.gmail_username))
    msg["To"] = ", ".join(cfg.to_list)
    if cfg.cc_list:
        msg["Cc"] = ", ".join(cfg.cc_list)

    msg["Message-ID"] = make_msgid()
    msg.attach(MIMEText(text_body or "", "plain", "utf-8"))

    if html_body:
        msg.attach(MIMEText(html_body, "html", "utf-8"))

    context = ssl.create_default_context()
    timeout_seconds = 30
    last_err: Exception | None = None

    for attempt in range(1, max_retries + 1):
        try:
            logging.info(f"Email send attempt {attempt} using {cfg.smtp_host}:{cfg.smtp_port}")

            if cfg.smtp_port == 465:
                with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, context=context, timeout=timeout_seconds) as server:
                    server.login(cfg.gmail_username, cfg.gmail_app_password)
                    server.sendmail(cfg.gmail_username, all_recipients, msg.as_string())
            else:
                with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=timeout_seconds) as server:
                    server.ehlo()
                    server.starttls(context=context)
                    server.ehlo()
                    server.login(cfg.gmail_username, cfg.gmail_app_password)
                    server.sendmail(cfg.gmail_username, all_recipients, msg.as_string())

            logging.info("Email sent successfully")
            return

        except smtplib.SMTPAuthenticationError as e:
            raise EmailSendError(
                "Authentication failed. Verify GMAIL_USERNAME and use the Gmail App Password, not your normal password."
            ) from e

        except (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected, socket.timeout, ConnectionError, OSError) as e:
            last_err = e
            backoff = min(60, 2 ** attempt)
            logging.warning(f"Transient SMTP error {type(e).__name__}. Retrying in {backoff} seconds.")
            time.sleep(backoff)

        except smtplib.SMTPException as e:
            raise EmailSendError(f"Non retryable SMTP error: {repr(e)}") from e

    raise EmailSendError(f"Failed to send after {max_retries} attempts. Last error: {repr(last_err)}")


def html_wrapper(title: str, inner: str) -> str:
    return f"""
<html>
  <body style="font-family: Arial, sans-serif; line-height: 1.45; background: #ffffff;">
    <div style="max-width: 900px; margin: 0 auto; padding: 18px;">
      <h2 style="margin: 0 0 12px 0;">{title}</h2>
      {inner}
      <hr style="margin-top: 18px; border: none; border-top: 1px solid #e6e6e6;" />
      <p style="color: #666; font-size: 12px; margin: 10px 0 0 0;">
        Sent automatically via GitHub Actions.
      </p>
    </div>
  </body>
</html>
""".strip()


def build_report(prices: dict, news: list[dict], errors: list[str]) -> tuple[str, str, str]:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    subject = f"Weekly Market Recap | {ts}"

    text_lines = []
    text_lines.append(f"Weekly Market Recap ({ts})")
    text_lines.append("")
    text_lines.append("Prices:")
    text_lines.append(safe_truncate(json.dumps(prices, indent=2, ensure_ascii=False)))
    text_lines.append("")
    text_lines.append("News:")
    text_lines.append(safe_truncate(json.dumps(news[:15], indent=2, ensure_ascii=False)))
    text_lines.append("")
    text_lines.append("Errors:")
    text_lines.append("None" if not errors else safe_truncate("\n\n".join(errors), 12000))
    text_body = "\n".join(text_lines)

    price_rows = []
    for asset, v in (prices or {}).items():
        latest = v.get("latest", "NA")
        week_change = v.get("week_change_pct", "NA")
        price_rows.append(
            "<tr>"
            f"<td style='padding:8px 10px; border-bottom:1px solid #efefef;'><b>{asset}</b></td>"
            f"<td style='padding:8px 10px; border-bottom:1px solid #efefef;'>{latest}</td>"
            f"<td style='padding:8px 10px; border-bottom:1px solid #efefef;'>{week_change}</td>"
            "</tr>"
        )
    if not price_rows:
        price_table = "<p>No pricing data returned by your API.</p>"
    else:
        price_table = (
            "<table style='border-collapse:collapse; width:100%;'>"
            "<tr>"
            "<th style='text-align:left; padding:8px 10px; border-bottom:2px solid #d9d9d9;'>Asset</th>"
            "<th style='text-align:left; padding:8px 10px; border-bottom:2px solid #d9d9d9;'>Latest</th>"
            "<th style='text-align:left; padding:8px 10px; border-bottom:2px solid #d9d9d9;'>Weekly Change</th>"
            "</tr>"
            + "".join(price_rows)
            + "</table>"
        )

    news_items = []
    for item in (news or [])[:15]:
        title = item.get("title", "")
        source = item.get("source", "")
        asset = item.get("asset", "")
        url = item.get("url", "")
        if url:
            news_items.append(
                "<li style='margin: 6px 0;'>"
                f"<b>{asset}</b> "
                f"<a href='{url}' style='text-decoration:none;'>{title}</a> "
                f"<span style='color:#666;'>({source})</span>"
                "</li>"
            )
        else:
            news_items.append(
                "<li style='margin: 6px 0;'>"
                f"<b>{asset}</b> {title} <span style='color:#666;'>({source})</span>"
                "</li>"
            )
    news_block = "<p>No news items returned by your API.</p>" if not news_items else "<ul style='padding-left: 18px;'>" + "".join(news_items) + "</ul>"

    if errors:
        errors_html = (
            "<div style='background:#f7f7f7; padding:12px; border-radius:10px; white-space:pre-wrap;'>"
            + safe_truncate("\n\n".join(errors), 12000)
            + "</div>"
        )
    else:
        errors_html = "<p>None</p>"

    inner = (
        "<h3 style='margin: 18px 0 8px 0;'>Prices</h3>"
        + price_table
        + "<h3 style='margin: 18px 0 8px 0;'>Top Asset News</h3>"
        + news_block
        + "<h3 style='margin: 18px 0 8px 0;'>Errors and Warnings</h3>"
        + errors_html
    )

    html_body = html_wrapper("Weekly Market Recap", inner)
    return subject, text_body, html_body


def fetch_prices_your_api() -> dict:
    """
    Replace the body of this function with your real pricing API logic.

    Expected output format example:
    {
      "SPY": {"latest": 000.00, "week_change_pct": "1.23%"},
      "Silver Eagle Coins": {"latest": 00.00, "week_change_pct": "0.45%"}
    }
    """
    return {
        "SPY": {"latest": "NA", "week_change_pct": "NA"},
        "Silver Eagle Coins": {"latest": "NA", "week_change_pct": "NA"},
    }


def fetch_news_your_api() -> list[dict]:
    """
    Replace the body of this function with your real news API logic.

    Expected output format example:
    [
      {"asset": "SPY", "title": "Headline", "source": "Publisher", "url": "https://..."},
      {"asset": "Silver Eagle Coins", "title": "Headline", "source": "Publisher", "url": "https://..."}
    ]
    """
    return []


def main() -> int:
    errors: list[str] = []
    prices: dict = {}
    news: list[dict] = []

    try:
        prices = fetch_prices_your_api()
    except Exception:
        errors.append("Pricing API failed.\n" + traceback.format_exc())

    try:
        news = fetch_news_your_api()
    except Exception:
        errors.append("News API failed.\n" + traceback.format_exc())

    try:
        subject, text_body, html_body = build_report(prices, news, errors)
    except Exception:
        subject = "Weekly Market Recap | Report Build Failed"
        text_body = "Report build failed.\n\n" + traceback.format_exc()
        html_body = None

    try:
        cfg = load_email_config()
        send_email(cfg, subject=subject, text_body=text_body, html_body=html_body)
        return 0
    except Exception:
        logging.error("Email send failed.\n" + traceback.format_exc())
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
