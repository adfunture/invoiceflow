import { useInfiniteQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { type DocumentItem, get, query } from "../api";
import { useApp } from "../app-context";
import { DocumentTable } from "../components/DocumentTable";
import { Button, Card, ErrorBox, Field, Input, PageHeader, Select, Spinner } from "../components/ui";
import { DOCUMENT_STATUS, DOCUMENT_TYPE } from "../format";

const PAGE = 50;

export function DocumentsPage() {
  const { companyId } = useApp();
  const [params, setParams] = useSearchParams();
  const filters = {
    status: params.get("status") ?? "",
    document_type: params.get("document_type") ?? "",
    supplier: params.get("supplier") ?? "",
    date_from: params.get("date_from") ?? "",
    date_to: params.get("date_to") ?? "",
  };
  const set = (key: keyof typeof filters, value: string) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    setParams(next, { replace: true });
  };

  const docs = useInfiniteQuery({
    queryKey: ["documents", companyId, filters],
    initialPageParam: null as number | null,
    queryFn: ({ pageParam }) => {
      // Standard: ignorierte ausblenden. "all" zeigt wirklich alle.
      const { status, ...rest } = filters;
      const statusParams = status === "all" ? {} : status ? { status } : { hide_ignored: true };
      return get<DocumentItem[]>(`/api/documents${query({ company_id: companyId, ...rest, ...statusParams, before_id: pageParam, limit: PAGE })}`);
    },
    getNextPageParam: (last) => (last.length === PAGE ? last[last.length - 1].id : null),
  });
  const rows = docs.data?.pages.flat() ?? [];

  return (
    <>
      <PageHeader title="Rechnungen" subtitle="Alle erkannten Dokumente aus den Postfächern" />
      <Card>
        <div className="mb-4 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-5">
          <Field label="Status">
            <Select value={filters.status} onChange={(e) => set("status", e.target.value)}>
              <option value="">Alle außer ignorierte</option>
              <option value="all">Alle (inkl. ignorierte)</option>
              {Object.entries(DOCUMENT_STATUS).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}
            </Select>
          </Field>
          <Field label="Dokumenttyp">
            <Select value={filters.document_type} onChange={(e) => set("document_type", e.target.value)}>
              <option value="">Alle</option>
              {Object.entries(DOCUMENT_TYPE).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </Select>
          </Field>
          <Field label="Lieferant">
            <Input type="search" placeholder="Name enthält …" defaultValue={filters.supplier}
              onKeyDown={(e) => e.key === "Enter" && set("supplier", e.currentTarget.value)}
              onBlur={(e) => set("supplier", e.target.value)} />
          </Field>
          <Field label="Rechnungsdatum von">
            <Input type="date" value={filters.date_from} onChange={(e) => set("date_from", e.target.value)} />
          </Field>
          <Field label="bis">
            <Input type="date" value={filters.date_to} onChange={(e) => set("date_to", e.target.value)} />
          </Field>
        </div>
        <ErrorBox error={docs.error} />
        {docs.isLoading ? <Spinner /> : <DocumentTable docs={rows} />}
        {docs.hasNextPage && (
          <div className="mt-4 text-center">
            <Button onClick={() => void docs.fetchNextPage()} disabled={docs.isFetchingNextPage}>
              {docs.isFetchingNextPage ? "Lädt …" : "Weitere laden"}
            </Button>
          </div>
        )}
      </Card>
    </>
  );
}
