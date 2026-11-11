import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import re

MAX_FEED_BYTES = 2_000_000


def clean_text(text):
    if not text: return ""
    # Strip messy HTML tags often embedded in RSS descriptions
    text = re.sub(r'<[^>]+>', '', text)
    # Clean up whitespace
    return " ".join(text.split())

def fetch_rss(url, limit=5):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        print("RSS Fetch Error: URL must use http or https")
        return
    try:
        limit = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        print("RSS Fetch Error: limit must be a number")
        return
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as response:
            body = response.read(MAX_FEED_BYTES + 1)
        if len(body) > MAX_FEED_BYTES:
            print(f"RSS Fetch Error: feed exceeds {MAX_FEED_BYTES} bytes")
            return
        root = ET.fromstring(body)
        items = [node for node in root.iter()
                 if node.tag.rsplit("}", 1)[-1] in ("item", "entry")]
        for i, item in enumerate(items[:limit], 1):
            fields = {child.tag.rsplit("}", 1)[-1]: "".join(child.itertext())
                      for child in item}
            title = fields.get("title", "")
            desc = fields.get("description") or fields.get("summary") or fields.get("encoded")
            print(f"{i}. {clean_text(title)}")
            if desc:
                print(f"   {clean_text(desc)[:250]}...\n")
    except (OSError, urllib.error.URLError, ET.ParseError, ValueError) as exc:
        print(f"RSS Fetch Error: {exc}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python rss.py <url> [limit]")
        sys.exit(1)
    fetch_rss(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 5)
