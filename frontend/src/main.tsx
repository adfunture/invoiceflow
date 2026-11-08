import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { ApiError } from "./api";
import { AppProvider, useApp } from "./app-context";
import { Layout } from "./components/Layout";
import { Spinner } from "./components/ui";
import "./index.css";
import { AuditPage } from "./pages/Audit";
import { DashboardPage } from "./pages/Dashboard";
import { DatevPage } from "./pages/Datev";
import { DocumentDetailPage } from "./pages/DocumentDetail";
import { DocumentsPage } from "./pages/Documents";
import { ChangePasswordPage, LoginPage } from "./pages/Login";
import { MailboxesPage } from "./pages/Mailboxes";
import { ReviewPage } from "./pages/Review";
import { SettingsPage } from "./pages/Settings";
import { TransmissionsPage } from "./pages/Transmissions";
import { XmlQueuePage } from "./pages/XmlQueue";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: (count, error) => !(error instanceof ApiError && [401, 403, 404].includes(error.status)) && count < 2,
      refetchOnWindowFocus: true,
    },
  },
});

// Abgelaufene Sitzung: Sitzungsdaten neu laden → Login-Seite.
queryClient.getQueryCache().subscribe((event) => {
  const error = event.query.state.error;
  if (event.type === "updated" && error instanceof ApiError && error.status === 401 && event.query.queryKey[0] !== "session") {
    void queryClient.invalidateQueries({ queryKey: ["session"] });
  }
});

function Root() {
  const { session, loading, isAdmin } = useApp();
  if (loading) return <div className="p-8"><Spinner /></div>;
  if (!session) return <LoginPage />;
  if (session.user.must_change_password) return <ChangePasswordPage />;
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<DashboardPage />} />
        <Route path="documents" element={<DocumentsPage />} />
        <Route path="documents/:id" element={<DocumentDetailPage />} />
        <Route path="review" element={<ReviewPage />} />
        <Route path="transmissions" element={<TransmissionsPage />} />
        <Route path="xml-queue" element={<XmlQueuePage />} />
        <Route path="mailboxes" element={<MailboxesPage />} />
        <Route path="datev" element={<DatevPage />} />
        <Route path="settings" element={<SettingsPage />} />
        {isAdmin && <Route path="audit" element={<AuditPage />} />}
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AppProvider>
          <Root />
        </AppProvider>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
