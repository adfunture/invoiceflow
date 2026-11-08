import { type FormEvent, useState } from "react";
import { post } from "../api";
import { useApp } from "../app-context";
import { Button, ErrorBox, Field, Input } from "../components/ui";

export function LoginPage() {
  const { login } = useApp();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(email, password);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="grid min-h-full place-items-center px-4 py-12">
      <form onSubmit={submit} className="w-full max-w-sm space-y-4 rounded-lg border border-slate-200 bg-white p-6 shadow-sm">
        <div className="flex items-center gap-2">
          <span className="grid h-8 w-8 place-items-center rounded-md bg-brand-700 text-sm font-bold text-white">IF</span>
          <div>
            <h1 className="text-lg font-semibold">InvoiceFlow</h1>
            <p className="text-xs text-slate-500">Rechnungseingang SBC symbio · Adfunture</p>
          </div>
        </div>
        <Field label="E-Mail">
          <Input type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} />
        </Field>
        <Field label="Passwort">
          <Input type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} />
        </Field>
        <ErrorBox error={error} />
        <Button type="submit" variant="primary" className="w-full" disabled={busy}>
          {busy ? "Anmelden …" : "Anmelden"}
        </Button>
      </form>
    </div>
  );
}

export function ChangePasswordPage() {
  const { refreshSession, logout } = useApp();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [repeat, setRepeat] = useState("");
  const [error, setError] = useState<unknown>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (next !== repeat) {
      setError(new Error("Die neuen Passwörter stimmen nicht überein"));
      return;
    }
    try {
      await post("/api/auth/change-password", { current_password: current, new_password: next });
      await refreshSession();
    } catch (err) {
      setError(err);
    }
  }

  return (
    <div className="grid min-h-full place-items-center px-4 py-12">
      <form onSubmit={submit} className="w-full max-w-sm space-y-4 rounded-lg border border-slate-200 bg-white p-6 shadow-sm">
        <h1 className="text-lg font-semibold">Passwort ändern</h1>
        <p className="text-sm text-slate-600">Bitte vor der ersten Nutzung ein eigenes Passwort setzen (mind. 12 Zeichen, drei Zeichenarten).</p>
        <Field label="Aktuelles Passwort"><Input type="password" autoComplete="current-password" required value={current} onChange={(e) => setCurrent(e.target.value)} /></Field>
        <Field label="Neues Passwort"><Input type="password" autoComplete="new-password" required minLength={12} value={next} onChange={(e) => setNext(e.target.value)} /></Field>
        <Field label="Wiederholen"><Input type="password" autoComplete="new-password" required value={repeat} onChange={(e) => setRepeat(e.target.value)} /></Field>
        <ErrorBox error={error} />
        <div className="flex gap-2">
          <Button type="submit" variant="primary" className="flex-1">Speichern</Button>
          <Button onClick={() => void logout()}>Abmelden</Button>
        </div>
      </form>
    </div>
  );
}
