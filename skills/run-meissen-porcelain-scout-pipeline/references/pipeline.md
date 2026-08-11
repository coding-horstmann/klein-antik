# Meissen Scout Pipeline

## Canonical Locations

- Klein-Antik repository: `C:/Users/HP/Desktop/Daily Work/Projekte/zeus/klein-antik`
- Existing collector repository for orientation: `C:/Users/HP/Documents/Codex/2026-07-24/ja-ich-hatte-eine-idee-und/work/antikfinder`
- Run root: `C:/Users/HP/Desktop/Daily Work/Projekte/zeus/klein-antik/runs/meissen-scout`
- Dashboard: `https://klein-antik-dashboard-production.up.railway.app/`

Railway production identifiers:

- Project: `149b9ccc-711a-47c2-b75b-c5c0e609f208`
- Environment: `4092832c-8c44-44e4-85a2-afadf3367c61`
- Dashboard service: `a87e4fd2-e500-424a-a83a-9957921d1eca`
- Importer service: `fbff031a-69c7-4335-b12a-55cd25cfca95`

Treat these values as deployment configuration. Verify them through Railway before a real run and update this reference if the deployment changes.

## Runtime Resolution

Resolve Python in this order:

1. `MEISSEN_SCOUT_PYTHON`
2. `<klein-antik>/.venv/Scripts/python.exe`
3. The current workspace Python runtime

Set `RAILWAY_CALLER=skill:use-railway@1.2.1` for Railway CLI operations. Never print complete environment variables or database URLs.

## Freeze A Run

Create a UTC timestamped directory such as `runs/meissen-scout/20260811T120000Z`. Never overwrite an earlier run.

Export references with the production `DATABASE_URL` injected by Railway:

```powershell
railway run --project 149b9ccc-711a-47c2-b75b-c5c0e609f208 --environment 4092832c-8c44-44e4-85a2-afadf3367c61 --service a87e4fd2-e500-424a-a83a-9957921d1eca --no-local -- python skills/run-meissen-porcelain-scout-pipeline/scripts/export_reference_corpus.py --output runs/meissen-scout/<run-id>/reference-corpus.json
```

The export script executes only a `SELECT`. It includes sold `market_listings` attached to the `meissen_porcelain` category. Record the printed SHA-256 value in `review.md`.

If Railway injects only an internal `DATABASE_URL` into a local `railway run` command, use the authenticated dashboard export instead. It executes the same read-only query inside the dashboard service:

```powershell
railway run --project 149b9ccc-711a-47c2-b75b-c5c0e609f208 --environment 4092832c-8c44-44e4-85a2-afadf3367c61 --service a87e4fd2-e500-424a-a83a-9957921d1eca --no-local -- python skills/run-meissen-porcelain-scout-pipeline/scripts/export_reference_corpus.py --dashboard-url https://klein-antik-dashboard-production.up.railway.app --output runs/meissen-scout/<run-id>/reference-corpus.json
```

Freeze deal collection separately as `deal-listings.json`. A real deal collection must run through a reviewed Railway collector that supports the selected source and porcelain search. Do not call Klein-Antik's existing `/api/deals/runs/start` route; it is designed for eBay active listings.

## Reference Profile

Classify references into structured fields before price comparison:

```json
{
  "reference_id": "auctionet:12345",
  "object_type": "figurine",
  "model_or_form": "A 1073",
  "decor": "polychrome",
  "artist_or_modeller": "Paul Scheurich",
  "period": "20th century",
  "dimensions_cm": {"height": 24.0},
  "piece_count": 1,
  "condition": ["restored"],
  "attribution_confidence": "medium",
  "mark_evidence": "crossed swords visible",
  "classification_evidence": ["title", "image"]
}
```

Use `unknown` rather than guessing. A group is exact only when object type and piece count agree and no known model, decor, dimensions, period, condition, or attribution conflict exists.

## Deal Dataset Schema

`deal-listings.json` must contain:

```json
{
  "run": {
    "run_id": "20260811T120000Z",
    "collected_at": "2026-08-11T12:00:00Z",
    "sources": ["auctionet"],
    "forbidden_sources_checked": true
  },
  "listings": [
    {
      "listing_id": "auctionet:98765",
      "source": "auctionet",
      "external_id": "98765",
      "title": "...",
      "url": "https://...",
      "image_urls": ["https://..."],
      "price_value": "120.00",
      "currency": "EUR",
      "price_eur": "120.00",
      "fx_rate": "1.0",
      "sale_mode": "auction",
      "auction_end": "2026-08-15T18:00:00Z",
      "collected_at": "2026-08-11T12:00:00Z"
    }
  ]
}
```

Use durable `source:external_id` IDs. Preserve raw snapshots or raw-result hashes when the collector provides them.

## Candidate Bundle Schema

`candidate-bundle.json` must contain `run`, `input_hashes`, and `candidates`. Each candidate requires:

```json
{
  "candidate_id": "M001",
  "listing_id": "auctionet:98765",
  "object_type": "figurine",
  "deal_price_eur": "120.00",
  "conservative_reference_eur": "500.00",
  "median_reference_eur": "650.00",
  "exact_comparable_count": 5,
  "reference_ids": ["auctionet:101", "lempertz:202"],
  "directional_spread_eur": "380.00",
  "price_ratio": "0.24",
  "priority": "A",
  "confidence": "medium",
  "risks": ["mark not legible"],
  "manual_review_required": true
}
```

Selected reference IDs must exist in the frozen reference corpus. `exact_comparable_count` must equal the number of unique selected reference IDs.

## Review Table

Sort by priority, then directional spread, then confidence. Show:

| ID | Object | Deal source | Deal price | Conservative reference | Median | Exact comps | Spread | Ratio | Confidence | Main risks | Link |
|---|---|---|---:|---:|---:|---:|---:|---:|---|---|---|

Explain exclusions and sparse groups after the table. Never describe the directional spread as guaranteed profit.

## Validation And Publication Boundary

Run:

```powershell
python skills/run-meissen-porcelain-scout-pipeline/scripts/validate_scout_bundle.py --references <reference-corpus.json> --deals <deal-listings.json> --candidates <candidate-bundle.json> --output <validation-report.json>
```

The first real run ends after successful validation and human review. There is no generic, non-eBay deal publication endpoint in the current Klein-Antik application. Implement and review that boundary separately before any later run may publish.
