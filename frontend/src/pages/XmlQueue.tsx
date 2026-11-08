import { useMutation, useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { type DocumentItem, get, post, query } from "../api";
import { useApp } from "../app-context";
import { Button, Card, Empty, ErrorBox, Notice, PageHeader, Spinner, Table, td } from "../components/ui";
import { fmtBytes, fmtDateTime } from "../format";

export function XmlQueuePage() {
  const { companyId, companyName, isAdmin } = useApp();
  const list = useQuery({
    queryKey: ["xml-queue", companyId],
    queryFn: () => get<DocumentItem[]>(`/api/datev/xml-queue${query({ company_id: companyId })}`),
  });
  const handOver = useMutation({ mutationFn: (id: number) => post(`/api/datev/xml-queue/${id}/hand-over`) });

  return (
    <>
      <PageHeader title="XML-Warteschlange" subtitle="Reine XML-Rechnungen (XRechnung) – unverändert gespeichert" />
      <div className="mb-4">
        <Notice>
          DATEV Upload Mail ist für reine XML-Rechnungen nicht vorgesehen. Bis eine DATEV-Schnittstelle angebunden ist,
          bitte das Original herunterladen und manuell in DATEV Unternehmen online hochladen.
        </Notice>
      </div>
      <Card>
        <ErrorBox error={list.error ?? handOver.error} />
        {list.isLoading ? <Spinner /> : (list.data ?? []).length === 0 ? <Empty>Keine XML-Rechnungen in der Warteschlange.</Empty> : (
          <Table head={["Unternehmen", "Datei", "Größe", "Eingegangen", ""]}>
            {list.data!.map((d) => (
              <tr key={d.id}>
                <td className={td}>{companyName(d.company_id)}</td>
                <td className={td}><Link className="text-brand-700 hover:underline" to={`/documents/${d.id}`}>{d.original_filename ?? `Dokument ${d.id}`}</Link></td>
                <td className={`${td} num`}>{fmtBytes(d.size_bytes)}</td>
                <td className={td}>{fmtDateTime(d.created_at)}</td>
                <td className={`${td} whitespace-nowrap text-right`}>
                  <a href={`/api/documents/${d.id}/original`} download={d.original_filename ?? undefined}><Button>Original</Button></a>
                  {isAdmin && <Button className="ml-2" onClick={() => handOver.mutate(d.id)} disabled={handOver.isPending}>An DATEV übergeben</Button>}
                </td>
              </tr>
            ))}
          </Table>
        )}
      </Card>
    </>
  );
}
