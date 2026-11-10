import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { type DocumentDetail, get, post, query, type Transmission } from "../api";
import { useApp } from "../app-context";
import { DocumentPreview } from "../components/DocumentPreview";
import { TransmissionActions } from "../components/TransmissionActions";
import { Badge, Button, Card, DescList, ErrorBox, Field, Input, Modal, Notice, PageHeader, Select, Spinner, StatusBadge, Textarea } from "../components/ui";
import {
  DOCUMENT_STATUS, DOCUMENT_TYPE, FIELD_SOURCE, fmtBytes, fmtDate, fmtDateTime, fmtMoney, SEND_MODE, SOURCE_KIND,
  TRANSMISSION_STATUS,
} from "../format";

const REVIEWABLE = new Set(["manual_review", "duplicate", "ignored"]);
// Nur maschinell in die Prüfung gelangte Belege; bewusste Entscheidungen bleiben unangetastet.
const REPROCESSABLE = new Set(["manual_review", "error"]);
const IN_PROGRESS = new Set(["pending", "processing"]);

function Source({ doc, field }: { doc: DocumentDetail; field: string }) {
  const src = doc.invoice?.field_sources?.[field];
  if (!src) return null;
  return <span className="ml-2 text-xs text-slate-400">({FIELD_SOURCE[src] ?? src})</span>;
}

export function DocumentDetailPage() {
  const { id } = useParams();
  const { companyName, canReview } = useApp();
  const qc = useQueryClient();
  const doc = useQuery({
    queryKey: ["document", id],
    queryFn: () => get<DocumentDetail>(`/api/documents/${id}`),
    // Während der Neuerkennung regelmäßig nachladen, bis der Worker fertig ist.
    refetchInterval: (q) => (q.state.data && IN_PROGRESS.has(q.state.data.status) ? 3000 : false),
  });
  const transmissions = useQuery({
    queryKey: ["transmissions", "document", id],
    queryFn: () => get<Transmission[]>(`/api/datev/transmissions${query({ document_id: id, include_test: true })}`),
  });
  const [dialog, setDialog] = useState<null | "approve" | "ignore" | "duplicate">(null);
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["document", id] });
    void qc.invalidateQueries({ queryKey: ["transmissions"] });
    void qc.invalidateQueries({ queryKey: ["summary"] });
    void qc.invalidateQueries({ queryKey: ["documents"] });
  };
  const dryRun = useMutation({
    mutationFn: () => post<{ preview: Record<string, unknown> }>(`/api/datev/documents/${id}/dry-run`),
    onSuccess: refresh,
  });
  const reprocess = useMutation({
    mutationFn: () => post<DocumentDetail>(`/api/documents/${id}/reprocess`),
    onSuccess: refresh,
  });

  if (doc.isLoading) return <Spinner />;
  if (doc.error || !doc.data) return <ErrorBox error={doc.error ?? "Nicht gefunden"} />;
  const d = doc.data;
  const inv = d.invoice;
  const warnings = inv?.raw_extraction?.warnings ?? [];

  return (
    <>
      <PageHeader
        title={inv?.supplier_name ? `${inv.supplier_name}${inv.invoice_number ? ` · ${inv.invoice_number}` : ""}` : d.original_filename ?? `Dokument ${d.id}`}
        subtitle={<span className="flex flex-wrap items-center gap-2">
          <StatusBadge map={DOCUMENT_STATUS} value={d.status} />
          <Badge>{DOCUMENT_TYPE[d.document_type] ?? d.document_type}</Badge>
          <span>{companyName(d.company_id)} · {d.mailbox_email}</span>
        </span>}
        actions={<>
          <Link to="/documents"><Button variant="ghost">Zur Liste</Button></Link>
          {canReview && REVIEWABLE.has(d.status) && <>
            <Button variant="primary" onClick={() => setDialog("approve")}>Freigeben</Button>
            <Button onClick={() => setDialog("ignore")}>Ignorieren</Button>
            <Button onClick={() => setDialog("duplicate")}>Als Dublette</Button>
          </>}
          {canReview && REPROCESSABLE.has(d.status) &&
            <Button disabled={reprocess.isPending} onClick={() => reprocess.mutate()}
              title="Erkennung erneut ausführen (Regeln, OCR, KI-Fallback)">Neu erkennen</Button>}
        </>}
      />
      {reprocess.error && <div className="mb-4"><ErrorBox error={reprocess.error} /></div>}
      {IN_PROGRESS.has(d.status) && (
        <div className="mb-4">
          <Notice tone="neutral">Wird neu erkannt – die Seite aktualisiert sich automatisch.</Notice>
        </div>
      )}

      {d.review_reason && (
        <div className="mb-4">
          <Notice tone={d.status === "manual_review" ? "warning" : "neutral"}>{d.review_reason}</Notice>
        </div>
      )}

      <div className="grid gap-5 xl:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)]">
        <Card title="Original">
          <DocumentPreview doc={d} />
        </Card>

        <div className="space-y-5">
          <Card title="Erkannte Rechnungsdaten">
            {!inv ? <p className="text-sm text-slate-500">Keine Rechnungsdaten erkannt.</p> : (
              <>
                <DescList items={[
                  ["Rechnungsnummer", <>{inv.invoice_number ?? "–"}<Source doc={d} field="invoice_number" /></>],
                  ["Rechnungsdatum", <>{fmtDate(inv.invoice_date)}<Source doc={d} field="invoice_date" /></>],
                  ["Leistungsdatum", fmtDate(inv.service_date)],
                  ["Lieferant", <>{inv.supplier_name ?? "–"}<Source doc={d} field="supplier_name" /></>],
                  ["Lieferantenadresse", inv.supplier_address],
                  ["USt-ID", <>{inv.supplier_vat_id ?? "–"}<Source doc={d} field="supplier_vat_id" /></>],
                  ["Netto", <span className="num">{fmtMoney(inv.net_amount, inv.currency)}</span>],
                  ["Umsatzsteuer", <span className="num">{fmtMoney(inv.tax_amount, inv.currency)}</span>],
                  ["Brutto", <span className="num font-semibold">{fmtMoney(inv.gross_amount, inv.currency)}<Source doc={d} field="gross_amount" /></span>],
                  ["Empfänger", inv.recipient_name],
                  ["IBAN", inv.iban],
                  ["Zahlungsziel", inv.due_date ? fmtDate(inv.due_date) : inv.payment_terms],
                  ["Bezug auf Rechnung", inv.referenced_invoice_number],
                  ["Erkennung", `${inv.extraction_method === "xml" ? "E-Rechnung (XML)" : inv.extraction_method === "ai" ? "KI-Fallback" : inv.extraction_method === "manual" ? "Manuell" : "Regeln"}${d.ai_confidence ? ` · KI-Konfidenz ${Math.round(Number(d.ai_confidence) * 100)} %` : ""}`],
                ]} />
                {warnings.length > 0 && (
                  <ul className="mt-3 space-y-1 text-xs text-amber-800">{warnings.map((w, i) => <li key={i}>⚠ {w}</li>)}</ul>
                )}
              </>
            )}
          </Card>

          <Card title="Herkunft">
            <DescList items={[
              ["Postfach", d.mailbox_email],
              ["Empfangen", fmtDateTime(d.received_at)],
              ["Absender", d.message_from],
              ["Betreff", d.message_subject],
              ["Datei", `${d.original_filename ?? "–"} · ${fmtBytes(d.size_bytes)}${d.page_count ? ` · ${d.page_count} S.` : ""}`],
              ["Format", `${SOURCE_KIND[d.source_kind] ?? d.source_kind}${d.has_embedded_xml ? " (XML eingebettet)" : ""}`],
              ["SHA-256", <code className="break-all text-xs">{d.sha256}</code>],
            ]} />
          </Card>

          {d.duplicates.length > 0 && (
            <Card title="Dubletten">
              <ul className="space-y-2 text-sm">
                {d.duplicates.map((dup, i) => {
                  const other = dup.document_id === d.id ? dup.duplicate_of_document_id : dup.document_id;
                  return (
                    <li key={i} className="flex flex-wrap items-center gap-2">
                      <Link className="font-medium text-brand-700 hover:underline" to={`/documents/${other}`}>Dokument {other}</Link>
                      <Badge>{dup.match_level === "file_hash" ? "identische Datei" : dup.match_level === "invoice_identity" ? "gleiche Rechnung" : "möglicher Konflikt"}</Badge>
                      <Badge tone={dup.decision === "needs_review" ? "warning" : "neutral"}>
                        {dup.decision === "confirmed_duplicate" ? "bestätigt" : dup.decision === "not_duplicate" ? "keine Dublette" : "zu prüfen"}
                      </Badge>
                    </li>
                  );
                })}
              </ul>
            </Card>
          )}

          <Card title="DATEV-Übertragung" actions={canReview && (
            <Button onClick={() => dryRun.mutate()} disabled={dryRun.isPending}>Testlauf (ohne Versand)</Button>
          )}>
            <ErrorBox error={dryRun.error} />
            {dryRun.data && (
              <div className="mb-3"><Notice tone="success">
                Testlauf: Mail an <b>{String(dryRun.data.preview.to)}</b>, Anhang „{String(dryRun.data.preview.attachment)}“ –
                nichts gesendet.
                {Array.isArray(dryRun.data.preview.document_blockers) && dryRun.data.preview.document_blockers.length > 0 &&
                  <> Hinweise: {(dryRun.data.preview.document_blockers as string[]).join("; ")}</>}
              </Notice></div>
            )}
            {(transmissions.data ?? []).length === 0 ? (
              <p className="text-sm text-slate-500">
                {d.status === "ready_for_datev" ? "Wird beim nächsten Durchlauf des DATEV-Workers eingeplant." : "Keine Übertragung."}
              </p>
            ) : (
              <ul className="space-y-3">
                {transmissions.data!.map((t) => (
                  <li key={t.id} className="rounded-md border border-slate-200 p-3 text-sm">
                    <div className="flex flex-wrap items-center gap-2">
                      <StatusBadge map={TRANSMISSION_STATUS} value={t.status} />
                      <Badge>{SEND_MODE[t.send_mode] ?? t.send_mode}</Badge>
                      {t.test_mode && <Badge tone="info">Test</Badge>}
                      <span className="text-slate-500">an {t.destination_address ?? "–"}</span>
                      <Link to={`/transmissions?open=${t.id}`} className="ml-auto text-brand-700 hover:underline">Protokoll</Link>
                    </div>
                    {t.last_error && <p className="mt-2 text-xs text-slate-600">{t.last_error}</p>}
                    {!t.test_mode && <div className="mt-2"><TransmissionActions t={t} onDone={refresh} /></div>}
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>
      </div>

      <ReviewDialog key={dialog ?? "closed"} doc={d} mode={dialog} onClose={() => setDialog(null)} onDone={() => { setDialog(null); refresh(); }} />
    </>
  );
}

const CORRECTABLE: [string, string, string][] = [
  ["invoice_number", "Rechnungsnummer", "text"],
  ["invoice_date", "Rechnungsdatum", "date"],
  ["supplier_name", "Lieferant", "text"],
  ["supplier_vat_id", "USt-ID Lieferant", "text"],
  ["net_amount", "Netto", "number"],
  ["tax_amount", "Umsatzsteuer", "number"],
  ["gross_amount", "Brutto", "number"],
  ["currency", "Währung", "text"],
];

const INVOICE_LIKE = ["invoice", "credit_note", "cancellation"];

function ReviewDialog({ doc, mode, onClose, onDone }: {
  doc: DocumentDetail; mode: null | "approve" | "ignore" | "duplicate"; onClose: () => void; onDone: () => void;
}) {
  const inv = doc.invoice;
  const [reason, setReason] = useState(mode === "approve" ? "Manuell geprüft" : "");
  const [dupOf, setDupOf] = useState("");
  const detectedType = inv?.document_type ?? doc.document_type;
  // Freigabe heißt "ist eine Rechnung"; unklare Art wird beim Freigeben zur Rechnung.
  const [docType, setDocType] = useState(INVOICE_LIKE.includes(detectedType) ? detectedType : "invoice");
  const [values, setValues] = useState<Record<string, string>>({});
  const mutation = useMutation({
    mutationFn: () => {
      const corrections: Record<string, string | null> = {};
      for (const [key] of CORRECTABLE) {
        if (key in values) corrections[key] = values[key] === "" ? null : values[key];
      }
      if (docType !== detectedType) corrections.document_type = docType;
      return post(`/api/documents/${doc.id}/review`, {
        decision: mode === "duplicate" ? "mark_duplicate" : mode,
        reason,
        duplicate_of_document_id: mode === "duplicate" ? Number(dupOf) : undefined,
        corrections: mode === "approve" && Object.keys(corrections).length ? corrections : undefined,
      });
    },
    onSuccess: onDone,
  });
  const titles = { approve: "Dokument freigeben", ignore: "Dokument ignorieren", duplicate: "Als Dublette markieren" };
  if (!mode) return null;
  const current = (key: string) => (inv as Record<string, unknown> | null)?.[key] as string | null | undefined;

  return (
    <Modal open title={titles[mode]} onClose={onClose} footer={<>
      <Button onClick={onClose}>Abbrechen</Button>
      <Button variant={mode === "approve" ? "primary" : "secondary"} disabled={reason.trim().length < 3 || mutation.isPending}
        onClick={() => mutation.mutate()}>Bestätigen</Button>
    </>}>
      {mode === "approve" && (
        <>
          <p className="text-sm text-slate-600">
            Der Beleg wird so wie er ist für DATEV eingeplant (Versand nur, wenn die DATEV-Freigabe es erlaubt).
            DATEV liest die Rechnungsdaten selbst aus – Angaben hier sind nicht nötig.
          </p>
          <Field label="Dokumenttyp">
            <Select value={docType} onChange={(e) => setDocType(e.target.value)}>
              {INVOICE_LIKE.map((k) => <option key={k} value={k}>{DOCUMENT_TYPE[k]}</option>)}
            </Select>
          </Field>
          <details className="rounded border border-slate-200 px-3 py-2 text-sm">
            <summary className="cursor-pointer text-slate-600">Rechnungsdaten korrigieren (optional)</summary>
            <div className="mt-3 grid grid-cols-2 gap-3">
              {CORRECTABLE.map(([key, label, type]) => (
                <Field key={key} label={label}>
                  <Input type={type} step={type === "number" ? "0.01" : undefined}
                    value={values[key] ?? current(key) ?? ""}
                    onChange={(e) => setValues({ ...values, [key]: e.target.value })} />
                </Field>
              ))}
            </div>
          </details>
        </>
      )}
      {mode === "duplicate" && (
        <Field label="Dublette von Dokument-ID">
          <Input inputMode="numeric" value={dupOf} onChange={(e) => setDupOf(e.target.value.replace(/\D/g, ""))} />
        </Field>
      )}
      <Field label="Begründung (wird protokolliert)">
        <Textarea rows={2} value={reason} onChange={(e) => setReason(e.target.value)} />
      </Field>
      <ErrorBox error={mutation.error} />
    </Modal>
  );
}
