import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { type Company, get, patch, put, type Setting } from "../api";
import { useApp } from "../app-context";
import { Button, Card, ErrorBox, Field, Input, PageHeader, Select, Spinner } from "../components/ui";

const LABELS: Record<string, string> = {
  "datev.test_mode": "DATEV-Testmodus",
  "datev.upload_mail_zugferd_enabled": "ZUGFeRD per Upload Mail senden",
  "datev.max_mail_size_mb": "Max. Mailgröße an DATEV (MB)",
  "datev.max_attempts": "Versuche bei vorübergehenden SMTP-Fehlern",
  "ai.enabled": "KI-Fallback aktiv",
  "ai.provider": "KI-Anbieter",
  "ai.min_confidence_percent": "KI-Mindestkonfidenz (%)",
  "ocr.provider": "OCR für Scan-PDFs",
  "attachments.max_size_mb": "Max. Anhangsgröße (MB)",
  "imap.max_message_size_mb": "Max. E-Mail-Größe (MB)",
  "retention.original_documents_years": "Aufbewahrung Originale (Jahre)",
  "retention.non_invoice_emails_days": "Aufbewahrung E-Mails ohne Rechnung (Tage)",
  "retention.auto_delete_enabled": "Automatische Löschung",
};
const CHOICES: Record<string, [string, string][]> = {
  "ai.provider": [["", "– keiner –"], ["anthropic", "Anthropic (Claude)"], ["openai", "OpenAI (noch nicht angebunden)"]],
  "ocr.provider": [["", "– aus –"], ["tesseract", "Tesseract (lokal)"], ["external", "Externer Dienst (noch nicht angebunden)"]],
};

export function SettingsPage() {
  const { isAdmin } = useApp();
  const settings = useQuery({ queryKey: ["settings", null], queryFn: () => get<Setting[]>("/api/settings") });
  return (
    <>
      <PageHeader title="Einstellungen" subtitle="Globale Laufzeit-Einstellungen · Änderungen werden protokolliert" />
      <div className="space-y-5">
        <Card title="Verarbeitung">
          <ErrorBox error={settings.error} />
          {settings.isLoading ? <Spinner /> : (
            <div className="divide-y divide-slate-100">
              {(settings.data ?? []).map((s) => <SettingRow key={s.key} s={s} editable={isAdmin} />)}
            </div>
          )}
        </Card>
        <CompaniesCard />
      </div>
    </>
  );
}

function SettingRow({ s, editable }: { s: Setting; editable: boolean }) {
  const qc = useQueryClient();
  const [value, setValue] = useState<string>(s.value === null ? "" : String(s.value));
  const mut = useMutation({
    mutationFn: (v: unknown) => put(`/api/settings/${s.key}`, { value: v }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["settings"] }),
  });
  const isBool = typeof s.default === "boolean";
  const isInt = typeof s.default === "number";
  const save = () => mut.mutate(isInt ? Number(value) : value === "" ? null : value);
  const sensitive = s.key === "ai.enabled" && s.value === false;

  return (
    <div className="grid gap-2 py-3 sm:grid-cols-[1fr_16rem] sm:items-center">
      <div>
        <div className="text-sm font-medium">{LABELS[s.key] ?? s.key}</div>
        <div className="text-xs text-slate-500">{s.description}</div>
        <ErrorBox error={mut.error} />
      </div>
      <div className="flex gap-2">
        {isBool ? (
          <Button disabled={!editable || mut.isPending} variant={s.value ? "primary" : "secondary"} onClick={() => {
            if (sensitive && !window.confirm("KI-Fallback aktivieren? Dokumenttexte werden an den KI-Anbieter übertragen. Nur mit AV-Vertrag!")) return;
            mut.mutate(!s.value);
          }}>{s.value ? "An" : "Aus"}</Button>
        ) : CHOICES[s.key] ? (
          <Select disabled={!editable} value={value} onChange={(e) => { setValue(e.target.value); mut.mutate(e.target.value || null); }}>
            {CHOICES[s.key].map(([k, l]) => <option key={k} value={k}>{l}</option>)}
          </Select>
        ) : (
          <>
            <Input disabled={!editable} inputMode="numeric" value={value} onChange={(e) => setValue(e.target.value)} />
            {editable && <Button onClick={save} disabled={mut.isPending || value === String(s.value)}>Speichern</Button>}
          </>
        )}
      </div>
    </div>
  );
}

function CompaniesCard() {
  const { companies, isAdmin } = useApp();
  return (
    <Card title="Unternehmen">
      <p className="mb-3 text-sm text-slate-600">
        Die eigene USt-ID wird nie als Lieferanten-USt-ID übernommen. Die Namensvarianten dienen der Prüfung,
        ob ein Beleg an das richtige Unternehmen adressiert ist.
      </p>
      <div className="space-y-4">{companies.map((c) => <CompanyRow key={c.id} c={c} editable={isAdmin} />)}</div>
    </Card>
  );
}

function CompanyRow({ c, editable }: { c: Company; editable: boolean }) {
  const qc = useQueryClient();
  const [vat, setVat] = useState(c.vat_id ?? "");
  const [aliases, setAliases] = useState(c.recipient_aliases.join("\n"));
  const mut = useMutation({
    mutationFn: () => patch(`/api/companies/${c.id}`, {
      vat_id: vat.trim() ? vat.replace(/\s/g, "").toUpperCase() : null,
      recipient_aliases: aliases.split("\n").map((a) => a.trim()).filter(Boolean),
    }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["companies"] }),
  });
  return (
    <div className="rounded-md border border-slate-200 p-3">
      <div className="mb-2 font-medium">{c.legal_name}</div>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Eigene USt-ID"><Input disabled={!editable} value={vat} onChange={(e) => setVat(e.target.value)} placeholder="DE…" /></Field>
        <Field label="Namensvarianten (eine je Zeile)">
          <textarea disabled={!editable} rows={3} value={aliases} onChange={(e) => setAliases(e.target.value)}
            className="block w-full rounded-md px-2.5 py-1.5 text-sm ring-1 ring-inset ring-slate-300 focus:ring-2 focus:ring-brand-600" />
        </Field>
      </div>
      <ErrorBox error={mut.error} />
      {editable && <div className="mt-2"><Button onClick={() => mut.mutate()} disabled={mut.isPending}>Speichern</Button></div>}
    </div>
  );
}
