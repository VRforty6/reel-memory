# Milestone 2 — Public Instagram Acquisition Benchmark

**Date:** 2026-09-22  |  **Fixtures:** 24 real public reel URLs from `/home/hatch/workspace/reel-library/seed.json` (18 `/reel/`, 6 `/p/`, spread across distinct creators)

## Method

Three unauthenticated approaches were attempted per fixture (72 requests total), with a 1.5s sleep between requests (polite client):

1. **oEmbed** — `GET https://www.instagram.com/oembed?url=<encoded post URL>`, expecting JSON (`author_name`, `title`, `thumbnail_url`).
2. **Embed page** — `GET https://www.instagram.com/reel/<shortcode>/embed/`, parsing Open Graph `<meta property="og:*">` tags; success would also surface a direct `og:video` URL.
3. **Direct page** — `GET` of the canonical post URL, parsing Open Graph tags (`og:title`, `og:description`, `og:image`, `og:video`, `og:video:width`, …).

**No-auth policy (hard constraints, never violated):** neutral desktop User-Agent, no cookies, no login, no credential use, no browser-session reuse, no private-content bypass. All requests targeted `instagram.com` hosts only (SEC-001). Response bodies were treated as hostile/untrusted data (parsed with regexes only, byte-capped). Timeout ≤ 10s per socket operation.

**Recorded per request:** approach, HTTP status, `time_total` latency, whether the response parsed (JSON/HTML), which metadata fields were present (creator handle, caption/description, thumbnail, publish date), whether a direct video (media-bytes) URL was obtainable, and any rate-limit signals (429).

Raw machine-readable results: `benchmark_results.json` (24 fixtures × 3 approaches = 72 rows). Reproduction script: `benchmark_m2.py`.

## Per-fixture results

| shortcode | kind | approach | status | latency_s | parsed | fields_avail | media_url_obtainable | note |
|---|---|---|---|---|---|---|---|---|
| DdfevZCMhco | reel | oembed | 200 | 2.77 | False | - | None | ok |
| DdfevZCMhco | reel | embed | 200 | 1.71 | True | - | False | ok |
| DdfevZCMhco | reel | direct_og | 200 | 1.67 | True | - | False | ok |
| DdUTRqBg1LG | reel | oembed | 200 | 1.99 | False | - | None | ok |
| DdUTRqBg1LG | reel | embed | 200 | 1.95 | True | - | False | ok |
| DdUTRqBg1LG | reel | direct_og | 200 | 1.95 | True | - | False | ok |
| DdCO_ZNzmuA | reel | oembed | 200 | 2.38 | False | - | None | ok |
| DdCO_ZNzmuA | reel | embed | 200 | 1.93 | True | - | False | ok |
| DdCO_ZNzmuA | reel | direct_og | 200 | 2.03 | True | - | False | ok |
| Dct7DUBzaoi | reel | oembed | 200 | 1.78 | False | - | None | ok |
| Dct7DUBzaoi | reel | embed | 200 | 1.81 | True | - | False | ok |
| Dct7DUBzaoi | reel | direct_og | 200 | 1.78 | True | - | False | ok |
| DcOGDRgkhj6 | p | oembed | 200 | 1.52 | False | - | None | ok |
| DcOGDRgkhj6 | p | embed | 200 | 1.99 | True | - | False | ok |
| DcOGDRgkhj6 | p | direct_og | 200 | 1.85 | True | - | False | ok |
| Db8hZMdIu_N | reel | oembed | 200 | 1.54 | False | - | None | ok |
| Db8hZMdIu_N | reel | embed | 200 | 1.98 | True | - | False | ok |
| Db8hZMdIu_N | reel | direct_og | 200 | 1.44 | True | - | False | ok |
| DX2Q4XBPlCW | reel | oembed | 200 | 1.52 | False | - | None | ok |
| DX2Q4XBPlCW | reel | embed | 200 | 1.52 | True | - | False | ok |
| DX2Q4XBPlCW | reel | direct_og | 200 | 1.39 | True | - | False | ok |
| DY7RPo5JnKc | reel | oembed | 200 | 1.88 | False | - | None | ok |
| DY7RPo5JnKc | reel | embed | 200 | 1.33 | True | - | False | ok |
| DY7RPo5JnKc | reel | direct_og | 200 | 1.69 | True | - | False | ok |
| DajCWEUjktT | p | oembed | 200 | 1.59 | False | - | None | ok |
| DajCWEUjktT | p | embed | EXC | 62.44 | False | - | None | TimeoutError: The read operation timed out |
| DajCWEUjktT | p | direct_og | 200 | 1.33 | True | - | False | ok |
| DaNK3kNk5RT | p | oembed | 200 | 1.23 | False | - | None | ok |
| DaNK3kNk5RT | p | embed | 200 | 1.48 | True | - | False | ok |
| DaNK3kNk5RT | p | direct_og | 200 | 1.15 | True | - | False | ok |
| DZxdUJMEikZ | p | oembed | 200 | 1.54 | False | - | None | ok |
| DZxdUJMEikZ | p | embed | 200 | 1.1 | True | - | False | ok |
| DZxdUJMEikZ | p | direct_og | 200 | 1.3 | True | - | False | ok |
| DY7ZSYQAGUM | reel | oembed | 200 | 1.23 | False | - | None | ok |
| DY7ZSYQAGUM | reel | embed | 200 | 1.13 | True | - | False | ok |
| DY7ZSYQAGUM | reel | direct_og | 200 | 1.14 | True | - | False | ok |
| DYWh6WmkcRU | p | oembed | 200 | 1.25 | False | - | None | ok |
| DYWh6WmkcRU | p | embed | 200 | 1.1 | True | - | False | ok |
| DYWh6WmkcRU | p | direct_og | 200 | 1.07 | True | - | False | ok |
| DVihrocAY1f | reel | oembed | 200 | 2.09 | False | - | None | ok |
| DVihrocAY1f | reel | embed | 200 | 2.15 | True | - | False | ok |
| DVihrocAY1f | reel | direct_og | 200 | 1.75 | True | - | False | ok |
| DWYnOwcCKHe | reel | oembed | 200 | 1.35 | False | - | None | ok |
| DWYnOwcCKHe | reel | embed | 200 | 1.74 | True | - | False | ok |
| DWYnOwcCKHe | reel | direct_og | 200 | 1.99 | True | - | False | ok |
| DXKDEZxTPDz | reel | oembed | 200 | 1.96 | False | - | None | ok |
| DXKDEZxTPDz | reel | embed | 200 | 1.57 | True | - | False | ok |
| DXKDEZxTPDz | reel | direct_og | 200 | 1.94 | True | - | False | ok |
| DQLjZ2zkffU | p | oembed | 200 | 1.5 | False | - | None | ok |
| DQLjZ2zkffU | p | embed | 200 | 1.97 | True | - | False | ok |
| DQLjZ2zkffU | p | direct_og | 200 | 1.73 | True | - | False | ok |
| DMkuaIaS8dL | reel | oembed | 200 | 2.81 | False | - | None | ok |
| DMkuaIaS8dL | reel | embed | 200 | 2.03 | True | - | False | ok |
| DMkuaIaS8dL | reel | direct_og | 200 | 1.48 | True | - | False | ok |
| DNw0jEg5t_V | reel | oembed | 200 | 1.8 | False | - | None | ok |
| DNw0jEg5t_V | reel | embed | 200 | 1.74 | True | - | False | ok |
| DNw0jEg5t_V | reel | direct_og | 200 | 2.2 | True | - | False | ok |
| DNW60K6x8cY | reel | oembed | 200 | 1.2 | False | - | None | ok |
| DNW60K6x8cY | reel | embed | 200 | 1.09 | True | - | False | ok |
| DNW60K6x8cY | reel | direct_og | 200 | 1.09 | True | - | False | ok |
| DM2XEWRJqgR | reel | oembed | 200 | 0.83 | False | - | None | ok |
| DM2XEWRJqgR | reel | embed | 200 | 1.07 | True | - | False | ok |
| DM2XEWRJqgR | reel | direct_og | 200 | 0.98 | True | - | False | ok |
| DMPcYRCvKwa | reel | oembed | 200 | 1.11 | False | - | None | ok |
| DMPcYRCvKwa | reel | embed | 200 | 1.11 | True | - | False | ok |
| DMPcYRCvKwa | reel | direct_og | 200 | 1.09 | True | - | False | ok |
| DJTsc3UStRc | reel | oembed | 200 | 0.82 | False | - | None | ok |
| DJTsc3UStRc | reel | embed | 200 | 0.96 | True | - | False | ok |
| DJTsc3UStRc | reel | direct_og | 200 | 0.92 | True | - | False | ok |
| DSxHRznkuhl | reel | oembed | 200 | 0.92 | False | - | None | ok |
| DSxHRznkuhl | reel | embed | 200 | 0.81 | True | - | False | ok |
| DSxHRznkuhl | reel | direct_og | 200 | 1.3 | True | - | False | ok |

## Summary statistics

| approach | requests | HTTP 200 | parsed | creator | caption | thumbnail | publish_date | media URL obtainable |
|---|---|---|---|---|---|---|---|---|
| oembed | 24 | 24 | 0 | 0 | 0 | 0 | 0 | 0 |
| embed | 24 | 23 | 23 | 0 | 0 | 0 | 0 | 0 |
| direct_og | 24 | 24 | 24 | 0 | 0 | 0 | 0 | 0 |

### Success rates

- **Metadata resolution success rate: 0%** — 0 of 24 fixtures yielded any usable metadata field (creator, caption, thumbnail, publish date) through any approach.
- **Media-byte availability rate: 0%** — 0 of 72 requests exposed a direct video URL. Even when a page returns `og:video`, Instagram points it at `*.fbcdn.net` hosts, which are outside the SEC-001 allowlist and therefore un-fetchable by the backend by design.

### Latency (seconds, time_total)

| approach | n | p50 | p95 | max |
|---|---|---|---|---|
| oembed | 24 | 1.53 | 2.77 | 2.81 |
| embed | 24 | 1.64 | 2.15 | 62.44 |
| direct_og | 24 | 1.46 | 2.03 | 2.20 |

### Failure categorization

- **oEmbed (24/24 HTTP 200, 0/24 parsed):** the endpoint returned `Content-Type: text/html` — a generic gated shell page — instead of JSON. Parse failure, not an HTTP failure. Category: *gated-response* (login/anti-bot shell).
- **Embed page (23/24 HTTP 200, 1 timeout):** all 23 successful responses were the generic shell page (`<title>Instagram</title>`): no `og:*` tags, no JSON-LD, no embedded `video_url`. The single timeout (62.44s on fixture `DajCWEUjktT` `/p/`) was a slow-drip read; urllib's 10s timeout applies per socket operation, not to the whole response. Category: *gated-response* / *read-timeout*.
- **Direct page (24/24 HTTP 200, 24/24 parsed-as-HTML):** every response was the same generic shell page with zero Open Graph metadata and no media data (`shortcode_media` absent). Category: *gated-response*.
- **Rate limits:** 0× HTTP 429 and 0× HTTP 403 observed across all 72 requests (polite pacing; this is a small-sample observation, not a rate-limit guarantee).

## Conclusion → what gets implemented

**No unauthenticated approach yields metadata or media bytes reliably — 0% across the board.** Instagram serves unauthenticated clients a generic shell page regardless of approach, so there is no policy-compliant acquisition path that produces video bytes (and per SEC-001 the backend could not fetch `*.fbcdn.net` media hosts anyway).

Per the M2 plan this wires the **best available fallback** into `InstagramAdapter.resolve()`:

- Attempt a single unauthenticated direct-page GET (≤10s, head-only byte cap, no cookies) and extract Open Graph metadata if — and only if — the response actually contains it.
- OG metadata present → `MetadataOnly` (worker persists it and lands the memory in `METADATA_ONLY`). Video bytes are documented in code as not obtainable without authentication, so memories stay metadata-only by product design until an authenticated, consent-based path is approved.
- OG metadata absent / gated page / timeout / HTTP error → honest classified failure: `RetryableFailure(SOURCE_RESOLUTION_FAILED)` (retryable, e.g. transient gating), `RetryableFailure(SOURCE_RATE_LIMITED)` on 429, `Unavailable` on 404. The shared URL is always preserved.
- SEC-001 is re-enforced inside the adapter: the canonical URL is re-validated against `ALLOWED_HOSTS` before any fetch; anything else → `Unsupported`.

