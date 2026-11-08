"""Kommandozeile: ``invoiceflow <befehl>``."""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

from sqlalchemy import select

from invoiceflow.config import get_settings
from invoiceflow.db.enums import UserRole
from invoiceflow.db.models import Company, DatevDestination, Mailbox, User, UserCompanyAccess
from invoiceflow.db.session import session_scope
from invoiceflow.logging_setup import configure_logging

def _alembic_ini() -> Path:
    for candidate in (Path(__file__).resolve().parent.parent / "alembic.ini", Path.cwd() / "alembic.ini"):
        if candidate.exists():
            return candidate
    raise SystemExit("alembic.ini nicht gefunden – aus dem backend-Verzeichnis starten")


def cmd_migrate(_: argparse.Namespace) -> int:
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config(str(_alembic_ini())), "head")
    return 0


def cmd_seed(_: argparse.Namespace) -> int:
    from invoiceflow.seed import seed

    with session_scope() as db:
        created = seed(db)
    print(f"Seed abgeschlossen: {created}")
    return 0


def cmd_create_user(args: argparse.Namespace) -> int:
    from invoiceflow.security.passwords import hash_password
    from invoiceflow.services.audit import Actor, audit

    password = os.environ.get("INVOICEFLOW_NEW_USER_PASSWORD") or getpass.getpass("Passwort: ")
    email = args.email.strip().lower()
    with session_scope() as db:
        if db.scalar(select(User).where(User.email == email)):
            print(f"Benutzer {email} existiert bereits", file=sys.stderr)
            return 1
        user = User(email=email, display_name=args.name or email, password_hash=hash_password(password),
                    role=UserRole(args.role), must_change_password=args.must_change_password)
        db.add(user)
        db.flush()
        for slug in args.company or []:
            company = db.scalar(select(Company).where(Company.slug == slug))
            if company is None:
                raise SystemExit(f"Unbekanntes Unternehmen: {slug}")
            db.add(UserCompanyAccess(user_id=user.id, company_id=company.id))
        audit(db, Actor.system("cli"), "user.created", entity_type="user", entity_id=user.id,
              details={"email": email, "role": args.role, "companies": args.company or []})
    print(f"Benutzer {email} ({args.role}) angelegt")
    return 0


def cmd_user_admin(args: argparse.Namespace) -> int:
    """Benutzer deaktivieren/reaktivieren oder Passwort zurücksetzen (beendet alle Sitzungen)."""
    from invoiceflow.security.passwords import hash_password
    from invoiceflow.security.sessions import revoke_all_sessions
    from invoiceflow.services.audit import Actor, audit

    email = args.email.strip().lower()
    with session_scope() as db:
        user = db.scalar(select(User).where(User.email == email))
        if user is None:
            print(f"Benutzer {email} nicht gefunden", file=sys.stderr)
            return 1
        if args.user_cmd == "deactivate":
            user.is_active = False
        elif args.user_cmd == "activate":
            user.is_active = True
        elif args.user_cmd == "reset-password":
            password = os.environ.get("INVOICEFLOW_NEW_USER_PASSWORD") or getpass.getpass("Neues Passwort: ")
            user.password_hash = hash_password(password)
            user.must_change_password = True
        if args.user_cmd != "activate":
            revoke_all_sessions(db, user.id)
        audit(db, Actor.system("cli"), f"user.{args.user_cmd}", entity_type="user", entity_id=user.id, details={"email": email})
    print(f"{email}: {args.user_cmd} ausgeführt")
    return 0


def cmd_ops(args: argparse.Namespace) -> int:
    """Betriebsaufgaben (als Railway-Cron oder manuell)."""
    from invoiceflow.services.audit import Actor, audit

    settings = get_settings()
    actor = Actor.system(f"ops:{args.task}")
    task = args.task

    if task == "monitor":
        from invoiceflow.ops.monitor import configured_notifiers, run_monitor

        with session_scope() as db:
            result = run_monitor(db, settings, configured_notifiers(settings))
        print(f"Befunde: {len(result.findings)}, gemeldet: {len(result.notified)}, behoben: {len(result.resolved)}")
        for f in result.findings:
            print(f"  [{f.severity}] {f.message}")
        for err in result.notification_errors:
            print(f"  Alarmversand fehlgeschlagen: {err}", file=sys.stderr)
        return 1 if result.notification_errors else 0

    if task == "maintenance":
        from invoiceflow.ops.maintenance import run_maintenance

        with session_scope() as db:
            print(f"Bereinigt: {run_maintenance(db)}")
        return 0

    if task == "retention-report":
        from invoiceflow.ops.retention import report

        with session_scope() as db:
            r = report(db)
        print(f"Aufbewahrung Originale: {r.retention_years} Jahre (automatische Löschung: nie)")
        for year, n in r.documents_by_expiry_year.items():
            print(f"  {n} Dokument(e) – Frist endet frühestens am 31.12.{year}")
        print(f"E-Mails ohne Rechnungsbezug älter als {r.non_invoice_days} Tage: {r.purge_candidates} "
              f"(automatische Bereinigung {'an' if r.auto_delete_enabled else 'aus'})")
        return 0

    if task == "purge-non-invoice":
        from invoiceflow.ops.retention import purge_non_invoice_emails
        from invoiceflow.storage import get_store

        with session_scope() as db:
            try:
                result = purge_non_invoice_emails(db, get_store(), actor, apply=args.apply)
            except PermissionError as exc:
                print(exc, file=sys.stderr)
                return 1
        verb = "bereinigt" if args.apply else "würden bereinigt (Probelauf, --apply zum Ausführen)"
        print(f"{result.messages} E-Mail(s) {verb}; Objekte gelöscht: {result.deleted_objects}, geteilt behalten: "
              f"{result.kept_shared_objects}, Fehler: {len(result.errors)}")
        return 1 if result.errors else 0

    if task == "backup":
        from invoiceflow.ops.backup import get_target, run_backup

        result = run_backup(settings, get_target(settings))
        with session_scope() as db:
            audit(db, actor, "ops.backup_completed", details={"key": result.key, "size_bytes": result.size_bytes,
                                                              "sha256": result.sha256, "deleted_old": len(result.deleted_old)})
        print(f"Backup {result.key} ({result.size_bytes // 1024} KB), alte entfernt: {len(result.deleted_old)}")
        return 0

    if task == "restore-check":
        from invoiceflow.ops.backup import get_target, restore_check

        result = restore_check(settings, get_target(settings), args.key)
        ok = not result.missing_tables
        with session_scope() as db:
            audit(db, actor, "ops.restore_check_passed" if ok else "ops.restore_check_failed",
                  details={"key": result.key, "entries": result.entries, "missing": result.missing_tables})
        print(f"{result.key}: {result.entries} Einträge, {len(result.tables)} Tabellen mit Daten"
              + (f", FEHLEND: {', '.join(result.missing_tables)}" if not ok else " – lesbar"))
        return 0 if ok else 1

    if task == "verify-storage":
        from invoiceflow.ops.backup import verify_storage
        from invoiceflow.storage import get_store

        with session_scope() as db:
            result = verify_storage(db, get_store(), args.sample)
            audit(db, actor, "ops.storage_verified", details={"checked": result.checked, "failed": result.failed[:50]})
        print(f"Geprüft: {result.checked}, fehlerhaft: {len(result.failed)}")
        for f in result.failed:
            print(f"  {f}", file=sys.stderr)
        return 1 if result.failed else 0

    if task == "go-live-check":
        from invoiceflow.ops.go_live import run_checks

        with session_scope() as db:
            checks = run_checks(db, settings)
        symbols = {"ok": "OK  ", "warn": "WARN", "fail": "FEHL"}
        for c in checks:
            print(f"[{symbols[c.status]}] {c.area}: {c.message}")
        fails = sum(c.status == "fail" for c in checks)
        warns = sum(c.status == "warn" for c in checks)
        print()
        print(f"{fails} blockierend, {warns} Hinweis(e)")
        return 1 if fails else 0
    raise SystemExit(f"Unbekannte Aufgabe: {task}")


def cmd_generate_key(_: argparse.Namespace) -> int:
    from invoiceflow.storage import generate_key

    print(generate_key())
    print("→ als DOCUMENT_ENCRYPTION_KEY in Railway (shared variable) speichern und getrennt sicher verwahren.",
          file=sys.stderr)
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    from invoiceflow.db.session import get_sessionmaker
    from invoiceflow.storage import get_store

    if args.kind == "imap":
        from invoiceflow.workers.imap_worker import run_forever

        run_forever(get_sessionmaker(), get_store(), get_settings())
        return 0
    if args.kind == "processing":
        from invoiceflow.recognition.pipeline import DocumentProcessor
        from invoiceflow.workers.processing_worker import run_forever as run_processing

        run_processing(DocumentProcessor(get_sessionmaker(), get_store()), get_sessionmaker())
        return 0
    if args.kind == "datev":
        from invoiceflow.datev.sender import DatevSender
        from invoiceflow.datev.smtp import SmtpTransport
        from invoiceflow.workers.base import make_worker_id
        from invoiceflow.workers.datev_worker import run_forever as run_datev

        run_datev(DatevSender(get_sessionmaker(), get_store(), get_settings(), SmtpTransport, make_worker_id("datev")))
        return 0
    raise SystemExit(f"Worker {args.kind} folgt in einer späteren Phase")


def cmd_sync_mailbox(args: argparse.Namespace) -> int:
    """Einmaliger Abruf eines Postfachs (Diagnose)."""
    from invoiceflow.db.session import get_sessionmaker
    from invoiceflow.mail.imap import default_factory
    from invoiceflow.mail.sync import MailboxSyncer
    from invoiceflow.storage import get_store
    from invoiceflow.workers.base import make_worker_id

    sessions = get_sessionmaker()
    with sessions() as db:
        mailbox = db.scalar(select(Mailbox).where(Mailbox.email_address == args.email.lower()))
    if mailbox is None:
        raise SystemExit(f"Unbekanntes Postfach: {args.email}")
    syncer = MailboxSyncer(sessions, get_store(), get_settings(), default_factory, make_worker_id("cli"))
    result = syncer.sync_mailbox(mailbox.id)
    print(f"{args.email}: {result.status}, importiert {result.imported}" + (f", Fehler: {result.error}" if result.error else ""))
    for f in result.folders:
        print(f"  {f.folder}: neu {f.imported}, bekannt {f.already_known}, vor Start {f.before_start}"
              + (" (UIDVALIDITY geändert)" if f.uidvalidity_changed else ""))
    return 0 if result.status == "ok" else 1


def cmd_check_config(_: argparse.Namespace) -> int:
    """Zeigt offene Konfigurationsschritte (ohne Secret-Werte)."""
    from invoiceflow.services.datev_gate import address_format_warnings, missing_steps
    from invoiceflow.services.mailboxes import activation_blockers

    settings = get_settings()
    print(f"Umgebung: {settings.app_env}, Importstart: {settings.import_start_at.isoformat()}")
    print(f"DATEV_ALLOW_REAL_SEND: {settings.datev_allow_real_send}")
    with session_scope() as db:
        for mb in db.scalars(select(Mailbox).order_by(Mailbox.company_id, Mailbox.email_address)):
            state = "aktiv" if mb.is_active else "inaktiv"
            blockers = activation_blockers(mb)
            print(f"- Postfach {mb.email_address} [{state}]" + (": " + "; ".join(blockers) if blockers else ": bereit"))
        for dest in db.scalars(select(DatevDestination).order_by(DatevDestination.company_id)):
            print(f"- DATEV {dest.company_id}/{dest.channel}: {dest.target_address} auto_send={dest.auto_send_enabled}")
            for w in address_format_warnings(dest.target_address):
                print(f"    Warnung: {w}")
            for m in missing_steps(dest):
                print(f"    offen: {m}")
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Umlaute auch in der Windows-Konsole
    configure_logging(get_settings().log_level)
    parser = argparse.ArgumentParser(prog="invoiceflow")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="Alembic-Migrationen anwenden").set_defaults(func=cmd_migrate)
    sub.add_parser("seed", help="Unternehmen, Postfächer und DATEV-Ziele anlegen (idempotent)").set_defaults(func=cmd_seed)
    sub.add_parser("check-config", help="Offene Konfigurationsschritte anzeigen").set_defaults(func=cmd_check_config)
    sub.add_parser("generate-key", help="Neuen DOCUMENT_ENCRYPTION_KEY erzeugen").set_defaults(func=cmd_generate_key)
    w = sub.add_parser("worker", help="Hintergrund-Worker starten")
    w.add_argument("kind", choices=["imap", "processing", "datev"])
    w.set_defaults(func=cmd_worker)
    sm = sub.add_parser("sync-mailbox", help="Ein Postfach einmalig abrufen")
    sm.add_argument("email")
    sm.set_defaults(func=cmd_sync_mailbox)
    o = sub.add_parser("ops", help="Betriebsaufgaben")
    o.add_argument("task", choices=["monitor", "maintenance", "retention-report", "purge-non-invoice", "backup",
                                     "restore-check", "verify-storage", "go-live-check"])
    o.add_argument("--apply", action="store_true", help="purge-non-invoice: wirklich löschen")
    o.add_argument("--key", help="restore-check: bestimmtes Backup (Standard: neuestes)")
    o.add_argument("--sample", type=int, help="verify-storage: nur N zufällige Objekte prüfen")
    o.set_defaults(func=cmd_ops)
    u = sub.add_parser("user", help="Benutzer verwalten")
    u.add_argument("user_cmd", choices=["deactivate", "activate", "reset-password"])
    u.add_argument("email")
    u.set_defaults(func=cmd_user_admin)
    p = sub.add_parser("create-user", help="Benutzer anlegen (Passwort per Prompt oder INVOICEFLOW_NEW_USER_PASSWORD)")
    p.add_argument("--email", required=True)
    p.add_argument("--name")
    p.add_argument("--role", choices=[r.value for r in UserRole], default=UserRole.viewer.value)
    p.add_argument("--company", action="append", help="Slug eines Unternehmens (mehrfach möglich, nicht nötig für admin)")
    p.add_argument("--must-change-password", action="store_true")
    p.set_defaults(func=cmd_create_user)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
