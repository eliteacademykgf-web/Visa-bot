"""Начальная схема: мониторинг слотов VFS, уведомления, служебные таблицы.

Revision ID: 0001
Revises:
Create Date: 2026-07-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Нативные enum'ы создаются явным SQL: так порядок создания типов и таблиц
# не зависит от того, какая таблица сослалась на тип первой.
NATIVE_ENUMS: dict[str, tuple[str, ...]] = {
    "employee_role": ("specialist", "supervisor", "admin", "developer"),
    "check_status": (
        "NO_SLOTS",
        "SLOT_AVAILABLE",
        "SLOT_CHANGED",
        "SLOT_DISAPPEARED",
        "AUTH_REQUIRED",
        "CAPTCHA_REQUIRED",
        "ACCESS_BLOCKED",
        "SITE_CHANGED",
        "SYSTEM_ERROR",
    ),
    "account_status": ("ok", "auth_required", "captcha_required", "blocked", "disabled"),
    "webhook_status": ("pending", "succeeded", "failed"),
}


def _enum(name: str) -> postgresql.ENUM:
    """Ссылка на уже созданный тип (без повторного CREATE TYPE)."""
    return postgresql.ENUM(*NATIVE_ENUMS[name], name=name, create_type=False)


def _ts(name: str, *, nullable: bool = True, default_now: bool = False) -> sa.Column:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=sa.text("now()") if default_now else None,
    )


def upgrade() -> None:
    for name, values in NATIVE_ENUMS.items():
        rendered = ", ".join(f"'{value}'" for value in values)
        op.execute(f"CREATE TYPE {name} AS ENUM ({rendered})")

    # ------------------------------------------------------------------ #
    # Справочники                                                        #
    # ------------------------------------------------------------------ #
    op.create_table(
        "cities",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("priority", sa.SmallInteger(), server_default=sa.text("100"), nullable=False),
        sa.Column("check_interval_minutes", sa.Integer(), server_default=sa.text("10"), nullable=False),
        sa.Column("night_interval_minutes", sa.Integer(), server_default=sa.text("20"), nullable=False),
        sa.Column("boost_interval_minutes", sa.Integer(), server_default=sa.text("5"), nullable=False),
        sa.Column("boost_window_minutes", sa.Integer(), server_default=sa.text("60"), nullable=False),
        sa.Column("night_start", sa.Time(), server_default=sa.text("'22:00'"), nullable=False),
        sa.Column("night_end", sa.Time(), server_default=sa.text("'07:00'"), nullable=False),
        _ts("created_at", nullable=False, default_now=True),
        _ts("updated_at", nullable=False, default_now=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cities")),
        sa.UniqueConstraint("code", name=op.f("uq_cities_code")),
        sa.CheckConstraint("check_interval_minutes > 0", name=op.f("ck_cities_check_interval_positive")),
        sa.CheckConstraint("night_interval_minutes > 0", name=op.f("ck_cities_night_interval_positive")),
        sa.CheckConstraint("boost_interval_minutes > 0", name=op.f("ck_cities_boost_interval_positive")),
    )

    op.create_table(
        "categories",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("subcategory_code", sa.String(64), nullable=False),
        sa.Column("subcategory_name", sa.String(128), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        _ts("created_at", nullable=False, default_now=True),
        _ts("updated_at", nullable=False, default_now=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_categories")),
        sa.UniqueConstraint("code", "subcategory_code", name="uq_categories_pair"),
    )

    op.create_table(
        "monitor_targets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("city_id", sa.Integer(), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.Column("applicants", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("check_interval_minutes", sa.Integer(), nullable=True),
        _ts("created_at", nullable=False, default_now=True),
        _ts("updated_at", nullable=False, default_now=True),
        sa.ForeignKeyConstraint(
            ["city_id"], ["cities.id"],
            name=op.f("fk_monitor_targets_city_id_cities"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["category_id"], ["categories.id"],
            name=op.f("fk_monitor_targets_category_id_categories"), ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_monitor_targets")),
        sa.UniqueConstraint("city_id", "category_id", "applicants", name="uq_monitor_targets_key"),
        sa.CheckConstraint("applicants BETWEEN 1 AND 20", name=op.f("ck_monitor_targets_applicants_range")),
        sa.CheckConstraint(
            "check_interval_minutes IS NULL OR check_interval_minutes > 0",
            name=op.f("ck_monitor_targets_override_interval_positive"),
        ),
    )
    op.create_index("ix_monitor_targets_active", "monitor_targets", ["is_active"])

    # ------------------------------------------------------------------ #
    # Сотрудники и учётные записи VFS                                    #
    # ------------------------------------------------------------------ #
    op.create_table(
        "employees",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=True),
        sa.Column("full_name", sa.String(255), nullable=False),
        sa.Column("role", _enum("employee_role"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("login", sa.String(64), nullable=True),
        sa.Column("password_hash", sa.String(255), nullable=True),
        _ts("created_at", nullable=False, default_now=True),
        _ts("updated_at", nullable=False, default_now=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_employees")),
        sa.UniqueConstraint("telegram_id", name=op.f("uq_employees_telegram_id")),
        sa.UniqueConstraint("login", name=op.f("uq_employees_login")),
        sa.CheckConstraint(
            "(login IS NULL) = (password_hash IS NULL)",
            name=op.f("ck_employees_login_password_together"),
        ),
    )
    op.create_index("ix_employees_role_active", "employees", ["role", "is_active"])

    op.create_table(
        "vfs_accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(64), nullable=False),
        sa.Column("username", sa.String(255), nullable=False),
        # Только шифротекст Fernet: пароль в открытом виде не хранится нигде.
        sa.Column("password_encrypted", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("status", _enum("account_status"), server_default="ok", nullable=False),
        sa.Column("status_note", sa.Text(), nullable=True),
        sa.Column("session_state", postgresql.JSONB(), nullable=True),
        _ts("session_saved_at"),
        _ts("last_login_at"),
        _ts("last_success_at"),
        sa.Column("consecutive_errors", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        _ts("paused_until"),
        _ts("created_at", nullable=False, default_now=True),
        _ts("updated_at", nullable=False, default_now=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vfs_accounts")),
        sa.UniqueConstraint("label", name=op.f("uq_vfs_accounts_label")),
        sa.CheckConstraint("consecutive_errors >= 0", name=op.f("ck_vfs_accounts_consecutive_errors_non_negative")),
    )
    op.create_index("ix_vfs_accounts_usable", "vfs_accounts", ["is_active", "status"])

    op.create_table(
        "account_login_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        _ts("attempted_at", nullable=False, default_now=True),
        sa.Column("succeeded", sa.Boolean(), nullable=False),
        sa.Column("reused_session", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["account_id"], ["vfs_accounts.id"],
            name=op.f("fk_account_login_log_account_id_vfs_accounts"), ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_account_login_log")),
    )
    op.create_index(
        "ix_account_login_log_account_attempted", "account_login_log", ["account_id", "attempted_at"]
    )

    # ------------------------------------------------------------------ #
    # Мониторинг                                                         #
    # ------------------------------------------------------------------ #
    op.create_table(
        "slot_checks",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=True),
        _ts("started_at", nullable=False),
        _ts("finished_at"),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("trigger", sa.String(16), server_default="schedule", nullable=False),
        sa.Column("status", _enum("check_status"), nullable=False),
        sa.Column("nearest_date", sa.Date(), nullable=True),
        sa.Column("available_dates", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("available_times", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("slots_count", sa.Integer(), nullable=True),
        sa.Column("site_message", sa.Text(), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("error_text", sa.Text(), nullable=True),
        sa.Column("screenshot_path", sa.String(512), nullable=True),
        sa.Column("html_path", sa.String(512), nullable=True),
        sa.ForeignKeyConstraint(
            ["target_id"], ["monitor_targets.id"],
            name=op.f("fk_slot_checks_target_id_monitor_targets"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["vfs_accounts.id"],
            name=op.f("fk_slot_checks_account_id_vfs_accounts"), ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_slot_checks")),
        sa.CheckConstraint("duration_ms IS NULL OR duration_ms >= 0", name=op.f("ck_slot_checks_duration_non_negative")),
        sa.CheckConstraint(
            "trigger IN ('schedule', 'manual', 'retry', 'boosted')",
            name=op.f("ck_slot_checks_trigger_valid"),
        ),
    )
    op.create_index("ix_slot_checks_target_started", "slot_checks", ["target_id", sa.text("started_at DESC")])
    op.create_index("ix_slot_checks_status_started", "slot_checks", ["status", sa.text("started_at DESC")])
    op.create_index("ix_slot_checks_started_at", "slot_checks", [sa.text("started_at DESC")])

    op.create_table(
        "slot_states",
        sa.Column("target_id", sa.Integer(), nullable=False),
        sa.Column("status", _enum("check_status"), nullable=False),
        sa.Column("nearest_date", sa.Date(), nullable=True),
        sa.Column("available_dates", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("available_times", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("slots_count", sa.Integer(), nullable=True),
        _ts("since", nullable=False),
        _ts("last_check_at", nullable=False),
        _ts("next_check_at"),
        _ts("last_slot_found_at"),
        _ts("last_notified_at"),
        sa.Column("consecutive_errors", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_check_id", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(
            ["target_id"], ["monitor_targets.id"],
            name=op.f("fk_slot_states_target_id_monitor_targets"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["last_check_id"], ["slot_checks.id"],
            name=op.f("fk_slot_states_last_check_id_slot_checks"), ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("target_id", name=op.f("pk_slot_states")),
        sa.CheckConstraint("consecutive_errors >= 0", name=op.f("ck_slot_states_state_errors_non_negative")),
    )
    op.create_index("ix_slot_states_next_check_at", "slot_states", ["next_check_at"])

    op.create_table(
        "slot_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=False),
        sa.Column("check_id", sa.BigInteger(), nullable=True),
        sa.Column("event_type", sa.String(24), nullable=False),
        sa.Column("previous_date", sa.Date(), nullable=True),
        sa.Column("new_date", sa.Date(), nullable=True),
        sa.Column("details", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        _ts("created_at", nullable=False, default_now=True),
        sa.ForeignKeyConstraint(
            ["target_id"], ["monitor_targets.id"],
            name=op.f("fk_slot_events_target_id_monitor_targets"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["check_id"], ["slot_checks.id"],
            name=op.f("fk_slot_events_check_id_slot_checks"), ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_slot_events")),
        sa.CheckConstraint(
            "event_type IN ('appeared', 'date_changed', 'new_dates', 'new_times', "
            "'count_increased', 'disappeared', 'still_available', 'error', 'recovered')",
            name=op.f("ck_slot_events_event_type_valid"),
        ),
    )
    op.create_index("ix_slot_events_target_created", "slot_events", ["target_id", sa.text("created_at DESC")])
    op.create_index("ix_slot_events_type_created", "slot_events", ["event_type", sa.text("created_at DESC")])

    # ------------------------------------------------------------------ #
    # Уведомления и реакции                                              #
    # ------------------------------------------------------------------ #
    op.create_table(
        "alerts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("assignee_id", sa.Integer(), nullable=True),
        _ts("created_at", nullable=False, default_now=True),
        _ts("sent_at"),
        _ts("first_reaction_at"),
        _ts("booked_at"),
        _ts("closed_at"),
        sa.Column("handover_count", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.ForeignKeyConstraint(
            ["event_id"], ["slot_events.id"],
            name=op.f("fk_alerts_event_id_slot_events"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["assignee_id"], ["employees.id"],
            name=op.f("fk_alerts_assignee_id_employees"), ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alerts")),
        sa.UniqueConstraint("event_id", name=op.f("uq_alerts_event_id")),
        sa.CheckConstraint("handover_count >= 0", name=op.f("ck_alerts_handover_count_non_negative")),
    )
    op.create_index(
        "ix_alerts_open_sent_at", "alerts", ["sent_at"],
        postgresql_where=sa.text("closed_at IS NULL"),
    )
    op.create_index("ix_alerts_assignee_created", "alerts", ["assignee_id", sa.text("created_at DESC")])

    op.create_table(
        "alert_reactions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("alert_id", sa.BigInteger(), nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        _ts("reacted_at", nullable=False),
        sa.Column("seconds_from_alert", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["alert_id"], ["alerts.id"],
            name=op.f("fk_alert_reactions_alert_id_alerts"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"], ["employees.id"],
            name=op.f("fk_alert_reactions_employee_id_employees"), ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alert_reactions")),
        # Один сотрудник не может дважды поставить одну реакцию: защита
        # от двойного нажатия на уровне БД.
        sa.UniqueConstraint("alert_id", "employee_id", "kind", name="uq_alert_reactions_once"),
        sa.CheckConstraint(
            "seconds_from_alert IS NULL OR seconds_from_alert >= 0",
            name=op.f("ck_alert_reactions_seconds_from_alert_non_negative"),
        ),
        sa.CheckConstraint(
            "kind IN ('accepted', 'checking', 'booked', 'gone', 'false_positive', 'handover')",
            name=op.f("ck_alert_reactions_kind_valid"),
        ),
    )
    op.create_index("ix_alert_reactions_employee_reacted", "alert_reactions", ["employee_id", "reacted_at"])
    op.create_index("ix_alert_reactions_kind_reacted", "alert_reactions", ["kind", "reacted_at"])

    op.create_table(
        "alert_escalations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("alert_id", sa.BigInteger(), nullable=False),
        sa.Column("level", sa.SmallInteger(), nullable=False),
        sa.Column("reason", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), server_default="sent", nullable=False),
        _ts("triggered_at", nullable=False, default_now=True),
        sa.Column("recipients", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["alert_id"], ["alerts.id"],
            name=op.f("fk_alert_escalations_alert_id_alerts"), ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alert_escalations")),
        sa.UniqueConstraint("alert_id", "level", name="uq_alert_escalations_level"),
        sa.CheckConstraint("level BETWEEN 0 AND 9", name=op.f("ck_alert_escalations_escalation_level_range")),
        sa.CheckConstraint(
            "reason IN ('timeout', 'undelivered', 'handover', 'no_recipient')",
            name=op.f("ck_alert_escalations_reason_valid"),
        ),
        sa.CheckConstraint("status IN ('sent', 'suppressed')", name=op.f("ck_alert_escalations_status_valid")),
    )

    # ------------------------------------------------------------------ #
    # Служебные таблицы                                                  #
    # ------------------------------------------------------------------ #
    op.create_table(
        "notification_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("alert_id", sa.BigInteger(), nullable=True),
        sa.Column("employee_id", sa.Integer(), nullable=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(48), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        _ts("sent_at", nullable=False, default_now=True),
        sa.Column("is_delivered", sa.Boolean(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["alert_id"], ["alerts.id"],
            name=op.f("fk_notification_log_alert_id_alerts"), ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"], ["employees.id"],
            name=op.f("fk_notification_log_employee_id_employees"), ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_log")),
        sa.CheckConstraint(
            "kind IN ('slot_alert', 'slot_changed', 'slot_disappeared', 'reminder', "
            "'escalation_l1', 'escalation_l2', 'escalation_l3', 'monitor_error', "
            "'monitor_recovered', 'webhook_failed', 'system')",
            name=op.f("ck_notification_log_kind_valid"),
        ),
    )
    op.create_index("ix_notification_log_alert_id_kind", "notification_log", ["alert_id", "kind"])
    op.create_index("ix_notification_log_sent_at", "notification_log", ["sent_at"])
    op.create_index(
        "ix_notification_log_failed_sent_at", "notification_log", ["sent_at"],
        postgresql_where=sa.text("is_delivered = false"),
    )

    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.BigInteger(), nullable=True),
        sa.Column("event", sa.String(32), server_default="slot_found", nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("attempts", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("status", _enum("webhook_status"), server_default="pending", nullable=False),
        sa.Column("last_status_code", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        _ts("next_retry_at"),
        _ts("created_at", nullable=False, default_now=True),
        _ts("completed_at"),
        sa.ForeignKeyConstraint(
            ["event_id"], ["slot_events.id"],
            name=op.f("fk_webhook_deliveries_event_id_slot_events"), ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_deliveries")),
    )
    op.create_index(
        "ix_webhook_deliveries_due", "webhook_deliveries", ["next_retry_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index("ix_webhook_deliveries_event_id", "webhook_deliveries", ["event_id"])

    op.create_table(
        "settings",
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        sa.Column("value_type", sa.String(16), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        _ts("updated_at", nullable=False, default_now=True),
        sa.Column("updated_by_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["updated_by_id"], ["employees.id"],
            name=op.f("fk_settings_updated_by_id_employees"), ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_settings")),
        sa.CheckConstraint(
            "value_type IN ('int', 'float', 'bool', 'str', 'json')",
            name=op.f("ck_settings_value_type_valid"),
        ),
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("actor_employee_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("entity", sa.String(64), nullable=False),
        sa.Column("entity_id", sa.String(64), nullable=True),
        sa.Column("payload", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        _ts("created_at", nullable=False, default_now=True),
        sa.ForeignKeyConstraint(
            ["actor_employee_id"], ["employees.id"],
            name=op.f("fk_audit_log_actor_employee_id_employees"), ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    op.create_index("ix_audit_log_entity_entity_id", "audit_log", ["entity", "entity_id"])
    op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"])

    # Аудит-лог неизменяем: запрет живёт в БД, а не в соглашении о том,
    # что панель не показывает кнопку «удалить» (ТЗ §21).
    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit_log_is_append_only() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_log is append-only: % is not allowed', TG_OP;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_log_no_update_delete
        BEFORE UPDATE OR DELETE ON audit_log
        FOR EACH ROW EXECUTE FUNCTION audit_log_is_append_only();
        """
    )

    _seed()


def _seed() -> None:
    """Начальные данные: города, категории, цели мониторинга, настройки."""
    # Казахстан с марта 2024 года целиком в UTC+5; зоны указаны по IANA,
    # а не сдвигом, чтобы будущее изменение правил подхватилось само.
    op.execute(
        """
        INSERT INTO cities (code, name, timezone, is_active, priority) VALUES
            ('almaty',          'Алматы',           'Asia/Almaty', true,  10),
            ('astana',          'Астана',           'Asia/Almaty', false, 20),
            ('atyrau',          'Атырау',           'Asia/Atyrau', false, 30),
            ('shymkent',        'Шымкент',          'Asia/Almaty', false, 40),
            ('ust_kamenogorsk', 'Усть-Каменогорск', 'Asia/Almaty', false, 50)
        """
    )
    # ТЗ §23: MVP — только Алматы. Остальные города заведены, но выключены
    # и включаются из панели после тестирования.

    op.execute(
        """
        INSERT INTO categories (code, name, subcategory_code, subcategory_name, is_active) VALUES
            ('D Visa Study', 'D Visa Study',
             'Enrollment at Universities', 'Enrollment at Universities', true),
            ('D Visa Study', 'D Visa Study',
             'Student - other than pre enrolment', 'Student - other than pre enrolment', false)
        """
    )
    # ТЗ §4: дополнительная подкатегория выключена по умолчанию.

    op.execute(
        """
        INSERT INTO monitor_targets (city_id, category_id, applicants, is_active)
        SELECT c.id, cat.id, 1, (c.code = 'almaty')
        FROM cities c
        CROSS JOIN categories cat
        WHERE cat.subcategory_code = 'Enrollment at Universities'
        """
    )

    op.execute(
        """
        INSERT INTO settings (key, value, value_type, description) VALUES
            ('escalation_level_1_minutes', '2', 'int',
             'Уровень 1: повторное уведомление сотруднику, минут от отправки'),
            ('escalation_level_2_minutes', '5', 'int',
             'Уровень 2: уведомление руководителю'),
            ('escalation_level_3_minutes', '10', 'int',
             'Уровень 3: эскалация резервному сотруднику'),
            ('repeat_notice_minutes', '30', 'int',
             'Через сколько напомнить о том же слоте, если он ещё доступен'),
            ('error_notice_after', '3', 'int',
             'После скольких ошибок подряд уведомлять администратора'),
            ('group_chat_id', 'null', 'str', 'ID группового чата для рассылки находок'),
            ('crm_webhook_url', 'null', 'str', 'URL вебхука CRM'),
            ('crm_webhook_secret', '""', 'str', 'Секрет для подписи вебхука (X-Signature)'),
            ('webhook_max_attempts', '6', 'int', 'Максимум попыток доставки вебхука'),
            ('webhook_backoff_base_seconds', '15', 'int',
             'База экспоненциальной задержки ретраев вебхука')
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_update_delete ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS audit_log_is_append_only()")

    for table in (
        "audit_log",
        "settings",
        "webhook_deliveries",
        "notification_log",
        "alert_escalations",
        "alert_reactions",
        "alerts",
        "slot_events",
        "slot_states",
        "slot_checks",
        "account_login_log",
        "vfs_accounts",
        "employees",
        "monitor_targets",
        "categories",
        "cities",
    ):
        op.drop_table(table)

    for name in NATIVE_ENUMS:
        op.execute(f"DROP TYPE IF EXISTS {name}")
