import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { get, post, query, type Transmission, type TransmissionDetail } from "../api";
import { useApp } from "../app-context";
import { TransmissionActions } from "../components/TransmissionActions";
import { Badge, Button, Card, Empty, ErrorBox, Field, Modal, Notice, PageHeader, Select, Spinner, StatusBadge, Table, td } from "../components/ui";
import { fmtDateTime, SEND_MODE, TRANSMISSION_STATUS } from "../format";

interface BulkSendResult {
  queued: number[];
  failed: { transmission_id: number; error: string }[];
}

export function TransmissionsPage() {
  const { companyId, companyName, canReview } = useApp();
  const qc = useQueryClient();
  const [params, setParams] = useSearchParams();
  const status = params.get("status") ?? "";
  const includeTest = params.get("test") === "1";
  const openId = params.get("open");
  const setParam = (k: string, v: string | null) => {
    const next = new URLSearchParams(params);
    if (v) next.set(k, v);
    else next.delete(k);
    setParams(next, { replace: true });
  };
  const list = useQuery({
    queryKey: ["transmissions", companyId, status, includeTest],
    queryFn: () => get<Transmission[]>(`/api/datev/transmissions${query({ company_id: companyId, status, include_test: includeTest, limit: 300 })}`),
    refetchInterval: 30_000,
  });
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["transmissions"] });
    void qc.invalidateQueries({ queryKey: ["summary"] });
  };
  // Auswählbar: gesendete (→ bestätigen) sowie wartende/fehlgeschlagene (→ senden).
  const rows = (list.data ?? []).filter((t) => !t.test_mode);
  const confirmable = rows.filter((t) => t.status === "sent_unconfirmed");
  const sendable = rows.filter((t) => t.status === "pending" || t.status === "failed");
  const selectable = [...confirmable, ...sendable];
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const confirmIds = confirmable.map((t) => t.id).filter((id) => selected.has(id));
  const sendIds = sendable.map((t) => t.id).filter((id) => selected.has(id));
  const confirm = useMutation({
    mutationFn: (transmissionIds: number[]) =>
      post("/api/datev/transmissions/bulk-confirm", { transmission_ids: transmissionIds, note: "In DATEV geprüft" }),
    onSuccess: () => {
      setSelected((prev) => new Set([...prev].filter((id) => !confirmIds.includes(id))));
      refresh();
    },
  });
  const send = useMutation({
    mutationFn: (transmissionIds: number[]) =>
      post<BulkSendResult>("/api/datev/transmissions/bulk-send", { transmission_ids: transmissionIds }),
    onSuccess: (result) => {
      // Nicht sendbare bleiben markiert, damit der Grund sichtbar bleibt.
      setSelected((prev) => new Set([...prev].filter((id) => !result.queued.includes(id))));
      refresh();
    },
  });
  const toggle = (id: number) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  const selectedCount = confirmIds.length + sendIds.length;
  const allSelected = selectable.length > 0 && selectedCount === selectable.length;

  return (
    <>
      <PageHeader title="DATEV-Versand" subtitle="Versandprotokoll aller Übertragungen an DATEV Upload Mail" />
      <Card>
        <div className="mb-4 flex flex-wrap items-end gap-3">
          <div className="w-60">
            <Field label="Status">
              <Select value={status} onChange={(e) => setParam("status", e.target.value)}>
                <option value="">Alle</option>
                {Object.entries(TRANSMISSION_STATUS).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}
              </Select>
            </Field>
          </div>
          <label className="flex items-center gap-2 pb-1.5 text-sm text-slate-700">
            <input type="checkbox" checked={includeTest} onChange={(e) => setParam("test", e.target.checked ? "1" : null)} />
            Testläufe anzeigen
          </label>
          {canReview && selectable.length > 0 && (
            <div className="ml-auto flex flex-wrap gap-2">
              {sendable.length > 0 && (
                <Button variant="primary" disabled={sendIds.length === 0 || send.isPending} onClick={() => send.mutate(sendIds)}>
                  {sendIds.length > 0 ? `${sendIds.length} senden` : "Senden"}
                </Button>
              )}
              {confirmable.length > 0 && (
                <Button variant="primary" disabled={confirmIds.length === 0 || confirm.isPending}
                        onClick={() => confirm.mutate(confirmIds)}>
                  {confirmIds.length > 0 ? `${confirmIds.length} in DATEV bestätigen` : "In DATEV bestätigen"}
                </Button>
              )}
            </div>
          )}
        </div>
        <ErrorBox error={list.error} />
        <ErrorBox error={confirm.error} />
        <ErrorBox error={send.error} />
        {send.data && send.data.failed.length > 0 && (
          <div className="mb-3">
            <Notice tone="warning">
              {send.data.queued.length} zum Versand eingeplant, {send.data.failed.length} nicht möglich:{" "}
              {send.data.failed.map((f) => `#${f.transmission_id}: ${f.error}`).join(" · ")}
            </Notice>
          </div>
        )}
        {list.isLoading ? <Spinner /> : (list.data ?? []).length === 0 ? <Empty>Keine Übertragungen.</Empty> : (
          <Table head={[
            ...(canReview ? [<input key="all" type="checkbox" aria-label="Alle auswählen" checked={allSelected}
                                    disabled={selectable.length === 0}
                                    onChange={() => setSelected(allSelected ? new Set() : new Set(selectable.map((t) => t.id)))} />] : []),
            "#", "Unternehmen", "Lieferant", "Status", "Art", "Versuche", "Gesendet", "Hinweis", "",
          ]}>
            {list.data!.map((t) => (
              <tr key={t.id} className={selected.has(t.id) ? "bg-brand-50" : "hover:bg-slate-50"}>
                {canReview && (
                  <td className={td}>
                    {!t.test_mode && ["sent_unconfirmed", "pending", "failed"].includes(t.status) && (
                      <input type="checkbox" aria-label="Auswählen" checked={selected.has(t.id)} onChange={() => toggle(t.id)} />
                    )}
                  </td>
                )}
                <td className={`${td} num text-slate-500`}>{t.id}</td>
                <td className={td}>{companyName(t.company_id)}</td>
                <td className={td}>
                  <Link className="font-medium text-brand-700 hover:underline" to={`/documents/${t.document_id}`}>
                    {t.supplier_name ?? t.original_filename ?? `Dokument ${t.document_id}`}
                  </Link>
                  {t.invoice_number && <div className="text-xs text-slate-500">Nr. {t.invoice_number}</div>}
                </td>
                <td className={td}><StatusBadge map={TRANSMISSION_STATUS} value={t.status} /></td>
                <td className={td}>{SEND_MODE[t.send_mode] ?? t.send_mode}{t.test_mode && <> <Badge tone="info">Test</Badge></>}</td>
                <td className={`${td} num`}>{t.attempts}/{t.max_attempts}</td>
                <td className={`${td} whitespace-nowrap`}>{fmtDateTime(t.sent_at)}</td>
                <td className={`${td} max-w-xs text-xs text-slate-600`}>{t.last_error}</td>
                <td className={td}><Button variant="ghost" onClick={() => setParam("open", String(t.id))}>Details</Button></td>
              </tr>
            ))}
          </Table>
        )}
      </Card>
      {openId && <TransmissionDialog id={openId} onClose={() => setParam("open", null)} onChanged={refresh} />}
    </>
  );
}

function TransmissionDialog({ id, onClose, onChanged }: { id: string; onClose: () => void; onChanged: () => void }) {
  const detail = useQuery({ queryKey: ["transmission", id], queryFn: () => get<TransmissionDetail>(`/api/datev/transmissions/${id}`) });
  const qc = useQueryClient();
  const t = detail.data;
  return (
    <Modal open title={`Übertragung #${id}`} onClose={onClose} footer={<Button onClick={onClose}>Schließen</Button>}>
      {!t ? <Spinner /> : (
        <div className="space-y-3 text-sm">
          <div className="flex flex-wrap gap-2">
            <StatusBadge map={TRANSMISSION_STATUS} value={t.status} />
            <Badge>{SEND_MODE[t.send_mode] ?? t.send_mode}</Badge>
          </div>
          <div className="grid grid-cols-[8rem_1fr] gap-y-1">
            <span className="text-slate-500">Ziel</span><span className="break-all">{t.destination_address ?? "–"}</span>
            <span className="text-slate-500">Message-ID</span><span className="break-all text-xs">{t.smtp_message_id ?? "–"}</span>
            <span className="text-slate-500">Gesendet</span><span>{fmtDateTime(t.sent_at)}</span>
            <span className="text-slate-500">Bestätigt</span><span>{fmtDateTime(t.confirmed_at)}</span>
            <span className="text-slate-500">Nächster Versuch</span><span>{fmtDateTime(t.next_attempt_at)}</span>
          </div>
          {t.last_error && <p className="rounded bg-slate-50 p-2 text-xs text-slate-700">{t.last_error}</p>}
          <div>
            <div className="mb-1 font-medium">Versuche</div>
            {t.attempts_log.length === 0 ? <p className="text-slate-500">Noch keine.</p> : (
              <ol className="space-y-1 text-xs">
                {t.attempts_log.map((a) => (
                  <li key={a.attempt_no} className="rounded border border-slate-100 p-2">
                    <b>#{a.attempt_no}</b> {fmtDateTime(a.started_at)} · {a.outcome ?? "läuft/unklar"}
                    {a.smtp_response_code ? ` · SMTP ${a.smtp_response_code}` : ""} {a.smtp_response ?? ""}
                    {a.error_message && <div className="text-rose-700">{a.error_message}</div>}
                  </li>
                ))}
              </ol>
            )}
          </div>
          {!t.test_mode && <TransmissionActions t={t} onDone={() => {
            onChanged();
            void qc.invalidateQueries({ queryKey: ["transmission", id] });
          }} />}
        </div>
      )}
      <ErrorBox error={detail.error} />
    </Modal>
  );
}
