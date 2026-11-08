import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { type DocumentItem, type EmailMessageItem, get, post, query } from "../api";
import { useApp } from "../app-context";
import { DocumentTable } from "../components/DocumentTable";
import { Button, Card, Empty, ErrorBox, Field, Modal, Notice, PageHeader, Select, Spinner, Textarea } from "../components/ui";
import { fmtDateTime } from "../format";

export function ReviewPage() {
  const { companyId } = useApp();
  const docs = useQuery({
    queryKey: ["documents", "review", companyId],
    queryFn: () => get<DocumentItem[]>(`/api/documents${query({ company_id: companyId, status: "manual_review", limit: 200 })}`),
  });
  const messages = useQuery({
    queryKey: ["messages", "review", companyId],
    queryFn: () => get<EmailMessageItem[]>(`/api/email-messages${query({ company_id: companyId, status: "manual_review", limit: 200 })}`),
  });

  return (
    <>
      <PageHeader title="Manuelle Prüfung" subtitle="Belege und E-Mails, die nicht automatisch entschieden werden konnten" />
      <div className="space-y-5">
        <Card title={`Dokumente (${docs.data?.length ?? "…"})`}>
          <ErrorBox error={docs.error} />
          {docs.isLoading ? <Spinner /> : <ReviewDocuments docs={docs.data ?? []} />}
        </Card>
        <Card title={`E-Mails (${messages.data?.length ?? "…"})`}>
          <p className="mb-3 text-sm text-slate-600">
            E-Mails ohne verwertbaren Anhang, mit Rechnungslinks, beschädigten oder blockierten Anhängen.
            Links werden nie automatisch aufgerufen – bitte nur öffnen, wenn der Absender bekannt ist.
          </p>
          <ErrorBox error={messages.error} />
          {messages.isLoading ? <Spinner /> : (messages.data ?? []).length === 0 ? <Empty>Keine offenen E-Mails.</Empty> : (
            <ul className="divide-y divide-slate-100">
              {messages.data!.map((m) => <MessageRow key={m.id} m={m} />)}
            </ul>
          )}
        </Card>
      </div>
    </>
  );
}

interface BulkIgnoreResult {
  ignored: number[];
  failed: { document_id: number; error: string }[];
}

function ReviewDocuments({ docs }: { docs: DocumentItem[] }) {
  const { canReview } = useApp();
  const qc = useQueryClient();
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("Keine Rechnung");
  // Nur noch vorhandene Dokumente zählen (nach Neuladen können Einträge verschwunden sein).
  const ids = docs.map((d) => d.id).filter((id) => selected.has(id));
  const mutation = useMutation({
    mutationFn: () => post<BulkIgnoreResult>("/api/documents/bulk-ignore", { document_ids: ids, reason }),
    onSuccess: (result) => {
      setSelected(new Set(result.failed.map((f) => f.document_id)));
      if (result.failed.length === 0) setOpen(false);
      void qc.invalidateQueries({ queryKey: ["documents"] });
      void qc.invalidateQueries({ queryKey: ["summary"] });
    },
  });
  const toggle = (id: number) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  const toggleAll = () => setSelected(ids.length === docs.length ? new Set() : new Set(docs.map((d) => d.id)));

  return (
    <>
      {canReview && docs.length > 0 && (
        <div className="mb-3 flex flex-wrap items-center gap-3 text-sm">
          <span className="text-slate-600">{ids.length} ausgewählt</span>
          <Button disabled={ids.length === 0} onClick={() => { mutation.reset(); setOpen(true); }}>Ignorieren</Button>
          {ids.length > 0 && <button className="text-slate-500 hover:underline" onClick={() => setSelected(new Set())}>Auswahl aufheben</button>}
        </div>
      )}
      <DocumentTable docs={docs} selection={canReview ? { selected, toggle, toggleAll } : undefined} />
      <Modal open={open} title={`${ids.length} Dokument(e) ignorieren`} onClose={() => setOpen(false)} footer={<>
        <Button onClick={() => setOpen(false)}>Abbrechen</Button>
        <Button variant="primary" disabled={reason.trim().length < 3 || ids.length === 0 || mutation.isPending}
                onClick={() => mutation.mutate()}>Ignorieren</Button>
      </>}>
        <p className="mb-3 text-sm text-slate-600">Ignorierte Dokumente gehen nicht an DATEV. Jede Änderung wird protokolliert.</p>
        <Field label="Vermerk"><Textarea rows={2} value={reason} onChange={(e) => setReason(e.target.value)} /></Field>
        {mutation.data && mutation.data.failed.length > 0 && (
          <Notice tone="warning">
            {mutation.data.ignored.length} ignoriert, {mutation.data.failed.length} nicht änderbar:{" "}
            {mutation.data.failed.map((f) => `#${f.document_id} (${f.error})`).join(", ")}
          </Notice>
        )}
        <ErrorBox error={mutation.error} />
      </Modal>
    </>
  );
}

function MessageRow({ m }: { m: EmailMessageItem }) {
  const { companyName, canReview } = useApp();
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [outcome, setOutcome] = useState<"processed" | "no_invoice">("processed");
  const [reason, setReason] = useState("");
  const mutation = useMutation({
    mutationFn: () => post(`/api/email-messages/${m.id}/resolve`, { outcome, reason }),
    onSuccess: () => {
      setOpen(false);
      void qc.invalidateQueries({ queryKey: ["messages"] });
      void qc.invalidateQueries({ queryKey: ["summary"] });
    },
  });
  return (
    <li className="py-3 text-sm">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="font-medium">{m.subject || "(ohne Betreff)"}</div>
          <div className="text-slate-500">{m.from_address} · {fmtDateTime(m.received_at)} · {companyName(m.company_id)}</div>
        </div>
        {canReview && <Button onClick={() => setOpen(true)}>Erledigt</Button>}
      </div>
      {m.review_reason && <div className="mt-2"><Notice tone="warning">{m.review_reason}</Notice></div>}
      {m.detected_invoice_links.length > 0 && (
        <ul className="mt-2 space-y-0.5 text-xs">
          {m.detected_invoice_links.map((l) => (
            <li key={l} className="break-all font-mono text-slate-600">{l}</li>
          ))}
        </ul>
      )}
      <Modal open={open} title="E-Mail als geprüft markieren" onClose={() => setOpen(false)} footer={<>
        <Button onClick={() => setOpen(false)}>Abbrechen</Button>
        <Button variant="primary" disabled={reason.trim().length < 3 || mutation.isPending} onClick={() => mutation.mutate()}>Speichern</Button>
      </>}>
        <Field label="Ergebnis">
          <Select value={outcome} onChange={(e) => setOutcome(e.target.value as "processed" | "no_invoice")}>
            <option value="processed">Rechnung manuell erfasst</option>
            <option value="no_invoice">Keine Rechnung</option>
          </Select>
        </Field>
        <Field label="Vermerk"><Textarea rows={2} value={reason} onChange={(e) => setReason(e.target.value)} /></Field>
        <ErrorBox error={mutation.error} />
      </Modal>
    </li>
  );
}
