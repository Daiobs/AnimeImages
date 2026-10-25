import copy
import importlib
from datetime import datetime, timezone
from types import SimpleNamespace

from django.core import signing
from django.db import IntegrityError, connection, migrations, transaction
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder
from django.db.models import NOT_PROVIDED, Sum
from django.test import RequestFactory, TransactionTestCase


BEFORE = "0029_providerhealthtestrun_download_smoke_details"
INTERMEDIATE = "0030_download_audit_events"
LATEST = "0031_separate_download_audit_events"
AUDIT_FIELDS = ("attempt_id", "phase", "transport", "evidence_source")


class DownloadAuditMigrationCompatibilityTests(TransactionTestCase):
    """Exercise real schema changes and old ORM writes on both CI databases."""

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        self.old_log = executor.loader.project_state(
            [("xhs_download", BEFORE)]
        ).apps.get_model("xhs_download", "DownloadRequestLog")
        self.addCleanup(self.migrate, LATEST)

    def migrate(self, name):
        executor = MigrationExecutor(connection)
        target = [("xhs_download", name)]
        executor.migrate(target)
        return executor.loader.project_state(target).apps

    def install_original_0030(self):
        self.migrate(BEFORE)
        module = importlib.import_module(f"xhs_download.migrations.{INTERMEDIATE}")
        original = module.Migration(INTERMEDIATE, "xhs_download")
        original.operations = copy.deepcopy(original.operations)
        for operation in original.operations:
            if isinstance(operation, migrations.AddField) and operation.name in AUDIT_FIELDS:
                operation.field.db_default = NOT_PROVIDED
        executor = MigrationExecutor(connection)
        state = executor.loader.project_state([("xhs_download", BEFORE)])
        # Execute the old migration's actual DDL and record it normally. Merely
        # changing loaded migration state would not reproduce the deployed bug.
        return executor.apply_migration(state, original).apps

    def audit_columns(self, model):
        with connection.cursor() as cursor:
            columns = connection.introspection.get_table_description(cursor, model._meta.db_table)
        return {column.name: column for column in columns if column.name in AUDIT_FIELDS}

    def assert_old_writes_work(self, log, prefix):
        for index in range(3):
            old = self.old_log.objects.create(
                request_id=f"{prefix}-{index}", status="success", actual_bytes=17 + index,
            )
            self.assertEqual(
                log.objects.filter(pk=old.pk).values_list(*AUDIT_FIELDS).get(),
                ("", "", "", ""),
            )
        columns = self.audit_columns(log)
        self.assertEqual(set(columns), set(AUDIT_FIELDS))
        for name, column in columns.items():
            with self.subTest(column=name):
                self.assertFalse(column.null_ok)
                self.assertIsNotNone(column.default)
                field = log._meta.get_field(name)
                self.assertEqual(field.db_default, "")
                self.assertEqual(field.default, "")
                self.assertTrue(field.blank)
                self.assertFalse(field.null)

    def complete_row(self, log, pk, *, audit=True):
        values = {}
        for index, field in enumerate(log._meta.local_concrete_fields):
            kind = field.get_internal_type()
            if field.primary_key or kind == "DateTimeField":
                continue
            if kind == "CharField":
                values[field.name] = f"saved-{field.name}"[:field.max_length]
            elif kind == "BooleanField":
                values[field.name] = True
            elif kind == "FloatField":
                values[field.name] = index + 0.125
            else:
                values[field.name] = index + 7
        values.update(
            status="failed", attempt_id=f"attempt-{pk}" if audit else "",
            phase="prepared" if audit else "", transport="browser_streaming" if audit else "",
            evidence_source="server_observed" if audit else "",
            policy_estimated_savings_bytes=-1234567,
        )
        row = log.objects.create(pk=pk, **values)
        log.objects.filter(pk=pk).update(
            created_at=datetime(2024, 2, 3, 4, 5, 6, 123456, tzinfo=timezone.utc),
        )
        return row.pk, log.objects.values().get(pk=row.pk)

    def test_fresh_0030_is_a_safe_intermediate_for_0029_orm(self):
        self.migrate(BEFORE)
        legacy = self.old_log.objects.create(request_id="before-0030", actual_bytes=42)
        apps = self.migrate(INTERMEDIATE)
        log = apps.get_model("xhs_download", "DownloadRequestLog")
        self.assertEqual(log.objects.get(pk=legacy.pk).actual_bytes, 42)
        self.assert_old_writes_work(log, "intermediate")
        self.assertFalse(MigrationRecorder(connection).migration_qs.filter(name=LATEST).exists())

    def test_0029_orm_inserts_after_full_upgrade_with_persistent_defaults(self):
        self.migrate(BEFORE)
        apps = self.migrate(LATEST)
        log = apps.get_model("xhs_download", "DownloadRequestLog")
        event = apps.get_model("xhs_download", "DownloadAuditEvent")
        self.assert_old_writes_work(log, "upgraded")
        self.assertEqual(event.objects.count(), 0)
        for name in AUDIT_FIELDS:
            with self.subTest(column=name), self.assertRaises(IntegrityError), transaction.atomic():
                log.objects.create(request_id="null-is-not-a-default", **{name: None})

    def test_original_0030_missing_database_defaults_are_actually_repaired(self):
        apps = self.install_original_0030()
        log = apps.get_model("xhs_download", "DownloadRequestLog")
        legacy_pk, legacy_values = self.complete_row(log, 81101, audit=False)
        event_pk, event_values = self.complete_row(log, 81102)
        self.assertTrue(all(column.default is None for column in self.audit_columns(log).values()))
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.old_log.objects.create(request_id="reproduces-old-0030-failure")
        apps = self.migrate(LATEST)
        log = apps.get_model("xhs_download", "DownloadRequestLog")
        event = apps.get_model("xhs_download", "DownloadAuditEvent")
        self.assert_old_writes_work(log, "repaired")
        self.assertEqual(log.objects.values().get(pk=legacy_pk), legacy_values)
        self.assertEqual(event.objects.values(*event_values).get(pk=event_pk), event_values)

    def test_history_transfer_preserves_all_scalars_ids_timestamps_and_indexes(self):
        self.migrate(BEFORE)
        apps = self.migrate(INTERMEDIATE)
        log = apps.get_model("xhs_download", "DownloadRequestLog")
        first_pk, first_values = self.complete_row(log, 91001)
        legacy_pk, legacy_values = self.complete_row(log, 91002, audit=False)
        last_pk, last_values = self.complete_row(log, 91003)
        with connection.cursor() as cursor:
            old_constraints = connection.introspection.get_constraints(cursor, log._meta.db_table)
        apps = self.migrate(LATEST)
        log = apps.get_model("xhs_download", "DownloadRequestLog")
        event = apps.get_model("xhs_download", "DownloadAuditEvent")
        self.assertEqual(list(log.objects.values()), [legacy_values])
        self.assertEqual(set(event.objects.values_list("pk", flat=True)), {first_pk, last_pk})
        for pk, snapshot in ((first_pk, first_values), (last_pk, last_values)):
            self.assertEqual(event.objects.values(*snapshot).get(pk=pk), snapshot)
            self.assertIsNone(event.objects.get(pk=pk).legacy_log_id)
        with connection.cursor() as cursor:
            self.assertEqual(
                connection.introspection.get_constraints(cursor, log._meta.db_table), old_constraints,
            )
        new_event = event.objects.create(request_id="after-sequence-reset")
        self.assertGreater(new_event.pk, last_pk)
        self.assertEqual(log.objects.get(pk=legacy_pk).actual_bytes, legacy_values["actual_bytes"])

    def test_partial_audit_markers_are_preserved_as_events_not_business_rows(self):
        self.migrate(BEFORE)
        apps = self.migrate(INTERMEDIATE)
        log = apps.get_model("xhs_download", "DownloadRequestLog")
        log.objects.create(request_id="attempt-only", attempt_id="incomplete")
        log.objects.create(request_id="phase-only", phase="unknown")
        before = list(log.objects.order_by("pk").values())
        apps = self.migrate(LATEST)
        self.assertFalse(apps.get_model("xhs_download", "DownloadRequestLog").objects.exists())
        event = apps.get_model("xhs_download", "DownloadAuditEvent")
        self.assertEqual(list(event.objects.order_by("pk").values(*before[0])), before)

    def test_partial_unique_constraints_allow_legacy_inserts_but_deduplicate_events(self):
        apps = self.migrate(LATEST)
        for model_name in ("DownloadRequestLog", "DownloadAuditEvent"):
            model = apps.get_model("xhs_download", model_name)
            with self.subTest(model=model_name):
                for fields in ({}, {"attempt_id": "a"}, {"phase": "prepared"}):
                    model.objects.create(request_id="legacy-1", **fields)
                    model.objects.create(request_id="legacy-2", **fields)
                fields = {"attempt_id": "a", "phase": "prepared", "transport": "browser_native"}
                model.objects.create(request_id="first-event", **fields)
                with self.assertRaises(IntegrityError), transaction.atomic():
                    model.objects.create(request_id="duplicate-event", **fields)
                model.objects.create(request_id="different-transport", **{**fields, "transport": "browser_blob"})
                model.objects.create(request_id="different-phase", **{**fields, "phase": "handed_off"})

    def test_code_rollback_and_forward_leave_upgraded_database_and_history_intact(self):
        self.migrate(BEFORE)
        old = self.old_log.objects.create(request_id="original-business", actual_bytes=100)
        apps = self.migrate(INTERMEDIATE)
        log = apps.get_model("xhs_download", "DownloadRequestLog")
        prepared_id, snapshot = self.complete_row(log, 98001)
        apps = self.migrate(LATEST)
        event = apps.get_model("xhs_download", "DownloadAuditEvent")
        applied = set(MigrationRecorder(connection).applied_migrations())
        # The code rollback uses the 0029 ORM against the still-upgraded schema.
        self.assertEqual(list(self.old_log.objects.values_list("pk", flat=True)), [old.pk])
        self.old_log.objects.filter(pk=old.pk).update(actual_bytes=101)
        resumed = self.old_log.objects.create(request_id="during-code-rollback", actual_bytes=102)
        self.assertEqual(event.objects.values(*snapshot).get(pk=prepared_id), snapshot)
        apps = self.migrate(LATEST)
        log = apps.get_model("xhs_download", "DownloadRequestLog")
        event = apps.get_model("xhs_download", "DownloadAuditEvent")
        self.assertEqual(set(MigrationRecorder(connection).applied_migrations()), applied)
        self.assertEqual(event.objects.values(*snapshot).get(pk=prepared_id), snapshot)
        self.assertEqual(event.objects.get(pk=prepared_id, phase="prepared").attempt_id, snapshot["attempt_id"])
        self.assertEqual(log.objects.get(pk=old.pk).actual_bytes, 101)
        self.assertEqual(log.objects.get(pk=resumed.pk).actual_bytes, 102)
        self.assertGreater(event.objects.create(request_id="after-code-forward").pk, prepared_id)

    def test_legacy_association_is_unique_and_old_orm_deletion_retains_event(self):
        apps = self.migrate(LATEST)
        log = apps.get_model("xhs_download", "DownloadRequestLog")
        event = apps.get_model("xhs_download", "DownloadAuditEvent")
        legacy = log.objects.create(request_id="associated-business")
        first = event.objects.create(request_id="associated-event", legacy_log_id=legacy.pk)
        event.objects.create(request_id="unassociated-event")
        with self.assertRaises(IntegrityError), transaction.atomic():
            event.objects.create(request_id="duplicate-association", legacy_log_id=legacy.pk)
        with transaction.atomic():
            self.old_log.objects.filter(pk=legacy.pk).delete()
        first.refresh_from_db()
        self.assertEqual(first.legacy_log_id, legacy.pk)
        self.assertFalse(log.objects.filter(pk=legacy.pk).exists())

    def test_old_readers_keep_business_counts_statuses_and_bytes_after_upgrade(self):
        self.migrate(BEFORE)
        self.old_log.objects.create(request_id="old-success", status="success", actual_bytes=123)
        self.old_log.objects.create(request_id="old-failed", status="failed", actual_bytes=45)

        def old_summary():
            rows = self.old_log.objects.all()
            return (rows.count(), rows.filter(status="success").count(),
                    rows.exclude(status="success").count(), rows.aggregate(total=Sum("actual_bytes")))

        expected = old_summary()
        apps = self.migrate(INTERMEDIATE)
        log = apps.get_model("xhs_download", "DownloadRequestLog")
        for phase in ("prepared", "transfer_started", "transfer_completed", "failed"):
            log.objects.create(request_id="audit-only", attempt_id="client", phase=phase,
                               evidence_source="client_observed", transport="browser_blob",
                               status="pending", actual_bytes=10000)
        self.assertNotEqual(old_summary(), expected)
        apps = self.migrate(LATEST)
        self.assertEqual(old_summary(), expected)
        event = apps.get_model("xhs_download", "DownloadAuditEvent")
        event.objects.create(request_id="new-event", phase="failed", actual_bytes=20000)
        self.assertEqual(old_summary(), expected)

    def test_pre_upgrade_receipt_still_accepts_observation_after_history_transfer(self):
        from .services.download_audit import SCHEMA, TOKEN_SALT, _identity, record_browser_download_audit

        self.migrate(BEFORE)
        apps = self.migrate(INTERMEDIATE)
        log = apps.get_model("xhs_download", "DownloadRequestLog")
        request = RequestFactory().post("/media/browser-download-telemetry/", REMOTE_ADDR="192.0.2.8")
        request.user = SimpleNamespace(is_staff=False, is_authenticated=False)
        identity = _identity(request)
        prepared = log.objects.create(
            request_id="pre-upgrade", attempt_id="receipt-survives", phase="prepared",
            evidence_source="server_observed", transport="",
            **{key: identity[key] for key in ("client_identity_hash", "ip_hash", "is_staff")},
        )
        token = signing.dumps({"schema": SCHEMA, "attempt_id": prepared.attempt_id,
                               "prepared_id": prepared.pk, "identity": identity}, salt=TOKEN_SALT)
        apps = self.migrate(LATEST)
        payload = {"schema": SCHEMA, "token": token, "phase": "handed_off", "transport": "browser_native"}
        self.assertTrue(record_browser_download_audit(request, payload))
        self.assertTrue(record_browser_download_audit(request, payload))
        event = apps.get_model("xhs_download", "DownloadAuditEvent")
        self.assertEqual(event.objects.filter(attempt_id=prepared.attempt_id).count(), 2)
        self.assertFalse(self.old_log.objects.exists())

    def test_schema_reverse_refuses_to_silently_drop_existing_events(self):
        apps = self.migrate(LATEST)
        event = apps.get_model("xhs_download", "DownloadAuditEvent")
        row = event.objects.create(request_id="history-must-survive")
        with self.assertRaisesMessage(IrreversibleError, "would destroy history"):
            self.migrate(INTERMEDIATE)
        self.assertTrue(event.objects.filter(pk=row.pk).exists())
        self.assertIn(("xhs_download", LATEST), MigrationRecorder(connection).applied_migrations())

    def test_failure_after_source_delete_rolls_back_the_entire_transfer(self):
        apps = self.install_original_0030()
        log = apps.get_model("xhs_download", "DownloadRequestLog")
        self.complete_row(log, 70101)
        self.complete_row(log, 70102)
        self.complete_row(log, 70103, audit=False)
        before = list(log.objects.order_by("pk").values())
        delete_sql = f'DELETE FROM {connection.ops.quote_name(log._meta.db_table)} WHERE'
        reached_delete = []

        def fail_after_delete(execute, sql, params, many, context):
            result = execute(sql, params, many, context)
            if sql.startswith(delete_sql):
                reached_delete.append(True)
                raise RuntimeError("injected failure after history removal")
            return result

        with connection.execute_wrapper(fail_after_delete):
            with self.assertRaisesMessage(RuntimeError, "injected failure"):
                self.migrate(LATEST)
        self.assertEqual(reached_delete, [True])
        self.assertEqual(list(log.objects.order_by("pk").values()), before)
        self.assertNotIn("xhs_download_downloadauditevent", connection.introspection.table_names())
        self.assertNotIn(("xhs_download", LATEST), MigrationRecorder(connection).applied_migrations())
        self.assertTrue(all(column.default is None for column in self.audit_columns(log).values()))
        apps = self.migrate(LATEST)
        event = apps.get_model("xhs_download", "DownloadAuditEvent")
        self.assertEqual(event.objects.count(), 2)

    def test_corrupted_copy_fails_verification_without_removing_history(self):
        self.migrate(BEFORE)
        apps = self.migrate(INTERMEDIATE)
        log = apps.get_model("xhs_download", "DownloadRequestLog")
        pk, snapshot = self.complete_row(log, 70201)
        destination = connection.ops.quote_name("xhs_download_downloadauditevent")

        def corrupt_copy(execute, sql, params, many, context):
            result = execute(sql, params, many, context)
            if sql.startswith(f"INSERT INTO {destination} ("):
                execute(f"UPDATE {destination} SET actual_bytes = actual_bytes + 1", [], False, context)
            return result

        with connection.execute_wrapper(corrupt_copy):
            with self.assertRaisesMessage(RuntimeError, "field verification failed"):
                self.migrate(LATEST)
        self.assertEqual(log.objects.values().get(pk=pk), snapshot)
        self.assertNotIn("xhs_download_downloadauditevent", connection.introspection.table_names())
        self.assertNotIn(("xhs_download", LATEST), MigrationRecorder(connection).applied_migrations())
