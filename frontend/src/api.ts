// API-Client: Session-Cookie (HttpOnly) + CSRF-Header für ändernde Anfragen.

let csrfToken: string | null = null;

export function setCsrfToken(token: string | null) {
  csrfToken = token;
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

function detailText(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((d) => (d && typeof d === "object" && "msg" in d ? String(d.msg) : String(d))).join("; ");
  }
  return "Unbekannter Fehler";
}

export async function api<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && csrfToken) headers["X-CSRF-Token"] = csrfToken;
  const response = await fetch(path, {
    method,
    headers,
    credentials: "same-origin",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (response.status === 204) return undefined as T;
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    throw new ApiError(response.status, detailText(data?.detail) || response.statusText);
  }
  return data as T;
}

export const get = <T,>(path: string) => api<T>("GET", path);
export const post = <T,>(path: string, body: unknown = {}) => api<T>("POST", path, body);
export const put = <T,>(path: string, body: unknown) => api<T>("PUT", path, body);
export const patch = <T,>(path: string, body: unknown) => api<T>("PATCH", path, body);

export function query(params: Record<string, string | number | boolean | null | undefined>): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== null && v !== undefined && v !== "") q.set(k, String(v));
  const s = q.toString();
  return s ? `?${s}` : "";
}

// --- Typen (entsprechen den Pydantic-Schemas im Backend) ----------------------

export type Role = "admin" | "reviewer" | "viewer";

export interface User {
  id: number;
  email: string;
  display_name: string;
  role: Role;
  must_change_password: boolean;
}

export interface SessionInfo {
  user: User;
  csrf_token: string;
  company_ids: number[] | null;
}

export interface Company {
  id: number;
  slug: string;
  legal_name: string;
  short_name: string;
  email_provider: string;
  vat_id: string | null;
  recipient_aliases: string[];
  is_active: boolean;
}

export interface Folder {
  id: number;
  folder_name: string;
  is_enabled: boolean;
  uidvalidity: number | null;
  last_seen_uid: number | null;
  backfill_completed_at: string | null;
  last_synced_at: string | null;
  last_error: string | null;
  exclusion_reason: string | null;
  discovered_at: string | null;
  missing_since: string | null;
}

export interface Mailbox {
  id: number;
  company_id: number;
  email_address: string;
  display_name: string | null;
  provider: string;
  imap_host: string | null;
  imap_port: number;
  imap_tls_mode: "ssl" | "starttls";
  imap_username: string | null;
  credential_ref: string;
  credential_configured: boolean;
  is_active: boolean;
  domain_verified_at: string | null;
  last_successful_sync_at: string | null;
  last_sync_started_at: string | null;
  sync_requested_at: string | null;
  last_error: string | null;
  last_error_at: string | null;
  consecutive_failures: number;
  activation_blockers: string[];
  folders: Folder[];
}

export interface ConnectionTest {
  ok: boolean;
  error: string | null;
  folders: string[];
  inbox_uidvalidity: number | null;
  inbox_messages: number | null;
}

export interface InvoiceMeta {
  document_type: string;
  invoice_number: string | null;
  invoice_date: string | null;
  service_date: string | null;
  supplier_name: string | null;
  supplier_address: string | null;
  supplier_vat_id: string | null;
  net_amount: string | null;
  tax_amount: string | null;
  gross_amount: string | null;
  currency: string | null;
  recipient_name: string | null;
  iban: string | null;
  payment_terms: string | null;
  due_date: string | null;
  referenced_invoice_number: string | null;
  extraction_method: string;
  confidence: string | null;
  field_sources: Record<string, string>;
  raw_extraction: { warnings?: string[]; is_correction?: boolean; other_invoice_numbers?: string[]; ai_used?: boolean };
}

export interface DocumentItem {
  id: number;
  company_id: number;
  email_message_id: number;
  original_filename: string | null;
  mime_type: string | null;
  sha256: string;
  size_bytes: number;
  source_kind: string;
  document_type: string;
  classification_method: string | null;
  ai_confidence: string | null;
  status: string;
  review_reason: string | null;
  page_count: number | null;
  has_embedded_xml: boolean;
  created_at: string;
  updated_at: string;
  invoice: InvoiceMeta | null;
  mailbox_email: string | null;
  received_at: string | null;
}

export interface Duplicate {
  document_id: number;
  duplicate_of_document_id: number;
  match_level: string;
  decision: string;
  details: Record<string, unknown>;
}

export interface DocumentDetail extends DocumentItem {
  duplicates: Duplicate[];
  message_subject: string | null;
  message_from: string | null;
}

export interface Attempt {
  attempt_no: number;
  destination_address: string | null;
  started_at: string;
  finished_at: string | null;
  outcome: string | null;
  smtp_response_code: number | null;
  smtp_response: string | null;
  error_message: string | null;
  test_mode: boolean;
  worker_id: string | null;
}

export interface Transmission {
  id: number;
  company_id: number;
  document_id: number;
  destination_address: string | null;
  file_sha256: string;
  status: string;
  test_mode: boolean;
  send_mode: string;
  attempts: number;
  max_attempts: number;
  next_attempt_at: string | null;
  sending_started_at: string | null;
  sent_at: string | null;
  smtp_message_id: string | null;
  last_smtp_response: string | null;
  last_error: string | null;
  confirmed_at: string | null;
  confirmed_by: number | null;
  requested_by: number | null;
  requested_at: string | null;
  created_at: string;
}

export interface TransmissionDetail extends Transmission {
  attempts_log: Attempt[];
}

export interface GateStep {
  key: string;
  label: string;
  done_at: string | null;
  done_by: number | null;
}

export interface DatevDestination {
  id: number;
  company_id: number;
  channel: string;
  target_address: string | null;
  address_format_warnings: string[];
  sender_address: string | null;
  smtp_host: string | null;
  smtp_port: number | null;
  smtp_tls_mode: "ssl" | "starttls";
  smtp_username: string | null;
  smtp_credential_ref: string | null;
  smtp_credential_configured: boolean;
  gate_steps: GateStep[];
  production_released_at: string | null;
  production_released_by: number | null;
  auto_send_enabled: boolean;
  send_allowed: boolean;
  send_blockers: string[];
  test_mode: boolean;
  notes: string | null;
}

export interface Setting {
  key: string;
  value: unknown;
  default: unknown;
  description: string;
  company_id: number | null;
}

export interface AuditEntry {
  id: number;
  occurred_at: string;
  company_id: number | null;
  actor_type: string;
  actor_user_id: number | null;
  actor_label: string | null;
  action: string;
  entity_type: string | null;
  entity_id: string | null;
  details: Record<string, unknown>;
}

export interface EmailMessageItem {
  id: number;
  company_id: number;
  mailbox_id: number;
  subject: string | null;
  from_address: string | null;
  received_at: string;
  attachment_count: number;
  detected_invoice_links: string[];
  body_mentions_invoice: boolean;
  status: string;
  review_reason: string | null;
}

export interface WorkerInfo {
  worker_id: string;
  worker_type: string;
  started_at: string;
  last_beat_at: string;
  stale: boolean;
  details: Record<string, unknown>;
}

export interface Summary {
  recognized_today: number;
  sent_total: number;
  sent_today: number;
  unconfirmed: number;
  awaiting_send: number;
  duplicates: number;
  errors: number;
  xml_queue: number;
  manual_review_documents: number;
  manual_review_messages: number;
  manual_review_transmissions: number;
  mailboxes_active: number;
  mailboxes_with_errors: number;
  stale_workers: string[];
  test_mode_hint: string | null;
}
