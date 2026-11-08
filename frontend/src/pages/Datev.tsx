import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, type DatevDestination, get, post, put, type Setting } from "../api";
import { useApp } from "../app-context";
import { Badge, Button, Card, DescList, ErrorBox, Field, Input, Modal, Notice, PageHeader, Select, Spinner, Textarea } from "../components/ui";
import { fmtDateTime } from "../format";

export function DatevPage() {
  const { companyId, isAdmin } = useApp();
  const list = useQuery({ queryKey: ["datev-destinations"], queryFn: () => get<DatevDestination[]>("/api/datev/destinations") });
  const settings = useQuery({ queryKey: ["settings", null], queryFn: () => get<Setting[]>("/api/settings") });
  const qc = useQueryClient();
  const testMode = settings.data?.find((s) => s.key === "datev.test_mode")?.value === true;
  const toggle = useMutation({
    mutationFn: (value: boolean) => put("/api/settings/datev.test_mode", { value }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["settings"] });
      void qc.invalidateQueries({ queryKey: ["datev-destinations"] });
    },
  });
  const dests = (list.data ?? []).filter((d) => companyId === null || d.company_id === companyId);

  return (
    <>
      <PageHeader title="DATEV-Einstellungen" subtitle="Zieladressen, Absender und Produktivfreigabe je Unternehmen" />
      <div className="mb-5">
        <Card title="Testmodus (alle Unternehmen)" actions={isAdmin && settings.data && (
          <Button variant={testMode ? "secondary" : "danger"} onClick={() => {
            if (testMode && !window.confirm("Testmodus ausschalten? Danach kann – bei erteilter Freigabe – echt an DATEV gesendet werden.")) return;
            toggle.mutate(!testMode);
          }}>{testMode ? "Testmodus ausschalten" : "Testmodus einschalten"}</Button>
        )}>
          <p className="text-sm">
            {testMode
              ? <><Badge tone="info">An</Badge> Es werden keine E-Mails an DATEV gesendet. Testläufe bauen die Mail nur auf.</>
              : <><Badge tone="warning">Aus</Badge> Echtversand möglich, sofern Freigabe und <code>DATEV_ALLOW_REAL_SEND</code> es erlauben.</>}
          </p>
          <ErrorBox error={toggle.error} />
        </Card>
      </div>
      <ErrorBox error={list.error} />
      {list.isLoading ? <Spinner /> : (
        <div className="grid gap-5 xl:grid-cols-2">{dests.map((d) => <DestinationCard key={d.id} d={d} />)}</div>
      )}
    </>
  );
}

function DestinationCard({ d }: { d: DatevDestination }) {
  const { companyName, isAdmin } = useApp();
  const qc = useQueryClient();
  const [dialog, setDialog] = useState<null | "address" | "verify" | "sender" | "release" | { step: string; label: string }>(null);
  const refresh = () => {
    setDialog(null);
    void qc.invalidateQueries({ queryKey: ["datev-destinations"] });
  };
  const base = `/api/datev/destinations/${d.id}`;
  const revoke = useMutation({ mutationFn: (step: string) => api("DELETE", `${base}/gate/${step}`), onSuccess: refresh });
  const autoSend = useMutation({ mutationFn: (enabled: boolean) => post(`${base}/auto-send`, { enabled }), onSuccess: refresh });
  const allDone = d.gate_steps.every((s) => s.done_at);

  return (
    <Card title={<span className="flex items-center gap-2">{companyName(d.company_id)}
      {d.send_allowed ? <Badge tone="success">Automatischer Versand aktiv</Badge> : <Badge>Kein automatischer Versand</Badge>}</span>}>
      <DescList items={[
        ["DATEV Upload Mail", <span className="break-all font-mono text-xs">{d.target_address ?? "–"}</span>],
        ["Absender", d.sender_address ? `${d.sender_address} über ${d.smtp_host}:${d.smtp_port}` : <span className="text-amber-700">nicht konfiguriert</span>],
        ["SMTP-Zugang", d.smtp_credential_ref ? <span>{d.smtp_credential_ref} {d.smtp_credential_configured ? <Badge tone="success">gesetzt</Badge> : <Badge tone="warning">fehlt</Badge>}</span> : "–"],
      ]} />
      {d.address_format_warnings.length > 0 && (
        <div className="mt-3"><Notice tone="warning">{d.address_format_warnings.join(" ")}</Notice></div>
      )}
      {isAdmin && (
        <div className="mt-3 flex flex-wrap gap-2">
          <Button onClick={() => setDialog("address")}>Adresse ändern</Button>
          <Button onClick={() => setDialog("sender")}>Absender ändern</Button>
        </div>
      )}

      <h3 className="mt-5 mb-2 text-sm font-semibold">Freigabe-Checkliste</h3>
      <ol className="space-y-2">
        {d.gate_steps.map((s, i) => (
          <li key={s.key} className="flex flex-wrap items-center gap-2 text-sm">
            <span className={`grid h-5 w-5 place-items-center rounded-full text-xs font-semibold ${s.done_at ? "bg-emerald-600 text-white" : "bg-slate-200 text-slate-600"}`}>
              {s.done_at ? "✓" : i + 1}
            </span>
            <span className="flex-1">{s.label}{s.done_at && <span className="ml-2 text-xs text-slate-500">{fmtDateTime(s.done_at)}</span>}</span>
            {isAdmin && (s.done_at
              ? <Button variant="ghost" onClick={() => revoke.mutate(s.key)}>Zurücknehmen</Button>
              : <Button onClick={() => setDialog(s.key === "address_verified" ? "verify" : { step: s.key, label: s.label })}>Bestätigen</Button>)}
          </li>
        ))}
        <li className="flex flex-wrap items-center gap-2 border-t border-slate-100 pt-2 text-sm">
          <span className={`grid h-5 w-5 place-items-center rounded-full text-xs font-semibold ${d.production_released_at ? "bg-emerald-600 text-white" : "bg-slate-200 text-slate-600"}`}>
            {d.production_released_at ? "✓" : "6"}
          </span>
          <span className="flex-1 font-medium">Produktivfreigabe{d.production_released_at && <span className="ml-2 text-xs font-normal text-slate-500">{fmtDateTime(d.production_released_at)}</span>}</span>
          {isAdmin && !d.production_released_at && <Button variant="primary" disabled={!allDone} onClick={() => setDialog("release")}>Freigeben</Button>}
        </li>
      </ol>

      {d.production_released_at && isAdmin && (
        <div className="mt-4 flex items-center gap-3">
          <span className="text-sm">Automatischer Versand: <b>{d.auto_send_enabled ? "an" : "aus"}</b></span>
          <Button variant={d.auto_send_enabled ? "danger" : "primary"} onClick={() => autoSend.mutate(!d.auto_send_enabled)}>
            {d.auto_send_enabled ? "Ausschalten" : "Einschalten"}
          </Button>
        </div>
      )}
      {d.send_blockers.length > 0 && (
        <details className="mt-4 text-sm">
          <summary className="cursor-pointer text-slate-600">Warum wird nicht automatisch gesendet? ({d.send_blockers.length})</summary>
          <ul className="mt-2 list-disc pl-5 text-slate-600">{d.send_blockers.map((b) => <li key={b}>{b}</li>)}</ul>
        </details>
      )}
      <ErrorBox error={revoke.error ?? autoSend.error} />

      {dialog === "address" && <ConfirmTextDialog title="DATEV-Zieladresse ändern" label="Neue Upload-Mail-Adresse (aus DATEV Unternehmen online kopieren)"
        warning="Alle Freigabeschritte werden zurückgesetzt und der automatische Versand ausgeschaltet."
        onClose={() => setDialog(null)} onSubmit={(v) => put(`${base}/address`, { target_address: v })} onDone={refresh} />}
      {dialog === "verify" && <ConfirmTextDialog title="Zieladresse verifizieren" label={`Adresse zur Bestätigung exakt erneut eingeben`}
        warning="Die Adresse direkt aus DATEV Unternehmen online übernehmen – nicht aus einem Screenshot."
        onClose={() => setDialog(null)} onSubmit={(v) => post(`${base}/verify-address`, { confirmation: v })} onDone={refresh} />}
      {dialog === "release" && <ConfirmTextDialog title="Produktivfreigabe" label='Zur Bestätigung exakt "PRODUKTIV FREIGEBEN" eingeben'
        warning="Danach kann der automatische Versand eingeschaltet werden. Die Freigabe wird mit Ihrem Namen protokolliert."
        onClose={() => setDialog(null)} onSubmit={(v) => post(`${base}/release`, { acknowledgement: v })} onDone={refresh} />}
      {dialog === "sender" && <SenderDialog d={d} onClose={() => setDialog(null)} onDone={refresh} />}
      {dialog && typeof dialog === "object" && (
        <StepDialog label={dialog.label} onClose={() => setDialog(null)}
          onSubmit={(note) => post(`${base}/gate/${dialog.step}`, { note: note || null })} onDone={refresh} />
      )}
    </Card>
  );
}

function ConfirmTextDialog({ title, label, warning, onClose, onSubmit, onDone }: {
  title: string; label: string; warning: string; onClose: () => void; onSubmit: (v: string) => Promise<unknown>; onDone: () => void;
}) {
  const [value, setValue] = useState("");
  const mut = useMutation({ mutationFn: () => onSubmit(value.trim()), onSuccess: onDone });
  return (
    <Modal open title={title} onClose={onClose} footer={<>
      <Button onClick={onClose}>Abbrechen</Button>
      <Button variant="primary" disabled={!value.trim() || mut.isPending} onClick={() => mut.mutate()}>Bestätigen</Button>
    </>}>
      <Notice tone="warning">{warning}</Notice>
      <Field label={label}><Input value={value} onChange={(e) => setValue(e.target.value)} autoFocus spellCheck={false} /></Field>
      <ErrorBox error={mut.error} />
    </Modal>
  );
}

function StepDialog({ label, onClose, onSubmit, onDone }: {
  label: string; onClose: () => void; onSubmit: (note: string) => Promise<unknown>; onDone: () => void;
}) {
  const [note, setNote] = useState("");
  const mut = useMutation({ mutationFn: () => onSubmit(note), onSuccess: onDone });
  return (
    <Modal open title={label} onClose={onClose} footer={<>
      <Button onClick={onClose}>Abbrechen</Button>
      <Button variant="primary" disabled={mut.isPending} onClick={() => mut.mutate()}>Bestätigen</Button>
    </>}>
      <p className="text-sm text-slate-600">Bestätigung wird mit Zeitpunkt und Ihrem Namen protokolliert.</p>
      <Field label="Vermerk (optional)"><Textarea rows={2} value={note} onChange={(e) => setNote(e.target.value)} /></Field>
      <ErrorBox error={mut.error} />
    </Modal>
  );
}

function SenderDialog({ d, onClose, onDone }: { d: DatevDestination; onClose: () => void; onDone: () => void }) {
  const [f, setF] = useState({
    sender_address: d.sender_address ?? "", smtp_host: d.smtp_host ?? "", smtp_port: String(d.smtp_port ?? 465),
    smtp_tls_mode: d.smtp_tls_mode ?? "ssl", smtp_username: d.smtp_username ?? "", smtp_credential_ref: d.smtp_credential_ref ?? "",
  });
  const mut = useMutation({
    mutationFn: () => put(`/api/datev/destinations/${d.id}/sender`, { ...f, smtp_port: Number(f.smtp_port) }),
    onSuccess: onDone,
  });
  const set = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setF({ ...f, [k]: e.target.value });
  return (
    <Modal open title="SMTP-Absender für DATEV" onClose={onClose} footer={<>
      <Button onClick={onClose}>Abbrechen</Button>
      <Button variant="primary" disabled={mut.isPending} onClick={() => mut.mutate()}>Speichern</Button>
    </>}>
      <Notice tone="warning">Der Absender muss in DATEV als berechtigter Absender hinterlegt sein. Eine Änderung setzt die Freigabe ab Schritt 2 zurück.</Notice>
      <Field label="Absenderadresse"><Input type="email" value={f.sender_address} onChange={set("sender_address")} /></Field>
      <div className="grid grid-cols-3 gap-3">
        <div className="col-span-2"><Field label="SMTP-Host"><Input value={f.smtp_host} onChange={set("smtp_host")} placeholder="smtp.strato.de" /></Field></div>
        <Field label="Port"><Input inputMode="numeric" value={f.smtp_port} onChange={set("smtp_port")} /></Field>
      </div>
      <Field label="Verschlüsselung">
        <Select value={f.smtp_tls_mode} onChange={set("smtp_tls_mode")}>
          <option value="ssl">SSL/TLS (465)</option>
          <option value="starttls">STARTTLS (587)</option>
        </Select>
      </Field>
      <Field label="Benutzername"><Input value={f.smtp_username} onChange={set("smtp_username")} /></Field>
      <Field label="Zugangsdaten-Variable" hint="z. B. INVOICEFLOW_SECRET_SMTP_ADFUNTURE_INVOICE">
        <Input value={f.smtp_credential_ref} onChange={set("smtp_credential_ref")} />
      </Field>
      <ErrorBox error={mut.error} />
    </Modal>
  );
}
