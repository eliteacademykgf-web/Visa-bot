"""Management-команды.

Использование:
    python -m app.cli createadmin --login admin --name "Иванов Иван"
    python -m app.cli setpassword --login admin
    python -m app.cli grantpanel --telegram-id 123456 --login boss
    python -m app.cli generate-key
    python -m app.cli add-account --label monitor-1 --username user@example.com
    python -m app.cli capture-session --label monitor-1
    python -m app.cli capture-selectors --label monitor-1
    python -m app.cli selftest
"""

import argparse
import asyncio
import getpass
import sys

import sqlalchemy as sa

from app.db.models import Employee
from app.db.session import dispose_engine, session_scope
from app.enums import EmployeeRole
from app.logging import configure_logging
from app.web.auth import hash_password

MIN_PASSWORD_LENGTH = 10


def _read_password(confirm: bool = True) -> str:
    """Прочитать пароль из терминала, не показывая ввод."""
    password = getpass.getpass("Пароль: ")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise SystemExit(f"Пароль короче {MIN_PASSWORD_LENGTH} символов.")
    if confirm and password != getpass.getpass("Повторите пароль: "):
        raise SystemExit("Пароли не совпадают.")
    return password


async def create_admin(login: str, name: str, telegram_id: int | None) -> None:
    """Создать администратора с доступом в панель.

    Регистрации извне нет: первый администратор заводится только этой
    командой, на сервере.
    """
    password = _read_password()
    async with session_scope() as session:
        exists = (
            await session.execute(sa.select(Employee).where(Employee.login == login))
        ).scalar_one_or_none()
        if exists is not None:
            raise SystemExit(f"Логин {login!r} уже занят.")

        employee = Employee(
            full_name=name,
            role=EmployeeRole.ADMIN,
            telegram_id=telegram_id,
            login=login,
            password_hash=hash_password(password),
        )
        session.add(employee)
        await session.flush()
        print(f"Администратор создан: id={employee.id}, логин={login}")


async def set_password(login: str) -> None:
    """Сменить пароль существующему пользователю панели."""
    password = _read_password()
    async with session_scope() as session:
        employee = (
            await session.execute(sa.select(Employee).where(Employee.login == login))
        ).scalar_one_or_none()
        if employee is None:
            raise SystemExit(f"Пользователь с логином {login!r} не найден.")
        employee.password_hash = hash_password(password)
        await session.flush()
        print(f"Пароль обновлён для {employee.full_name}.")


async def grant_panel(telegram_id: int, login: str) -> None:
    """Выдать существующему сотруднику доступ в панель."""
    password = _read_password()
    async with session_scope() as session:
        employee = (
            await session.execute(
                sa.select(Employee).where(Employee.telegram_id == telegram_id)
            )
        ).scalar_one_or_none()
        if employee is None:
            raise SystemExit(f"Сотрудник с telegram_id={telegram_id} не найден.")
        employee.login = login
        employee.password_hash = hash_password(password)
        await session.flush()
        print(f"Доступ выдан: {employee.full_name} -> {login}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.cli", description="Управление системой мониторинга")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("createadmin", help="Создать первого администратора")
    create.add_argument("--login", required=True)
    create.add_argument("--name", required=True)
    create.add_argument("--telegram-id", type=int, default=None)

    password = sub.add_parser("setpassword", help="Сменить пароль")
    password.add_argument("--login", required=True)

    grant = sub.add_parser("grantpanel", help="Выдать доступ в панель")
    grant.add_argument("--telegram-id", type=int, required=True)
    grant.add_argument("--login", required=True)

    sub.add_parser("generate-key", help="Сгенерировать ключ шифрования учётных данных")

    capture = sub.add_parser(
        "capture-session",
        help="Разовый ручной вход в VFS и сохранение сессии (обход Turnstile не выполняется)",
    )
    capture.add_argument("--label", required=True)

    selectors = sub.add_parser(
        "capture-selectors",
        help="Записать сценарий записи на приём: HTML шагов и сетевые ответы",
    )
    selectors.add_argument("--label", required=True)

    sub.add_parser(
        "selftest",
        help="Отправить пробное уведомление получателям — проверка доставки",
    )

    account = sub.add_parser("add-account", help="Завести учётную запись VFS")
    account.add_argument("--label", required=True)
    account.add_argument("--username", required=True)

    return parser


async def _dispatch(args: argparse.Namespace) -> None:
    try:
        if args.command == "createadmin":
            await create_admin(args.login, args.name, args.telegram_id)
        elif args.command == "setpassword":
            await set_password(args.login)
        elif args.command == "grantpanel":
            await grant_panel(args.telegram_id, args.login)
        elif args.command == "add-account":
            await add_account(args.label, args.username)
        elif args.command == "capture-session":
            from app.monitor.session_capture import capture as capture_session

            await capture_session(args.label)
        elif args.command == "capture-selectors":
            from app.monitor.selector_capture import capture as capture_selectors

            await capture_selectors(args.label)
        elif args.command == "selftest":
            from app.monitor.selftest import run as run_selftest

            await run_selftest()
    finally:
        await dispose_engine()


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = build_parser().parse_args(argv)
    # Генерация ключа не трогает БД — незачем поднимать соединение.
    if args.command == "generate-key":
        generate_key()
        return 0
    asyncio.run(_dispatch(args))
    return 0


async def add_account(label: str, username: str) -> None:
    """Завести или обновить учётную запись VFS.

    Пароль читается из терминала и сразу шифруется: в открытом виде он
    не попадает ни в БД, ни в логи, ни в панель (ТЗ §13, §21, §22).
    """
    from app.db.models import VfsAccount
    from app.monitor import crypto

    password = _read_password()
    try:
        encrypted = crypto.encrypt(password)
    except crypto.CredentialsError as exc:
        raise SystemExit(str(exc)) from exc

    async with session_scope() as session:
        existing = (
            await session.execute(sa.select(VfsAccount).where(VfsAccount.label == label))
        ).scalar_one_or_none()
        if existing is not None:
            existing.username = username
            existing.password_encrypted = encrypted
            # Пароль сменили — сохранённая сессия почти наверняка мертва.
            existing.session_state = None
            print(f"Учётная запись {label} обновлена.")
        else:
            session.add(
                VfsAccount(label=label, username=username, password_encrypted=encrypted)
            )
            print(f"Учётная запись {label} добавлена.")


def generate_key() -> None:
    """Сгенерировать ключ шифрования учётных данных."""
    from app.monitor.crypto import generate_key as make_key

    print(make_key())
    print(
        "Сохраните значение в CREDENTIALS_KEY. Смена ключа сделает сохранённые "
        "пароли нечитаемыми — их придётся ввести заново.",
        file=sys.stderr,
    )




if __name__ == "__main__":
    sys.exit(main())
