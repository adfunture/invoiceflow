import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError, getCsrfToken, query } from "../api";
import { useApp } from "../app-context";
import { Button, ErrorBox, Field, Input, Modal, Notice, Select } from "./ui";

interface Result {
  name: string;
  ok: boolean;
  error?: string;
}

async function uploadFile(file: File, companyId: number, note: string): Promise<void> {
  const headers: Record<string, string> = { Accept: "application/json", "Content-Type": "application/octet-stream" };
  const csrf = getCsrfToken();
  if (csrf) headers["X-CSRF-Token"] = csrf;
  const response = await fetch(
    `/api/documents/upload${query({ company_id: companyId, filename: file.name || "upload", note: note || undefined })}`,
    { method: "POST", headers, credentials: "same-origin", body: file },
  );
  if (!response.ok) {
    const data = await response.json().catch(() => null);
    throw new ApiError(response.status, typeof data?.detail === "string" ? data.detail : response.statusText);
  }
}

/**
 * Belege hochladen: Papierbeleg (Foto), PDF aus WhatsApp oder einem Kundenportal.
 * Auf dem Handy öffnet das Dateifeld Kamera, Fotos oder Dateien.
 */
export function UploadDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { companies, companyId } = useApp();
  const qc = useQueryClient();
  const [target, setTarget] = useState<number | null>(companyId ?? (companies.length === 1 ? companies[0].id : null));
  const [files, setFiles] = useState<File[]>([]);
  const [note, setNote] = useState("");
  const [results, setResults] = useState<Result[]>([]);
  const mutation = useMutation({
    mutationFn: async () => {
      const out: Result[] = [];
      for (const file of files) {
        try {
          await uploadFile(file, target!, note);
          out.push({ name: file.name, ok: true });
        } catch (exc) {
          out.push({ name: file.name, ok: false, error: exc instanceof Error ? exc.message : String(exc) });
        }
      }
      return out;
    },
    onSuccess: (out) => {
      setResults(out);
      setFiles([]);
      void qc.invalidateQueries({ queryKey: ["documents"] });
      void qc.invalidateQueries({ queryKey: ["summary"] });
    },
  });
  const close = () => {
    setResults([]);
    setFiles([]);
    setNote("");
    onClose();
  };

  return (
    <Modal open={open} title="Belege hochladen" onClose={close} footer={<>
      <Button onClick={close}>Schließen</Button>
      <Button variant="primary" disabled={!target || files.length === 0 || mutation.isPending} onClick={() => mutation.mutate()}>
        {mutation.isPending ? "Lädt hoch …" : `${files.length || ""} Hochladen`.trim()}
      </Button>
    </>}>
      <p className="mb-3 text-sm text-slate-600">
        PDF, XML-Rechnung oder Foto (JPG/PNG). Der Beleg wird wie ein E-Mail-Anhang erkannt, auf Dubletten geprüft
        und – wenn alles passt – für DATEV eingeplant. Fotos brauchen eingeschaltete OCR.
      </p>
      <Field label="Unternehmen">
        <Select value={target ?? ""} onChange={(e) => setTarget(e.target.value ? Number(e.target.value) : null)}>
          <option value="">Bitte wählen</option>
          {companies.map((c) => <option key={c.id} value={c.id}>{c.short_name}</option>)}
        </Select>
      </Field>
      <Field label="Dateien">
        <input type="file" multiple accept="application/pdf,application/xml,text/xml,image/jpeg,image/png,.pdf,.xml"
          className="block w-full text-sm" onChange={(e) => setFiles(Array.from(e.target.files ?? []))} />
      </Field>
      <Field label="Notiz (optional)">
        <Input value={note} maxLength={500} placeholder="z. B. Tankbeleg, per WhatsApp vom Lieferanten"
          onChange={(e) => setNote(e.target.value)} />
      </Field>
      <ErrorBox error={mutation.error} />
      {results.length > 0 && (
        <Notice tone={results.every((r) => r.ok) ? "info" : "warning"}>
          <ul className="space-y-0.5">
            {results.map((r) => (
              <li key={r.name}>{r.ok ? "✓" : "✗"} {r.name}{r.ok ? " – wird erkannt" : `: ${r.error}`}</li>
            ))}
          </ul>
        </Notice>
      )}
    </Modal>
  );
}
