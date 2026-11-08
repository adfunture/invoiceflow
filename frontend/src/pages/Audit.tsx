import { useInfiniteQuery } from "@tanstack/react-query";
import { useState } from "react";
import { type AuditEntry, get, query } from "../api";
import { useApp } from "../app-context";
import { Button, Card, ErrorBox, Field, PageHeader, Select, Spinner, Table, td } from "../components/ui";
import { fmtDateTime } from "../format";

const ENTITY: Record<string, string> = {
  "": "Alle", document: "Dokumente", datev_transmission: "DATEV-Versand", datev_destination: "DATEV-Ziele",
  mailbox: "Postfächer", setting: "Einstellungen", email_message: "E-Mails", company: "Unternehmen", user: "Benutzer",
};

export function AuditPage() {
  const { companyId, companyName } = useApp();
  const [entity, setEntity] = useState("");
  const log = useInfiniteQuery({
    queryKey: ["audit", companyId, entity],
    initialPageParam: null as string | null,
    queryFn: ({ pageParam }) =>
      get<AuditEntry[]>(`/api/audit-logs${query({ company_id: companyId, entity_type: entity, before: pageParam, limit: 100 })}`),
    getNextPageParam: (last) => (last.length === 100 ? last[last.length - 1].occurred_at : null),
  });
  const rows = log.data?.pages.flat() ?? [];
  return (
    <>
      <PageHeader title="Protokoll" subtitle="Unveränderliches Audit-Log aller Änderungen und Versände" />
      <Card>
        <div className="mb-4 w-60">
          <Field label="Bereich">
            <Select value={entity} onChange={(e) => setEntity(e.target.value)}>
              {Object.entries(ENTITY).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </Select>
          </Field>
        </div>
        <ErrorBox error={log.error} />
        {log.isLoading ? <Spinner /> : (
          <Table head={["Zeitpunkt", "Wer", "Aktion", "Objekt", "Unternehmen", "Details"]}>
            {rows.map((e) => (
              <tr key={e.id}>
                <td className={`${td} whitespace-nowrap`}>{fmtDateTime(e.occurred_at)}</td>
                <td className={td}>{e.actor_label ?? e.actor_type}</td>
                <td className={`${td} font-mono text-xs`}>{e.action}</td>
                <td className={td}>{e.entity_type ? `${ENTITY[e.entity_type] ?? e.entity_type} ${e.entity_id ?? ""}` : "–"}</td>
                <td className={td}>{e.company_id ? companyName(e.company_id) : "–"}</td>
                <td className={`${td} max-w-md`}><code className="block truncate text-xs text-slate-600" title={JSON.stringify(e.details)}>{JSON.stringify(e.details)}</code></td>
              </tr>
            ))}
          </Table>
        )}
        {log.hasNextPage && <div className="mt-4 text-center"><Button onClick={() => void log.fetchNextPage()}>Ältere laden</Button></div>}
      </Card>
    </>
  );
}
