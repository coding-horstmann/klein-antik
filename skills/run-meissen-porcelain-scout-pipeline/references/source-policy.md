# Non-eBay Source Policy

## Absolute Exclusions

Never query, open, import, or derive deal candidates from:

- eBay websites or redirects
- eBay APIs, Browse API, Finding API, or Product Research
- SerpApi eBay results
- Klein-Antik's eBay deal worker or `/api/deals/runs/start`
- datasets whose original deal source cannot be identified

Reject a batch if any source name, URL host, redirect target, command, or metadata contains `ebay` or `serpapi`.

## Source Matrix

Only Auctionet is enabled for the first Meißen porcelain pilot. Every other source below remains disabled until its own Railway pilot has met the source-expansion gate.

### Enabled

- Auctionet: active porcelain category with a dedicated Meißen query; frozen batch only, no publishing.

### Pending Railway Pilots

- Private listings: Blocket, DBA, Tori, Tradera, Willhaben, Marktplaats, 2dehands.
- Auction houses and regional auctions: Bukowskis, Bruun Rasmussen, Interencheres, Snapphane Auktioner, Catawiki.
- Later only after separate review: Dorotheum, Mehlis, Drouot.

Before each source is enabled, verify that its current Railway collector supports porcelain categories or arbitrary Meissen queries and produces stable URLs, images, prices, currencies, and external IDs. If it only supports furniture, stop and report the gap. Do not alter a furniture query and assume equivalent coverage.

Do not add a new source merely because it appears searchable. Review collector behavior, terms, access controls, pagination, availability checks, and deduplication first.

## Discovery Vocabulary

Use source-language variants where supported:

- `Meissen`
- `Meißen`
- `Meissner Porzellan`
- `Meissen porcelain`
- `porcelaine de Meissen`
- `porcelaine de Saxe`
- `Meissen porslin`
- `Meissen porselen`

Also inspect broad porcelain, ceramics, tableware, figurine, vase, service, cup, plate, and decorative-object categories. Broad discovery is important because private sellers may omit the manufacturer. It does not lower the evidence standard: a broad-category item becomes a candidate only after Meissen attribution evidence is found.

## Collection Rules

- Run governed collectors through Railway. If visual diagnosis is necessary, use only the Codex in-app Browser for one-off inspection; never use a local browser or browser automation for bulk collection.
- Freeze every collected record before model review.
- Deduplicate by `source:external_id`, then flag likely cross-posts by image and title similarity.
- Preserve original price and currency. Use a single dated ECB exchange-rate snapshot for ranking.
- Preserve ended, unavailable, or withdrawn status. Exclude unavailable items from the actionable table but retain them in the audit trail.
- Do not bypass login walls, CAPTCHAs, access controls, or explicit blocking.

## Source Expansion Gate

A proposed source can enter the allowlist only after a pilot demonstrates:

1. non-eBay origin and canonical URLs;
2. repeatable Railway collection;
3. useful Meissen or broad-porcelain coverage;
4. stable external IDs and image access;
5. parseable price, currency, status, and end time where applicable;
6. acceptable duplicate and false-positive rates;
7. an availability check before review.

Document pilot results before enabling recurring scans.
