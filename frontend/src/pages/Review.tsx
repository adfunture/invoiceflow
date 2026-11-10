import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { type DocumentItem, type EmailMessageItem, get, post, query } from "../api";
import { useApp } from "../app-context";
import { DocumentTable } from "../components/DocumentTable";
import { Button, Card, Empty, ErrorBox, Field, Modal, Notice, PageHeader, Spinner, Textarea } from "../components/ui";
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
          {messages.isLoading ? <Spinner /> : <ReviewMessages messages={messages.data ?? []} />}
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
  // Neu erkennen: z. B. nachdem KI-Schlüssel oder OCR eingerichtet wurden. Die Belege verlassen die Liste,
  // bis der Processing-Worker sie neu bewertet hat.
  const reprocess = useMutation({
    mutationFn: () => post<{ queued: number[]; failed: { document_id: number; error: string }[] }>(
      "/api/documents/bulk-reprocess", { document_ids: ids }),
    onSuccess: (result) => {
      setSelected(new Set(result.failed.map((f) => f.document_id)));
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
          <Button disabled={ids.length === 0 || reprocess.isPending} onClick={() => reprocess.mutate()}
            title="Erkennung erneut ausführen (Regeln, OCR, KI-Fallback)">Neu erkennen</Button>
          {ids.length > 0 && <button className="text-slate-500 hover:underline" onClick={() => setSelected(new Set())}>Auswahl aufheben</button>}
        </div>
      )}
      {reprocess.data && (
        <div className="mb-3">
          <Notice tone={reprocess.data.failed.length ? "warning" : "info"}>
            {reprocess.data.queued.length} Beleg(e) werden neu erkannt
            {reprocess.data.failed.length > 0 && <>, {reprocess.data.failed.length} nicht möglich:{" "}
              {reprocess.data.failed.map((f) => `#${f.document_id} (${f.error})`).join(", ")}</>}
          </Notice>
        </div>
      )}
      <ErrorBox error={reprocess.error} />
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

function ReviewMessages({ messages }: { messages: EmailMessageItem[] }) {
  const { companyName, canReview, isAdmin } = useApp();
  const qc = useQueryClient();
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const ids = messages.map((m) => m.id).filter((id) => selected.has(id));
  // Absenderregeln: "block" = nichts mehr importieren, "attachment_only" = nur noch Mails mit Anhang.
  type SenderMode = "block" | "attachment_only";
  const block = useMutation({
    mutationFn: ({ messageIds, mode }: { messageIds: number[]; mode: SenderMode }) =>
      post<{ blocked: string[]; resolved: number[] }>("/api/email-messages/block-senders", { message_ids: messageIds, mode }),
    onSuccess: () => {
      setSelected(new Set());
      void qc.invalidateQueries({ queryKey: ["messages"] });
      void qc.invalidateQueries({ queryKey: ["summary"] });
      void qc.invalidateQueries({ queryKey: ["settings"] });
    },
  });
  const confirmBlock = (messageIds: number[], mode: SenderMode) => {
    const senders = [...new Set(messages.filter((m) => messageIds.includes(m.id) && m.from_address)
      .map((m) => m.from_address!.toLowerCase()))];
    if (senders.length === 0) return;
    const list = senders.join("\n");
    const text = mode === "block"
      ? "Diese Absender blockieren? Ihre Mails werden künftig gar nicht mehr importiert – auch nicht mit "
        + `PDF-Rechnung (alle Postfächer):\n\n${list}\n\nAufheben unter Einstellungen → Blockierte Absender.`
      : "Von diesen Absendern nur noch Mails mit Anhang importieren? Benachrichtigungen ohne Anhang werden "
        + `übersprungen, PDF-Rechnungen kommen weiter an (alle Postfächer):\n\n${list}\n\n`
        + "Aufheben unter Einstellungen → Absender nur mit Anhang.";
    if (!window.confirm(text)) return;
    block.mutate({ messageIds, mode });
  };

  // Ein Klick genügt: "Erledigt" schließt die E-Mail ab, der Vermerk wird automatisch protokolliert.
  const mutation = useMutation({
    mutationFn: (messageIds: number[]) =>
      post("/api/email-messages/bulk-resolve", { message_ids: messageIds, outcome: "no_invoice", reason: "Manuell geprüft" }),
    onSuccess: () => {
      setSelected(new Set());
      void qc.invalidateQueries({ queryKey: ["messages"] });
      void qc.invalidateQueries({ queryKey: ["summary"] });
    },
  });
  const toggle = (id: number) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  if (messages.length === 0) return <Empty>Keine offenen E-Mails.</Empty>;

  return (
    <>
      {canReview && (
        <div className="mb-2 flex flex-wrap items-center gap-3 text-sm">
          <label className="flex items-center gap-2 text-slate-600">
            <input type="checkbox" checked={ids.length === messages.length}
                   onChange={() => setSelected(ids.length === messages.length ? new Set() : new Set(messages.map((m) => m.id)))} />
            Alle auswählen
          </label>
          <Button disabled={ids.length === 0 || mutation.isPending} onClick={() => mutation.mutate(ids)}>
            {ids.length > 0 ? `${ids.length} erledigt` : "Erledigt"}
          </Button>
          {isAdmin && <>
            <Button disabled={ids.length === 0 || block.isPending} onClick={() => confirmBlock(ids, "attachment_only")}
              title="Benachrichtigungen ohne Anhang dieser Absender künftig überspringen">
              Nur mit Anhang
            </Button>
            <Button disabled={ids.length === 0 || block.isPending} onClick={() => confirmBlock(ids, "block")}
              title="Mails dieser Absender künftig gar nicht mehr importieren">
              Absender blockieren
            </Button>
          </>}
        </div>
      )}
      <ErrorBox error={mutation.error} />
      <ErrorBox error={block.error} />
      <ul className="divide-y divide-slate-100">
        {messages.map((m) => (
          <li key={m.id} className="flex gap-3 py-3 text-sm">
            {canReview && <input type="checkbox" className="mt-1" aria-label="Auswählen" checked={selected.has(m.id)}
                                 onChange={() => toggle(m.id)} />}
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div className="min-w-0">
                  <div className="font-medium">{m.subject || "(ohne Betreff)"}</div>
                  <div className="text-slate-500">{m.from_address} · {fmtDateTime(m.received_at)} · {companyName(m.company_id)}</div>
                </div>
                <div className="flex gap-2">
                  {isAdmin && m.from_address && (
                    <Button variant="ghost" disabled={block.isPending} onClick={() => confirmBlock([m.id], "attachment_only")}
                      title="Benachrichtigungen ohne Anhang dieses Absenders künftig überspringen">Nur mit Anhang</Button>
                  )}
                  {canReview && <Button disabled={mutation.isPending} onClick={() => mutation.mutate([m.id])}>Erledigt</Button>}
                </div>
              </div>
              {m.review_reason && <div className="mt-2"><Notice tone="warning">{m.review_reason}</Notice></div>}
              {m.detected_invoice_links.length > 0 && (
                <ul className="mt-2 space-y-0.5 text-xs">
                  {m.detected_invoice_links.map((l) => (
                    <li key={l} className="break-all font-mono text-slate-600">{l}</li>
                  ))}
                </ul>
              )}
            </div>
          </li>
        ))}
      </ul>
    </>
  );
}
