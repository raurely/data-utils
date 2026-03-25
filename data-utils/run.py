"""
Scheduled data task
==========================================
Fetches remote data and sends
a notification when changes are detected.

State is persisted via known_products.json committed back to the repo
after each run by the workflow.
"""

import requests
import smtplib
import json
import os
import sys
import logging
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from bs4 import BeautifulSoup

# ─────────────────────────────────────────────
#  CONFIG — all values come from GitHub Secrets
#  (never hard-coded here)
# ─────────────────────────────────────────────

GMAIL_ADDRESS      = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
# Primary email recipient
ALERT_TO_EMAIL     = os.environ.get("ALERT_TO_EMAIL", GMAIL_ADDRESS)

# Additional email recipients — comma-separated in the ALERT_TO_EMAILS secret
# e.g. "colleague@gmail.com, spouse@gmail.com"
ALERT_TO_EMAILS_RAW = os.environ.get("ALERT_TO_EMAILS", "")
ALERT_TO_EMAILS     = [e.strip() for e in ALERT_TO_EMAILS_RAW.split(",") if e.strip()]

# All recipients combined (primary + additional)
ALL_EMAIL_RECIPIENTS = list(dict.fromkeys([ALERT_TO_EMAIL] + ALERT_TO_EMAILS))

# Google Voice SMS gateway — set via ALERT_TO_SMS secret
ALERT_TO_SMS       = os.environ.get("ALERT_TO_SMS", "")

# Optional comma-separated watchlist, e.g. "blanton,weller,eagle rare"
# Leave empty (or don't set the secret) to alert on ANY new product.
WATCH_LIST_RAW     = os.environ.get("WATCH_LIST", "")
WATCH_LIST         = [w.strip().lower() for w in WATCH_LIST_RAW.split(",") if w.strip()]

STATE_FILE         = "data/known_products.json"

PAGES = [
    {
        "name": "Whiskey Release",
        "url":  "https://www.finewineandgoodspirits.com/whiskey-release/whiskey-release",
    },
]

# ─────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  STATE
# ─────────────────────────────────────────────

def load_known() -> set:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(data.get("products", []))
    return set()

def save_known(products: set, new_count: int):
    os.makedirs("data", exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "products": sorted(list(products)),
            "last_updated": datetime.utcnow().isoformat() + "Z",
            "total_tracked": len(products),
        }, f, indent=2)
    log.info(f"State saved: {len(products)} products tracked.")

# ─────────────────────────────────────────────
#  SCRAPING
# ─────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

BOURBON_KEYWORDS = [
    "blanton", "weller", "buffalo trace", "eagle rare", "stagg", "e.h. taylor",
    "elmer t. lee", "van winkle", "pappy", "four roses", "wild turkey",
    "woodford", "knob creek", "maker's mark", "old forester", "baker's",
    "booker's", "elijah craig", "heaven hill", "larceny", "russell",
    "benchmark", "whiskey", "bourbon", "rye whiskey", "scotch", "willett",
    "rowan's creek", "michter", "angel's envy", "rabbit hole",
]

def fetch_page(url: str) -> str | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=25)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as e:
        log.error(f"Fetch failed for {url}: {e}")
        return None

def parse_products(html: str, source_name: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    products = []

    # Remove nav, footer, script, style noise
    for tag in soup.select("nav, footer, script, style, header"):
        tag.decompose()

    # Try structured product selectors
    selectors = [
        ".product-tile", ".product-item", "[data-sku]",
        ".product-name", ".product__name", "h3.name",
        ".product-listing__item", "li.product",
        ".product-grid__item", ".product-card",
        "[class*='product']",
    ]

    for sel in selectors:
        els = soup.select(sel)
        if len(els) >= 2:  # need at least 2 to trust the selector
            for el in els:
                name = " ".join(el.get_text(separator=" ", strip=True).split())
                if 4 < len(name) < 250:
                    price_el = el.select_one(".price, .product-price, [class*='price']")
                    price = price_el.get_text(strip=True) if price_el else ""
                    link_el = el.select_one("a[href]")
                    link = link_el["href"] if link_el else ""
                    if link and not link.startswith("http"):
                        link = "https://www.finewineandgoodspirits.com" + link
                    products.append({"name": name, "price": price, "link": link, "source": source_name})
            log.info(f"  Parsed {len(products)} products via selector '{sel}'")
            return products

    # Fallback: keyword scan
    log.info("  No structured elements found — running keyword scan.")
    seen = set()
    for line in soup.get_text(separator="\n").splitlines():
        line = " ".join(line.strip().split())
        if not line or len(line) > 200 or len(line) < 5:
            continue
        if any(kw in line.lower() for kw in BOURBON_KEYWORDS):
            if line not in seen:
                seen.add(line)
                products.append({"name": line, "price": "", "link": "", "source": source_name})

    log.info(f"  Keyword scan: {len(products)} candidate lines.")
    return products

def matches_watchlist(name: str) -> bool:
    if not WATCH_LIST:
        return True
    lower = name.lower()
    return any(kw in lower for kw in WATCH_LIST)

# ─────────────────────────────────────────────
#  EMAIL
# ─────────────────────────────────────────────

def send_alert(new_products: list[dict]):
    count = len(new_products)
    subject = f"🥃 PA Bourbon Drop — {count} new release{'s' if count > 1 else ''} on FWGS"

    plain_lines = [
        f"PA Bourbon Watch detected {count} new product(s) on the FWGS Whiskey Release page.",
        "",
    ]
    for p in new_products:
        plain_lines.append(f"  • {p['name']}")
        if p.get("price"):
            plain_lines.append(f"    Price: {p['price']}")
        if p.get("link"):
            plain_lines.append(f"    {p['link']}")
        plain_lines.append("")
    plain_lines += [
        "─" * 48,
        "Go buy it before it's gone:",
        "https://www.finewineandgoodspirits.com/whiskey-release/whiskey-release",
        "",
        f"Sent: {datetime.now().strftime('%A %B %d, %Y at %I:%M %p')}",
        "— PA Bourbon Watch",
    ]

    product_rows = ""
    for p in new_products:
        name_html = (
            f'<a href="{p["link"]}" style="color:#c8860a;text-decoration:none;">{p["name"]}</a>'
            if p.get("link") else p["name"]
        )
        product_rows += f"""
        <tr>
          <td style="padding:11px 14px;border-bottom:1px solid #2a2520;font-size:14px;color:#e8dcc8;line-height:1.4;">{name_html}</td>
          <td style="padding:11px 14px;border-bottom:1px solid #2a2520;font-size:14px;color:#f5a623;white-space:nowrap;">{p.get('price','—')}</td>
        </tr>"""

    timestamp = datetime.now().strftime("%B %d, %Y · %I:%M %p")
    html_body = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#0d0b08;font-family:'Courier New',monospace;">
  <div style="max-width:540px;margin:32px auto;background:#141210;border:1px solid #2a2520;border-radius:4px;overflow:hidden;">

    <div style="background:#1a1200;border-bottom:3px solid #c8860a;padding:24px 28px;">
      <p style="margin:0 0 6px;font-size:10px;letter-spacing:3px;text-transform:uppercase;color:#7a5006;">
        Fine Wine &amp; Good Spirits · Whiskey Release
      </p>
      <h1 style="margin:0;font-size:24px;color:#f5a623;letter-spacing:-0.5px;">🥃 Bourbon Drop Detected</h1>
    </div>

    <div style="padding:24px 28px 8px;">
      <p style="color:#9a8e7a;font-size:13px;margin:0 0 18px;line-height:1.6;">
        {count} new product{'s' if count > 1 else ''} just appeared on the
        <a href="https://www.finewineandgoodspirits.com/whiskey-release/whiskey-release"
           style="color:#c8860a;">FWGS Whiskey Release page</a>.
        Move fast.
      </p>

      <table style="width:100%;border-collapse:collapse;border:1px solid #2a2520;border-radius:3px;overflow:hidden;">
        <thead>
          <tr style="background:#1a1200;">
            <th style="padding:8px 14px;font-size:9px;letter-spacing:2px;text-transform:uppercase;color:#7a5006;text-align:left;">Product</th>
            <th style="padding:8px 14px;font-size:9px;letter-spacing:2px;text-transform:uppercase;color:#7a5006;text-align:left;">Price</th>
          </tr>
        </thead>
        <tbody>{product_rows}</tbody>
      </table>

      <div style="margin:22px 0;">
        <a href="https://www.finewineandgoodspirits.com/whiskey-release/whiskey-release"
           style="display:inline-block;background:#c8860a;color:#0d0b08;text-decoration:none;
                  font-size:10px;letter-spacing:2px;text-transform:uppercase;font-weight:bold;
                  padding:13px 30px;border-radius:2px;">
          View Release Page →
        </a>
      </div>
    </div>

    <div style="padding:14px 28px;border-top:1px solid #2a2520;font-size:10px;color:#4a4540;letter-spacing:1px;">
      PA BOURBON WATCH · {timestamp}
    </div>
  </div>
</body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_ADDRESS
    msg["To"]      = ALERT_TO_EMAIL
    msg.attach(MIMEText("\n".join(plain_lines), "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD.replace(" ", ""))

            # Send full HTML email to all recipients
            for recipient in ALL_EMAIL_RECIPIENTS:
                msg.replace_header("To", recipient)
                server.sendmail(GMAIL_ADDRESS, recipient, msg.as_string())
                log.info(f"✓ Email alert sent to {recipient}")

            # Send short plain-text SMS via Google Voice gateway
            if ALERT_TO_SMS:
                sms_lines = ["🥃 FWGS Drop:"]
                for p in new_products[:3]:
                    sms_lines.append(f"• {p['name'][:50]}")
                if len(new_products) > 3:
                    sms_lines.append(f"...and {len(new_products) - 3} more")
                sms_lines.append("finewineandgoodspirits.com/whiskey-release/whiskey-release")

                sms_msg = MIMEText("\n".join(sms_lines), "plain")
                sms_msg["Subject"] = "FWGS Drop"
                sms_msg["From"]    = GMAIL_ADDRESS
                sms_msg["To"]      = ALERT_TO_SMS
                server.sendmail(GMAIL_ADDRESS, ALERT_TO_SMS, sms_msg.as_string())
                log.info(f"✓ SMS alert sent to {ALERT_TO_SMS}")

    except smtplib.SMTPAuthenticationError:
        log.error("Gmail auth failed — check GMAIL_APP_PASSWORD secret.")
        sys.exit(1)
    except Exception as e:
        log.error(f"Email send failed: {e}")
        sys.exit(1)

# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────

def main():
    log.info("=" * 50)
    log.info("Task starting")
    log.info(f"Destination: {ALERT_TO_EMAIL or "(not configured)"}")
    log.info(f"Watchlist: {', '.join(WATCH_LIST) if WATCH_LIST else 'ALL products'}")
    log.info("=" * 50)

    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        log.error("GMAIL_ADDRESS or GMAIL_APP_PASSWORD secrets not set. Exiting.")
        sys.exit(1)

    known = load_known()
    first_run = len(known) == 0
    log.info(f"Known products loaded: {len(known)}")

    all_new = []

    for page in PAGES:
        log.info(f"Fetching: {page['name']}")
        html = fetch_page(page["url"])
        if not html:
            log.warning(f"  Skipping {page['name']} — fetch failed.")
            continue

        products = parse_products(html, page["name"])
        log.info(f"  {len(products)} products found on page.")

        for p in products:
            key = p["name"]
            if key not in known:
                if not first_run and matches_watchlist(key):
                    all_new.append(p)
                    log.info(f"  NEW: {key[:80]}")
                known.add(key)

    save_known(known, len(all_new))

    if first_run:
        log.info(f"First run complete. Baseline of {len(known)} products saved. No alert sent.")
    elif all_new:
        log.info(f"Sending alert for {len(all_new)} new product(s).")
        send_alert(all_new)
    else:
        log.info("No new products detected. Nothing to send.")

    log.info("Done.")

if __name__ == "__main__":
    main()
