---
name: run-meissen-porcelain-scout-pipeline
description: Run an auditable Meissen porcelain scouting workflow that uses the Klein-Antik sold-price archive as reference evidence and searches only approved non-eBay European deal sources. Use it to freeze the current reference corpus, collect or reuse a frozen deal batch, review objects independently, select object-compatible comparables, rank potentially underpriced listings, validate every candidate, and prepare a human-review bundle without publishing or buying.
---

# Run Meissen Porcelain Scout Pipeline

Create a reproducible Meissen scouting run. Treat prices as directional evidence, require exact object compatibility, and stop before publication.

Read [pipeline.md](references/pipeline.md) and [source-policy.md](references/source-policy.md) before running commands.

## Non-Negotiable Rules

- Never search eBay, call an eBay API, scrape an eBay page, use SerpApi, or use an eBay-derived deal endpoint.
- Abort if a source URL, redirect, source name, input file, or command contains `ebay` or `serpapi`.
- Use only the non-eBay sources allowed by [source-policy.md](references/source-policy.md).
- Run the first real pipeline execution as a dry run. Produce files for review and stop before database or dashboard publication.
- Never buy, bid, message a seller, or claim authenticity automatically.
- Do not force a candidate quota. An empty result is valid.

## Workflow

1. Resolve the canonical repositories, Railway service, runtime, and run directory from [pipeline.md](references/pipeline.md).
2. Export the current sold Meissen reference corpus with `scripts/export_reference_corpus.py`. Use a read-only database connection, freeze the JSON file, and record its SHA-256 hash.
3. Report the actual reference count, source distribution, currencies, and price bases. Do not reuse a remembered count.
4. Create `reference-profile.json` with `scripts/build_reference_profile.py`. Its title-only classifications are preliminary. Enrich shortlisted references further with image evidence for object type, model or form number, decor, artist or modeller, period, dimensions, piece count, condition, attribution confidence, and mark evidence.
5. Build reference groups from genuinely comparable objects. Use sold prices only for numerical value bands. Preserve original prices and price bases.
6. Collect or reuse a fresh frozen batch from approved non-eBay sources. Keep `Meissen` as an explicit discovery query and add bounded broad porcelain-category discovery where the collector supports it. A broad-category result needs later image or mark evidence before it may be considered Meissen. Run governed collectors through Railway; do not improvise direct marketplace browser scraping. For visual diagnosis, use only the Codex in-app Browser and never use it for bulk collection.
7. Freeze every collected listing before ranking. Preserve source, external ID, canonical URL, image URLs, original price, currency, timestamp, and raw evidence.
8. Perform two separate reviews:
   - `zero-shot`: inspect the deal listing without opening the price corpus; identify the object and risks.
   - `reference-pass`: select exact comparables and calculate a conservative reference band.
9. Keep the zero-shot output separate and complete it first. If isolated tasks are unavailable, use separate files and do not open the reference corpus during the zero-shot pass.
10. Take the union of candidates from both reviews. Agreement is evidence, not a requirement.
11. Calculate directional value using the rules in [pipeline.md](references/pipeline.md). Mark fewer than three exact comparables as sparse evidence.
12. Validate the frozen inputs and candidate bundle with `scripts/validate_scout_bundle.py`.
13. Present the review table and artifact paths. Stop before publication and ask for an explicit review decision.

## Comparable Selection

Require the same object family before considering price evidence. Then narrow by model or form, decor, artist or modeller, period, dimensions, piece count, condition, and attribution.

Reject generic visual similarity. A plate is not a comparable for a vase, teapot, figurine, candlestick, or service. A single piece is not directly comparable to a multi-piece service. The words `porcelain`, `Meissen`, or crossed-swords imagery alone do not establish equivalence.

Downgrade or reject references described as `after Meissen`, `Meissen style`, `Dresden`, `Saxony`, reproduction, second quality, damaged, restored, incomplete, or uncertain unless the deal has the same limitation.

Treat marks and signatures as supporting evidence only. Require manual authenticity review for every candidate.

## Valuation Rules

- Use sold results for numerical reference bands; keep active offers separate.
- Preserve whether a result is hammer, realised, or premium-included. Do not silently normalize mixed price bases.
- Convert currencies using one dated ECB snapshot per run. Store the rate and original amount.
- Use the 25th percentile of exact comparable sold prices as the conservative reference when at least three comparables exist. Also report median, 75th percentile, range, and count.
- With one or two exact comparables, label the result `sparse`; do not assign priority A or B.
- Calculate `directional_spread_eur = conservative_reference_eur - deal_price_eur` and `price_ratio = deal_price_eur / conservative_reference_eur`.
- Default ranking is configurable: A at ratio at most 0.35 and spread at least EUR 300; B at ratio at most 0.50 and spread at least EUR 150; otherwise watch or reject.
- Do not deduct fees, buyer premiums, shipping, taxes, restoration, or resale costs in this workflow. State that the spread is directional, not net profit.

## Required Output

Create a timestamped run directory containing:

- `reference-corpus.json`
- `reference-profile.json`
- `deal-listings.json`
- `zero-shot.json`
- `reference-pass.json`
- `candidate-bundle.json`
- `validation-report.json`
- `review.md`

Every candidate must contain an exact deal URL, image evidence, deal price, object classification, selected reference IDs, conservative and median reference prices, comparable count, directional spread, confidence, risks, and `manual_review_required: true`.

## Stop Conditions

- Abort on any eBay or SerpApi dependency.
- Abort if frozen input hashes, source coverage, IDs, prices, URLs, or reference links cannot be verified.
- Stop and report an implementation gap if an approved Railway collector does not support porcelain discovery. Never reuse a furniture query silently.
- Stop after the validated review bundle. The current dashboard deal route is eBay-specific and is not a valid publication target for this skill.
