import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { type DocumentItem, get, query, type Summary, type WorkerInfo } from "../api";
import { useApp } from "../app-context";
import { DocumentTable } from "../components/DocumentTable";
import { Card, ErrorBox, Notice, PageHeader, Spinner } from "../components/ui";
import { fmtDateTime } from "../format";

function Stat({ label, value, to, tone = "default", hint }: {
  label: string; value: number; to?: string; tone?: "default" | "warning" | "danger"; hint?: string;
}) {
  const color = tone === "danger" && value > 0 ? "text-rose-700" : tone === "warning" && value > 0 ? "text-amber-700" : "text-slate-900";
  const body = (
    <div className="h-full rounded-lg border border-slate-200 bg-white px-4 py-3 shadow-xs transition hover:border-slate-300">
      <div className="text-xs font-medium text-slate-500">{label}</div>
      <div className={`num mt-1 text-2xl font-semibold ${color}`}>{value}</div>
      {hint && <div className="mt-0.5 text-xs text-slate-500">{hint}</div>}
    </div>
  );
  return to ? <Link to={to} className="block">{body}</Link> : body;
}

export function DashboardPage() {
  const { companyId } = useApp();
  const summary = useQuery({
    queryKey: ["summary", companyId],
    queryFn: () => get<Summary>(`/api/dashboard/summary${query({ company_id: companyId })}`),
    refetchInterval: 30_000,
  });
  const recent = useQuery({
    queryKey: ["documents", "recent", companyId],
    queryFn: () => get<DocumentItem[]>(`/api/documents${query({ company_id: companyId, limit: 10 })}`),
  });
  const workers = useQuery({ queryKey: ["workers"], queryFn: () => get<WorkerInfo[]>("/api/workers"), refetchInterval: 60_000 });
  const alerts = useQuery({
    queryKey: ["alerts"],
    queryFn: () => get<{ key: string; severity: string; message: string; first_seen_at: string }[]>("/api/dashboard/alerts"),
    refetchInterval: 60_000,
  });
  const s = summary.data;

  return (
    <>
      <PageHeader title="Übersicht" subtitle="Rechnungseingang und DATEV-Versand auf einen Blick" />
      <ErrorBox error={summary.error} />
      {!s ? <Spinner /> : (
        <div className="space-y-5">
          {s.test_mode_hint && <Notice>{s.test_mode_hint}. Das ist bis zur Produktivfreigabe so gewollt.</Notice>}
          {(alerts.data ?? []).map((a) => (
            <Notice key={a.key} tone={a.severity === "critical" ? "danger" : "warning"}>
              {a.message} <span className="text-xs opacity-75">· seit {fmtDateTime(a.first_seen_at)}</span>
            </Notice>
          ))}
          {s.stale_workers.length > 0 && (
            <Notice tone="danger">Hintergrunddienst meldet sich nicht: {s.stale_workers.join(", ")}</Notice>
          )}
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <Stat label="Heute erkannte Rechnungen" value={s.recognized_today} to="/documents" />
            <Stat label="An DATEV gesendet" value={s.sent_total} hint={`davon heute ${s.sent_today}`} to="/transmissions" />
            <Stat label="Unbestätigte DATEV-Übertragungen" value={s.unconfirmed} tone="warning" to="/transmissions?status=sent_unconfirmed" />
            <Stat label="Wartet auf Versand" value={s.awaiting_send} to="/transmissions?status=pending" />
            <Stat label="Zur manuellen Prüfung" value={s.manual_review_documents + s.manual_review_messages} tone="warning" to="/review"
              hint={`${s.manual_review_documents} ${s.manual_review_documents === 1 ? "Dokument" : "Dokumente"} · ${s.manual_review_messages} ${s.manual_review_messages === 1 ? "E-Mail" : "E-Mails"}`} />
            <Stat label="Unklarer Versand" value={s.manual_review_transmissions} tone="danger" to="/transmissions?status=manual_review" />
            <Stat label="Dubletten" value={s.duplicates} to="/documents?status=duplicate" />
            <Stat label="Fehler" value={s.errors} tone="danger" to="/transmissions?status=failed" />
            <Stat label="XML-Warteschlange" value={s.xml_queue} to="/xml-queue" />
            <Stat label="Aktive Postfächer" value={s.mailboxes_active} to="/mailboxes" />
            <Stat label="Postfächer mit Abruffehler" value={s.mailboxes_with_errors} tone="danger" to="/mailboxes" />
          </div>
          <Card title="Zuletzt eingegangen" actions={<Link className="text-sm text-brand-700 hover:underline" to="/documents">Alle anzeigen</Link>}>
            {recent.isLoading ? <Spinner /> : <DocumentTable docs={recent.data ?? []} compact />}
          </Card>
          <Card title="Hintergrunddienste">
            {(workers.data ?? []).length === 0 ? (
              <p className="text-sm text-slate-500">Noch kein Worker hat sich gemeldet.</p>
            ) : (
              <ul className="divide-y divide-slate-100 text-sm">
                {workers.data!.map((w) => (
                  <li key={w.worker_id} className="flex flex-wrap justify-between gap-2 py-1.5">
                    <span className="font-medium">{w.worker_type}</span>
                    <span className={w.stale ? "text-rose-700" : "text-slate-500"}>
                      {w.stale ? "keine Rückmeldung seit " : "aktiv · "}{fmtDateTime(w.last_beat_at)}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>
      )}
    </>
  );
}
