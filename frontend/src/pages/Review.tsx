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
          {docs.isLoading ? <Spinner /> : <DocumentTable docs={docs.data ?? []} />}
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
