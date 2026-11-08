# Dashboard

React 18 + TypeScript + Tailwind CSS 4 (Vite). Der Build (`dist/`) wird im
Docker-Image von FastAPI unter `/` ausgeliefert; die API liegt unter `/api`
(gleiche Domain → HttpOnly-Session-Cookie + CSRF-Header, kein CORS nötig).

```bash
npm install
npm run dev      # http://localhost:5173, leitet /api an http://localhost:8091 weiter
npm run build    # Typprüfung + Produktionsbuild nach dist/
```

| Seite | Inhalt |
|---|---|
| Übersicht | Kennzahlen (heute erkannt, gesendet, unbestätigt, Dubletten, Fehler, XML, Prüfung), Worker-Status |
| Rechnungen | Tabelle mit Filtern (Unternehmen, Status, Typ, Lieferant, Datum), Nachladen |
| Detail | Original-Vorschau, erkannte Daten mit Quelle, Herkunft, Dubletten, DATEV-Übertragung, Prüfaktionen |
| Prüfung | Dokumente und E-Mails in manueller Prüfung |
| DATEV-Versand | Versandprotokoll, Versuche, Senden/Bestätigen/Klären |
| XML-Warteschlange | Reine XML-Rechnungen, Download, Übergabe (sobald Schnittstelle vorhanden) |
| Postfächer | Status, Testverbindung, Domain bestätigen, (de)aktivieren, Jetzt abrufen, Ordner |
| DATEV-Einstellungen | Testmodus, Zieladresse, Absender, Freigabe-Checkliste, Produktivfreigabe, Auto-Versand |
| Einstellungen | Laufzeit-Einstellungen, eigene USt-IDs und Namensvarianten der Unternehmen |
| Protokoll | Audit-Log (nur Admin) |
