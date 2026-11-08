// Anzeigeformate und deutsche Bezeichnungen.

const dateFmt = new Intl.DateTimeFormat("de-DE", { day: "2-digit", month: "2-digit", year: "numeric" });
const dateTimeFmt = new Intl.DateTimeFormat("de-DE", {
  day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit",
});

export function fmtDate(value: string | null | undefined): string {
  if (!value) return "–";
  const d = /^\d{4}-\d{2}-\d{2}$/.test(value) ? new Date(`${value}T00:00:00`) : new Date(value);
  return Number.isNaN(d.getTime()) ? value : dateFmt.format(d);
}

export function fmtDateTime(value: string | null | undefined): string {
  if (!value) return "–";
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value : dateTimeFmt.format(d);
}

export function fmtMoney(amount: string | null | undefined, currency: string | null | undefined): string {
  if (amount === null || amount === undefined) return "–";
  const n = Number(amount);
  if (Number.isNaN(n)) return amount;
  try {
    return new Intl.NumberFormat("de-DE", { style: "currency", currency: currency || "EUR" }).format(n);
  } catch {
    return `${n.toFixed(2)} ${currency ?? ""}`;
  }
}

export function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export const DOCUMENT_STATUS: Record<string, { label: string; tone: Tone }> = {
  pending: { label: "Wartet", tone: "neutral" },
  processing: { label: "In Verarbeitung", tone: "info" },
  extracted: { label: "Erkannt", tone: "info" },
  ready_for_datev: { label: "Bereit für DATEV", tone: "success" },
  transmitted: { label: "An DATEV übertragen", tone: "success" },
  awaiting_datev_xml_integration: { label: "XML-Warteschlange", tone: "info" },
  manual_review: { label: "Prüfung nötig", tone: "warning" },
  duplicate: { label: "Dublette", tone: "neutral" },
  ignored: { label: "Ignoriert", tone: "neutral" },
  error: { label: "Fehler", tone: "danger" },
};

export const TRANSMISSION_STATUS: Record<string, { label: string; tone: Tone }> = {
  pending: { label: "Wartet auf Freigabe", tone: "neutral" },
  ready: { label: "Versand geplant", tone: "info" },
  sending: { label: "Wird gesendet", tone: "info" },
  sent_unconfirmed: { label: "Gesendet, unbestätigt", tone: "warning" },
  confirmed: { label: "In DATEV bestätigt", tone: "success" },
  failed: { label: "Fehlgeschlagen", tone: "danger" },
  manual_review: { label: "Unklar – prüfen", tone: "danger" },
};

export const MESSAGE_STATUS: Record<string, { label: string; tone: Tone }> = {
  fetched: { label: "Abgerufen", tone: "neutral" },
  processing: { label: "In Verarbeitung", tone: "info" },
  processed: { label: "Verarbeitet", tone: "success" },
  no_invoice: { label: "Keine Rechnung", tone: "neutral" },
  manual_review: { label: "Prüfung nötig", tone: "warning" },
  error: { label: "Fehler", tone: "danger" },
};

export const DOCUMENT_TYPE: Record<string, string> = {
  invoice: "Rechnung",
  credit_note: "Gutschrift",
  cancellation: "Stornorechnung",
  offer: "Angebot",
  delivery_note: "Lieferschein",
  dunning: "Mahnung",
  order_confirmation: "Auftragsbestätigung",
  other: "Sonstiges",
  unclear: "Unklar",
};

export const SOURCE_KIND: Record<string, string> = {
  pdf: "PDF",
  scan_pdf: "Scan-PDF",
  zugferd_pdf: "ZUGFeRD/Factur-X",
  xml_xrechnung: "XRechnung",
  xml_other: "XML",
  image: "Bild",
  email_body: "E-Mail-Text",
  unknown: "Unbekannt",
};

export const FIELD_SOURCE: Record<string, string> = {
  xml: "XML", text: "PDF-Text", text_header: "PDF-Kopf (abgeleitet)", ocr: "OCR", ai: "KI (verifiziert)", manual: "Manuell",
};

export const SEND_MODE: Record<string, string> = {
  automatic: "Automatisch", manual: "Manuell", test_delivery: "Testlieferung", dry_run: "Testlauf",
};

export type Tone = "neutral" | "info" | "success" | "warning" | "danger";
