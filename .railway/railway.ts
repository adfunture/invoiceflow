// Railway Infrastructure as Code für InvoiceFlow.
// Prüfen:   npx railway config plan
// Anwenden: npx railway config apply   (nur nach Durchsicht des Plans)
//
// Nicht hier ausdrückbar und daher im Railway-Dashboard bzw. per CLI zu setzen (siehe docs/DEPLOYMENT.md):
// Standardregion des Projekts (EU West) vor dem ersten apply, öffentliche Domain für "web",
// Werte aller preserve()-Variablen (Secrets).
import { bucket, defineRailway, github, group, postgres, preserve, project, ref, service } from "railway/iac";

const REPO = "adfunture/invoiceflow";
const BRANCH = "main";
// EU-Betrieb: Compute in EU West (Amsterdam). Buckets explizit "ams" – ohne Angabe landen sie in sjc (USA).
// Die Bucket-Region ist nachträglich nicht änderbar.
const EU_REGION = "europe-west4-drams3a";
const EU = { [EU_REGION]: 1 };

export default defineRailway(() => {
  const db = postgres("postgres", { region: EU_REGION });
  const documents = bucket("invoiceflow-documents", { region: "ams" });
  // Getrennter Bucket für Datenbank-Backups. Empfehlung (docs/BACKUP_RESTORE.md): zusätzlich ein Ziel
  // außerhalb dieses Railway-Projekts; dann BACKUP_S3_* auf den externen Bucket umstellen.
  const backups = bucket("invoiceflow-backups", { region: "ams" });

  const source = github(REPO, { branch: BRANCH });
  const build = { builder: "DOCKERFILE" as const, dockerfilePath: "Dockerfile" };

  // Gemeinsame Variablen aller Services (Web, Worker, Betriebs-Crons)
  const env = {
    APP_ENV: "production",
    LOG_LEVEL: "INFO",
    COOKIE_SECURE: "true",
    DATABASE_URL: db.env.DATABASE_URL,
    STORAGE_BACKEND: "s3",
    S3_BUCKET: ref(documents, "BUCKET"),
    S3_ENDPOINT_URL: ref(documents, "ENDPOINT"),
    S3_REGION: ref(documents, "REGION"),
    S3_ACCESS_KEY_ID: ref(documents, "ACCESS_KEY_ID"),
    S3_SECRET_ACCESS_KEY: ref(documents, "SECRET_ACCESS_KEY"),
    BACKUP_S3_BUCKET: ref(backups, "BUCKET"),
    BACKUP_S3_ENDPOINT_URL: ref(backups, "ENDPOINT"),
    BACKUP_S3_REGION: ref(backups, "REGION"),
    BACKUP_S3_ACCESS_KEY_ID: ref(backups, "ACCESS_KEY_ID"),
    BACKUP_S3_SECRET_ACCESS_KEY: ref(backups, "SECRET_ACCESS_KEY"),
    // Werte werden im Railway-Dashboard gesetzt und hier nur beibehalten.
    // DOCUMENT_ENCRYPTION_KEY: `invoiceflow generate-key`, zusätzlich offline sichern!
    DOCUMENT_ENCRYPTION_KEY: preserve(),
    ALLOWED_HOSTS: preserve(), // z. B. ["invoiceflow.sbc-symbio.de"]
    PUBLIC_BASE_URL: preserve(),
    // Bleibt bis zur Produktivfreigabe ungesetzt (= false). Nur bewusst im Dashboard auf true setzen.
    DATEV_ALLOW_REAL_SEND: preserve(),
    // Postfach- und SMTP-Passwörter (Namen siehe .env.example)
    INVOICEFLOW_SECRET_IMAP_SBC_MAIL: preserve(),
    INVOICEFLOW_SECRET_IMAP_SBC_SENNINGER: preserve(),
    INVOICEFLOW_SECRET_IMAP_SBC_INVOICE: preserve(),
    INVOICEFLOW_SECRET_IMAP_ADFUNTURE_INFO: preserve(),
    INVOICEFLOW_SECRET_IMAP_ADFUNTURE_INVOICE: preserve(),
    INVOICEFLOW_SECRET_SMTP_SBC_INVOICE: preserve(),
    INVOICEFLOW_SECRET_SMTP_ADFUNTURE_INVOICE: preserve(),
    // Alarme
    ALERT_EMAIL_TO: preserve(),
    ALERT_EMAIL_FROM: preserve(),
    ALERT_SMTP_HOST: preserve(),
    ALERT_SMTP_USERNAME: preserve(),
    ALERT_SMTP_CREDENTIAL_REF: preserve(),
    INVOICEFLOW_SECRET_SMTP_ALERTS: preserve(),
    ALERT_TELEGRAM_CHAT_ID: preserve(),
    ALERT_TELEGRAM_TOKEN_REF: preserve(),
    INVOICEFLOW_SECRET_TELEGRAM_BOT: preserve(),
    // KI-Fallback (Standard aus; erst nach AV-Vertrag)
    ANTHROPIC_API_KEY: preserve(),
  };

  const web = service("web", {
    source,
    build,
    start: "uvicorn invoiceflow.api.app:app --host 0.0.0.0 --port 8080 --no-server-header",
    // Migrationen + Stammdaten vor jedem Deploy; ein Fehler stoppt das Deployment
    preDeploy: "invoiceflow migrate && invoiceflow seed",
    healthcheck: "/health/ready",
    healthcheckTimeout: 120,
    replicas: EU,
    env: { ...env, PORT: "8080" },
  });

  const worker = (name: string, kind: string, drainingSeconds: number) =>
    service(name, {
      source,
      build,
      start: `invoiceflow worker ${kind}`,
      replicas: EU, // genau eine Instanz
      deploy: { restartPolicyType: "ALWAYS", drainingSeconds },
      env,
    });

  const cron = (name: string, command: string, schedule: string) =>
    service(name, {
      source,
      build,
      start: command,
      replicas: EU,
      deploy: { cronSchedule: schedule, restartPolicyType: "NEVER" },
      env,
    });

  return project("invoiceflow", {
    resources: [
      web,
      group("Worker", [
        worker("worker-imap", "imap", 60),
        worker("worker-processing", "processing", 120),
        worker("worker-datev", "datev", 180),
      ]),
      group("Betrieb", [
        cron("ops-monitor", "sh -c 'invoiceflow ops monitor; invoiceflow ops maintenance'", "*/10 * * * *"),
        cron("ops-backup", "sh -c 'invoiceflow ops backup && invoiceflow ops verify-storage --sample 50'", "30 1 * * *"),
        cron("ops-restore-check", "invoiceflow ops restore-check", "0 4 * * 0"),
      ]),
      group("Daten", [db, documents, backups]),
    ],
  });
});
