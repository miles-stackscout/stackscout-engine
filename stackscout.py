import os, sqlite3, csv, base64, time, re
from datetime import date, datetime
from dotenv import load_dotenv
from apify_client import ApifyClient
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, Attachment, FileContent, FileName, FileType, Disposition

load_dotenv()
APIFY_TOKEN = os.getenv("APIFY_TOKEN")
SENDGRID_KEY = os.getenv("SENDGRID_API_KEY")
FROM_EMAIL = "leads@stackscout.org"
DB_PATH = "stackscout.db"

def scrape_shopify_stores():
    client = ApifyClient(APIFY_TOKEN)
    all_domains = []

    queries = [
        '"powered by shopify" site:myshopify.com',
        '"powered by shopify" -site:shopify.com'
    ]
    for query in queries:
        try:
            run = client.actor("apify/google-search-scraper").call(
                run_input={
                    "queries": query,
                    "resultsPerPage": 50,
                    "maxPagesPerQuery": 1,
                    "languageCode": "en",
                    "mobileResults": False,
                },
                wait_secs=30
            )
            items = client.dataset(run["defaultDatasetId"]).list_items().items
            for item in items:
                # Each item contains an "organicResults" array
                for result in item.get("organicResults", []):
                    url = result.get("url", "")
                    if url:
                        domain = re.sub(r'^https?://(?:www\.)?', '', url).split('/')[0]
                        if domain and '.' in domain and len(domain) > 6:
                            if not any(x in domain.lower() for x in ['google.com','facebook.com','youtube.com']):
                                all_domains.append(domain)
        except Exception as e:
            print(f"Search error: {e}")
    return list(set(all_domains))

def enrich_store(domain):
    """Visit store and extract title, email, social links."""
    import requests
    from bs4 import BeautifulSoup
    
    data = {"store_title": "", "contact_email": "", "socials": ""}
    url = f"https://{domain}"
    try:
        resp = requests.get(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }, timeout=10)
        if resp.status_code != 200:
            return data
        soup = BeautifulSoup(resp.text, "html.parser")
        # Title
        title_tag = soup.find("title")
        if title_tag:
            title = title_tag.get_text().strip()
            title = re.sub(r'\s*[–—-]\s*Shopify\s*$', '', title)
            title = re.sub(r'\s*[–—-]\s*Powered by Shopify\s*$', '', title)
            data["store_title"] = title[:120]
        # Email
        emails = re.findall(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", resp.text)
        real_emails = [e for e in emails if not any(x in e.lower() for x in
            ["noreply","no-reply","example","test@","support@","info@","privacy@","admin@"])]
        if real_emails:
            data["contact_email"] = real_emails[0]
        # Social links
        socials = set()
        social_domains = ["instagram.com","facebook.com","twitter.com","x.com",
                          "linkedin.com","tiktok.com","pinterest.com","youtube.com"]
        for a in soup.find_all("a", href=True):
            href = a["href"].lower()
            for sd in social_domains:
                if sd in href and href not in socials:
                    socials.add(a["href"])
        data["socials"] = ", ".join(sorted(socials)[:5])
    except Exception as e:
        print(f"  Enrich error {domain}: {e}")
    return data

def dedupe_and_save(domains):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS seen (
        domain TEXT PRIMARY KEY, first_seen TEXT, last_seen TEXT,
        source_app TEXT, store_title TEXT, contact_email TEXT, socials TEXT)""")
    new = []
    now = datetime.now().isoformat()
    for domain in domains:
        c.execute("SELECT domain FROM seen WHERE domain=?", (domain,))
        if c.fetchone() is None:
            enrich = enrich_store(domain)
            c.execute("INSERT INTO seen (domain,first_seen,last_seen,source_app,store_title,contact_email,socials) VALUES (?,?,?,?,?,?,?)",
                      (domain, now, now, "Google Search", enrich["store_title"], enrich["contact_email"], enrich["socials"]))
            if c.rowcount > 0:
                new.append({"domain": domain, **enrich})
        else:
            c.execute("UPDATE seen SET last_seen=? WHERE domain=?", (now, domain))
    conn.commit()
    conn.close()
    return new

def generate_csv(entries):
    filename = f"leads_{date.today().isoformat()}.csv"
    with open(filename, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["domain","date_found","source_app","store_title","contact_email","social_links"])
        for e in entries:
            writer.writerow([e["domain"], date.today().isoformat(), "Google Search",
                             e.get("store_title",""), e.get("contact_email",""), e.get("socials","")])
    return filename

def send_csv_via_sendgrid(csv_path, recipient):
    if not SENDGRID_KEY:
        print("SendGrid key not set")
        return
    import pandas as pd
    try:
        df = pd.read_csv(csv_path)
        lead_count = len(df)
    except Exception as e:
        print(f"CSV error: {e}")
        return
    table_html = '<table style="width:100%;border-collapse:collapse;font-size:13px;font-family:Arial,Helvetica,sans-serif;">'
    table_html += '<tr style="background:#0d1117;color:#58a6ff;">'
    for col in df.columns:
        table_html += f'<th style="padding:10px 8px;text-align:left;font-weight:600;">{col}</th>'
    table_html += '</tr>'
    for i, (_, row) in enumerate(df.iterrows()):
        bg = '#f8fafc' if i % 2 == 0 else '#ffffff'
        table_html += f'<tr style="background:{bg};">'
        for val in row:
            display = str(val) if pd.notna(val) else ''
            table_html += f'<td style="padding:8px;border-bottom:1px solid #e2e8f0;color:#1e293b;">{display}</td>'
        table_html += '</tr>'
    table_html += '</table>'

    today_str = date.today().strftime('%A, %B %d')
    html = f'''<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f4f6f9;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f6f9;">
<tr><td align="center" style="padding:40px 20px;">
<table width="650" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:12px;overflow:hidden;">
<tr><td style="background:#0d1117;padding:30px 30px 20px 30px;text-align:center;">
<h1 style="margin:0;color:#58a6ff;font-size:28px;">⚡ StackScout</h1>
<p style="margin:8px 0 0 0;color:#8b949e;font-size:14px;">Daily Lead Report &middot; {today_str}</p>
</td></tr>
<tr><td style="padding:30px 30px 16px 30px;">
<p style="margin:0 0 16px 0;font-size:16px;color:#1e293b;">Good morning,</p>
<p style="margin:0 0 24px 0;font-size:15px;color:#475569;">Here are <strong>{lead_count} new Shopify stores</strong> detected this morning.</p>
</td></tr>
<tr><td style="padding:0 30px 24px 30px;">{table_html}</td></tr>
<tr><td style="padding:0 30px 30px 30px;text-align:center;">
<div style="display:inline-block;background:#e8f0fe;border:1px solid #58a6ff;border-radius:8px;padding:16px 24px;">
<p style="margin:0 0 4px 0;font-size:15px;color:#0d1117;font-weight:600;">📎 Attachment: {os.path.basename(csv_path)}</p>
<p style="margin:0;font-size:13px;color:#475569;">Open the attachment to view the full CSV.</p>
</div>
</td></tr>
<tr><td style="background:#f8fafc;padding:20px 30px;text-align:center;">
<p style="margin:0;font-size:11px;color:#94a3b8;">You're receiving this because you subscribed to StackScout.<br>Reply with 'unsubscribe' to stop.</p>
</td></tr>
</table>
</td></tr>
</table>
</body>
</html>'''

    with open(csv_path, "rb") as f:
        data = f.read()
    attachment = Attachment()
    attachment.file_content = FileContent(base64.b64encode(data).decode())
    attachment.file_type = FileType("text/csv")
    attachment.file_name = FileName(os.path.basename(csv_path))
    attachment.disposition = Disposition("attachment")
    message = Mail(
        from_email=FROM_EMAIL,
        to_emails=recipient,
        subject=f"🔥 {lead_count} new Shopify stores — {today_str}",
        html_content=html
    )
    message.attachment = attachment
    try:
        sg = SendGridAPIClient(SENDGRID_KEY)
        sg.send(message)
        return True
    except Exception as e:
        print(f"SendGrid error: {e}")
        return False

if __name__ == "__main__":
    print("Searching Google for new Shopify stores (via Apify)...")
    domains = scrape_shopify_stores()
    print(f"  Found {len(domains)} unique domains")
    new = dedupe_and_save(domains)
    print(f"  {len(new)} new stores saved after enrichment")
    if new:
        csv_file = generate_csv(new)
        print(f"  CSV saved: {csv_file}")
        conn = sqlite3.connect(DB_PATH)
        subs = [r[0] for r in conn.execute("SELECT email FROM subscribers").fetchall()]
        conn.close()
        for email in subs:
            if send_csv_via_sendgrid(csv_file, email):
                print(f"  Emailed to {email}")
            else:
                print(f"  Failed to email {email}")
    else:
        print("  No new stores today.")
