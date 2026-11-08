import { useQuery, useQueryClient } from "@tanstack/react-query";
import { createContext, type ReactNode, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { ApiError, type Company, get, post, type SessionInfo, setCsrfToken } from "./api";

interface AppState {
  session: SessionInfo | null;
  loading: boolean;
  companies: Company[];
  companyId: number | null; // globaler Filter; null = alle zugänglichen Unternehmen
  setCompanyId: (id: number | null) => void;
  companyName: (id: number | null | undefined) => string;
  login: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  refreshSession: () => Promise<unknown>;
  isAdmin: boolean;
  canReview: boolean;
}

const Ctx = createContext<AppState | null>(null);
const COMPANY_KEY = "invoiceflow.company";

function readStoredCompany(): number | null {
  try {
    const v = localStorage.getItem(COMPANY_KEY);
    return v ? Number(v) : null;
  } catch {
    return null;
  }
}

export function AppProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const sessionQuery = useQuery({
    queryKey: ["session"],
    queryFn: async () => {
      try {
        const s = await get<SessionInfo>("/api/auth/me");
        setCsrfToken(s.csrf_token);
        return s;
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) return null;
        throw e;
      }
    },
    retry: false,
    staleTime: 60_000,
  });
  const session = sessionQuery.data ?? null;

  const companiesQuery = useQuery({
    queryKey: ["companies"],
    queryFn: () => get<Company[]>("/api/companies"),
    enabled: !!session && !session.user.must_change_password,
  });
  const companies = companiesQuery.data ?? [];

  const [companyId, setCompanyIdState] = useState<number | null>(readStoredCompany);
  const setCompanyId = useCallback((id: number | null) => {
    setCompanyIdState(id);
    try {
      if (id === null) localStorage.removeItem(COMPANY_KEY);
      else localStorage.setItem(COMPANY_KEY, String(id));
    } catch {
      /* Speicher nicht verfügbar – Auswahl gilt nur für diese Sitzung */
    }
  }, []);
  useEffect(() => {
    if (companyId !== null && companies.length && !companies.some((c) => c.id === companyId)) setCompanyId(null);
  }, [companies, companyId, setCompanyId]);

  const login = useCallback(async (email: string, password: string) => {
    const s = await post<SessionInfo>("/api/auth/login", { email, password });
    setCsrfToken(s.csrf_token);
    qc.setQueryData(["session"], s);
    await qc.invalidateQueries({ predicate: (q) => q.queryKey[0] !== "session" });
  }, [qc]);

  const logout = useCallback(async () => {
    try {
      await post("/api/auth/logout");
    } finally {
      setCsrfToken(null);
      qc.clear();
      qc.setQueryData(["session"], null);
    }
  }, [qc]);

  const value = useMemo<AppState>(() => ({
    session,
    loading: sessionQuery.isLoading,
    companies,
    companyId,
    setCompanyId,
    companyName: (id) => companies.find((c) => c.id === id)?.short_name ?? (id ? `#${id}` : "–"),
    login,
    logout,
    refreshSession: () => sessionQuery.refetch(),
    isAdmin: session?.user.role === "admin",
    canReview: session?.user.role === "admin" || session?.user.role === "reviewer",
  }), [session, sessionQuery, companies, companyId, setCompanyId, login, logout]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useApp(): AppState {
  const v = useContext(Ctx);
  if (!v) throw new Error("AppProvider fehlt");
  return v;
}
