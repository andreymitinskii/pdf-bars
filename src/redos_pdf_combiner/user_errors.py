from __future__ import annotations

import errno
import re
from pathlib import Path


class UserNotice(Exception):
    """An expected input/environment limitation with a recovery action."""

    def __init__(self, title: str, message: str):
        super().__init__(message)
        self.title = title


def input_path(value) -> Path:
    path = Path(value).expanduser()
    if not path.is_file():
        raise UserNotice("Файл недоступен", f"Не удалось найти исходный файл:\n{path}\n\n"
                         "Файл мог быть перемещён, удалён или находиться на отключённом сетевом диске. "
                         "Проверьте доступность папки и добавьте файл заново. "
                         "Если файл передан из другой программы, сохраните его в постоянную папку и откройте эту копию.")
    return path


def user_notice(error: BaseException, source_paths=()) -> UserNotice | None:
    if isinstance(error, UserNotice):
        return error
    if is_permission_denied(error):
        return UserNotice("Нет доступа к папке", WRITE_ACCESS_MESSAGE)
    if isinstance(error, FileNotFoundError):
        for value in source_paths:
            path = Path(value).expanduser()
            if (str(path) in str(error) or str(value) in str(error)) and not path.is_file():
                try:
                    input_path(path)
                except UserNotice as notice:
                    return notice
    return None


def is_permission_denied(error: BaseException) -> bool:
    seen = set()
    pending = [error]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, PermissionError) or getattr(current, "errno", None) in {errno.EACCES, errno.EPERM}:
            return True
        if getattr(current, "winerror", None) == 5:
            return True
        message = str(current) + " " + str(getattr(current, "stderr", ""))
        if re.search(r"\[errno\s+13\]|permission denied|access is denied|отказано в доступе", message, re.I):
            return True
        pending.extend(item for item in (current.__cause__, current.__context__) if item is not None)
    return False


WRITE_ACCESS_MESSAGE = (
    "Запись в выбранную папку невозможна.\n\n"
    "Если это сетевая папка, у вашей учётной записи может не быть прав на запись. "
    "Выберите другую папку, например «Документы», и сохраните файл повторно. "
    "Для записи в сетевую папку обратитесь к администратору."
)
