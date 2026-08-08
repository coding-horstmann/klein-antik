# klein-antik

Marktpreis-Datenbank fuer einen Antiquitaeten- und Design-Dealfinder. Der
Importer durchsucht fuer 110 fest definierte Begriffe oeffentlich erreichbare
Auktionsarchive und stellt die Ergebnisse in einem Review-Dashboard bereit.

## Aktueller Umfang

- 110 Suchbegriffe in neun Kategorien
- Auctionet, Quittenbaum, Lempertz und Bruun Rasmussen
- 238 kategoriegesteuerte Quellenabfragen je vollstaendigem Lauf
- getrennte Kennzeichnung von Verkaufspreis, Angebot, aktuellem Gebot,
  Schaetzung, unverkauft und unbekannt
- dokumentierte Preisgrundlage, etwa Hammerpreis oder Preis inklusive Aufgeld
- Bilder und Links zur Originalquelle
- Deduplizierung je Quelle und Objekt-ID
- Bewertung, Verwendungsart, Tags und Notizen je Objekt
- Bewertung und Notiz je Suchbegriff
- nachvollziehbare Laeufe und fehlgeschlagene Quellenabfragen in Postgres
- keine SerpApi- oder eBay-API-Kosten

Die Suchmatrix liegt in `config/search_queries.json`. Die bisherige
eBay-/SerpApi-Struktur bleibt in der Datenbank erhalten, wird vom aktuellen
Marktpreis-Importer aber nicht veraendert.

## Preisinterpretation

Die Quellen verwenden unterschiedliche Preisbegriffe. Das Dashboard zeigt
daher nicht nur den Betrag, sondern auch `price_status` und `price_basis`.
Aufgeld, Steuer und Versand werden nicht vereinheitlicht. Die Daten dienen als
Marktrichtung und muessen vor einem Einkauf am Originalobjekt geprueft werden.

## Dienste

Beide Anwendungsdienste verwenden denselben Code und dieselbe
Postgres-Datenbank.

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
MARKET_REQUEST_INTERVAL_SECONDS=2
MARKET_RESULTS_PER_SOURCE=30
```

## Lokal pruefen

```powershell
python -m pip install -r requirements.txt
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```
