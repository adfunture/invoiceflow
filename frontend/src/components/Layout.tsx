import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { get, query, type Summary } from "../api";
import { useApp } from "../app-context";
import { Select } from "./ui";

interface NavItem {
  to: string;
  label: string;
  count?: (s: Summary) => number;
  adminOnly?: boolean;
}

const NAV: NavItem[] = [
  { to: "/", label: "Übersicht" },
  { to: "/documents", label: "Rechnungen" },
  { to: "/review", label: "Prüfung", count: (s) => s.manual_review_documents + s.manual_review_messages },
  { to: "/transmissions", label: "DATEV-Versand", count: (s) => s.manual_review_transmissions },
  { to: "/xml-queue", label: "XML-Warteschlange", count: (s) => s.xml_queue },
  { to: "/mailboxes", label: "Postfächer", count: (s) => s.mailboxes_with_errors },
  { to: "/datev", label: "DATEV-Einstellungen" },
  { to: "/settings", label: "Einstellungen" },
  { to: "/audit", label: "Protokoll", adminOnly: true },
];

export function Layout() {
  const { session, companies, companyId, setCompanyId, logout, isAdmin } = useApp();
  const [open, setOpen] = useState(false);
  const { data: summary } = useQuery({
    queryKey: ["summary", companyId],
    queryFn: () => get<Summary>(`/api/dashboard/summary${query({ company_id: companyId })}`),
    refetchInterval: 60_000,
  });

  const nav = (
    <nav className="space-y-0.5" aria-label="Hauptnavigation">
      {NAV.filter((n) => !n.adminOnly || isAdmin).map((item) => {
        const n = summary && item.count ? item.count(summary) : 0;
        return (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.to === "/"}
            onClick={() => setOpen(false)}
            className={({ isActive }) =>
              `flex items-center justify-between rounded-md px-3 py-2 text-sm font-medium ${
                isActive ? "bg-brand-50 text-brand-800" : "text-slate-700 hover:bg-slate-100"
              }`
            }
          >
            <span>{item.label}</span>
            {n > 0 && <span className="num rounded-full bg-amber-100 px-2 text-xs font-semibold text-amber-900">{n}</span>}
          </NavLink>
        );
      })}
    </nav>
  );

  return (
    <div className="min-h-full lg:flex">
      <aside className={`${open ? "block" : "hidden"} border-b border-slate-200 bg-white px-3 py-4 lg:block lg:min-h-screen lg:w-60 lg:shrink-0 lg:border-r lg:border-b-0`}>
        <div className="mb-5 hidden items-center gap-2 px-3 lg:flex">
          <span className="grid h-7 w-7 place-items-center rounded-md bg-brand-700 text-xs font-bold text-white">IF</span>
          <span className="font-semibold">InvoiceFlow</span>
        </div>
        {nav}
      </aside>
      <div className="min-w-0 flex-1">
        <header className="sticky top-0 z-30 flex flex-wrap items-center gap-3 border-b border-slate-200 bg-white/95 px-4 py-2.5 backdrop-blur">
          <button type="button" className="rounded-md p-1.5 text-slate-700 hover:bg-slate-100 lg:hidden" onClick={() => setOpen(!open)}
            aria-label="Menü" aria-expanded={open}>
            <svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M3 5h14M3 10h14M3 15h14" /></svg>
          </button>
          <span className="font-semibold lg:hidden">InvoiceFlow</span>
          <div className="w-52">
            <Select aria-label="Unternehmen" value={companyId ?? ""} onChange={(e) => setCompanyId(e.target.value ? Number(e.target.value) : null)}>
              {companies.length > 1 && <option value="">Alle Unternehmen</option>}
              {companies.map((c) => <option key={c.id} value={c.id}>{c.short_name}</option>)}
            </Select>
          </div>
          {summary?.test_mode_hint && (
            <span className="hidden rounded-md bg-sky-50 px-2 py-1 text-xs text-sky-800 ring-1 ring-sky-200 md:inline">
              Kein Echtversand an DATEV
            </span>
          )}
          <div className="ml-auto flex items-center gap-3 text-sm">
            <span className="hidden text-slate-600 sm:inline">{session?.user.display_name}</span>
            <button type="button" onClick={() => void logout()} className="rounded-md px-2 py-1 text-slate-700 hover:bg-slate-100">
              Abmelden
            </button>
          </div>
        </header>
        <main className="mx-auto max-w-7xl px-4 py-6">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
