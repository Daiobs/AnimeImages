"""Real shadow-storage migrations and historical ORM compatibility, offline."""
from datetime import date, datetime, timezone
from unittest.mock import patch

from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder
from django.test import TransactionTestCase, skipUnlessDBFeature


APP = "xhs_download"
BEFORE = "0031_separate_download_audit_events"
TARGET = "0032_pipeline_shadow_storage"
SHADOW = {
    "difference_codes": ["SHADOW_ERROR"],
    "field_coverage": {"title": True, "size": False},
    "invariant_violations": [],
}


class PipelineShadowMigrationCompatibilityTests(TransactionTestCase):
    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        initial_leaf_nodes = executor.loader.graph.leaf_nodes()
        self.addCleanup(lambda: MigrationExecutor(connection).migrate(initial_leaf_nodes))
        apps = self.migrate(BEFORE)
        self.old_run = apps.get_model(APP, "ProviderHealthTestRun")
        self.old_setting = apps.get_model(APP, "ProviderHealthTestSetting")

    def migrate(self, name):
        executor = MigrationExecutor(connection)
        target = [(APP, name)]
        executor.migrate(target)
        return executor.loader.project_state(target).apps

    def upgraded_models(self):
        apps = self.migrate(TARGET)
        return (
            apps.get_model(APP, "ProviderHealthTestRun"),
            apps.get_model(APP, "ProviderHealthTestSetting"),
            apps.get_model(APP, "PipelineShadowDailyCount"),
        )

    def seed_existing_rows(self):
        timestamp = datetime(2025, 2, 3, 4, 5, 6, 123456, tzinfo=timezone.utc)
        self.old_run.objects.create(
            request_id="before-shadow", test_key="health_ready", status="success",
            provider="douyin_native", duration_ms=17, actor_username="fixture-staff",
            is_staff=True, hard_enabled=True, result_fingerprint="a" * 64,
            download_smoke_details={"status": "skipped", "reasons": ["controlled"]},
        )
        self.old_run.objects.create(
            request_id="before-shadow-null", test_key="health_ready", status="skipped",
            download_smoke_details=None,
        )
        self.old_run.objects.update(created_at=timestamp)
        self.old_setting.objects.create(
            singleton_key=1, douyin_url="https://example.test/controlled-work",
            daily_enabled=True, daily_test_keys=["health_ready", "douyin_native_parse"],
            email_notifications_enabled=True, email_recipients=["staff@example.test"],
            failure_threshold=3, notification_cooldown_hours=48,
            notify_recovery=False, retention_days=30,
        )
        self.old_setting.objects.update(updated_at=timestamp)
        return (list(self.old_run.objects.order_by("pk").values()),
                self.old_setting.objects.values().get())

    def assert_existing_rows_unchanged(self, run, setting, snapshots):
        runs_before, setting_before = snapshots
        self.assertEqual(list(run.objects.order_by("pk").values(*runs_before[0])), runs_before)
        self.assertEqual(setting.objects.values(*setting_before).get(), setting_before)

    def column(self, model, field):
        with connection.cursor() as cursor:
            columns = connection.introspection.get_table_description(cursor, model._meta.db_table)
        return next(column for column in columns if column.name == field)

    def test_existing_rows_are_unchanged_with_null_details_and_shadow_off(self):
        snapshots = self.seed_existing_rows()
        run, setting, count = self.upgraded_models()
        self.assert_existing_rows_unchanged(run, setting, snapshots)
        self.assertEqual(list(run.objects.values_list("shadow_details", flat=True)), [None, None])
        self.assertIs(setting.objects.get().public_shadow_enabled, False)
        self.assertEqual(count.objects.count(), 0)
        self.assertIn((APP, TARGET), MigrationRecorder(connection).applied_migrations())

    def test_storage_field_defaults_are_persistent_database_facts(self):
        run, setting, count = self.upgraded_models()
        details = run._meta.get_field("shadow_details")
        self.assertEqual(details.get_internal_type(), "JSONField")
        self.assertTrue(details.null)
        self.assertTrue(self.column(run, "shadow_details").null_ok)
        switch = setting._meta.get_field("public_shadow_enabled")
        self.assertEqual(switch.get_internal_type(), "BooleanField")
        self.assertIs(switch.default, False)
        self.assertIs(switch.db_default, False)
        self.assertFalse(switch.null)
        column = self.column(setting, "public_shadow_enabled")
        self.assertFalse(column.null_ok)
        self.assertIsNotNone(column.default)
        with self.assertRaises(IntegrityError), transaction.atomic():
            setting.objects.create(singleton_key=1, public_shadow_enabled=None)
        self.assertEqual(
            {field.name for field in count._meta.local_concrete_fields},
            {"id", "date", "platform", "provider", "difference_code", "count"},
        )

    def test_0031_orm_inserts_and_saves_work_against_the_upgraded_schema(self):
        run, setting, count = self.upgraded_models()
        self.assertNotIn("shadow_details", {field.name for field in self.old_run._meta.fields})
        self.assertNotIn("public_shadow_enabled", {field.name for field in self.old_setting._meta.fields})
        # These model classes cannot send either new column in their INSERTs.
        legacy_run = self.old_run.objects.create(
            request_id="old-code-after-upgrade", test_key="health_ready", status="success",
        )
        legacy_setting = self.old_setting.objects.create(singleton_key=1, daily_enabled=True)
        self.assertIsNone(run.objects.get(pk=legacy_run.pk).shadow_details)
        self.assertIs(setting.objects.get(pk=legacy_setting.pk).public_shadow_enabled, False)
        legacy_run.status = "failed"
        legacy_run.save()
        legacy_setting.daily_enabled = False
        legacy_setting.save()
        self.assertEqual(run.objects.get(pk=legacy_run.pk).status, "failed")
        self.assertFalse(setting.objects.get(pk=legacy_setting.pk).daily_enabled)
        self.assertIsNone(run.objects.get(pk=legacy_run.pk).shadow_details)
        self.assertIs(setting.objects.get(pk=legacy_setting.pk).public_shadow_enabled, False)
        self.assertEqual(count.objects.count(), 0)

    def test_code_rollback_preserves_new_history_and_forward_reads_it(self):
        self.seed_existing_rows()
        run, setting, count = self.upgraded_models()
        saved_run = run.objects.get(request_id="before-shadow")
        run.objects.filter(pk=saved_run.pk).update(shadow_details=SHADOW)
        setting.objects.update(public_shadow_enabled=True)
        counter = count.objects.create(
            date=date(2026, 10, 26), platform="douyin", provider="douyin_native",
            difference_code="SHADOW_ERROR", count=7,
        )
        counter_before = count.objects.values().get(pk=counter.pk)
        applied = set(MigrationRecorder(connection).applied_migrations())

        # Code rollback only: retain 0032 and exercise the old model classes.
        old_run = self.old_run.objects.get(pk=saved_run.pk)
        old_run.duration_ms = 29
        old_run.save()
        old_setting = self.old_setting.objects.get()
        old_setting.retention_days = 45
        old_setting.save()
        inserted = self.old_run.objects.create(
            request_id="during-code-rollback", test_key="health_ready", status="skipped",
        )
        self.assertEqual(set(MigrationRecorder(connection).applied_migrations()), applied)
        self.assertEqual(run.objects.get(pk=saved_run.pk).shadow_details, SHADOW)
        self.assertTrue(setting.objects.get().public_shadow_enabled)
        self.assertEqual(count.objects.values().get(pk=counter.pk), counter_before)

        run, setting, count = self.upgraded_models()
        self.assertEqual(set(MigrationRecorder(connection).applied_migrations()), applied)
        self.assertEqual(run.objects.get(pk=saved_run.pk).shadow_details, SHADOW)
        self.assertEqual(run.objects.get(pk=saved_run.pk).duration_ms, 29)
        self.assertIsNone(run.objects.get(pk=inserted.pk).shadow_details)
        self.assertTrue(setting.objects.get().public_shadow_enabled)
        self.assertEqual(setting.objects.get().retention_days, 45)
        self.assertEqual(count.objects.values().get(pk=counter.pk), counter_before)

    def test_daily_count_uniqueness_is_enforced_on_exactly_four_dimensions(self):
        _run, _setting, count = self.upgraded_models()
        dimensions = {
            "date": date(2026, 10, 26), "platform": "douyin",
            "provider": "douyin_native", "difference_code": "SHADOW_ERROR",
        }
        count.objects.create(**dimensions, count=3)
        with self.assertRaises(IntegrityError), transaction.atomic():
            count.objects.create(**dimensions, count=99)
        for key, value in (
            ("date", date(2026, 10, 27)), ("platform", "xiaohongshu"),
            ("provider", "tiantianjiexi_generic"), ("difference_code", "FIELD_MISSING"),
        ):
            with self.subTest(dimension=key):
                count.objects.create(**{**dimensions, key: value}, count=1)
        self.assertEqual(count.objects.count(), 5)
        self.assertEqual(count.objects.get(**dimensions).count, 3)
        with connection.cursor() as cursor:
            constraints = connection.introspection.get_constraints(cursor, count._meta.db_table)
        self.assertTrue(any(
            constraint["unique"] and tuple(constraint["columns"]) == tuple(dimensions)
            for constraint in constraints.values()
        ))

    @skipUnlessDBFeature("can_rollback_ddl")
    def test_failure_after_last_storage_operation_rolls_back_schema_and_preserves_rows(self):
        snapshots = self.seed_existing_rows()
        executor = MigrationExecutor(connection)
        migration = executor.loader.graph.nodes[(APP, TARGET)]
        self.assertTrue(migration.atomic)
        target_apps = executor.loader.project_state([(APP, TARGET)]).apps
        count_table = target_apps.get_model(APP, "PipelineShadowDailyCount")._meta.db_table
        operation = migration.operations[-1]
        original_forwards = operation.database_forwards
        reached = []

        def fail_after_ddl(*args, **kwargs):
            original_forwards(*args, **kwargs)
            reached.append(connection.in_atomic_block)
            raise RuntimeError("injected failure after shadow storage DDL")

        # All real operations run inside the migration's transaction first.
        with patch.object(operation, "database_forwards", side_effect=fail_after_ddl):
            with self.assertRaisesMessage(RuntimeError, "injected failure after shadow storage DDL"):
                executor.migrate([(APP, TARGET)])
        self.assertEqual(reached, [True])
        self.assertNotIn((APP, TARGET), MigrationRecorder(connection).applied_migrations())
        self.assertNotIn(count_table, connection.introspection.table_names())
        for model, missing_field in ((self.old_run, "shadow_details"), (self.old_setting, "public_shadow_enabled")):
            with connection.cursor() as cursor:
                columns = connection.introspection.get_table_description(cursor, model._meta.db_table)
            self.assertNotIn(missing_field, {column.name for column in columns})
        self.assert_existing_rows_unchanged(self.old_run, self.old_setting, snapshots)

        run, setting, count = self.upgraded_models()
        self.assert_existing_rows_unchanged(run, setting, snapshots)
        self.assertEqual(list(run.objects.values_list("shadow_details", flat=True)), [None, None])
        self.assertFalse(setting.objects.get().public_shadow_enabled)
        self.assertEqual(count.objects.count(), 0)
