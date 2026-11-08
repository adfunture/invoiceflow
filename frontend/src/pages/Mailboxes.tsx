import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { type ConnectionTest, get, type Mailbox, patch, post, query } from "../api";
import { useApp } from "../app-context";
import { Badge, Button, Card, DescList, ErrorBox, Field, Input, Modal, Notice, PageHeader, Select, Spinner } from "../components/ui";
import { fmtDateTime } from "../format";

export function MailboxesPage() {
  const { companyId } = useApp();
  const list = useQuery({
    queryKey: ["mailboxes", companyId],
    queryFn: () => get<Mailbox[]>(`/api/mailboxes${query({ company_id: companyId })}`),
    refetchInterval: 30_000,
  });
  return (
    <>
      <PageHeader title="Postfächer" subtitle="Abruf alle 5 Minuten · alle Ordner inkl. Unterordner · nur lesend · ab 08.11.2026" />
      <ErrorBox error={list.error} />
      {list.isLoading ? <Spinner /> : (
        <div className="grid gap-4 lg:grid-cols-2">
          {(list.data ?? []).map((m) => <MailboxCard key={m.id} m={m} />)}
        </div>
      )}
    </>
  );
}

function MailboxCard({ m }: { m: Mailbox }) {
  const { companyName, isAdmin } = useApp();
  const qc = useQueryClient();
  const [edit, setEdit] = useState(false);
  const [verify, setVerify] = useState(false);
  const [test, setTest] = useState<ConnectionTest | null>(null);
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["mailboxes"] });
    void qc.invalidateQueries({ queryKey: ["summary"] });
  };
  const testMut = useMutation({ mutationFn: () => post<ConnectionTest>(`/api/mailboxes/${m.id}/test-connection`), onSuccess: setTest });
  const activeMut = useMutation({ mutationFn: (active: boolean) => post(`/api/mailboxes/${m.id}/active`, { active }), onSuccess: refresh });
  const syncMut = useMutation({ mutationFn: () => post(`/api/mailboxes/${m.id}/sync-now`), onSuccess: refresh });
  const domain = m.email_address.split("@")[1];

  const status = m.is_active
    ? m.consecutive_failures > 0 ? <Badge tone="danger">Abruffehler</Badge> : <Badge tone="success">Aktiv</Badge>
    : <Badge>Inaktiv</Badge>;

  return (
    <Card title={<span className="flex items-center gap-2">{m.email_address} {status}</span>}
      actions={isAdmin && <>
        <Button onClick={() => testMut.mutate()} disabled={testMut.isPending}>{testMut.isPending ? "Teste …" : "Verbindung testen"}</Button>
        <Button onClick={() => setEdit(true)}>Bearbeiten</Button>
      </>}>
      <DescList items={[
        ["Unternehmen", companyName(m.company_id)],
        ["IMAP", m.imap_host ? `${m.imap_host}:${m.imap_port} (${m.imap_tls_mode === "ssl" ? "SSL/TLS" : "STARTTLS"})` : <span className="text-amber-700">nicht konfiguriert</span>],
        ["Benutzer", m.imap_username],
        ["Zugangsdaten", <span>{m.credential_ref} {m.credential_configured ? <Badge tone="success">gesetzt</Badge> : <Badge tone="warning">fehlt</Badge>}</span>],
        ["Domain", m.domain_verified_at ? `${domain} bestätigt` : <span className="text-amber-700">{domain} nicht bestätigt</span>],
        ["Letzter Abruf", fmtDateTime(m.last_successful_sync_at)],
        ["Ordner", `${m.folders.filter((f) => f.is_enabled && !f.missing_since).length} überwacht, `
          + `${m.folders.filter((f) => !f.is_enabled).length} ausgeschlossen`],
      ]} />
      <FolderList m={m} editable={isAdmin} onChanged={refresh} />
      {m.last_error && <div className="mt-3"><Notice tone="danger">{fmtDateTime(m.last_error_at)}: {m.last_error}</Notice></div>}
      {m.activation_blockers.length > 0 && (
        <div className="mt-3"><Notice tone="warning">
          Vor der Aktivierung offen: <ul className="mt-1 list-disc pl-5">{m.activation_blockers.map((b) => <li key={b}>{b}</li>)}</ul>
        </Notice></div>
      )}
      {test && (
        <div className="mt-3"><Notice tone={test.ok ? "success" : "danger"}>
          {test.ok ? `Verbindung OK · INBOX: ${test.inbox_messages} Nachrichten · Ordner: ${test.folders.join(", ")}` : test.error}
        </Notice></div>
      )}
      <ErrorBox error={testMut.error ?? activeMut.error ?? syncMut.error} />
      {isAdmin && (
        <div className="mt-4 flex flex-wrap gap-2">
          {!m.domain_verified_at && <Button onClick={() => setVerify(true)}>Domain bestätigen</Button>}
          {m.is_active ? <>
            <Button onClick={() => syncMut.mutate()} disabled={syncMut.isPending}>Jetzt abrufen</Button>
            <Button variant="danger" onClick={() => activeMut.mutate(false)}>Deaktivieren</Button>
          </> : (
            <Button variant="primary" disabled={m.activation_blockers.length > 0} onClick={() => activeMut.mutate(true)}>Aktivieren</Button>
          )}
        </div>
      )}
      {edit && <EditMailbox m={m} onClose={() => setEdit(false)} onSaved={() => { setEdit(false); refresh(); }} />}
      {verify && <VerifyDomain m={m} domain={domain} onClose={() => setVerify(false)} onSaved={() => { setVerify(false); refresh(); }} />}
    </Card>
  );
}

function FolderList({ m, editable, onChanged }: { m: Mailbox; editable: boolean; onChanged: () => void }) {
  const [open, setOpen] = useState(false);
  const toggle = useMutation({
    mutationFn: (f: { folder_name: string; enabled: boolean }) => post(`/api/mailboxes/${m.id}/folders`, f),
    onSuccess: onChanged,
  });
  const sorted = [...m.folders].sort((a, b) => a.folder_name.localeCompare(b.folder_name, "de"));
  return (
    <div className="mt-3">
      <button type="button" className="text-sm text-brand-700 hover:underline" onClick={() => setOpen(!open)} aria-expanded={open}>
        {open ? "Ordner ausblenden" : `Alle ${m.folders.length} Ordner anzeigen`}
      </button>
      {open && (
        <ul className="mt-2 divide-y divide-slate-100 rounded-md border border-slate-200 text-sm">
          {sorted.map((f) => (
            <li key={f.id} className="flex flex-wrap items-center gap-2 px-3 py-1.5">
              <span className="min-w-0 flex-1 break-all font-mono text-xs">{f.folder_name}</span>
              {f.missing_since ? <Badge tone="danger">fehlt auf dem Server</Badge>
                : f.is_enabled ? <Badge tone="success">überwacht</Badge>
                : <Badge tone="neutral">ausgeschlossen</Badge>}
              {f.exclusion_reason && <span className="w-full text-xs text-slate-500">{f.exclusion_reason}</span>}
              {f.last_error && <span className="w-full text-xs text-rose-700">{f.last_error}</span>}
              {editable && !f.missing_since && (
                <Button variant="ghost" disabled={toggle.isPending} onClick={() => {
                  if (!f.is_enabled && f.exclusion_reason
                    && !window.confirm(`„${f.folder_name}“ wurde automatisch ausgeschlossen: ${f.exclusion_reason}. Trotzdem überwachen?`)) return;
                  toggle.mutate({ folder_name: f.folder_name, enabled: !f.is_enabled });
                }}>{f.is_enabled ? "Ausschließen" : "Überwachen"}</Button>
              )}
            </li>
          ))}
        </ul>
      )}
      <ErrorBox error={toggle.error} />
    </div>
  );
}

function EditMailbox({ m, onClose, onSaved }: { m: Mailbox; onClose: () => void; onSaved: () => void }) {
  const [form, setForm] = useState({
    imap_host: m.imap_host ?? "", imap_port: String(m.imap_port), imap_tls_mode: m.imap_tls_mode,
    imap_username: m.imap_username ?? "", credential_ref: m.credential_ref, display_name: m.display_name ?? "",
  });
  const [folder, setFolder] = useState("");
  const save = useMutation({
    mutationFn: async () => {
      await patch(`/api/mailboxes/${m.id}`, {
        imap_host: form.imap_host || null, imap_port: Number(form.imap_port), imap_tls_mode: form.imap_tls_mode,
        imap_username: form.imap_username || null, credential_ref: form.credential_ref, display_name: form.display_name || null,
      });
      if (folder.trim()) await post(`/api/mailboxes/${m.id}/folders`, { folder_name: folder.trim(), enabled: true });
    },
    onSuccess: onSaved,
  });
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setForm({ ...form, [k]: e.target.value });
  return (
    <Modal open title={`Postfach ${m.email_address}`} onClose={onClose} footer={<>
      <Button onClick={onClose}>Abbrechen</Button>
      <Button variant="primary" onClick={() => save.mutate()} disabled={save.isPending}>Speichern</Button>
    </>}>
      <p className="text-xs text-slate-500">Das Unternehmen eines Postfachs ist fest und kann nicht geändert werden. Passwörter werden nur als Railway-Variable gepflegt.</p>
      <div className="grid grid-cols-3 gap-3">
        <div className="col-span-2"><Field label="IMAP-Host"><Input value={form.imap_host} onChange={set("imap_host")} placeholder="imap.strato.de" /></Field></div>
        <Field label="Port"><Input inputMode="numeric" value={form.imap_port} onChange={set("imap_port")} /></Field>
      </div>
      <Field label="Verschlüsselung">
        <Select value={form.imap_tls_mode} onChange={set("imap_tls_mode")}>
          <option value="ssl">SSL/TLS (993)</option>
          <option value="starttls">STARTTLS (143)</option>
        </Select>
      </Field>
      <Field label="Benutzername"><Input value={form.imap_username} onChange={set("imap_username")} /></Field>
      <Field label="Zugangsdaten-Variable" hint="Name der Railway-Variable, z. B. INVOICEFLOW_SECRET_IMAP_…">
        <Input value={form.credential_ref} onChange={set("credential_ref")} />
      </Field>
      <Field label="Ordner manuell hinzufügen (optional)" hint="Normalerweise nicht nötig: Alle Ordner werden bei jedem Abruf automatisch erkannt.">
        <Input value={folder} onChange={(e) => setFolder(e.target.value)} placeholder="z. B. Rechnungen" />
      </Field>
      <ErrorBox error={save.error} />
    </Modal>
  );
}

function VerifyDomain({ m, domain, onClose, onSaved }: { m: Mailbox; domain: string; onClose: () => void; onSaved: () => void }) {
  const [value, setValue] = useState("");
  const mut = useMutation({ mutationFn: () => post(`/api/mailboxes/${m.id}/verify-domain`, { confirmation: value }), onSuccess: onSaved });
  return (
    <Modal open title="Domain bestätigen" onClose={onClose} footer={<>
      <Button onClick={onClose}>Abbrechen</Button>
      <Button variant="primary" onClick={() => mut.mutate()} disabled={!value || mut.isPending}>Bestätigen</Button>
    </>}>
      <p className="text-sm text-slate-600">Zur Bestätigung die Domain <b>{domain}</b> eintippen. Damit wird dokumentiert, dass dieses Postfach zum Unternehmen gehört.</p>
      <Input value={value} onChange={(e) => setValue(e.target.value)} autoFocus />
      <ErrorBox error={mut.error} />
    </Modal>
  );
}
