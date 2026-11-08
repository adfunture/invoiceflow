import { Link } from "react-router-dom";
import type { DocumentItem } from "../api";
import { useApp } from "../app-context";
import { DOCUMENT_STATUS, DOCUMENT_TYPE, fmtDate, fmtMoney } from "../format";
import { Empty, StatusBadge, Table, td } from "./ui";

export function DocumentTable({ docs, compact = false }: { docs: DocumentItem[]; compact?: boolean }) {
  const { companyName, companies } = useApp();
  if (docs.length === 0) return <Empty>Keine Dokumente gefunden.</Empty>;
  const showCompany = companies.length > 1;
  return (
    <Table head={[
      ...(showCompany ? ["Unternehmen"] : []), "Lieferant", "Rechnungsnr.", "Datum", "Betrag",
      ...(compact ? [] : ["Postfach", "Typ"]), "Status",
    ]}>
      {docs.map((d) => (
        <tr key={d.id} className="hover:bg-slate-50">
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
