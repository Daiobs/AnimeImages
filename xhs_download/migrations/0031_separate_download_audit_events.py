from django.core.management.color import no_style
from django.db import migrations, models
from django.db.migrations.exceptions import IrreversibleError
from django.db.models import NOT_PROVIDED


AUDIT_FIELDS = ("attempt_id", "phase", "transport", "evidence_source")


def repair_legacy_database_defaults(apps, schema_editor):
    """Repair installations that already applied 0030 before it had db_default."""
    vendor = schema_editor.connection.vendor
    if vendor not in {"postgresql", "sqlite"}:
        raise NotImplementedError("Download audit migration requires PostgreSQL or SQLite")
    log = apps.get_model("xhs_download", "DownloadRequestLog")
    # 0030 now declares the correct state, but its old deployed SQL did not
    # persist defaults. Force real DDL even when the loaded states are equal.
    # SQLite rebuilds the table using ALL fields from this corrected state.
    names = AUDIT_FIELDS[:1] if vendor == "sqlite" else AUDIT_FIELDS
    for name in names:
        field = log._meta.get_field(name)
        old_field = field.clone()
        old_field.db_default = NOT_PROVIDED
        old_field.set_attributes_from_name(name)
        old_field.model = log
        schema_editor.alter_field(log, old_field, field)


def transfer_audit_history(apps, schema_editor):
    connection = schema_editor.connection
    if not connection.in_atomic_block:
        raise RuntimeError("Download audit history must be transferred atomically")
    log = apps.get_model("xhs_download", "DownloadRequestLog")
    event = apps.get_model("xhs_download", "DownloadAuditEvent")
    quote = schema_editor.quote_name
    source = quote(log._meta.db_table)
    destination = quote(event._meta.db_table)
    columns = [quote(field.column) for field in log._meta.local_concrete_fields]
    column_sql = ", ".join(columns)
    predicate = f"({quote('phase')} <> %s OR {quote('attempt_id')} <> %s)"
    distinct = "IS DISTINCT FROM" if connection.vendor == "postgresql" else "IS NOT"
    differences = " OR ".join(f"s.{column} {distinct} e.{column}" for column in columns)
    source_predicate = f"(s.{quote('phase')} <> %s OR s.{quote('attempt_id')} <> %s)"
    with connection.cursor() as cursor:
        if connection.vendor == "postgresql":
            cursor.execute(f"LOCK TABLE {source}, {destination} IN SHARE ROW EXCLUSIVE MODE")
        cursor.execute(f"SELECT COUNT(*) FROM {source} WHERE {predicate}", ["", ""])
        expected = cursor.fetchone()[0]
        cursor.execute(f"SELECT COUNT(*) FROM {destination}")
        if cursor.fetchone()[0]:
            raise RuntimeError("Download audit destination must be empty before history transfer")
        # INSERT SELECT bypasses auto_now_add and preserves every scalar, PK,
        # and timestamp exactly, including prepared_id in already issued receipts.
        cursor.execute(
            f"INSERT INTO {destination} ({column_sql}) "
            f"SELECT {column_sql} FROM {source} WHERE {predicate}",
            ["", ""],
        )
        cursor.execute(f"SELECT COUNT(*) FROM {destination}")
        if cursor.fetchone()[0] != expected:
            raise RuntimeError("Download audit history count verification failed")
        cursor.execute(
            f"SELECT COUNT(*) FROM {source} s LEFT JOIN {destination} e ON s.id = e.id "
            f"WHERE {source_predicate} AND (e.id IS NULL OR {differences})",
            ["", ""],
        )
        if cursor.fetchone()[0]:
            raise RuntimeError("Download audit history field verification failed")
        cursor.execute(f"DELETE FROM {source} WHERE {predicate}", ["", ""])
        if cursor.rowcount != expected:
            raise RuntimeError("Download audit history source count changed during transfer")
        # PostgreSQL sequences need explicit repair after inserting preserved IDs;
        # SQLite advances its AUTOINCREMENT sequence for explicit IDs itself.
        for sql in connection.ops.sequence_reset_sql(no_style(), [event]):
            cursor.execute(sql)


def refuse_to_drop_audit_history(apps, schema_editor):
    event = apps.get_model("xhs_download", "DownloadAuditEvent")
    if schema_editor.connection.vendor == "postgresql":
        # Keep the empty-table check and the subsequent DROP TABLE indivisible
        # with respect to writers, just as the forward transfer is.
        table = schema_editor.quote_name(event._meta.db_table)
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(f"LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE")
    if event.objects.using(schema_editor.connection.alias).exists():
        raise IrreversibleError(
            "Download audit events exist; reversing 0031 would destroy history. "
            "Roll back application code with the upgraded database left in place."
        )


class Migration(migrations.Migration):
    atomic = True

    dependencies = [
        ("xhs_download", "0030_download_audit_events"),
    ]

    operations = [
        migrations.RunPython(repair_legacy_database_defaults, migrations.RunPython.noop),
        migrations.CreateModel(
            name='DownloadAuditEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('request_id', models.CharField(db_index=True, max_length=128)),
                ('client_identity_hash', models.CharField(blank=True, default='', max_length=64)),
                ('ip_hash', models.CharField(blank=True, db_index=True, default='', max_length=64)),
                ('is_staff', models.BooleanField(db_index=True, default=False)),
                ('status', models.CharField(choices=[('success', '成功'), ('failed', '失败'), ('blocked', '拦截'), ('rate_limited', '限流'), ('pending', '待终态'), ('unknown', '未知'), ('cancelled', '取消')], db_index=True, default='success', max_length=16)),
                ('download_type', models.CharField(blank=True, db_index=True, default='', max_length=32)),
                ('attempt_id', models.CharField(blank=True, db_default='', db_index=True, default='', max_length=64)),
                ('phase', models.CharField(blank=True, choices=[('prepared', '准备就绪'), ('handed_off', '已移交'), ('transfer_started', '传输开始'), ('transfer_completed', '传输完成（非磁盘保存）'), ('failed', '失败'), ('cancelled', '取消'), ('unknown', '结果未知')], db_default='', db_index=True, default='', max_length=24)),
                ('transport', models.CharField(blank=True, choices=[('browser_native', '原生直链'), ('browser_streaming', 'SW 流式'), ('browser_blob', '浏览器 Blob'), ('server_attachment', '服务端转交'), ('server_zip', '服务端 ZIP'), ('server_merge', '服务端合流')], db_default='', db_index=True, default='', max_length=32)),
                ('evidence_source', models.CharField(blank=True, choices=[('server_observed', '服务端观测'), ('client_observed', '客户端上报（非落盘确认）')], db_default='', db_index=True, default='', max_length=24)),
                ('item_count', models.PositiveSmallIntegerField(default=0)),
                ('image_count', models.PositiveSmallIntegerField(default=0)),
                ('video_count', models.PositiveSmallIntegerField(default=0)),
                ('estimated_bytes', models.PositiveBigIntegerField(default=0)),
                ('actual_bytes', models.PositiveBigIntegerField(default=0)),
                ('route_type', models.CharField(blank=True, db_index=True, default='', max_length=32)),
                ('route_reason', models.CharField(blank=True, default='', max_length=200)),
                ('server_egress_estimated_bytes', models.PositiveBigIntegerField(default=0)),
                ('browser_egress_estimated_bytes', models.PositiveBigIntegerField(default=0)),
                ('provider', models.CharField(blank=True, db_index=True, default='', max_length=80)),
                ('source_platform', models.CharField(blank=True, db_index=True, default='', max_length=64)),
                ('error_code', models.CharField(blank=True, db_index=True, default='', max_length=80)),
                ('error_type', models.CharField(blank=True, db_index=True, default='', max_length=120)),
                ('error_message_short', models.CharField(blank=True, default='', max_length=300)),
                ('duration_ms', models.PositiveIntegerField(default=0)),
                ('policy_cost_score', models.FloatField(default=0)),
                ('policy_provider_score', models.FloatField(default=0)),
                ('policy_routing_score', models.FloatField(default=0)),
                ('final_policy_route', models.CharField(blank=True, db_index=True, default='', max_length=32)),
                ('policy_confidence', models.FloatField(default=0)),
                ('policy_active', models.BooleanField(db_index=True, default=False)),
                ('policy_decision_source', models.CharField(blank=True, db_index=True, default='', max_length=80)),
                ('policy_estimated_savings_bytes', models.BigIntegerField(default=0)),
                ('recovery_count', models.PositiveSmallIntegerField(default=0)),
                ('fallback_count', models.PositiveSmallIntegerField(default=0)),
                ('recovery_action', models.CharField(blank=True, db_index=True, default='', max_length=80)),
                ('recovery_reason', models.CharField(blank=True, default='', max_length=200)),
                ('fallback_from_route', models.CharField(blank=True, db_index=True, default='', max_length=32)),
                ('fallback_to_route', models.CharField(blank=True, db_index=True, default='', max_length=32)),
                ('failure_type', models.CharField(blank=True, db_index=True, default='', max_length=64)),
                ('auto_recovery_count', models.PositiveSmallIntegerField(default=0)),
                ('provider_auto_switch_count', models.PositiveSmallIntegerField(default=0)),
                ('failure_to_recovery_ms', models.PositiveIntegerField(default=0)),
                ('healing_action', models.CharField(blank=True, db_index=True, default='', max_length=80)),
                ('healing_risk', models.CharField(blank=True, db_index=True, default='', max_length=16)),
                ('healing_confidence', models.FloatField(default=0)),
                ('legacy_log_id', models.PositiveBigIntegerField(blank=True, null=True, unique=True)),
            ],
            options={
                'verbose_name': '下载审计事件',
                'verbose_name_plural': '下载审计事件',
                'ordering': ('-created_at',),
                'indexes': [models.Index(fields=['created_at', 'status'], name='xhs_downloa_created_e6493a_idx'), models.Index(fields=['attempt_id', 'created_at'], name='xhs_downloa_attempt_b1bb84_idx'), models.Index(fields=['phase', 'created_at'], name='xhs_downloa_phase_5914fe_idx'), models.Index(fields=['transport', 'created_at'], name='xhs_downloa_transpo_28c9e4_idx'), models.Index(fields=['provider', 'created_at'], name='xhs_downloa_provide_60c9d2_idx')],
                'constraints': [models.UniqueConstraint(condition=models.Q(models.Q(('attempt_id', ''), _negated=True), models.Q(('phase', ''), _negated=True)), fields=('attempt_id', 'phase', 'transport'), name='download_audit_stage_once')],
            },
        ),
        migrations.RunPython(transfer_audit_history, refuse_to_drop_audit_history),
    ]
