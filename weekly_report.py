# weekly_report.py
# Weekly Market Recap email via Gmail SMTP
# SPY weekly pricing from Alpha Vantage TIME_SERIES_WEEKLY_ADJUSTED
# Silver proxy via SLV weekly pricing from Alpha Vantage TIME_SERIES_WEEKLY_ADJUSTED
# Headlines from NewsAPI
# Always attempts email send even if APIs fail

from __future__ import annotations

import json
import logging
import os
import socket
import ssl
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, make_msgid
from typing import Any

import smtplib
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# ========= Exceptions =========

class ConfigError(Exception):
    pass


class EmailSendError(Exception):
    pass


class DataFetchError(Exception):
    pass


class RateLimitError(DataFetchError):
    pass


# ========= Helpers =========

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


def safe_truncate(text: str, limit: int = 14000) -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n[truncated]"


def escape_html(text: Any) -> str:
    if text is None:
        return ""
    s = str(text)
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def as_float(x: Any) -> float | None:
    try:
        if x is None:
            return None
        if isinstance(x, (int, float)):
            return float(x)
        s = str(x).strip()
        if s.endswith("%"):
            s = s[:-1].strip()
        return float(s)
    except Exception:
        return None


def fmt_money(x: Any) -> str:
    f = as_float(x)
    if f is None:
        return "NA"
    if abs(f) >= 1000:
        return f"{f:,.2f}"
    return f"{f:.2f}"


def fmt_pct(x: Any) -> str:
    f = as_float(x)
    if f is None:
        return "NA"
    return f"{f:.2f}%"


def badge_style(week_change_pct: Any) -> tuple[str, str]:
    f = as_float(week_change_pct)
    if f is None:
        return "badge neutral", ""
    if f > 0:
        return "badge up", "▲"
    if f < 0:
        return "badge down", "▼"
    return "badge neutral", ""


# ========= Email config and sender =========

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


def send_email(cfg: EmailConfig, subject: str, text_body: str, html_body: str | None) -> None:
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
    max_retries = 6
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
            raise EmailSendError("Authentication failed. Verify Gmail username and App Password.") from e

        except (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected, socket.timeout, ConnectionError, OSError) as e:
            last_err = e
            backoff = min(90, 2 ** attempt)
            logging.warning(f"Transient SMTP error {type(e).__name__}. Retrying in {backoff} seconds.")
            time.sleep(backoff)

        except smtplib.SMTPException as e:
            raise EmailSendError(f"Non retryable SMTP error: {repr(e)}") from e

    raise EmailSendError(f"Failed to send after {max_retries} attempts. Last error: {repr(last_err)}")


# ========= HTTP session =========

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "weekly-market-report/1.4", "Accept": "application/json"})
    return s


# ========= Alpha Vantage with throttling =========

ALPHAVANTAGE_BASE_URL = "https://www.alphavantage.co/query"

AV_MIN_SECONDS_BETWEEN_CALLS = 1.2
AV_RATE_LIMIT_BACKOFF_SECONDS = [15, 30, 60]

_last_av_call_ts = 0.0


def _av_throttle() -> None:
    global _last_av_call_ts
    now = time.time()
    wait = AV_MIN_SECONDS_BETWEEN_CALLS - (now - _last_av_call_ts)
    if wait > 0:
        time.sleep(wait)
    _last_av_call_ts = time.time()


def av_get_json(session: requests.Session, params: dict[str, Any]) -> dict[str, Any]:
    last_err: Exception | None = None

    for attempt in range(1, len(AV_RATE_LIMIT_BACKOFF_SECONDS) + 2):
        _av_throttle()

        try:
            r = session.get(ALPHAVANTAGE_BASE_URL, params=params, timeout=30)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            raise DataFetchError(f"Alpha Vantage request failed: {repr(e)}") from e

        if not isinstance(data, dict):
            raise DataFetchError("Alpha Vantage returned non JSON response")

        if "Error Message" in data:
            raise DataFetchError(f"Alpha Vantage error: {data.get('Error Message')}")

        note = data.get("Note")
        info = data.get("Information")

        if note or (info and "Thank you for using Alpha Vantage" in str(info)):
            last_err = RateLimitError(f"Alpha Vantage rate limit: {note or info}")
            if attempt <= len(AV_RATE_LIMIT_BACKOFF_SECONDS):
                sleep_s = AV_RATE_LIMIT_BACKOFF_SECONDS[attempt - 1]
                logging.warning(f"Alpha Vantage rate limited. Backing off for {sleep_s} seconds.")
                time.sleep(sleep_s)
                continue
            raise last_err

        return data

    raise DataFetchError(f"Alpha Vantage failed after retries. Last error: {repr(last_err)}")


def av_weekly_equity_close(session: requests.Session, api_key: str, symbol: str) -> tuple[float, float]:
    data = av_get_json(
        session,
        {"function": "TIME_SERIES_WEEKLY_ADJUSTED", "symbol": symbol, "apikey": api_key},
    )

    ts = data.get("Weekly Adjusted Time Series")
    if not isinstance(ts, dict) or not ts:
        raise DataFetchError(f"Alpha Vantage did not return weekly equity time series for {symbol}")

    dates = sorted(ts.keys(), reverse=True)
    if len(dates) < 2:
        raise DataFetchError(f"Not enough weekly points to compute weekly change for {symbol}")

    latest = ts[dates[0]]
    prev = ts[dates[1]]

    latest_close = float(latest.get("5. adjusted close") or latest["4. close"])
    prev_close = float(prev.get("5. adjusted close") or prev["4. close"])
    if prev_close == 0:
        raise DataFetchError("Previous close was 0, cannot compute percent change")

    week_change_pct = (latest_close / prev_close - 1.0) * 100.0
    return latest_close, week_change_pct


# ========= NewsAPI =========

NEWSAPI_BASE_URL = "https://newsapi.org/v2/everything"


def newsapi_get(session: requests.Session, api_key: str, query: str, page_size: int = 5) -> list[dict[str, Any]]:
    params = {
        "q": query,
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": page_size,
        "apiKey": api_key,
    }
    try:
        r = session.get(NEWSAPI_BASE_URL, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        raise DataFetchError(f"NewsAPI request failed: {repr(e)}") from e

    if not isinstance(data, dict):
        raise DataFetchError("NewsAPI returned non JSON response")

    if data.get("status") != "ok":
        raise DataFetchError(f"NewsAPI error: {data.get('message') or 'unknown'}")

    articles = data.get("articles") or []
    if not isinstance(articles, list):
        return []

    out: list[dict[str, Any]] = []
    for a in articles:
        if not isinstance(a, dict):
            continue
        out.append(
            {
                "title": a.get("title") or "",
                "source": (a.get("source") or {}).get("name") if isinstance(a.get("source"), dict) else "",
                "url": a.get("url") or "",
                "publishedAt": a.get("publishedAt") or "",
            }
        )
    return out


# ========= Email rendering =========

def html_wrapper(title: str, subtitle: str, body_html: str) -> str:
    return f"""
<html>
  <body style="margin:0; padding:0; background:#f5f7fb;">
    <div style="display:none; max-height:0px; overflow:hidden; opacity:0;">Weekly Market Recap</div>
    <table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="background:#f5f7fb; padding:24px 0;">
      <tr>
        <td align="center">
          <table role="presentation" cellpadding="0" cellspacing="0" width="920" style="max-width:920px; width:100%;">
            <tr>
              <td style="padding:0 16px;">
                <div style="background:#ffffff; border:1px solid #e8edf6; border-radius:16px; overflow:hidden; box-shadow:0 6px 24px rgba(20, 35, 60, 0.06);">
                  <div style="padding:18px 20px; background:linear-gradient(135deg, #eef6ff 0%, #f4f2ff 100%); border-bottom:1px solid #e8edf6;">
                    <div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Arial,sans-serif; font-size:18px; font-weight:700; color:#0f172a;">
                      {escape_html(title)}
                    </div>
                    <div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Arial,sans-serif; font-size:13px; color:#475569; margin-top:6px;">
                      {escape_html(subtitle)}
                    </div>
                  </div>
                  <div style="padding:18px 20px;">{body_html}</div>
                  <div style="padding:14px 20px; border-top:1px solid #e8edf6; background:#fbfcff;">
                    <div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Arial,sans-serif; font-size:12px; color:#64748b;">
                      Sent automatically via GitHub Actions.
                    </div>
                  </div>
                </div>
              </td>
            </tr>
            <tr><td style="height:14px;"></td></tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>
""".strip()


def prices_table(prices: dict[str, dict[str, Any]]) -> str:
    rows = []
    for asset, v in (prices or {}).items():
        latest = v.get("latest")
        week_change_pct = v.get("week_change_pct")
        klass, arrow = badge_style(week_change_pct)

        rows.append(
            f"""
<tr>
  <td style="padding:12px 12px; border-bottom:1px solid #eef2f8; font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Arial,sans-serif; color:#0f172a; font-size:14px;">
    <div style="font-weight:700;">{escape_html(asset)}</div>
  </td>
  <td style="padding:12px 12px; border-bottom:1px solid #eef2f8; font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Arial,sans-serif; color:#0f172a; font-size:14px; text-align:right;">
    {fmt_money(latest)}
  </td>
  <td style="padding:12px 12px; border-bottom:1px solid #eef2f8; font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Arial,sans-serif; font-size:14px; text-align:right;">
    <span class="{klass}" style="display:inline-block; padding:6px 10px; border-radius:999px; font-weight:700;">
      {arrow} {fmt_pct(week_change_pct)}
    </span>
  </td>
</tr>
""".strip()
        )

    if not rows:
        return "<p style='margin:0; color:#475569; font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Arial,sans-serif;'>No pricing data returned.</p>"

    return f"""
<style>
  .badge {{ border: 1px solid transparent; }}
  .badge.up {{ background:#eefaf3; color:#116a3a; border-color:#cfeedd; }}
  .badge.down {{ background:#fff1f2; color:#9f1239; border-color:#fecdd3; }}
  .badge.neutral {{ background:#f1f5f9; color:#334155; border-color:#e2e8f0; }}
</style>

<table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="border-collapse:collapse; border:1px solid #e8edf6; border-radius:14px; overflow:hidden;">
  <tr style="background:#f8fafc;">
    <th style="text-align:left; padding:12px 12px; font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Arial,sans-serif; font-size:12px; color:#475569; letter-spacing:0.04em; text-transform:uppercase;">Asset</th>
    <th style="text-align:right; padding:12px 12px; font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Arial,sans-serif; font-size:12px; color:#475569; letter-spacing:0.04em; text-transform:uppercase;">Latest</th>
    <th style="text-align:right; padding:12px 12px; font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Arial,sans-serif; font-size:12px; color:#475569; letter-spacing:0.04em; text-transform:uppercase;">Weekly Change</th>
  </tr>
  {''.join(rows)}
</table>
""".strip()


def news_cards(news: list[dict[str, Any]]) -> str:
    items = []
    for item in (news or [])[:10]:
        asset = escape_html(item.get("asset", ""))
        title = escape_html(item.get("title", ""))
        source = escape_html(item.get("source", ""))
        url = item.get("url", "")

        link_html = f"<a href='{escape_html(url)}' style='color:#0f172a; text-decoration:none;'>{title}</a>" if url else title

        items.append(
            f"""
<li style="margin:10px 0; padding:10px 12px; border:1px solid #e8edf6; border-radius:12px; background:#ffffff;">
  <div style="font-size:12px; color:#64748b; margin-bottom:4px; font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Arial,sans-serif;">
    <span style="font-weight:700; color:#0f172a;">{asset}</span>
    <span style="margin-left:8px;">{source}</span>
  </div>
  <div style="font-size:14px; color:#0f172a; font-weight:700; font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Arial,sans-serif;">
    {link_html}
  </div>
</li>
""".strip()
        )

    if not items:
        return "<p style='margin:0; color:#475569; font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Arial,sans-serif;'>No headlines returned.</p>"

    return f"<ul style='list-style:none; padding:0; margin:0;'>{''.join(items)}</ul>"


def errors_block(errors: list[str]) -> str:
    if not errors:
        return "<p style='margin:0; color:#475569; font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Arial,sans-serif;'>None</p>"

    combined = escape_html(safe_truncate("\n\n".join(errors), 12000))
    return f"""
<div style="border:1px solid #ffe4e6; background:#fff1f2; border-radius:14px; padding:12px;">
  <div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Arial,sans-serif; font-weight:700; color:#9f1239; font-size:13px; margin-bottom:8px;">
    Issues detected
  </div>
  <pre style="margin:0; white-space:pre-wrap; font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace; font-size:12px; color:#7f1d1d;">{combined}</pre>
</div>
""".strip()


def build_report(prices: dict[str, dict[str, Any]], news: list[dict[str, Any]], errors: list[str]) -> tuple[str, str, str]:
    ts_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    subject = f"Weekly Market Recap | {ts_utc}"

    text_body = "\n".join(
        [
            f"Weekly Market Recap ({ts_utc})",
            "",
            "Prices:",
            safe_truncate(json.dumps(prices, indent=2, ensure_ascii=False)),
            "",
            "News:",
            safe_truncate(json.dumps(news[:15], indent=2, ensure_ascii=False)),
            "",
            "Errors:",
            "None" if not errors else safe_truncate("\n\n".join(errors), 12000),
        ]
    )

    body = f"""
<div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Arial,sans-serif;">
  <div style="display:flex; gap:12px; flex-wrap:wrap; margin-bottom:16px;">
    <div style="flex:1; min-width:280px; background:#ffffff; border:1px solid #e8edf6; border-radius:16px; padding:14px;">
      <div style="font-size:12px; color:#64748b; letter-spacing:0.04em; text-transform:uppercase;">Coverage</div>
      <div style="margin-top:6px; font-size:14px; color:#0f172a; font-weight:700;">SPY and Silver (SLV proxy)</div>
      <div style="margin-top:6px; font-size:12px; color:#475569;">Weekly change plus key headlines</div>
    </div>
  </div>

  <div style="background:#ffffff; border:1px solid #e8edf6; border-radius:16px; padding:14px; margin-bottom:16px;">
    <div style="font-size:12px; color:#64748b; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:10px;">Prices</div>
    {prices_table(prices)}
  </div>

  <div style="background:#ffffff; border:1px solid #e8edf6; border-radius:16px; padding:14px; margin-bottom:16px;">
    <div style="font-size:12px; color:#64748b; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:10px;">Top Asset News</div>
    {news_cards(news)}
  </div>

  <div style="background:#ffffff; border:1px solid #e8edf6; border-radius:16px; padding:14px;">
    <div style="font-size:12px; color:#64748b; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:10px;">Errors and Warnings</div>
    {errors_block(errors)}
  </div>
</div>
""".strip()

    html_body = html_wrapper("Weekly Market Recap", f"Generated {ts_utc}", body)
    return subject, text_body, html_body


# ========= Fetchers =========

def fetch_prices(session: requests.Session) -> dict[str, dict[str, Any]]:
    av_key = (env_str("ALPHAVANTAGE_API_KEY") or "").strip()
    if not av_key:
        raise DataFetchError("Missing ALPHAVANTAGE_API_KEY")

    spy_latest, spy_week = av_weekly_equity_close(session, av_key, "SPY")
    slv_latest, slv_week = av_weekly_equity_close(session, av_key, "SLV")

    return {
        "SPY": {"latest": spy_latest, "week_change_pct": spy_week},
        "Silver (SLV proxy)": {"latest": slv_latest, "week_change_pct": slv_week},
    }


def fetch_news(session: requests.Session) -> list[dict[str, Any]]:
    api_key = (env_str("NEWSAPI_API_KEY") or "").strip()
    if not api_key:
        raise DataFetchError("Missing NEWSAPI_API_KEY")

    out: list[dict[str, Any]] = []

    # Tight queries: require key terms, exclude noise, keep it asset-specific.
    queries = [
        ("SPY", '("SPY" OR "SPDR S&P 500" OR "S&P 500 ETF") AND (ETF OR "S&P 500") -crypto -bitcoin -tesla'),
        ("Silver (SLV proxy)", '("SLV" OR "iShares Silver Trust" OR "silver ETF") AND (silver OR bullion OR metals) -gold -bitcoin -crypto'),
    ]

    for asset, q in queries:
        articles = newsapi_get(session, api_key, q, page_size=3)
        for a in articles:
            out.append(
                {
                    "asset": asset,
                    "title": a.get("title", ""),
                    "source": a.get("source", ""),
                    "url": a.get("url", ""),
                    "publishedAt": a.get("publishedAt", ""),
                }
            )

    out.sort(key=lambda x: str(x.get("publishedAt") or ""), reverse=True)
    return out[:6]


# ========= Main =========

def main() -> int:
    errors: list[str] = []
    prices: dict[str, dict[str, Any]] = {}
    news: list[dict[str, Any]] = []

    session = make_session()

    try:
        prices = fetch_prices(session)
    except Exception:
        errors.append("Pricing failed.\n" + traceback.format_exc())

    try:
        news = fetch_news(session)
    except Exception:
        errors.append("News failed.\n" + traceback.format_exc())

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
