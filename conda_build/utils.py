# Copyright (C) 2014 Anaconda, Inc
# SPDX-License-Identifier: BSD-3-Clause
from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import json
import logging
import logging.config
import mmap
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse as urlparse
import urllib.request as urllib
from collections import OrderedDict, defaultdict
from collections.abc import Iterable
from functools import cache, partial
from glob import glob
from io import StringIO
from itertools import filterfalse
from json.decoder import JSONDecodeError
from locale import getpreferredencoding
from os import walk
from os.path import (
    abspath,
    dirname,
    expanduser,
    expandvars,
    getmtime,
    getsize,
    isdir,
    isfile,
    islink,
    join,
)
from pathlib import Path
from threading import Thread
from typing import TYPE_CHECKING, overload

import conda_package_handling.api
import filelock
import libarchive
import yaml
from conda.base.constants import (
    CONDA_PACKAGE_EXTENSION_V1,  # noqa: F401
    CONDA_PACKAGE_EXTENSION_V2,  # noqa: F401
    CONDA_PACKAGE_EXTENSIONS,
    KNOWN_SUBDIRS,
)
from conda.base.context import context
from conda.common.path import win_path_to_unix
from conda.exceptions import CondaHTTPError
from conda.gateways.connection.download import download
from conda.gateways.disk.create import TemporaryDirectory
from conda.gateways.disk.read import compute_sum
from conda.models.channel import Channel
from conda.models.match_spec import MatchSpec
from conda.models.records import PackageRecord
from conda.models.version import VersionOrder
from conda.utils import unix_path_to_win

from .deprecations import deprecated
from .exceptions import BuildLockError

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import TypeVar

    from .metadata import MetaData

    T = TypeVar("T")
    K = TypeVar("K")
    V = TypeVar("V")

on_win = sys.platform == "win32"
on_mac = sys.platform == "darwin"
on_linux = sys.platform == "linux"

codec = getpreferredencoding() or "utf-8"
deprecated.constant(
    "25.3",
    "25.5",
    "root_script_dir",
    os.path.join(context.root_prefix, "Scripts" if on_win else "bin"),
)
mmap_MAP_PRIVATE = 0 if on_win else mmap.MAP_PRIVATE
mmap_PROT_READ = 0 if on_win else mmap.PROT_READ
mmap_PROT_WRITE = 0 if on_win else mmap.PROT_WRITE

DEFAULT_SUBDIRS = set(KNOWN_SUBDIRS)

RUN_EXPORTS_TYPES = {
    "weak",
    "strong",
    "noarch",
    "weak_constrains",
    "strong_constrains",
}

PY_TMPL = r"""
# -*- coding: utf-8 -*-
import re
import sys

from %(module)s import %(import_name)s

if __name__ == '__main__':
    sys.argv[0] = re.sub(r'(-script\.pyw?|\.exe)?$', '', sys.argv[0])
    sys.exit(%(func)s())
"""

# filenames accepted as recipe meta files
VALID_METAS = ("meta.yaml", "meta.yml", "conda.yaml", "conda.yml")

VALID_SCHEMA_LOCATIONS = ("http://schemas.conda.org/", "https://schemas.conda.org/")
FALLBACK_MENUINST_SCHEMA = (
    "https://schemas.conda.org/menuinst/menuinst-1-1-0.schema.json"
)


@cache
def stat_file(path):
    return os.stat(path)


def directory_size_slow(path):
    total_size = 0
    seen = set()

    for root, _, files in walk(path):
        for f in files:
            try:
                stat = stat_file(os.path.join(root, f))
            except OSError:
                continue

            if stat.st_ino in seen:
                continue

            seen.add(stat.st_ino)

            total_size += stat.st_size
    return total_size


def directory_size(path):
    try:
        if on_win:
            command = 'dir /s "{}"'  # Windows path can have spaces
            out = subprocess.check_output(command.format(path), shell=True)
        else:
            command = "du -s {}"
            out = subprocess.check_output(
                command.format(path).split(), stderr=subprocess.PIPE
            )

        if hasattr(out, "decode"):
            try:
                out = out.decode(errors="ignore")
            # This isn't important anyway so give up. Don't try search on bytes.
            except (UnicodeDecodeError, IndexError):
                if on_win:
                    return 0
                else:
                    pass
        if on_win:
            # Windows can give long output, we need only 2nd to last line
            out = out.strip().rsplit("\r\n", 2)[-2]
            pattern = r"\s([\d\W]+).+"  # Language and punctuation neutral
            out = re.search(pattern, out.strip()).group(1).strip()
            out = out.replace(",", "").replace(".", "").replace(" ", "")
        else:
            out = out.split()[0]
    except subprocess.CalledProcessError:
        out = directory_size_slow(path)

    try:
        return int(out)  # size in bytes
    except ValueError:
        return 0


class DummyPsutilProcess:
    def children(self, *args, **kwargs):
        return []


def _setup_rewrite_pipe(env):
    """Rewrite values of env variables back to $ENV in stdout

    Takes output on the pipe and finds any env value
    and rewrites it as the env key

    Useful for replacing "~/conda/conda-bld/pkg_<date>/_h_place..." with "$PREFIX"

    Returns an FD to be passed to Popen(stdout=...)
    """
    # replacements is the env dict reversed,
    # ordered by the length of the value so that longer replacements
    # always occur first in case of common prefixes
    replacements = OrderedDict()
    for k, v in sorted(env.items(), key=lambda kv: len(kv[1]), reverse=True):
        replacements[v] = k

    r_fd, w_fd = os.pipe()
    r = os.fdopen(r_fd, "rt")
    if on_win:
        replacement_t = "%{}%"
    else:
        replacement_t = "${}"

    def rewriter():
        while True:
            try:
                line = r.readline()
                if not line:
                    # reading done
                    r.close()
                    os.close(w_fd)
                    return
                for s, key in replacements.items():
                    line = line.replace(s, replacement_t.format(key))
                sys.stdout.write(line)
            except UnicodeDecodeError:
                try:
                    txt = os.read(r, 10000)
                    sys.stdout.write(txt or "")
                except TypeError:
                    pass

    t = Thread(target=rewriter)
    t.daemon = True
    t.start()

    return w_fd


class PopenWrapper:
    # Small wrapper around subprocess.Popen to allow memory usage monitoring
    # copied from ProtoCI, https://github.com/ContinuumIO/ProtoCI/blob/59159bc2c9f991fbfa5e398b6bb066d7417583ec/protoci/build2.py#L20  # NOQA

    def __init__(self, *args, **kwargs):
        self.elapsed = None
        self.rss = 0
        self.vms = 0
        self.returncode = None
        self.disk = 0
        self.processes = 1

        self.out, self.err = self._execute(*args, **kwargs)

    def _execute(self, *args, **kwargs):
        try:
            import psutil

            psutil_exceptions = (
                psutil.NoSuchProcess,
                psutil.AccessDenied,
                psutil.NoSuchProcess,
            )
        except ImportError as e:
            psutil = None
            psutil_exceptions = (OSError, ValueError)
            log = get_logger(__name__)
            log.warning(f"psutil import failed.  Error was {e}")
            log.warning(
                "only disk usage and time statistics will be available.  Install psutil to "
                "get CPU time and memory usage statistics."
            )

        # The polling interval (in seconds)
        time_int = kwargs.pop("time_int", 2)

        disk_usage_dir = kwargs.get("cwd", sys.prefix)

        # Create a process of this (the parent) process
        parent = psutil.Process(os.getpid()) if psutil else DummyPsutilProcess()

        cpu_usage = defaultdict(dict)

        # Using the convenience Popen class provided by psutil
        start_time = time.time()
        _popen = (
            psutil.Popen(*args, **kwargs)
            if psutil
            else subprocess.Popen(*args, **kwargs)
        )
        try:
            while self.returncode is None:
                # We need to get all of the children of our process since our
                # process spawns other processes.  Collect all of the child
                # processes

                rss = 0
                vms = 0
                processes = 0
                # We use the parent process to get mem usage of all spawned processes
                for child in parent.children(recursive=True):
                    child_cpu_usage = cpu_usage.get(child.pid, {})
                    try:
                        mem = child.memory_info()
                        rss += mem.rss
                        vms += mem.rss
                        # listing child times are only available on linux, so we don't use them.
                        #    we are instead looping over children and getting each individually.
                        #    https://psutil.readthedocs.io/en/latest/#psutil.Process.cpu_times
                        cpu_stats = child.cpu_times()
                        child_cpu_usage["sys"] = cpu_stats.system
                        child_cpu_usage["user"] = cpu_stats.user
                        cpu_usage[child.pid] = child_cpu_usage
                    except psutil_exceptions:
                        # process already died.  Just ignore it.
                        continue
                    processes += 1

                # Sum the memory usage of all the children together (2D columnwise sum)
                self.rss = max(rss, self.rss)
                self.vms = max(vms, self.vms)
                self.cpu_sys = sum(child["sys"] for child in cpu_usage.values())
                self.cpu_user = sum(child["user"] for child in cpu_usage.values())
                self.processes = max(processes, self.processes)

                # Get disk usage
                self.disk = max(directory_size(disk_usage_dir), self.disk)

                time.sleep(time_int)
                self.elapsed = time.time() - start_time
                self.returncode = _popen.poll()

        except KeyboardInterrupt:
            _popen.kill()
            raise

        self.disk = max(directory_size(disk_usage_dir), self.disk)
        self.elapsed = time.time() - start_time
        return _popen.stdout, _popen.stderr

    def __repr__(self):
        return str(
            {
                "elapsed": self.elapsed,
                "rss": self.rss,
                "vms": self.vms,
                "disk": self.disk,
                "processes": self.processes,
                "cpu_user": self.cpu_user,
                "cpu_sys": self.cpu_sys,
                "returncode": self.returncode,
            }
        )


def _func_defaulting_env_to_os_environ(func, *popenargs, **kwargs):
    if "env" not in kwargs:
        kwargs = kwargs.copy()
        env_copy = os.environ.copy()
        kwargs.update({"env": env_copy})
    kwargs["env"] = {str(key): str(value) for key, value in kwargs["env"].items()}
    _args = []
    if "stdin" not in kwargs:
        kwargs["stdin"] = subprocess.PIPE
    for arg in popenargs:
        # arguments to subprocess need to be bytestrings
        if sys.version_info.major < 3 and hasattr(arg, "encode"):
            arg = arg.encode(codec)
        elif sys.version_info.major >= 3 and hasattr(arg, "decode"):
            arg = arg.decode(codec)
        _args.append(str(arg))

    stats = kwargs.get("stats")
    if "stats" in kwargs:
        del kwargs["stats"]

    rewrite_stdout_env = kwargs.pop("rewrite_stdout_env", None)
    if rewrite_stdout_env:
        kwargs["stdout"] = _setup_rewrite_pipe(rewrite_stdout_env)

    out = None
    if stats is not None:
        proc = PopenWrapper(_args, **kwargs)
        if func == "output":
            out = proc.out.read()

        if proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, _args)

        stats.update(
            {
                "elapsed": proc.elapsed,
                "disk": proc.disk,
                "processes": proc.processes,
                "cpu_user": proc.cpu_user,
                "cpu_sys": proc.cpu_sys,
                "rss": proc.rss,
                "vms": proc.vms,
            }
        )
    else:
        if func == "call":
            subprocess.check_call(_args, **kwargs)
        else:
            if "stdout" in kwargs:
                del kwargs["stdout"]
            out = subprocess.check_output(_args, **kwargs)
    return out


def check_call_env(popenargs, **kwargs):
    return _func_defaulting_env_to_os_environ("call", *popenargs, **kwargs)


def check_output_env(popenargs, **kwargs):
    return _func_defaulting_env_to_os_environ(
        "output", stdout=subprocess.PIPE, *popenargs, **kwargs
    ).rstrip()


def bytes2human(n):
    # http://code.activestate.com/recipes/578019
    # >>> bytes2human(10000)
    # '9.8K'
    # >>> bytes2human(100001221)
    # '95.4M'
    symbols = ("K", "M", "G", "T", "P", "E", "Z", "Y")
    prefix = {}
    for i, s in enumerate(symbols):
        prefix[s] = 1 << (i + 1) * 10
    for s in reversed(symbols):
        if n >= prefix[s]:
            value = float(n) / prefix[s]
            return f"{value:.1f}{s}"
    return f"{n}B"


def seconds2human(s):
    m, s = divmod(s, 60)
    h, m = divmod(int(m), 60)
    return f"{h:d}:{m:02d}:{s:04.1f}"


def get_recipe_abspath(recipe):
    """resolve recipe dir as absolute path.  If recipe is a tarball rather than a folder,
    extract it and return the extracted directory.

    Returns the absolute path, and a boolean flag that is true if a tarball has been extracted
    and needs cleanup.
    """
    if isfile(recipe):
        if recipe.lower().endswith(decompressible_exts) or recipe.lower().endswith(
            CONDA_PACKAGE_EXTENSIONS
        ):
            recipe_dir = tempfile.mkdtemp()
            if recipe.lower().endswith(CONDA_PACKAGE_EXTENSIONS):
                import conda_package_handling.api

                conda_package_handling.api.extract(recipe, recipe_dir)
            else:
                tar_xf(recipe, recipe_dir)
            # At some stage the old build system started to tar up recipes.
            recipe_tarfile = os.path.join(recipe_dir, "info", "recipe.tar")
            if isfile(recipe_tarfile):
                tar_xf(recipe_tarfile, os.path.join(recipe_dir, "info"))
            need_cleanup = True
        else:
            print(f"Ignoring non-recipe: {recipe}")
            return (None, None)
    else:
        recipe_dir = abspath(os.path.join(os.getcwd(), recipe))
        need_cleanup = False
    if not os.path.exists(recipe_dir):
        raise ValueError(f"Package or recipe at path {recipe_dir} does not exist")
    return recipe_dir, need_cleanup


@contextlib.contextmanager
def try_acquire_locks(locks, timeout):
    """Try to acquire all locks.

    If any lock can't be immediately acquired, free all locks.
    If the timeout is reached withou acquiring all locks, free all locks and raise.

    http://stackoverflow.com/questions/9814008/multiple-mutex-locking-strategies-and-why-libraries-dont-use-address-comparison
    """
    t = time.time()
    while time.time() - t < timeout:
        # Continuously try to acquire all locks.
        # By passing a short timeout to each individual lock, we give other
        # processes that might be trying to acquire the same locks (and may
        # already hold some of them) a chance to the remaining locks - and
        # hopefully subsequently release them.
        try:
            for lock in locks:
                lock.acquire(timeout=0.1)
        except filelock.Timeout:
            # If we failed to acquire a lock, it is important to release all
            # locks we may have already acquired, to avoid wedging multiple
            # processes that try to acquire the same set of locks.
            # That is, we want to avoid a situation where processes 1 and 2 try
            # to acquire locks A and B, and proc 1 holds lock A while proc 2
            # holds lock B.
            for lock in locks:
                lock.release()
        else:
            break
    else:
        # If we reach this point, we weren't able to acquire all locks within
        # the specified timeout. We shouldn't be holding any locks anymore at
        # this point, so we just raise an exception.
        raise BuildLockError("Failed to acquire all locks")

    try:
        yield
    finally:
        for lock in locks:
            lock.release()


# with each of these, we are copying less metadata.  This seems to be necessary
#   to cope with some shared filesystems with some virtual machine setups.
#  See https://github.com/conda/conda-build/issues/1426
def _copy_with_shell_fallback(src, dst):
    is_copied = False
    for func in (shutil.copy2, shutil.copy, shutil.copyfile):
        try:
            func(src, dst)
            is_copied = True
            break
        except (OSError, PermissionError):
            continue
    if not is_copied:
        try:
            subprocess.check_call(
                f"cp -a {src} {dst}",
                shell=True,
                stderr=subprocess.PIPE,
                stdout=subprocess.PIPE,
            )
        except subprocess.CalledProcessError as e:
            if not os.path.isfile(dst):
                raise OSError(f"Failed to copy {src} to {dst}.  Error was: {e}")


def get_prefix_replacement_paths(src, dst):
    ssplit = src.split(os.path.sep)
    dsplit = dst.split(os.path.sep)
    while ssplit and ssplit[-1] == dsplit[-1]:
        del ssplit[-1]
        del dsplit[-1]
    return os.path.join(*ssplit), os.path.join(*dsplit)


def copy_into(
    src, dst, timeout=900, symlinks=False, lock=None, locking=True, clobber=False
):
    """Copy all the files and directories in src to the directory dst"""
    log = get_logger(__name__)
    if symlinks and islink(src):
        try:
            os.makedirs(os.path.dirname(dst))
        except OSError:
            pass
        if os.path.lexists(dst):
            os.remove(dst)
        src_base, dst_base = get_prefix_replacement_paths(src, dst)
        src_target = os.readlink(src)
        src_replaced = src_target.replace(src_base, dst_base)
        os.symlink(src_replaced, dst)
        try:
            st = os.lstat(src)
            mode = stat.S_IMODE(st.st_mode)
            os.lchmod(dst, mode)
        except:
            pass  # lchmod not available
    elif isdir(src):
        merge_tree(
            src,
            dst,
            symlinks,
            timeout=timeout,
            lock=lock,
            locking=locking,
            clobber=clobber,
        )

    else:
        if isdir(dst):
            dst_fn = os.path.join(dst, os.path.basename(src))
        else:
            dst_fn = dst

        if os.path.isabs(src):
            src_folder = os.path.dirname(src)
        else:
            if os.path.sep in dst_fn:
                src_folder = os.path.dirname(dst_fn)
                if not os.path.isdir(src_folder):
                    os.makedirs(src_folder)
            else:
                src_folder = os.getcwd()

        if os.path.islink(src) and not os.path.exists(os.path.realpath(src)):
            log.warning("path %s is a broken symlink - ignoring copy", src)
            return

        if not lock and locking:
            lock = get_lock(src_folder, timeout=timeout)
        locks = [lock] if locking else []
        with try_acquire_locks(locks, timeout):
            # if intermediate folders not not exist create them
            dst_folder = os.path.dirname(dst)
            if dst_folder and not os.path.exists(dst_folder):
                try:
                    os.makedirs(dst_folder)
                except OSError:
                    pass
            try:
                _copy_with_shell_fallback(src, dst_fn)
            except shutil.Error:
                log.debug(
                    "skipping %s - already exists in %s", os.path.basename(src), dst
                )


def move_with_fallback(src, dst):
    try:
        shutil.move(src, dst)
    except PermissionError:
        try:
            copy_into(src, dst)
            os.unlink(src)
        except PermissionError:
            log = get_logger(__name__)
            log.debug(
                f"Failed to copy/remove path from {src} to {dst} due to permission error"
            )


# http://stackoverflow.com/a/22331852/1170370
def copytree(src, dst, symlinks=False, ignore=None, dry_run=False):
    if not os.path.exists(dst):
        os.makedirs(dst)
        shutil.copystat(src, dst)
    lst = os.listdir(src)
    if ignore:
        excl = ignore(src, lst)
        lst = [x for x in lst if x not in excl]

    # do not copy lock files
    if ".conda_lock" in lst:
        lst.remove(".conda_lock")

    dst_lst = [os.path.join(dst, item) for item in lst]

    if not dry_run:
        for idx, item in enumerate(lst):
            s = os.path.join(src, item)
            d = dst_lst[idx]
            if symlinks and os.path.islink(s):
                if os.path.lexists(d):
                    os.remove(d)
                os.symlink(os.readlink(s), d)
                try:
                    st = os.lstat(s)
                    mode = stat.S_IMODE(st.st_mode)
                    os.lchmod(d, mode)
                except:
                    pass  # lchmod not available
            elif os.path.isdir(s):
                copytree(s, d, symlinks, ignore)
            else:
                _copy_with_shell_fallback(s, d)

    return dst_lst


def is_subdir(child, parent, strict=True):
    """
    Check whether child is a (strict) subdirectory of parent.
    """
    parent = Path(parent).resolve()
    child = Path(child).resolve()
    if strict:
        return parent in child.parents
    return child == parent or parent in child.parents


def merge_tree(
    src, dst, symlinks=False, timeout=900, lock=None, locking=True, clobber=False
):
    """
    Merge src into dst recursively by copying all files from src into dst.
    Return a list of all files copied.

    Like copytree(src, dst), but raises an error if merging the two trees
    would overwrite any files.
    """
    assert not is_subdir(dst, src, strict=False), (
        "Can't merge/copy source into subdirectory of itself.  "
        "Please create separate spaces for these things.\n"
        f"  src: {src}\n"
        f"  dst: {dst}"
    )

    new_files = copytree(src, dst, symlinks=symlinks, dry_run=True)
    existing = [f for f in new_files if isfile(f)]

    if existing and not clobber:
        raise OSError(f"Can't merge {src} into {dst}: file exists: {existing[0]}")

    locks = []
    if locking:
        if not lock:
            lock = get_lock(src, timeout=timeout)
        locks = [lock]
    with try_acquire_locks(locks, timeout):
        copytree(src, dst, symlinks=symlinks)


# purpose here is that we want *one* lock per location on disk.  It can be locked or unlocked
#    at any time, but the lock within this process should all be tied to the same tracking
#    mechanism.
_lock_folders = (
    os.path.join(context.root_prefix, "locks"),
    os.path.expanduser(os.path.join("~", ".conda_build_locks")),
)


def get_lock(folder, timeout=900):
    fl = None
    try:
        location = os.path.abspath(os.path.normpath(folder))
    except OSError:
        location = folder
    b_location = location
    if hasattr(b_location, "encode"):
        b_location = b_location.encode()

    # Hash the entire filename to avoid collisions.
    lock_filename = hashlib.sha256(b_location).hexdigest()

    if hasattr(lock_filename, "decode"):
        lock_filename = lock_filename.decode()
    for locks_dir in _lock_folders:
        try:
            if not os.path.isdir(locks_dir):
                os.makedirs(locks_dir)
            lock_file = os.path.join(locks_dir, lock_filename)
            with open(lock_file, "w") as f:
                f.write("")
            fl = filelock.FileLock(lock_file, timeout)
            break
        except OSError:
            continue
    else:
        raise RuntimeError(
            "Could not write locks folder to either system location ({})"
            "or user location ({}).  Aborting.".format(*_lock_folders)
        )
    return fl


def get_conda_operation_locks(locking=True, bldpkgs_dirs=None, timeout=900):
    locks = []
    bldpkgs_dirs = ensure_list(bldpkgs_dirs)
    # locks enabled by default
    if locking:
        for folder in (*context.pkgs_dirs[:1], *bldpkgs_dirs):
            if not os.path.isdir(folder):
                os.makedirs(folder)
            lock = get_lock(folder, timeout=timeout)
            locks.append(lock)
        # lock used to generally indicate a conda operation occurring
        locks.append(get_lock("conda-operation", timeout=timeout))
    return locks


# This is the lowest common denominator of the formats supported by our libarchive/python-libarchive-c
# packages across all platforms
decompressible_exts = (
    ".7z",
    ".tar",
    ".tar.bz2",
    ".tar.gz",
    ".tar.lzma",
    ".tar.xz",
    ".tar.z",
    ".tar.zst",
    ".tgz",
    ".whl",
    ".zip",
    ".rpm",
    ".deb",
)


def _tar_xf_fallback(tarball, dir_path, mode="r:*"):
    from .os_utils.external import find_executable

    if tarball.lower().endswith(".tar.z"):
        uncompress = find_executable("uncompress")
        if not uncompress:
            uncompress = find_executable("gunzip")
        if not uncompress:
            sys.exit(
                """\
uncompress (or gunzip) is required to unarchive .z source files.
"""
            )
        check_call_env([uncompress, "-f", tarball])
        tarball = tarball[:-2]

    t = tarfile.open(tarball, mode)
    members = t.getmembers()
    for i, member in enumerate(members, 0):
        if os.path.isabs(member.name):
            member.name = os.path.relpath(member.name, "/")
        cwd = os.path.realpath(os.getcwd())
        if not os.path.realpath(member.name).startswith(cwd):
            member.name = member.name.replace("../", "")
        if not os.path.realpath(member.name).startswith(cwd):
            sys.exit("tarball contains unsafe path: " + member.name + " cwd is: " + cwd)
        members[i] = member

    t.extractall(path=dir_path)
    t.close()


def tar_xf_file(tarball, entries):
    entries = ensure_list(entries)
    if not os.path.isabs(tarball):
        tarball = os.path.join(os.getcwd(), tarball)
    result = None
    n_found = 0
    with libarchive.file_reader(tarball) as archive:
        for entry in archive:
            if entry.name in entries:
                n_found += 1
                for block in entry.get_blocks():
                    if result is None:
                        result = bytes(block)
                    else:
                        result += block
                break
    if n_found != len(entries):
        raise KeyError()
    return result


def tar_xf_getnames(tarball):
    if not os.path.isabs(tarball):
        tarball = os.path.join(os.getcwd(), tarball)
    result = []
    with libarchive.file_reader(tarball) as archive:
        for entry in archive:
            result.append(entry.name)
    return result


def tar_xf(tarball, dir_path):
    flags = (
        libarchive.extract.EXTRACT_TIME
        | libarchive.extract.EXTRACT_PERM
        | libarchive.extract.EXTRACT_SECURE_NODOTDOT
        | libarchive.extract.EXTRACT_SECURE_SYMLINKS
        | libarchive.extract.EXTRACT_SECURE_NOABSOLUTEPATHS
    )
    if not os.path.isabs(tarball):
        tarball = os.path.join(os.getcwd(), tarball)
    try:
        with tmp_chdir(os.path.realpath(dir_path)):
            libarchive.extract_file(tarball, flags)
    except libarchive.exception.ArchiveError:
        # try again, maybe we are on Windows and the archive contains symlinks
        # https://github.com/conda/conda-build/issues/3351
        # https://github.com/libarchive/libarchive/pull/1030
        if tarball.lower().endswith(
            (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.z", ".tar.xz")
        ):
            _tar_xf_fallback(tarball, dir_path)
        else:
            raise


def file_info(path):
    return {
        "size": getsize(path),
        "md5": compute_sum(path, "md5"),
        "sha256": compute_sum(path, "sha256"),
        "mtime": getmtime(path),
    }


def comma_join(items: Iterable[str], conjunction: str = "and") -> str:
    """
    Like ', '.join(items) but with and

    Examples:

    >>> comma_join(['a'])
    'a'
    >>> comma_join(['a', 'b'])
    'a and b'
    >>> comma_join(['a', 'b', 'c'])
    'a, b, and c'
    """
    items = tuple(items)
    if len(items) <= 2:
        return f"{items[0]} {conjunction} {items[1]}"
    return f"{', '.join(items[:-1])}, {conjunction} {items[-1]}"


def safe_print_unicode(*args, **kwargs):
    """
    prints unicode strings to stdout using configurable `errors` handler for
    encoding errors

    :param args: unicode strings to print to stdout
    :param sep: separator (defaults to ' ')
    :param end: ending character (defaults to '\n')
    :param errors: error handler for encoding errors (defaults to 'replace')
    """
    sep = kwargs.pop("sep", " ")
    end = kwargs.pop("end", "\n")
    errors = kwargs.pop("errors", "replace")
    func = sys.stdout.buffer.write
    line = sep.join(args) + end
    encoding = sys.stdout.encoding or "utf8"
    func(line.encode(encoding, errors))


def rec_glob(path, patterns, ignores=None):
    """
    Recursively searches path for filename patterns.

    :param path: path within to search for files
    :param patterns: list of filename patterns to search for
    :param ignore: list of directory patterns to ignore in search
    :return: list of paths in path satisfying patterns/ignore
    """
    patterns = ensure_list(patterns)
    ignores = ensure_list(ignores)

    for path, dirs, files in walk(path):
        # remove directories to ignore
        for ignore in ignores:
            for d in fnmatch.filter(dirs, ignore):
                dirs.remove(d)

        # return filepaths that match a pattern
        for pattern in patterns:
            for f in fnmatch.filter(files, pattern):
                yield os.path.join(path, f)


def convert_unix_path_to_win(path):
    from .os_utils.external import find_executable

    if find_executable("cygpath"):
        cmd = f"cygpath -w {path}"
        path = subprocess.getoutput(cmd)

    else:
        path = unix_path_to_win(path)
    return path


def convert_win_path_to_unix(path):
    from .os_utils.external import find_executable

    if find_executable("cygpath"):
        cmd = f"cygpath -u {path}"
        path = subprocess.getoutput(cmd)

    else:
        path = win_path_to_unix(path)
    return path


# Used for translating local paths into url (file://) paths
#   http://stackoverflow.com/a/14298190/1170370
def path2url(path):
    return urlparse.urljoin("file:", urllib.pathname2url(path))


def get_stdlib_dir(prefix, py_ver):
    if on_win:
        lib_dir = os.path.join(prefix, "Lib")
    else:
        lib_dir = os.path.join(prefix, "lib")
        python_folder = glob(os.path.join(lib_dir, "python?.*"), recursive=True)
        python_folder = sorted(filterfalse(islink, python_folder))
        if python_folder:
            lib_dir = os.path.join(lib_dir, python_folder[0])
        else:
            lib_dir = os.path.join(lib_dir, f"python{py_ver}")
    return lib_dir


def get_site_packages(prefix, py_ver):
    return os.path.join(get_stdlib_dir(prefix, py_ver), "site-packages")


def get_build_folders(croot: str | os.PathLike | Path) -> list[str]:
    # remember, glob is not a regex.
    return glob(os.path.join(croot, "*" + "[0-9]" * 10 + "*"), recursive=True)


def prepend_bin_path(env, prefix, prepend_prefix=False):
    env["PATH"] = join(prefix, "bin") + os.pathsep + env["PATH"]
    if on_win:
        env["PATH"] = (
            join(prefix, "Library", "mingw-w64", "bin")
            + os.pathsep
            + join(prefix, "Library", "usr", "bin")
            + os.pathsep
            + join(prefix, "Library", "bin")
            + os.pathsep
            + join(prefix, "Scripts")
            + os.pathsep
            + env["PATH"]
        )
        prepend_prefix = True  # windows has Python in the prefix.  Use it.
    if prepend_prefix:
        env["PATH"] = prefix + os.pathsep + env["PATH"]
    return env


# not currently used.  Leaving in because it may be useful for when we do things
#   like load setup.py data, and we need the modules from some prefix other than
#   the root prefix, which is what conda-build runs from.
@contextlib.contextmanager
def sys_path_prepended(prefix):
    path_backup = sys.path[:]
    if on_win:
        sys.path.insert(1, os.path.join(prefix, "lib", "site-packages"))
    else:
        lib_dir = os.path.join(prefix, "lib")
        python_dir = glob(os.path.join(lib_dir, r"python[0-9\.]*"), recursive=True)
        if python_dir:
            python_dir = python_dir[0]
            sys.path.insert(1, os.path.join(python_dir, "site-packages"))
    try:
        yield
    finally:
        sys.path = path_backup


@contextlib.contextmanager
def path_prepended(prefix, prepend_prefix=True):
    # FIXME: Unclear why prepend_prefix=True for all platforms.
    old_path = os.environ["PATH"]
    os.environ["PATH"] = prepend_bin_path(os.environ.copy(), prefix, prepend_prefix)[
        "PATH"
    ]
    try:
        yield
    finally:
        os.environ["PATH"] = old_path


bin_dirname = "Scripts" if on_win else "bin"

entry_pat = re.compile(r"\s*([\w\-\.]+)\s*=\s*([\w.]+):([\w.]+)\s*$")


def iter_entry_points(items):
    for item in items:
        m = entry_pat.match(item)
        if m is None:
            sys.exit(f"Error cound not match entry point: {item!r}")
        yield m.groups()


def create_entry_point(path, module, func, config):
    """Creates an entry point for legacy noarch_python builds"""
    import_name = func.split(".")[0]
    pyscript = PY_TMPL % {"module": module, "func": func, "import_name": import_name}
    if on_win:
        with open(path + "-script.py", "w") as fo:
            if os.path.isfile(os.path.join(config.host_prefix, "python_d.exe")):
                fo.write("#!python_d\n")
            fo.write(pyscript)
            copy_into(
                join(dirname(__file__), f"cli-{str(config.host_arch)}.exe"),
                path + ".exe",
                config.timeout,
            )
    else:
        if os.path.islink(path):
            os.remove(path)
        with open(path, "w") as fo:
            if not config.noarch:
                fo.write(f"#!{config.host_python}\n")
            fo.write(pyscript)
        os.chmod(path, 0o775)


def create_entry_points(items, config):
    """Creates entry points for legacy noarch_python builds"""
    if not items:
        return
    bin_dir = join(config.host_prefix, bin_dirname)
    if not isdir(bin_dir):
        os.mkdir(bin_dir)
    for cmd, module, func in iter_entry_points(items):
        create_entry_point(join(bin_dir, cmd), module, func, config)


# Return all files in dir, and all its subdirectories, ending in pattern
def get_ext_files(start_path, pattern):
    for root, _, files in walk(start_path):
        for f in files:
            if f.endswith(pattern):
                yield os.path.join(root, f)


_posix_exes_cache = {}


def convert_path_for_cygwin_or_msys2(exe, path):
    "If exe is a Cygwin or MSYS2 executable then filters it through `cygpath -u`"
    if not on_win:
        return path
    if exe not in _posix_exes_cache:
        with open(exe, "rb") as exe_file:
            exe_binary = exe_file.read()
            msys2_cygwin = re.findall(b"(cygwin1.dll|msys-2.0.dll)", exe_binary)
            _posix_exes_cache[exe] = True if msys2_cygwin else False
    if _posix_exes_cache[exe]:
        try:
            path = (
                check_output_env(["cygpath", "-u", path])
                .splitlines()[0]
                .decode(getpreferredencoding())
            )
        except OSError:
            log = get_logger(__name__)
            log.debug(
                "cygpath executable not found.  Passing native path.  This is OK for msys2."
            )
    return path


def get_skip_message(m: MetaData) -> str:
    return (
        f"Skipped: {m.name()} from {m.path} defines build/skip for this configuration "
        f"({ ({k: m.config.variant[k] for k in m.get_used_vars()}) })."
    )


def package_has_file(package_path, file_path, refresh_mode="modified"):
    # This version does nothing to the package cache.
    with TemporaryDirectory() as td:
        if file_path.startswith("info"):
            conda_package_handling.api.extract(
                package_path, dest_dir=td, components="info"
            )
        elif package_path.endswith(".tar.bz2"):
            conda_package_handling.api.extract(
                package_path, dest_dir=td, components=file_path
            )
        else:
            conda_package_handling.api.extract(
                package_path, dest_dir=td, components="pkg"
            )
        resolved_file_path = os.path.join(td, file_path)
        if os.path.exists(resolved_file_path):
            # TODO :: Remove this text-mode load. Files are binary.
            try:
                with open(resolved_file_path) as f:
                    content = f.read()
            except UnicodeDecodeError:
                with open(resolved_file_path, "rb") as f:
                    content = f.read()
        else:
            content = False
        return content


def ensure_list(arg: T | Iterable[T] | None, include_dict: bool = True) -> list[T]:
    """
    Ensure the object is a list. If not return it in a list.

    :param arg: Object to ensure is a list
    :type arg: any
    :param include_dict: Whether to treat `dict` as a `list`
    :type include_dict: bool, optional
    :return: `arg` as a `list`
    :rtype: list
    """
    if arg is None:
        return []
    elif islist(arg, include_dict=include_dict):
        return list(arg)
    else:
        return [arg]


def islist(
    arg: T | Iterable[T],
    uniform: bool = False,
    include_dict: bool = True,
) -> bool:
    """
    Check whether `arg` is a `list`. Optionally determine whether the list elements
    are all uniform.

    When checking for generic uniformity (`uniform=True`) we check to see if all
    elements are of the first element's type (`type(arg[0]) == type(arg[1])`). For
    any other kinds of uniformity checks are desired provide a uniformity function:

    .. code-block:: pycon
        # uniformity function checking if elements are str and not empty
        >>> truthy_str = lambda e: isinstance(e, str) and e
        >>> islist(["foo", "bar"], uniform=truthy_str)
        True
        >>> islist(["", "bar"], uniform=truthy_str)
        False
        >>> islist([0, "bar"], uniform=truthy_str)
        False

    .. note::
        Testing for uniformity will consume generators.

    :param arg: Object to ensure is a `list`
    :type arg: any
    :param uniform: Whether to check for uniform or uniformity function
    :type uniform: bool, function, optional
    :param include_dict: Whether to treat `dict` as a `list`
    :type include_dict: bool, optional
    :return: Whether `arg` is a `list`
    :rtype: bool
    """
    if isinstance(arg, str) or not isinstance(arg, Iterable):
        # str and non-iterables are not lists
        return False
    elif not include_dict and isinstance(arg, dict):
        # do not treat dict as a list
        return False
    elif not uniform:
        # short circuit for non-uniformity
        return True

    # NOTE: not checking for Falsy arg since arg may be a generator
    # WARNING: if uniform != False and arg is a generator then arg will be consumed

    if uniform is True:
        arg = iter(arg)
        try:
            etype = type(next(arg))
        except StopIteration:
            # StopIteration: list is empty, an empty list is still uniform
            return True
        # check for explicit type match, do not allow the ambiguity of isinstance
        uniform = lambda e: type(e) == etype  # noqa: E721

    try:
        return all(uniform(e) for e in arg)
    except (ValueError, TypeError):
        # ValueError, TypeError: uniform function failed
        return False


@contextlib.contextmanager
def tmp_chdir(dest):
    curdir = os.getcwd()
    try:
        os.chdir(dest)
        yield
    finally:
        os.chdir(curdir)


def expand_globs(
    path_list: str | os.PathLike | Path | Iterable[str | os.PathLike | Path],
    root_dir: str | os.PathLike | Path,
) -> list[str]:
    files = []
    for path in ensure_list(path_list):
        path = str(path)
        if not os.path.isabs(path):
            path = os.path.join(root_dir, path)
        if os.path.isfile(path):
            files.append(path)
        elif os.path.islink(path):
            files.append(path)
        elif os.path.isdir(path):
            for root, dirnames, fs in walk(path):
                files.extend(os.path.join(root, f) for f in fs)
                for folder in dirnames:
                    if os.path.islink(os.path.join(root, folder)):
                        files.append(os.path.join(root, folder))
        else:
            # File compared to the globs use / as separator independently of the os
            glob_files = glob(path, recursive=True)
            if not glob_files:
                log = get_logger(__name__)
                log.warning(f"Glob {path} did not match in root_dir {root_dir}")
            # https://docs.python.org/3/library/glob.html#glob.glob states that
            # "whether or not the results are sorted depends on the file system".
            # Avoid this potential ambiguity by sorting. (see #4185)
            files.extend(sorted(glob_files))
    prefix_path_re = re.compile("^" + re.escape(f"{root_dir}{os.path.sep}"))
    return [prefix_path_re.sub("", f, 1) for f in files]


def find_recipe(path: str) -> str:
    """recurse through a folder, locating valid meta files (see VALID_METAS).  Raises error if more than one is found.

    Returns full path to meta file to be built.

    If we have a base level meta file and other supplemental (nested) ones, use the base level.
    """
    # if initial path is absolute then any path we find (via rec_glob)
    # will also be absolute
    if not os.path.isabs(path):
        path = os.path.normpath(os.path.join(os.getcwd(), path))

    if os.path.isfile(path):
        if os.path.basename(path) in VALID_METAS:
            return path
        raise OSError(
            "{} is not a valid meta file ({})".format(path, ", ".join(VALID_METAS))
        )

    results = list(rec_glob(path, VALID_METAS, ignores=(".AppleDouble",)))

    if not results:
        raise OSError(
            "No meta files ({}) found in {}".format(", ".join(VALID_METAS), path)
        )

    if len(results) == 1:
        return results[0]

    # got multiple valid meta files
    # check if a meta file is defined on the base level in which case use that one

    metas = [m for m in VALID_METAS if os.path.isfile(os.path.join(path, m))]
    if len(metas) == 1:
        get_logger(__name__).warning(
            "Multiple meta files found. "
            f"The {metas[0]} file in the base directory ({path}) "
            "will be used."
        )
        return os.path.join(path, metas[0])

    raise OSError(
        "More than one meta files ({}) found in {}".format(", ".join(VALID_METAS), path)
    )


class LoggingContext:
    default_loggers = [
        "conda",
        "binstar",
        "install",
        "conda.install",
        "fetch",
        "conda.instructions",
        "fetch.progress",
        "print",
        "progress",
        "dotupdate",
        "stdoutlog",
        "requests",
        "conda.core.package_cache_data",
        "conda.plan",
        "conda.gateways.disk.delete",
        "conda_build",
        "conda_build.index",
        "conda_build.noarch_python",
        "urllib3.connectionpool",
        "conda_index",
        "conda_index.index",
        "conda_index.index.convert_cache",
    ]

    def __init__(self, level=logging.WARN, handler=None, close=True, loggers=None):
        self.level = level
        self.old_levels = {}
        self.handler = handler
        self.close = close
        self.quiet = context.quiet
        if not loggers:
            self.loggers = LoggingContext.default_loggers
        else:
            self.loggers = loggers

    def __enter__(self):
        for logger in self.loggers:
            if isinstance(logger, str):
                log = logging.getLogger(logger)
            self.old_levels[logger] = log.level
            log.setLevel(
                self.level
                if ("install" not in logger or self.level < logging.INFO)
                else self.level + 10
            )
        if self.handler:
            self.logger.addHandler(self.handler)

        context.quiet = True

    def __exit__(self, et, ev, tb):
        for logger, level in self.old_levels.items():
            logging.getLogger(logger).setLevel(level)
        if self.handler:
            self.logger.removeHandler(self.handler)
        if self.handler and self.close:
            self.handler.close()

        context.quiet = self.quiet

        # implicit return of None => don't swallow exceptions


def get_installed_packages(path):
    """
    Scan all json files in 'path' and return a dictionary with their contents.
    Files are assumed to be in 'index.json' format.
    """
    installed = dict()
    for filename in glob(os.path.join(path, "conda-meta", "*.json"), recursive=True):
        with open(filename) as file:
            data = json.load(file)
            installed[data["name"]] = data
    return installed


# http://stackoverflow.com/a/10743550/1170370
@contextlib.contextmanager
def capture():
    import sys

    oldout, olderr = sys.stdout, sys.stderr
    try:
        out = [StringIO(), StringIO()]
        sys.stdout, sys.stderr = out
        yield out
    finally:
        sys.stdout, sys.stderr = oldout, olderr
        out[0] = out[0].getvalue()
        out[1] = out[1].getvalue()


# copied from conda; added in 4.3, not currently part of exported functionality
@contextlib.contextmanager
def env_var(name, value, callback=None):
    # NOTE: will likely want to call reset_context() when using this function, so pass
    #       it as callback
    name, value = str(name), str(value)
    saved_env_var = os.environ.get(name)
    try:
        os.environ[name] = value
        if callback:
            callback()
        yield
    finally:
        if saved_env_var:
            os.environ[name] = saved_env_var
        else:
            del os.environ[name]
        if callback:
            callback()


def trim_empty_keys(dict_):
    to_remove = set()
    negative_means_empty = ("final", "noarch_python", "zip_keys")
    for k, v in dict_.items():
        if hasattr(v, "keys"):
            trim_empty_keys(v)
        # empty lists and empty strings, and None are always empty.
        if v == list() or v == "" or v is None or v == dict():
            to_remove.add(k)
        # other things that evaluate as False may not be "empty" - things can be manually set to
        #     false, and we need to keep that setting.
        if not v and k in negative_means_empty:
            to_remove.add(k)
    if "zip_keys" in dict_ and not any(v for v in dict_["zip_keys"]):
        to_remove.add("zip_keys")
    for k in to_remove:
        del dict_[k]


def _increment(version, alpha_ver):
    try:
        if alpha_ver:
            suffix = "a"
        else:
            suffix = ".0a0"
        last_version = str(int(version) + 1) + suffix
    except ValueError:
        last_version = chr(ord(version) + 1)
    return last_version


def apply_pin_expressions(version, min_pin="x.x.x.x.x.x.x", max_pin="x"):
    pins = [len(p.split(".")) if p else None for p in (min_pin, max_pin)]
    parsed_version = VersionOrder(version).version[1:]
    nesting_position = None
    flat_list = []
    for idx, item in enumerate(parsed_version):
        if isinstance(item, list):
            nesting_position = idx
            flat_list.extend(item)
        else:
            flat_list.append(item)
    if max_pin and len(max_pin.split(".")) > len(flat_list):
        pins[1] = len(flat_list)
    versions = ["", ""]
    # first idx is lower bound pin; second is upper bound pin.
    #    pin value is number of places to pin.
    for p_idx, pin in enumerate(pins):
        if pin:
            # flat_list is the blown-out representation of the version
            for v_idx, v in enumerate(flat_list[:pin]):
                # upper bound pin
                if p_idx == 1 and v_idx == pin - 1:
                    # is the last place an alphabetic character?  OpenSSL, JPEG
                    alpha_ver = str(flat_list[min(pin, len(flat_list) - 1)]).isalpha()
                    v = _increment(v, alpha_ver)
                versions[p_idx] += str(v)
                if v_idx != nesting_position:
                    versions[p_idx] += "."
            if versions[p_idx][-1] == ".":
                versions[p_idx] = versions[p_idx][:-1]
    if versions[0]:
        if version.endswith(".*"):
            version_order = VersionOrder(version[:-2])
        elif version.endswith("*"):
            version_order = VersionOrder(version[:-1])
        else:
            version_order = VersionOrder(version)
        if version_order < VersionOrder(versions[0]):
            # If the minimum is greater than the version this is a pre-release build.
            # Use the version as the lower bound
            versions[0] = ">=" + version
        else:
            versions[0] = ">=" + versions[0]
    if versions[1]:
        versions[1] = "<" + versions[1]
    return ",".join([v for v in versions if v])


def filter_files(
    files_list,
    prefix,
    filter_patterns=(
        r"(.*[\\/])?\.git[\\/].*",
        r"(.*[\\/])?\.git$",
        r"(.*)?\.DS_Store.*",
        r".*\.la$",
        r"conda-meta.*",
        r".*\.conda_trash(?:_\d+)*$",
    ),
):
    """Remove things like the .git directory from the list of files to be copied"""
    for pattern in filter_patterns:
        r = re.compile(pattern)
        files_list = set(files_list) - set(filter(r.match, files_list))
    return [
        f
        for f in files_list
        if not os.path.isdir(os.path.join(prefix, f))
        or os.path.islink(os.path.join(prefix, f))
    ]


def filter_info_files(files_list, prefix):
    return filter_files(
        files_list,
        prefix,
        filter_patterns=(
            "info[\\\\/]index.json",
            "info[\\\\/]files",
            "info[\\\\/]paths.json",
            "info[\\\\/]about.json",
            "info[\\\\/]has_prefix",
            "info[\\\\/]hash_input_files",  # legacy, not used anymore
            "info[\\\\/]hash_input.json",
            "info[\\\\/]run_exports.yaml",  # legacy
            "info[\\\\/]run_exports.json",  # current
            "info[\\\\/]git",
            "info[\\\\/]recipe[\\\\/].*",
            "info[\\\\/]recipe_log.json",
            "info[\\\\/]recipe.tar",
            "info[\\\\/]test[\\\\/].*",
            "info[\\\\/]LICENSE.txt",  # legacy, some tests rely on this
            "info[\\\\/]licenses[\\\\/]*",
            "info[\\\\/]prelink_messages[\\\\/]*",
            "info[\\\\/]requires",
            "info[\\\\/]meta",
            "info[\\\\/]platform",
            "info[\\\\/]no_link",
            "info[\\\\/]link.json",
            "info[\\\\/]icon.png",
        ),
    )


def rm_rf(path: str | os.PathLike) -> None:
    from conda.core.prefix_data import delete_prefix_from_linked_data
    from conda.gateways.disk.delete import rm_rf

    rm_rf(str(path))
    delete_prefix_from_linked_data(str(path))


# https://stackoverflow.com/a/31459386/1170370
class LessThanFilter(logging.Filter):
    def __init__(self, exclusive_maximum, name=""):
        super().__init__(name)
        self.max_level = exclusive_maximum

    def filter(self, record):
        # non-zero return means we log this message
        return 1 if record.levelno < self.max_level else 0


class GreaterThanFilter(logging.Filter):
    def __init__(self, exclusive_minimum, name=""):
        super().__init__(name)
        self.min_level = exclusive_minimum

    def filter(self, record):
        # non-zero return means we log this message
        return 1 if record.levelno > self.min_level else 0


# unclutter logs - show messages only once
class DuplicateFilter(logging.Filter):
    def __init__(self):
        self.msgs = set()

    def filter(self, record):
        log = record.msg not in self.msgs
        self.msgs.add(record.msg)
        return int(log)


dedupe_filter = DuplicateFilter()
info_debug_stdout_filter = LessThanFilter(logging.WARNING)
warning_error_stderr_filter = GreaterThanFilter(logging.INFO)
level_formatter = logging.Formatter("%(levelname)s: %(message)s")

# set filelock's logger to only show warnings by default
logging.getLogger("filelock").setLevel(logging.WARN)

# quiet some of conda's less useful output
logging.getLogger("conda.core.linked_data").setLevel(logging.WARN)
logging.getLogger("conda.gateways.disk.delete").setLevel(logging.WARN)
logging.getLogger("conda.gateways.disk.test").setLevel(logging.WARN)


def reset_deduplicator():
    """Most of the time, we want the deduplication.  There are some cases (tests especially)
    where we want to be able to control the duplication."""
    global dedupe_filter
    dedupe_filter = DuplicateFilter()


def get_logger(name, level=logging.INFO, dedupe=True, add_stdout_stderr_handlers=True):
    config_file = None
    if log_config_file := context.conda_build.get("log_config_file"):
        config_file = abspath(expanduser(expandvars(log_config_file)))
    # by loading config file here, and then only adding handlers later, people
    # should be able to override conda-build's logger settings here.
    if config_file:
        with open(config_file) as f:
            config_dict = yaml.safe_load(f)
        logging.config.dictConfig(config_dict)
        level = config_dict.get("loggers", {}).get(name, {}).get("level", level)
    log = logging.getLogger(name)
    if log.level != level:
        log.setLevel(level)
    if dedupe:
        log.addFilter(dedupe_filter)

    # these are defaults.  They can be overridden by configuring a log config yaml file.
    top_pkg = name.split(".")[0]
    if top_pkg == "conda_build":
        # we don't want propagation in CLI, but we do want it in tests
        # this is a pytest limitation: https://github.com/pytest-dev/pytest/issues/3697
        logging.getLogger(top_pkg).propagate = "PYTEST_CURRENT_TEST" in os.environ
    if add_stdout_stderr_handlers and not log.handlers:
        stdout_handler = logging.StreamHandler(sys.stdout)
        stderr_handler = logging.StreamHandler(sys.stderr)
        stdout_handler.addFilter(info_debug_stdout_filter)
        stderr_handler.addFilter(warning_error_stderr_filter)
        stderr_handler.setFormatter(level_formatter)
        stdout_handler.setLevel(level)
        stderr_handler.setLevel(level)
        log.addHandler(stdout_handler)
        log.addHandler(stderr_handler)
    return log


def _equivalent(base_value, value, path):
    equivalent = value == base_value
    if isinstance(value, str) and isinstance(base_value, str):
        if not os.path.isabs(base_value):
            base_value = os.path.abspath(
                os.path.normpath(os.path.join(path, base_value))
            )
        if not os.path.isabs(value):
            value = os.path.abspath(os.path.normpath(os.path.join(path, value)))
        equivalent |= base_value == value
    return equivalent


def merge_or_update_dict(
    base, new, path="", merge=True, raise_on_clobber=False, add_missing_keys=True
):
    if base == new:
        return base
    log = get_logger(__name__)
    for key, value in new.items():
        if key in base or add_missing_keys:
            base_value = base.get(key, value)
            if hasattr(value, "keys"):
                base_value = merge_or_update_dict(
                    base_value, value, path, merge, raise_on_clobber=raise_on_clobber
                )
                base[key] = base_value
            elif hasattr(value, "__iter__") and not isinstance(value, str):
                if merge:
                    if base_value != value:
                        try:
                            base_value.extend(value)
                        except (TypeError, AttributeError):
                            base_value = value
                    try:
                        base[key] = list(base_value)
                    except TypeError:
                        base[key] = base_value
                else:
                    base[key] = value
            else:
                if (
                    base_value
                    and merge
                    and not _equivalent(base_value, value, path)
                    and raise_on_clobber
                ):
                    log.debug(
                        f"clobbering key {key} (original value {base_value}) with value {value}"
                    )
                if value is None and key in base:
                    del base[key]
                else:
                    base[key] = value
    return base


def merge_dicts_of_lists(
    dol1: Mapping[K, Iterable[V]],
    dol2: Mapping[K, Iterable[V]],
) -> dict[K, list[V]]:
    """
    From Alex Martelli: https://stackoverflow.com/a/1495821/3257826
    """
    keys = set(dol1).union(dol2)
    no = []
    return {k: dol1.get(k, no) + dol2.get(k, no) for k in keys}


def prefix_files(prefix: str | os.PathLike | Path) -> set[str]:
    """
    Returns a set of all files in prefix.
    """
    prefix = f"{os.path.abspath(prefix)}{os.path.sep}"
    prefix_files: set[str] = set()
    for root, directories, files in walk(prefix):
        # this is effectively os.path.relpath, just hacked to be faster
        relroot = root[len(prefix) :].lstrip(os.path.sep)
        # add all files
        prefix_files.update(join(relroot, file) for file in files)
        # add all symlink directories (they are "files")
        prefix_files.update(
            join(relroot, directory)
            for directory in directories
            if islink(join(root, directory))
        )
    return prefix_files


def mmap_mmap(
    fileno,
    length,
    tagname=None,
    flags=0,
    prot=mmap_PROT_READ | mmap_PROT_WRITE,
    access=None,
    offset=0,
):
    """
    Hides the differences between mmap.mmap on Windows and Unix.
    Windows has `tagname`.
    Unix does not, but makes up for it with `flags` and `prot`.
    On both, the default value for `access` is determined from how the file
    was opened so must not be passed in at all to get this default behaviour.
    """
    if on_win:
        if access:
            return mmap.mmap(
                fileno, length, tagname=tagname, access=access, offset=offset
            )
        else:
            return mmap.mmap(fileno, length, tagname=tagname)
    else:
        if access:
            return mmap.mmap(
                fileno, length, flags=flags, prot=prot, access=access, offset=offset
            )
        else:
            return mmap.mmap(fileno, length, flags=flags, prot=prot)


def remove_pycache_from_scripts(build_prefix):
    """Remove pip created pycache directory from bin or Scripts."""
    if on_win:
        scripts_path = os.path.join(build_prefix, "Scripts")
    else:
        scripts_path = os.path.join(build_prefix, "bin")

    if os.path.isdir(scripts_path):
        for entry in os.listdir(scripts_path):
            entry_path = os.path.join(scripts_path, entry)
            if os.path.isdir(entry_path) and entry.strip(os.sep) == "__pycache__":
                shutil.rmtree(entry_path)

            elif os.path.isfile(entry_path) and entry_path.endswith(".pyc"):
                os.remove(entry_path)


def sort_list_in_nested_structure(dictionary, omissions=""):
    """Recurse through a nested dictionary and sort any lists that are found.

    If the list that is found contains anything but strings, it is skipped
    as we can't compare lists containing different types. The omissions argument
    allows for certain sections of the dictionary to be omitted from sorting.
    """
    for field, value in dictionary.items():
        if isinstance(value, dict):
            for key in value.keys():
                section = dictionary[field][key]
                if isinstance(section, dict):
                    sort_list_in_nested_structure(section)
                elif (
                    isinstance(section, list)
                    and f"{field}/{key}" not in omissions
                    and all(isinstance(item, str) for item in section)
                ):
                    section.sort()

        # there's a possibility for nested lists containing dictionaries
        # in this case we recurse until we find a list to sort
        elif isinstance(value, list):
            for element in value:
                if isinstance(element, dict):
                    sort_list_in_nested_structure(element)
            try:
                value.sort()
            except TypeError:
                pass


# group one: package name
# group two: version (allows _, +, . in version)
# group three: build string - mostly not used here.  Match primarily matters
#        to specify when not to add .*

# if you are seeing mysterious unsatisfiable errors, with the package you're building being the
#    unsatisfiable part, then you probably need to update this regex.

spec_needing_star_re = re.compile(
    r"([\w\d\.\-\_]+)\s+((?<![><=])[\w\d\.\-\_]+?(?!\*))(\s+[\w\d\.\_]+)?$"
)  # NOQA
spec_ver_needing_star_re = re.compile(r"^([0-9a-zA-Z\.]+)$")


@overload
def ensure_valid_spec(spec: str, warn: bool = False) -> str: ...


@overload
def ensure_valid_spec(spec: MatchSpec, warn: bool = False) -> MatchSpec: ...


def ensure_valid_spec(spec: str | MatchSpec, warn: bool = False) -> str | MatchSpec:
    if isinstance(spec, MatchSpec):
        if (
            hasattr(spec, "version")
            and spec.version
            and (not spec.get("build", ""))
            and spec_ver_needing_star_re.match(str(spec.version))
        ):
            if str(spec.name) not in ("python", "numpy") or str(spec.version) != "x.x":
                spec = MatchSpec(
                    "{} {}".format(str(spec.name), str(spec.version) + ".*")
                )
    else:
        match = spec_needing_star_re.match(spec)
        # ignore exact pins (would be a 3rd group)
        if match and not match.group(3):
            if match.group(1) in ("python", "numpy") and match.group(2) == "x.x":
                spec = spec_needing_star_re.sub(r"\1 \2", spec)
            else:
                if "*" not in spec:
                    if match.group(1) not in ("python", "vc") and warn:
                        log = get_logger(__name__)
                        log.warning(
                            f"Adding .* to spec '{spec}' to ensure satisfiability.  Please "
                            "consider putting {{{{ var_name }}}}.* or some relational "
                            "operator (>/</>=/<=) on this spec in meta.yaml, or if req is "
                            "also a build req, using {{{{ pin_compatible() }}}} jinja2 "
                            "function instead.  See "
                            "https://conda.io/docs/user-guide/tasks/build-packages/variants.html#pinning-at-the-variant-level"
                        )
                    spec = spec_needing_star_re.sub(r"\1 \2.*", spec)
    return spec


def insert_variant_versions(requirements_dict, variant, env):
    build_deps = ensure_list(requirements_dict.get("build")) + ensure_list(
        requirements_dict.get("host")
    )
    reqs = ensure_list(requirements_dict.get(env))
    for key, val in variant.items():
        regex = re.compile(r"^({})(?:\s*$)".format(key.replace("_", "[-_]")))
        matches = [regex.match(pkg) for pkg in reqs]
        if any(matches):
            for i, x in enumerate(matches):
                if x and (env in ("build", "host") or x.group(1) in build_deps):
                    del reqs[i]
                    if not isinstance(val, str):
                        val = val[0]
                    reqs.insert(i, ensure_valid_spec(" ".join((x.group(1), val))))

    xx_re = re.compile(r"([0-9a-zA-Z\.\-\_]+)\s+x\.x")

    matches = [xx_re.match(pkg) for pkg in reqs]
    if any(matches):
        for i, x in enumerate(matches):
            if x:
                del reqs[i]
                reqs.insert(
                    i,
                    ensure_valid_spec(" ".join((x.group(1), variant.get(x.group(1))))),
                )
    if reqs:
        requirements_dict[env] = reqs


def match_peer_job(target_matchspec, other_m, this_m=None):
    """target_matchspec comes from the recipe.  target_variant is the variant from the recipe whose
    deps we are matching.  m is the peer job, which must satisfy conda and also have matching keys
    for any keys that are shared between target_variant and m.config.variant"""
    name, version, build = other_m.name(), other_m.version(), ""
    matchspec_matches = target_matchspec.match(
        PackageRecord(
            name=name,
            version=version,
            build=build,
            build_number=other_m.build_number(),
        )
    )

    variant_matches = True
    if this_m:
        other_m_used_vars = other_m.get_used_loop_vars()
        for v in this_m.get_used_loop_vars():
            if v in other_m_used_vars:
                variant_matches &= this_m.config.variant[v] == other_m.config.variant[v]
    return matchspec_matches and variant_matches


def expand_reqs(reqs_entry):
    if not hasattr(reqs_entry, "keys"):
        original = ensure_list(reqs_entry)[:]
        reqs_entry = (
            {"host": ensure_list(original), "run": ensure_list(original)}
            if original
            else {}
        )
    else:
        for sec in reqs_entry:
            reqs_entry[sec] = ensure_list(reqs_entry[sec])
    return reqs_entry


def sha256_checksum(filename, buffersize=65536):
    if islink(filename) and not isfile(filename):
        # symlink to nowhere so an empty file
        # this is the sha256 hash of an empty file
        return "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    if not isfile(filename):
        return None
    sha256 = hashlib.sha256()
    with open(filename, "rb") as f:
        for block in iter(lambda: f.read(buffersize), b""):
            sha256.update(block)
    return sha256.hexdigest()


def compute_content_hash(
    directory: str | Path, algorithm="sha256", skip: Iterable[str] = ()
) -> str:
    """
    Given a directory, recursively scan all its contents (without following symlinks) and sort them
    by their full path. For each entry in the contents table, compute the hash for the concatenated
    bytes of:

    - UTF-8 encoded path, relative to the input directory. Backslashes are normalized
      to forward slashes before encoding.
    - Then, depending on the type:
        - For regular files, the UTF-8 bytes of an `F` separator, followed by:
          - UTF-8 bytes of the line-ending normalized text (`\r\n` to `\n`), if the file is text.
          - The raw bytes of the file contents, if binary.
          - If it can't be read, error out.
        - For a directory, the UTF-8 bytes of a `D` separator, and nothing else.
        - For a symlink, the UTF-8 bytes of an `L` separator, followed by the UTF-8 encoded bytes
          for the path it points to. Backslashes MUST be normalized to forward slashes before
          encoding.
        - For any other types, error out.
    - UTF-8 encoded bytes of the string `-`, as separator.

    Parameters
    ----------
    directory: The path whose contents will be hashed
    algorithm: Name of the algorithm to be used, as expected by `hashlib.new()`
    skip: iterable of paths that should not be checked. If a path ends with a slash, it's
          interpreted as a directory that won't be traversed. It matches the relative paths
          already slashed-normalized (i.e. backwards slashes replaced with forward slashes).

    Returns
    -------
    str
        The hexdigest of the computed hash, as described above.
    """
    hasher = hashlib.new(algorithm)
    for path in sorted(Path(directory).rglob("*"), key=str):
        relpath = path.relative_to(directory)
        relpathstr = str(relpath).replace("\\", "/")
        if skip and any(
            (
                # Skip directories like .git/
                skip_item.endswith("/")
                and relpathstr.startswith(skip_item)
                or f"{relpathstr}/" == skip_item
            )
            # Skip full relpath match
            or relpathstr == skip_item
            for skip_item in skip
        ):
            continue
        # encode the relative path to directory, for files, dirs and others
        hasher.update(relpathstr.encode("utf-8"))
        if path.is_symlink():
            hasher.update(b"L")
            hasher.update(str(path.readlink()).replace("\\", "/").encode("utf-8"))
        elif path.is_dir():
            hasher.update(b"D")
        elif path.is_file():
            hasher.update(b"F")
            # We need to normalize line endings for Windows-Unix compat
            # Attempt normalized line-by-line hashing (text mode). If
            # Python fails to open in text mode, then it's binary and we hash
            # the raw bytes directly.
            try:
                try:
                    ten_mb = 10 * 1024 * 1024
                    with tempfile.SpooledTemporaryFile(max_size=ten_mb) as tmp:
                        with open(path) as fh:
                            for line in fh:
                                # Accumulate all line-ending normalized lines first
                                # to make sure the whole file is read. This prevents
                                # partial updates to the hash with hybrid text/binary
                                # files (e.g. like the constructor shell installers).
                                tmp.write(line.replace("\r\n", "\n").encode("utf-8"))
                        tmp.flush()
                        tmp.seek(0)
                        for chunk in iter(partial(tmp.read, 8192), b""):
                            hasher.update(chunk)
                except UnicodeDecodeError:
                    # file must be binary, read the bytes directly
                    with open(path, "rb") as fh:
                        for chunk in iter(partial(fh.read, 8192), b""):
                            hasher.update(chunk)
            except OSError as exc:
                raise RuntimeError(
                    f"Could not read file '{relpath}' in directory '{directory}'. "
                    f"Content hash verification cannot continue. Error: {exc}"
                )
        else:
            raise RuntimeError(
                f"Can't detect type for path '{relpath}' in directory '{directory}'. "
                "Content hash verification cannot continue."
            )
        hasher.update(b"-")
    return hasher.hexdigest()


def write_bat_activation_text(file_handle, m):
    from .os_utils.external import find_executable

    file_handle.write(f'call "{context.root_prefix}\\condabin\\conda_hook.bat"\n')
    for key, value in context.conda_exe_vars_dict.items():
        file_handle.write(f'set "{key}={value or ""}"\n')
    if m.is_cross:
        # HACK: we need both build and host envs "active" - i.e. on PATH,
        #     and with their activate.d scripts sourced. Conda only
        #     lets us activate one, though. This is a
        #     vile hack to trick conda into "stacking"
        #     two environments.
        #
        # Net effect: binaries come from host first, then build
        #
        # Conda 4.4 may break this by reworking the activate scripts.
        #  ^^ shouldn't be true
        # In conda 4.4, export CONDA_MAX_SHLVL=2 to stack envs to two
        #   levels deep.
        # conda 4.4 does require that a conda-meta/history file
        #   exists to identify a valid conda environment
        # conda 4.6 changes this one final time, by adding a '--stack' flag to the 'activate'
        #   command, and 'activate' does not stack environments by default without that flag
        history_file = join(m.config.host_prefix, "conda-meta", "history")
        if not isfile(history_file):
            if not isdir(dirname(history_file)):
                os.makedirs(dirname(history_file))
            open(history_file, "a").close()

        file_handle.write(
            f'call "{context.root_prefix}\\condabin\\conda.bat" activate "{m.config.host_prefix}"\n'
        )

    # Write build prefix activation AFTER host prefix, so that its executables come first
    file_handle.write(
        f'call "{context.root_prefix}\\condabin\\conda.bat" activate --stack "{m.config.build_prefix}"\n'
    )

    ccache = find_executable("ccache", m.config.build_prefix, False)
    if ccache:
        if isinstance(ccache, list):
            ccache = ccache[0]
        ccache_methods = {}
        ccache_methods["env_vars"] = False
        ccache_methods["symlinks"] = False
        ccache_methods["native"] = False
        if hasattr(m.config, "ccache_method"):
            ccache_methods[m.config.ccache_method] = True
        for method, value in ccache_methods.items():
            if value:
                if method == "env_vars":
                    file_handle.write(f'set "CC={ccache} %CC%"\n')
                    file_handle.write(f'set "CXX={ccache} %CXX%"\n')
                elif method == "symlinks":
                    dirname_ccache_ln_bin = join(m.config.build_prefix, "ccache-ln-bin")
                    file_handle.write(f"mkdir {dirname_ccache_ln_bin}\n")
                    file_handle.write(f"pushd {dirname_ccache_ln_bin}\n")
                    # If you use mklink.exe instead of mklink here it breaks as it's a builtin.
                    for ext in (".exe", ""):
                        # MSVC
                        file_handle.write(f"mklink cl{ext} {ccache}\n")
                        file_handle.write(f"mklink link{ext} {ccache}\n")
                        # GCC
                        file_handle.write(f"mklink gcc{ext} {ccache}\n")
                        file_handle.write(f"mklink g++{ext} {ccache}\n")
                        file_handle.write(f"mklink cc{ext} {ccache}\n")
                        file_handle.write(f"mklink c++{ext} {ccache}\n")
                        file_handle.write(f"mklink as{ext} {ccache}\n")
                        file_handle.write(f"mklink ar{ext} {ccache}\n")
                        file_handle.write(f"mklink nm{ext} {ccache}\n")
                        file_handle.write(f"mklink ranlib{ext} {ccache}\n")
                        file_handle.write(f"mklink gcc-ar{ext} {ccache}\n")
                        file_handle.write(f"mklink gcc-nm{ext} {ccache}\n")
                        file_handle.write(f"mklink gcc-ranlib{ext} {ccache}\n")
                    file_handle.write("popd\n")
                    file_handle.write(
                        f"set PATH={dirname_ccache_ln_bin};{os.path.dirname(ccache)};%PATH%\n"
                    )
                elif method == "native":
                    pass
                else:
                    print("ccache method {} not implemented")


channeldata_cache = {}


def download_channeldata(channel_url):
    global channeldata_cache
    if channel_url.startswith("file://") or channel_url not in channeldata_cache:
        urls = Channel.from_value(channel_url).urls()
        urls = {url.rsplit("/", 1)[0] for url in urls}
        data = {}
        for url in urls:
            with TemporaryDirectory() as td:
                tf = os.path.join(td, "channeldata.json")
                try:
                    download(url + "/channeldata.json", tf)
                    with open(tf) as f:
                        new_channeldata = json.load(f)
                except (JSONDecodeError, CondaHTTPError):
                    new_channeldata = {}
            merge_or_update_dict(data, new_channeldata)
        channeldata_cache[channel_url] = data
    else:
        data = channeldata_cache[channel_url]
    return data


def shutil_move_more_retrying(src, dest, debug_name):
    log = get_logger(__name__)
    log.info(f"Renaming {debug_name} directory '{src}' to '{dest}'")
    attempts_left = 5

    while attempts_left > 0:
        if os.path.exists(dest):
            rm_rf(dest)
        try:
            log.info(f"shutil.move({debug_name})={src}, dest={dest})")
            shutil.move(src, dest)
            if attempts_left != 5:
                log.warning(
                    f"shutil.move({debug_name}={src}, dest={dest}) succeeded on attempt number {6 - attempts_left}"
                )
            attempts_left = -1
        except:
            attempts_left = attempts_left - 1
        if attempts_left > 0:
            log.warning(
                f"Failed to rename {debug_name} directory, check with strace, struss or procmon. "
                "Will sleep for 3 seconds and try again!"
            )
            import time

            time.sleep(3)
        elif attempts_left != -1:
            log.error(
                f"Failed to rename {debug_name} directory despite sleeping and retrying."
            )


def is_conda_pkg(pkg_path: str) -> bool:
    """
    Determines whether string is pointing to a valid conda pkg
    """
    path = Path(pkg_path)

    return path.is_file() and (
        any(path.name.endswith(ext) for ext in CONDA_PACKAGE_EXTENSIONS)
    )


def package_record_to_requirement(prec: PackageRecord) -> str:
    return f"{prec.name} {prec.version} {prec.build}"


@contextlib.contextmanager
def set_umask(mask: int = 0) -> Iterable[None]:
    current = os.umask(mask)
    yield
    os.umask(current)


@contextlib.contextmanager
def create_file_with_permissions(path: str, permissions: int):
    """
    Opens a new file for writing, with permissions set from creation time.
    This is achieved by creating a temporary directory in the same parent
    directory, opening a new file inside with the right permissions,
    yielding the descriptor so the caller can add the necessary contents,
    and then moving the temporary file to the target location, with preserved
    permissions.

    The umask is temporarily reset during this process, and then restored.
    This is needed so permissions can be applied as intended. Without a zeroed
    umask, the system umask might filter the passed value to a different one.
    For example, given a system umask=022, passing 666 will result in a file
    with permissions 644.
    """

    def opener(path, flags):
        return os.open(path, flags, mode=permissions)

    dirname = os.path.dirname(path)
    with set_umask(), TemporaryDirectory(dir=dirname) as tmpdir:
        tmp_path = os.path.join(tmpdir, secrets.token_urlsafe(64))
        with open(tmp_path, "w", opener=opener) as fh:
            yield fh

        shutil.move(tmp_path, path)
