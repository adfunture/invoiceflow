import { useEffect, useState } from "react";
import type { DocumentItem } from "../api";
import { Button, ErrorBox, Spinner } from "./ui";

/** Zeigt das Original. PDF/Bild werden als Blob-URL eingebettet (die App-CSP erlaubt nur blob: in Frames). */
export function DocumentPreview({ doc }: { doc: DocumentItem }) {
  const [url, setUrl] = useState<string | null>(null);
  const [xml, setXml] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const href = `/api/documents/${doc.id}/original`;

  useEffect(() => {
    let objectUrl: string | null = null;
    let cancelled = false;
    setUrl(null);
    setXml(null);
    setError(null);
    fetch(href, { credentials: "same-origin" })
      .then(async (r) => {
        if (!r.ok) throw new Error(`Original nicht abrufbar (HTTP ${r.status})`);
        const blob = await r.blob();
        if (cancelled) return;
        if (doc.mime_type === "application/xml") {
          setXml(await blob.text());
        } else {
          objectUrl = URL.createObjectURL(new Blob([blob], { type: doc.mime_type ?? "application/octet-stream" }));
          setUrl(objectUrl);
        }
      })
      .catch((e) => !cancelled && setError(e));
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [href, doc.mime_type]);

  const download = (
    <a href={href} download={doc.original_filename ?? undefined}>
      <Button>Original herunterladen</Button>
    </a>
  );

  if (error) return <div className="space-y-3"><ErrorBox error={error} />{download}</div>;
  if (xml !== null) {
    return (
      <div className="space-y-2">
        <pre className="max-h-[70vh] overflow-auto rounded-md bg-slate-900 p-3 text-xs leading-relaxed text-slate-100">{xml}</pre>
        {download}
      </div>
    );
  }
  if (!url) return <Spinner label="Original wird geladen …" />;
  return (
    <div className="space-y-2">
      {doc.mime_type?.startsWith("image/") ? (
        <img src={url} alt={doc.original_filename ?? "Beleg"} className="max-h-[75vh] w-full rounded-md border border-slate-200 object-contain" />
      ) : doc.mime_type === "application/pdf" ? (
        <iframe title="Belegvorschau" src={url} className="h-[75vh] w-full rounded-md border border-slate-200" />
      ) : (
        <p className="text-sm text-slate-600">Keine Vorschau für {doc.mime_type ?? "diesen Dateityp"}.</p>
      )}
      {download}
    </div>
  );
}
