#!/usr/bin/env python3
"""Copy the published Webflow site to a static folder for GitHub Pages.

- Fetches every page of SOURCE_URL and rewrites links to TARGET_URL.
- Removes the "Made in Webflow" badge.
- Points "Add to Apple Calendar" at webcal://<target>/events.ics.
- Builds events.ics from the events listed on the home page.
"""
import html as htmllib, os, re, sys, time, urllib.request, urllib.error, xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

SOURCE = os.environ.get("SOURCE_URL", "https://swiftevents-dev.webflow.io").rstrip("/")
TARGET = os.environ.get("TARGET_URL", "https://swiftevents.dev").rstrip("/")
OUT = os.environ.get("OUT_DIR", "site")
MIN_PAGES = int(os.environ.get("MIN_PAGES", "3"))

src_host = urlparse(SOURCE).netloc
tgt_host = urlparse(TARGET).netloc
WEBCAL = f"webcal://{tgt_host}/events.ics"


def fetch(url, tries=4):
    err = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "swift-events-mirror/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return 404, e.read()
            err = e
        except Exception as e:  # network hiccup: retry
            err = e
        time.sleep(2 * (i + 1))
    raise RuntimeError(f"Could not fetch {url}: {err}")


class Links(HTMLParser):
    def __init__(self):
        super().__init__(); self.hrefs = []
    def handle_starttag(self, tag, attrs):
        if tag == "a":
            for k, v in attrs:
                if k == "href" and v: self.hrefs.append(v)


def norm(path):
    path = urlparse(path).path or "/"
    return "/" + path.strip("/") if path != "/" else "/"


BADGE_CSS = "<style>.w-webflow-badge{display:none!important}</style>"


def remove_badge(html):
    # Webflow's script adds the "Made in Webflow" badge when <html> carries data-wf-status.
    html = re.sub(r'\sdata-wf-status="[^"]*"', "", html)
    # Backup in case the badge is added another way: hide it with CSS.
    if BADGE_CSS not in html:
        html = html.replace("</head>", BADGE_CSS + "</head>", 1)
    return html


def apple_calendar(html):
    # Whatever the Apple Calendar button points to in Webflow, make it the webcal subscription.
    return re.sub(r'(<a\b[^>]*?\bhref=")[^"]*("[^>]*>\s*Add to Apple Calendar\s*</a>)',
                  lambda m: m.group(1) + WEBCAL + m.group(2), html)


def rewrite(html):
    html = html.replace(SOURCE, TARGET).replace("http://" + src_host, TARGET)
    html = html.replace("//" + src_host, "//" + tgt_host).replace(src_host, tgt_host)
    return apple_calendar(remove_badge(html))


def save(path, body):
    # /about is saved as about.html (not about/index.html) so GitHub Pages serves /about without
    # redirecting to /about/, which would stop Webflow from marking the current page in the menu.
    rel = "index.html" if path == "/" else path.strip("/") + ".html"
    dest = os.path.join(OUT, rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w", encoding="utf-8") as f:
        f.write(rewrite(body))


# ---------- events.ics ----------

ROW = re.compile(r'<a\b[^>]*data-se="row"[^>]*>.*?</a>', re.S)


def field(row, name):
    m = re.search(r'data-se="%s"[^>]*>(.*?)</(?:div|span)>' % name, row, re.S)
    if not m:
        return ""
    text = re.sub(r"<[^>]+>", "", m.group(1))
    return re.sub(r"\s+", " ", htmllib.unescape(text)).strip()


def parse_date(s):
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            pass
    return None


def ics_text(s):
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line):
    # RFC 5545: lines longer than 75 octets continue on the next line, starting with a space.
    out, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > 75:
            out.append(cur.decode("utf-8")); cur = b" " + b
        else:
            cur += b
    out.append(cur.decode("utf-8"))
    return "\r\n".join(out)


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def build_calendar(home_html):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Swift Events//EN", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", "X-WR-CALNAME:Swift Events", "X-WR-TIMEZONE:Europe/Madrid",
             "REFRESH-INTERVAL;VALUE=DURATION:PT12H", "X-PUBLISHED-TTL:PT12H"]
    seen, count = set(), 0
    for m in ROW.finditer(home_html):
        row = m.group(0)
        start = parse_date(field(row, "start"))
        if not start:
            continue  # dates not announced yet
        end = parse_date(field(row, "end")) or start
        name = field(row, "name")
        href = re.search(r'href="([^"]*)"', row)
        city, country = field(row, "city"), field(row, "country")
        loc = ", ".join(x for x in dict.fromkeys([city, country]) if x)
        uid = f"{slug(name)}-{start.year}@{tgt_host}"  # stable while the name and year stay the same
        if uid in seen:
            continue
        seen.add(uid)
        lines += ["BEGIN:VEVENT", f"UID:{uid}", "SEQUENCE:0", f"DTSTAMP:{stamp}",
                  f"SUMMARY:{ics_text(name)}"]
        desc = field(row, "desc")
        if desc:
            lines.append(f"DESCRIPTION:{ics_text(desc)}")
        if href and href.group(1).startswith("http"):
            lines.append(f"URL:{htmllib.unescape(href.group(1))}")
        if loc:
            lines.append(f"LOCATION:{ics_text(loc)}")
        lines += [f"DTSTART;VALUE=DATE:{start:%Y%m%d}", f"DTEND;VALUE=DATE:{end + timedelta(days=1):%Y%m%d}",
                  "END:VEVENT"]
        count += 1
    lines.append("END:VCALENDAR")
    with open(os.path.join(OUT, "events.ics"), "w", encoding="utf-8", newline="") as f:
        f.write("\r\n".join(fold(l) for l in lines) + "\r\n")
    return count


def main():
    os.makedirs(OUT, exist_ok=True)
    queue, seen, pages = ["/"], set(), []
    home = ""
    status, sm = fetch(SOURCE + "/sitemap.xml")
    if status == 200:
        try:
            for loc in ET.fromstring(sm).iter("{http://www.sitemaps.org/schemas/sitemap/0.9}loc"):
                queue.append(norm(loc.text.strip()))
        except ET.ParseError:
            pass
    while queue:
        path = queue.pop(0)
        if path in seen:
            continue
        seen.add(path)
        status, body = fetch(SOURCE + path)
        if status != 200:
            print(f"skip {path} ({status})"); continue
        html = body.decode("utf-8", "replace")
        if path == "/":
            home = html
        save(path, html); pages.append(path)
        p = Links(); p.feed(html)
        for href in p.hrefs:
            u = urlparse(urljoin(SOURCE + path, href))
            if u.netloc in ("", src_host) and u.scheme in ("http", "https", ""):
                if not re.search(r"\.[a-z0-9]{2,5}$", u.path, re.I):
                    queue.append(norm(u.path))
        print(f"saved {path}")
    # 404 page: Webflow serves its 404 page for any missing path
    status, body = fetch(SOURCE + "/this-page-does-not-exist-404")
    with open(os.path.join(OUT, "404.html"), "w", encoding="utf-8") as f:
        f.write(rewrite(body.decode("utf-8", "replace")))
    with open(os.path.join(OUT, "CNAME"), "w") as f:
        f.write(tgt_host + "\n")
    open(os.path.join(OUT, ".nojekyll"), "w").close()
    with open(os.path.join(OUT, "robots.txt"), "w") as f:
        f.write(f"User-agent: *\nAllow: /\n\nSitemap: {TARGET}/sitemap.xml\n")
    with open(os.path.join(OUT, "sitemap.xml"), "w") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n')
        for p in sorted(pages):
            f.write(f"  <url><loc>{TARGET}{'' if p == '/' else p}</loc></url>\n")
        f.write("</urlset>\n")
    events = build_calendar(home)
    print(f"Done: {len(pages)} pages, {events} events in events.ics")
    if len(pages) < MIN_PAGES:
        sys.exit("Too few pages copied, something is wrong. Not deploying.")
    if events < 5:
        sys.exit("Too few events found for events.ics, something is wrong. Not deploying.")


if __name__ == "__main__":
    main()
