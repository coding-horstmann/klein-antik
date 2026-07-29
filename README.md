# klein-antik

Pilot fuer einen Antiquitaeten- und Design-Dealfinder. Der erste Schritt sammelt
verkaufte eBay-Referenzen ueber 110 fest definierte SerpApi-Suchen und stellt sie
in einem Review-Dashboard bereit.

## Pilotumfang

- 110 Suchbegriffe in neun Kategorien
- genau eine eBay-Ergebnisseite je Suchbegriff
- maximal 200 Ergebnisse je Suche
- nur verkaufte Artikel
- gebraucht oder Zustand nicht angegeben
- keine Produktdetailabfragen
- Deduplizierung ueber die eBay-Produkt-ID
- Bewertung, Verwendungsart, Tags und Notizen je Listing
- Bewertung und Notiz je Suchbegriff
- pausierbarer, nachvollziehbarer Lauf in Postgres

Die Suchmatrix liegt in `config/search_queries.json`.

## Dienste

Beide Anwendungsdienste verwenden denselben Code und dieselbe Postgres-Datenbank.

```text
klein-antik-dashboard
  APP_MODE=dashboard

klein-antik-importer
  APP_MODE=worker
```

Erforderliche Variablen:

```text
DATABASE_URL
PYTHONPATH=src
APP_MODE=dashboard|worker
DASHBOARD_USER=niklas
DASHBOARD_PASSWORD=<geheim>
SERPAPI_API_KEY_PRIMARY=<nur beim Importer>
SERPAPI_REQUEST_INTERVAL_SECONDS=75
MAX_SEARCHES_PER_RUN=110
```

Ohne `SERPAPI_API_KEY_PRIMARY` bleibt der Importer im Wartezustand. Ein Lauf kann
erst im Dashboard gestartet werden, wenn der Importer einen vorhandenen Key
gemeldet hat.

## Lokal pruefen

```powershell
python -m pip install -r requirements.txt
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```
