"""
Quick CLI helper to create a new tracking pixel.

Usage:
    python generate_pixel.py "Follow-up to Edsteps" https://your-app.onrender.com
"""

import sys
import urllib.request
import urllib.parse
import json

def main():
    if len(sys.argv) < 3:
        print('Usage: python generate_pixel.py "<label>" <base_url>')
        print('Example: python generate_pixel.py "Edsteps outreach" https://your-app.onrender.com')
        sys.exit(1)

    label = sys.argv[1]
    base_url = sys.argv[2].rstrip("/")
    query = urllib.parse.urlencode({"label": label})
    url = f"{base_url}/new?{query}"

    with urllib.request.urlopen(url) as resp:
        data = json.loads(resp.read())

    print("\nTracking ID:", data["tracking_id"])
    print("Pixel URL:  ", data["pixel_url"])
    print("\nPaste this into your email's HTML body (pixel only):\n")
    print(data["html_snippet"])
    if "disclosure_html" in data:
        print("\nOr combined snippet with transparency disclosure:\n")
        print(data["combined_snippet"])
    print(f"\nCheck status anytime at: {base_url}/dashboard")

if __name__ == "__main__":
    main()
