import { Link } from "react-router-dom";
import type { DocumentItem } from "../api";
import { useApp } from "../app-context";
import { DOCUMENT_STATUS, DOCUMENT_TYPE, fmtDate, fmtMoney } from "../format";
import { Empty, StatusBadge, Table, td } from "./ui";

export interface DocumentSelection {
  selected: Set<number>;
  toggle: (id: number) => void;
  toggleAll: () => void;
}

export function DocumentTable({ docs, compact = false, selection }: {
  docs: DocumentItem[];
  compact?: boolean;
  selection?: DocumentSelection;
}) {
  const { companyName, companies } = useApp();
  if (docs.length === 0) return <Empty>Keine Dokumente gefunden.</Empty>;
  const showCompany = companies.length > 1;
  const allSelected = selection !== undefined && docs.every((d) => selection.selected.has(d.id));
  return (
    <Table head={[
      ...(selection ? [<input key="all" type="checkbox" aria-label="Alle auswählen" checked={allSelected}
                              onChange={selection.toggleAll} />] : []),
      ...(showCompany ? ["Unternehmen"] : []), "Lieferant", "Rechnungsnr.", "Datum", "Betrag",
      ...(compact ? [] : ["Postfach", "Typ"]), "Status",
    ]}>
      {docs.map((d) => (
        <tr key={d.id} className={selection?.selected.has(d.id) ? "bg-brand-50" : "hover:bg-slate-50"}>
          {selection && (
            <td className={td}>
              <input type="checkbox" aria-label="Auswählen" checked={selection.selected.has(d.id)}
                     onChange={() => selection.toggle(d.id)} />
            </td>
          )}
          {showCompany && <td className={td}>{companyName(d.company_id)}</td>}
          <td className={td}>
            <Link to={`/documents/${d.id}`} className="font-medium text-slate-900 hover:text-brand-700 hover:underline">
              {d.invoice?.supplier_name ?? d.original_filename ?? `Dokument ${d.id}`}
            </Link>
          </td>
          <td className={`${td} num`}>{d.invoice?.invoice_number ?? "–"}</td>
          <td className={`${td} num whitespace-nowrap`}>{fmtDate(d.invoice?.invoice_date)}</td>
          <td className={`${td} num whitespace-nowrap text-right`}>{fmtMoney(d.invoice?.gross_amount, d.invoice?.currency)}</td>
          {!compact && <td className={`${td} text-slate-600`}>{d.mailbox_email}</td>}
          {!compact && <td className={td}>{DOCUMENT_TYPE[d.document_type] ?? d.document_type}</td>}
          <td className={td}>
            <StatusBadge map={DOCUMENT_STATUS} value={d.status} />
          </td>
        </tr>
      ))}
    </Table>
  );
}
