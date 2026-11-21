"""Landlock (Linux ≥ 5.13): ограничение файловой системы процесса политики средствами ядра, без root
и без внешних библиотек (ctypes + системные вызовы landlock_create_ruleset / landlock_add_rule /
landlock_restrict_self).

Landlock работает по белому списку: после ``restrict(rules)`` процесс и все его потомки могут
открывать только пути под правилами из списка и только с указанными правами; всё остальное — EACCES
(в том числе каталоги эпизодов, файлы оценщика и /proc других процессов). Снять ограничение нельзя.
Набор прав зависит от версии ABI ядра:
  ABI 1 — чтение, запись, создание и удаление (переименование и жёсткие ссылки между каталогами
          запрещены всегда); ABI 2 — то же с правом REFER; ABI 3 — усечение файлов (TRUNCATE);
  ABI 5 — ioctl устройств; ABI 6 — сигналы и абстрактные unix-сокеты только внутри своего домена.
Ограничения ptrace действуют с ABI 1: процесс в домене не может отлаживать процессы вне его.
Сеть (TCP, ABI 4) не ограничивается.

Если Landlock недоступен (не Linux, старое ядро, выключен при загрузке, вызов запрещён seccomp),
``restrict`` ничего не ограничивает и возвращает причину — вызывающий решает, что записать в журнал.
"""
from __future__ import annotations

import ctypes
import errno
import os
import stat
import sys

# номера вызовов из общей таблицы (asm-generic): одинаковы на x86_64, arm64, riscv64, arm, i386
_SYS_CREATE_RULESET, _SYS_ADD_RULE, _SYS_RESTRICT_SELF = 444, 445, 446
_CREATE_RULESET_VERSION = 1 << 0
_RULE_PATH_BENEATH = 1
_PR_SET_DUMPABLE, _PR_SET_NO_NEW_PRIVS = 4, 38

EXECUTE, WRITE_FILE, READ_FILE, READ_DIR = 1 << 0, 1 << 1, 1 << 2, 1 << 3
REMOVE_DIR, REMOVE_FILE, MAKE_CHAR, MAKE_DIR, MAKE_REG = 1 << 4, 1 << 5, 1 << 6, 1 << 7, 1 << 8
MAKE_SOCK, MAKE_FIFO, MAKE_BLOCK, MAKE_SYM = 1 << 9, 1 << 10, 1 << 11, 1 << 12
REFER, TRUNCATE, IOCTL_DEV = 1 << 13, 1 << 14, 1 << 15  # ABI 2, 3, 5
SCOPE_ABSTRACT_UNIX_SOCKET, SCOPE_SIGNAL = 1 << 0, 1 << 1  # ABI 6
_FILE_ONLY = EXECUTE | WRITE_FILE | READ_FILE | TRUNCATE | IOCTL_DEV  # права, допустимые в правиле для файла

ACCESS = {
    "r": READ_FILE | READ_DIR,
    "rx": READ_FILE | READ_DIR | EXECUTE,
    "rw": READ_FILE | READ_DIR | WRITE_FILE | TRUNCATE | REMOVE_DIR | REMOVE_FILE | MAKE_DIR | MAKE_REG | MAKE_SYM
          | MAKE_SOCK | MAKE_FIFO | REFER,
    "dev": READ_FILE | WRITE_FILE | TRUNCATE | IOCTL_DEV,
}


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64), ("handled_access_net", ctypes.c_uint64),
                ("scoped", ctypes.c_uint64)]


class _PathBeneathAttr(ctypes.Structure):
    _pack_ = 1  # в ядре __attribute__((packed))
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def _libc():
    lib = ctypes.CDLL(None, use_errno=True)
    lib.syscall.restype = ctypes.c_long
    lib.prctl.restype = ctypes.c_int
    return lib


def _err(what: str) -> str:
    e = ctypes.get_errno()
    return "%s: %s" % (what, errno.errorcode.get(e, str(e)))


def abi() -> tuple[int, str]:
    """(версия ABI Landlock, "") или (0, причина недоступности)."""
    if not sys.platform.startswith("linux"):
        return 0, "не Linux"
    try:
        lib = _libc()
    except (OSError, AttributeError) as e:
        return 0, "libc: %s" % e
    v = lib.syscall(ctypes.c_long(_SYS_CREATE_RULESET), None, ctypes.c_size_t(0), ctypes.c_uint32(_CREATE_RULESET_VERSION))
    if v < 0:
        e = ctypes.get_errno()
        if e == errno.ENOSYS:
            return 0, "ядро без Landlock или вызов запрещён seccomp (ENOSYS)"
        if e == errno.EOPNOTSUPP:
            return 0, "Landlock выключен при загрузке ядра (EOPNOTSUPP)"
        return 0, _err("landlock_create_ruleset")
    return int(v), ""


def handled_access(version: int) -> int:
    h = (1 << 13) - 1  # ABI 1: EXECUTE … MAKE_SYM
    if version >= 2:
        h |= REFER
    if version >= 3:
        h |= TRUNCATE
    if version >= 5:
        h |= IOCTL_DEV
    return h


def restrict(rules) -> tuple[int, str]:
    """Ограничить текущий процесс (и будущих потомков) правилами ``rules`` — списком (путь, вид),
    вид — ключ ACCESS. Пути, которых нет, пропускаются. Возвращает (ABI, "") или (0, причина), если
    ограничение не наложено. Вызывать до запуска потоков: landlock_restrict_self ограничивает только
    вызывающий поток (и потомков, созданных после)."""
    if not sys.platform.startswith("linux"):
        return 0, "не Linux"
    try:
        lib = _libc()
    except (OSError, AttributeError) as e:
        return 0, "libc: %s" % e
    # без новых привилегий (setuid, file capabilities) — условие landlock_restrict_self без CAP_SYS_ADMIN
    if lib.prctl(ctypes.c_int(_PR_SET_NO_NEW_PRIVS), ctypes.c_ulong(1), ctypes.c_ulong(0), ctypes.c_ulong(0),
                 ctypes.c_ulong(0)) != 0:
        return 0, _err("prctl(PR_SET_NO_NEW_PRIVS)")
    version, why = abi()
    if not version:
        return 0, why
    try:
        threads = len(os.listdir("/proc/self/task"))
    except OSError:
        threads = 1
    if threads > 1:
        return 0, "в процессе %d потоков: Landlock ограничил бы только один" % threads
    handled = handled_access(version)
    attr = _RulesetAttr(handled, 0, SCOPE_ABSTRACT_UNIX_SOCKET | SCOPE_SIGNAL if version >= 6 else 0)
    size = 24 if version >= 6 else 16 if version >= 4 else 8
    fd = lib.syscall(ctypes.c_long(_SYS_CREATE_RULESET), ctypes.byref(attr), ctypes.c_size_t(size), ctypes.c_uint32(0))
    if fd < 0:
        return 0, _err("landlock_create_ruleset")
    try:
        for path, kind in rules:
            try:
                pfd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            except OSError:
                continue  # пути нет — разрешать нечего
            try:
                access = ACCESS[kind] & handled
                if not stat.S_ISDIR(os.fstat(pfd).st_mode):
                    access &= _FILE_ONLY
                rule = _PathBeneathAttr(access, pfd)
                # отказ по одному правилу (например, особая файловая система) — меньше доступа, не больше
                lib.syscall(ctypes.c_long(_SYS_ADD_RULE), ctypes.c_int(fd), ctypes.c_int(_RULE_PATH_BENEATH),
                            ctypes.byref(rule), ctypes.c_uint32(0))
            finally:
                os.close(pfd)
        if lib.syscall(ctypes.c_long(_SYS_RESTRICT_SELF), ctypes.c_int(fd), ctypes.c_uint32(0)) != 0:
            return 0, _err("landlock_restrict_self")
    finally:
        os.close(fd)
    return version, ""


def set_non_dumpable() -> bool:
    """PR_SET_DUMPABLE=0 (Linux): /proc/<pid>/{environ,fd,mem,maps} этого процесса принадлежат root и
    закрыты для других процессов того же пользователя, ptrace к нему без CAP_SYS_PTRACE запрещён."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        lib = _libc()
    except (OSError, AttributeError):
        return False
    return lib.prctl(ctypes.c_int(_PR_SET_DUMPABLE), ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0),
                     ctypes.c_ulong(0)) == 0
