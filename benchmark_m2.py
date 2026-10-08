"""Milestone 2 benchmark: unauthenticated public Instagram reel acquisition.

Three approaches per fixture, all unauthenticated, neutral desktop UA, no
cookies, timeouts <= 10s, 1.5s sleep between requests (polite client):

  a. oEmbed:  https://www.instagram.com/oembed?url=<encoded post URL>
  b. Embed page: https://www.instagram.com/reel/<shortcode>/embed/
  c. Direct page GET of the canonical URL, parsing Open Graph meta tags.

Writes benchmark_results.json. Runs network calls — NOT a unit test.
"""
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
TIMEOUT = 10
SLEEP_S = 1.5

OG_TAG_RE = re.compile(
    r'<meta\s+[^>]*property=["\'](og:[^"\']+)["\'][^>]*content=["\']([^"\']*)["\']',
    re.IGNORECASE,
)
OG_TAG_RE_ALT = re.compile(
    r'<meta\s+[^>]*content=["\']([^"\']*)["\'][^>]*property=["\'](og:[^"\']+)["\']',
    re.IGNORECASE,
)


def fetch(url):
    """Returns (status:int|'EXC', latency_s, body:str, note:str). Never raises."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    t0 = time.perf_counter()
    try:
        resp = urllib.request.urlopen(req, timeout=TIMEOUT)
        body = resp.read(1_500_000)  # cap at 1.5MB
        dt = time.perf_counter() - t0
        return resp.status, dt, body.decode("utf-8", "replace"), "ok"
    except urllib.error.HTTPError as e:
        dt = time.perf_counter() - t0
        try:
            body = e.read(100_000).decode("utf-8", "replace")
        except Exception:
            body = ""
        return e.code, dt, body, f"HTTPError {e.code}"
    except urllib.error.URLError as e:
        dt = time.perf_counter() - t0
        return "EXC", dt, "", f"URLError: {e.reason}"
    except Exception as e:  # timeouts, decode problems, etc.
        dt = time.perf_counter() - t0
        return "EXC", dt, "", f"{type(e).__name__}: {e}"


def parse_og(html):
    og = {}
    for prop, content in OG_TAG_RE.findall(html):
        og.setdefault(prop.lower(), content)
    for content, prop in OG_TAG_RE_ALT.findall(html):
        og.setdefault(prop.lower(), content)
    return og


def summarize(fields_present):
    keys = ["creator", "caption", "thumbnail", "publish_date", "media_url"]
    return {k: (k in fields_present) for k in keys}


def main():
    fixtures = json.load(open("benchmark_fixtures.json"))
    results = []
    for fx in fixtures:
        sc = fx["shortcode"]
        kind = fx["kind"]
        base = f"https://www.instagram.com/{'reel' if kind == 'reel' else 'p'}/{sc}/"

        # --- a. oEmbed ---
        oembed_url = "https://www.instagram.com/oembed?url=" + urllib.parse.quote(base, safe="")
        status, lat, body, note = fetch(oembed_url)
        rec = {"shortcode": sc, "kind": kind, "approach": "oembed", "status": status,
               "latency_s": round(lat, 2), "note": note}
        if status == 200:
            try:
                obj = json.loads(body)
                fields = []
                if obj.get("author_name"):
                    fields.append("creator")
                if obj.get("title"):
                    fields.append("caption")
                if obj.get("thumbnail_url"):
                    fields.append("thumbnail")
                rec["parsed"] = True
                rec["fields"] = summarize(fields)
                rec["media_url_obtainable"] = False  # oEmbed never returns a video URL
            except Exception as e:
                rec["parsed"] = False
                rec["parse_error"] = str(e)[:120]
        else:
            rec["parsed"] = False
        results.append(rec)
        time.sleep(SLEEP_S)

        # --- b. embed page ---
        emb_url = f"https://www.instagram.com/reel/{sc}/embed/"
        status, lat, body, note = fetch(emb_url)
        rec = {"shortcode": sc, "kind": kind, "approach": "embed", "status": status,
               "latency_s": round(lat, 2), "note": note}
        if status == 200:
            og = parse_og(body)
            video = og.get("og:video") or og.get("og:video:secure_url")
            fields = []
            if og.get("og:title"):
                fields.append("creator")
            if og.get("og:description"):
                fields.append("caption")
            if og.get("og:image") or og.get("og:image:secure_url"):
                fields.append("thumbnail")
            rec["parsed"] = True
            rec["fields"] = summarize(fields)
            rec["media_url_obtainable"] = bool(video)
            if video:
                rec["media_url_host"] = urllib.parse.urlparse(video).hostname
        else:
            rec["parsed"] = False
        results.append(rec)
        time.sleep(SLEEP_S)

        # --- c. direct page, OG tags ---
        status, lat, body, note = fetch(base)
        rec = {"shortcode": sc, "kind": kind, "approach": "direct_og", "status": status,
               "latency_s": round(lat, 2), "note": note}
        if status == 200:
            og = parse_og(body)
            loginish = "login" in body[:2000].lower() and not og.get("og:description")
            fields = []
            if og.get("og:title"):
                fields.append("creator")
            if og.get("og:description"):
                fields.append("caption")
            if og.get("og:image") or og.get("og:image:secure_url"):
                fields.append("thumbnail")
            video = og.get("og:video") or og.get("og:video:secure_url")
            rec["parsed"] = True
            rec["fields"] = summarize(fields)
            rec["media_url_obtainable"] = bool(video)
            if video:
                rec["media_url_host"] = urllib.parse.urlparse(video).hostname
            rec["looks_like_login_page"] = loginish
            rec["html_bytes"] = len(body)
        else:
            rec["parsed"] = False
        results.append(rec)
        time.sleep(SLEEP_S)
        print(f"done {sc} ({kind})", flush=True)

    json.dump(results, open("benchmark_results.json", "w"), indent=1)
    print(f"wrote {len(results)} results")


if __name__ == "__main__":
    main()
