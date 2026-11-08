import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { post, type Transmission } from "../api";
import { useApp } from "../app-context";
import { Button, ErrorBox, Field, Modal, Notice, Textarea } from "./ui";

type Action = "send" | "test_delivery" | "confirm" | "resolve_sent" | "resolve_not_sent";

const TEXT: Record<Action, { title: string; button: string; info: string }> = {
  send: { title: "Beleg an DATEV senden", button: "Jetzt senden",
    info: "Der DATEV-Worker sendet den Beleg beim nächsten Durchlauf. Voraussetzung: Produktivfreigabe, Testmodus aus." },
  test_delivery: { title: "Testlieferung an DATEV", button: "Testlieferung senden",
    info: "Echter Versand vor der Produktivfreigabe, um Schritt 5 der Checkliste zu prüfen. Nur für Admins." },
  confirm: { title: "Eingang in DATEV bestätigen", button: "Bestätigen",
    info: "Nur bestätigen, wenn der Beleg in DATEV Unternehmen online sichtbar ist." },
  resolve_sent: { title: "Unklaren Versand klären: angekommen", button: "Als zugestellt markieren",
    info: "Der Beleg ist in DATEV vorhanden. Er wird nicht erneut gesendet." },
  resolve_not_sent: { title: "Unklaren Versand klären: nicht angekommen", button: "Erneut einplanen",
    info: "Der Beleg ist in DATEV NICHT vorhanden. Er wird wieder eingeplant und erneut gesendet." },
};

export function TransmissionActions({ t, onDone }: { t: Transmission; onDone: () => void }) {
  const { isAdmin, canReview } = useApp();
  const [action, setAction] = useState<Action | null>(null);
  const [note, setNote] = useState("");
  const mutation = useMutation({
    mutationFn: async (a: Action) => {
      const base = `/api/datev/transmissions/${t.id}`;
      if (a === "send" || a === "test_delivery") return post(`${base}/send`, { mode: a === "send" ? "manual" : "test_delivery" });
      if (a === "confirm") return post(`${base}/confirm`, { note });
      return post(`${base}/resolve`, { was_sent: a === "resolve_sent", reason: note });
    },
    onSuccess: () => {
      setAction(null);
      setNote("");
      onDone();
    },
  });
  if (!canReview) return null;

  const buttons: Action[] = [];
  if (t.status === "pending" || t.status === "failed") buttons.push("send");
  if ((t.status === "pending" || t.status === "failed") && isAdmin) buttons.push("test_delivery");
  if (t.status === "sent_unconfirmed") buttons.push("confirm");
  if (t.status === "manual_review" && isAdmin) buttons.push("resolve_sent", "resolve_not_sent");
  if (!buttons.length) return null;
  const needsNote = action === "confirm" || action === "resolve_sent" || action === "resolve_not_sent";

  return (
    <>
      <div className="flex flex-wrap gap-2">
        {buttons.map((b) => (
          <Button key={b} variant={b === "resolve_not_sent" ? "secondary" : b === "confirm" || b === "send" ? "primary" : "secondary"}
            onClick={() => { mutation.reset(); setAction(b); }}>
            {TEXT[b].button}
          </Button>
        ))}
      </div>
      {action && (
        <Modal open title={TEXT[action].title} onClose={() => setAction(null)} footer={<>
          <Button onClick={() => setAction(null)}>Abbrechen</Button>
          <Button variant="primary" disabled={mutation.isPending || (needsNote && note.trim().length < 3)}
            onClick={() => mutation.mutate(action)}>{TEXT[action].button}</Button>
        </>}>
          <Notice tone={action === "test_delivery" || action === "resolve_not_sent" ? "warning" : "info"}>{TEXT[action].info}</Notice>
          <p className="text-sm text-slate-600">Ziel: <b>{t.destination_address ?? "aus DATEV-Einstellungen"}</b></p>
          {needsNote && (
            <Field label="Vermerk (wird protokolliert)">
              <Textarea rows={2} value={note} onChange={(e) => setNote(e.target.value)} />
            </Field>
          )}
          <ErrorBox error={mutation.error} />
        </Modal>
      )}
    </>
  );
}
