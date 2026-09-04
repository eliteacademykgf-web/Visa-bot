"""Перечисления предметной области.

Часть значений хранится в БД как нативные enum'ы PostgreSQL — это устойчивые
бизнес-правила, менять которые следует осознанно и отдельной миграцией.
Остальные хранятся как varchar + CHECK: они будут пополняться регулярно,
и пересоздать CHECK дешевле, чем делать ALTER TYPE.
"""

from enum import StrEnum

# --------------------------------------------------------------------------
# Нативные enum'ы PostgreSQL
# --------------------------------------------------------------------------


class EmployeeRole(StrEnum):
    """Роль сотрудника (ТЗ §22)."""

    SPECIALIST = "specialist"
    """Визовый специалист: получает уведомления, реагирует, не видит пароль VFS."""

    SUPERVISOR = "supervisor"
    """Руководитель визового отдела: назначает ответственного, видит аналитику."""

    ADMIN = "admin"
    """Администратор: настройки, учётные записи, запуск и остановка."""

    DEVELOPER = "developer"
    """Разработчик: технические ошибки и логи, без доступа к персональным данным."""


class CheckStatus(StrEnum):
    """Унифицированный статус результата проверки (ТЗ §7).

    Разделение важно: первые четыре значения появляются ТОЛЬКО после сравнения
    с предыдущей проверкой. Парсер их не возвращает — он сообщает наблюдение
    (см. ObservedStatus), а SLOT_CHANGED и SLOT_DISAPPEARED выводит движок
    сравнения. Иначе парсер пришлось бы учить помнить прошлое состояние,
    а он должен оставаться функцией от одной страницы.
    """

    NO_SLOTS = "NO_SLOTS"
    SLOT_AVAILABLE = "SLOT_AVAILABLE"
    SLOT_CHANGED = "SLOT_CHANGED"
    SLOT_DISAPPEARED = "SLOT_DISAPPEARED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    CAPTCHA_REQUIRED = "CAPTCHA_REQUIRED"
    ACCESS_BLOCKED = "ACCESS_BLOCKED"
    SITE_CHANGED = "SITE_CHANGED"
    SYSTEM_ERROR = "SYSTEM_ERROR"

    @property
    def is_error(self) -> bool:
        """Технический сбой, требующий внимания администратора."""
        return self in _ERROR_STATUSES

    @property
    def is_slot_present(self) -> bool:
        """Слот виден в этой проверке."""
        return self in (CheckStatus.SLOT_AVAILABLE, CheckStatus.SLOT_CHANGED)

    @property
    def stops_monitoring(self) -> bool:
        """Проверки по учётной записи должны остановиться до вмешательства (§20)."""
        return self in (
            CheckStatus.AUTH_REQUIRED,
            CheckStatus.CAPTCHA_REQUIRED,
            CheckStatus.ACCESS_BLOCKED,
        )


_ERROR_STATUSES = frozenset(
    {
        CheckStatus.AUTH_REQUIRED,
        CheckStatus.CAPTCHA_REQUIRED,
        CheckStatus.ACCESS_BLOCKED,
        CheckStatus.SITE_CHANGED,
        CheckStatus.SYSTEM_ERROR,
    }
)


class ObservedStatus(StrEnum):
    """Что парсер увидел на странице — без знания о прошлых проверках."""

    NO_SLOTS = "no_slots"
    SLOTS_PRESENT = "slots_present"
    AUTH_REQUIRED = "auth_required"
    CAPTCHA_REQUIRED = "captcha_required"
    ACCESS_BLOCKED = "access_blocked"
    SITE_CHANGED = "site_changed"
    SYSTEM_ERROR = "system_error"


class AccountStatus(StrEnum):
    """Состояние учётной записи VFS (ТЗ §13, §20)."""

    OK = "ok"
    AUTH_REQUIRED = "auth_required"
    CAPTCHA_REQUIRED = "captcha_required"
    BLOCKED = "blocked"
    DISABLED = "disabled"

    @property
    def is_usable(self) -> bool:
        return self is AccountStatus.OK


class WebhookStatus(StrEnum):
    """Состояние доставки вебхука в CRM (outbox)."""

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


# --------------------------------------------------------------------------
# varchar + CHECK
# --------------------------------------------------------------------------


class CheckTrigger(StrEnum):
    """Почему запустилась проверка."""

    SCHEDULE = "schedule"
    MANUAL = "manual"
    RETRY = "retry"
    BOOSTED = "boosted"


class SlotEventType(StrEnum):
    """Событие, обнаруженное сравнением с предыдущей проверкой (ТЗ §10).

    Именно события, а не сами проверки, порождают уведомления. Проверка,
    не изменившая картину, событие не создаёт — иначе сотрудник получал бы
    сообщение каждые пять минут и перестал бы их читать.
    """

    APPEARED = "appeared"
    """Слот появился после отсутствия."""

    DATE_CHANGED = "date_changed"
    """Изменилась ближайшая дата."""

    NEW_DATES = "new_dates"
    """В списке появились новые даты."""

    NEW_TIMES = "new_times"
    """Появилось новое доступное время."""

    COUNT_INCREASED = "count_increased"
    """Увеличилось количество доступных слотов."""

    DISAPPEARED = "disappeared"
    """Ранее доступный слот больше не отображается."""

    STILL_AVAILABLE = "still_available"
    """Слот на месте, но истёк интервал повторного напоминания."""

    ERROR = "error"
    """Критическая ошибка мониторинга."""

    RECOVERED = "recovered"
    """Мониторинг восстановился после ошибки."""

    @property
    def is_urgent(self) -> bool:
        """Требует немедленной реакции: слот появился или стал ближе."""
        return self in (SlotEventType.APPEARED, SlotEventType.DATE_CHANGED)


class ReactionKind(StrEnum):
    """Реакция сотрудника на уведомление (ТЗ §11)."""

    ACCEPTED = "accepted"
    CHECKING = "checking"
    BOOKED = "booked"
    GONE = "gone"
    FALSE_POSITIVE = "false_positive"
    HANDOVER = "handover"

    @property
    def is_terminal(self) -> bool:
        """Реакция закрывает уведомление, продолжения не ждём."""
        return self in (
            ReactionKind.BOOKED,
            ReactionKind.GONE,
            ReactionKind.FALSE_POSITIVE,
        )

    @property
    def stops_escalation(self) -> bool:
        """Эскалация останавливается: сотрудник взял уведомление в работу."""
        return self is not ReactionKind.HANDOVER


class EscalationReason(StrEnum):
    """Причина эскалации — почему сработал уровень."""

    TIMEOUT = "timeout"
    UNDELIVERED = "undelivered"
    HANDOVER = "handover"
    NO_RECIPIENT = "no_recipient"


class EscalationStatus(StrEnum):
    """Что произошло с уровнем эскалации.

    SUPPRESSED — уровень был просрочен, но не отправлен, потому что
    одновременно оказался просрочен более высокий. Строка всё равно пишется:
    без неё уникальный индекс (alert_id, level) не удержит уровень от
    срабатывания на следующем тике, а журнал будет утверждать, что уровня
    не было вовсе.
    """

    SENT = "sent"
    SUPPRESSED = "suppressed"


class NotificationKind(StrEnum):
    """Тип исходящего сообщения. Список растёт чаще прочих."""

    SLOT_ALERT = "slot_alert"
    SLOT_CHANGED = "slot_changed"
    SLOT_DISAPPEARED = "slot_disappeared"
    REMINDER = "reminder"
    ESCALATION_L1 = "escalation_l1"
    ESCALATION_L2 = "escalation_l2"
    ESCALATION_L3 = "escalation_l3"
    MONITOR_ERROR = "monitor_error"
    MONITOR_RECOVERED = "monitor_recovered"
    WEBHOOK_FAILED = "webhook_failed"
    SYSTEM = "system"


class SettingValueType(StrEnum):
    """Тип значения настройки — для валидации при редактировании из панели."""

    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    STR = "str"
    JSON = "json"


# Уровни эскалации по ТЗ §11: 2 мин — повтор дежурному, 5 мин — руководителю,
# 10 мин — резервному сотруднику.
ESCALATION_LEVEL_REMINDER = 1
ESCALATION_LEVEL_SUPERVISOR = 2
ESCALATION_LEVEL_BACKUP = 3
