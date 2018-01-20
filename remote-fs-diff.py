#!/usr/bin/env python3

"""
A simple file comparison utility for listing file difference remotely via SSH.
Copyright Robert Krahn 2018
"""

import sys
from os import uname, stat, walk as dir_walk
from os.path import join, exists, relpath, realpath, basename, expanduser
import fnmatch
from time import gmtime, strftime
import argparse
import pickle
from subprocess import PIPE, Popen
import subprocess
from collections import namedtuple
import json

from typing import List, IO, cast

if sys.version_info.major < 3:
    raise Exception("{} needs python 3".format(__file__))

default_ignore_files = [
    ".DS_Store",
    ".git",
    "*.pyc"
]
default_ignore_paths = []
default_roots = None

def read_default_config():
    global default_ignore_files
    global default_ignore_paths
    global default_roots

    config_file = expanduser("~/.fsdiffrc")
    if not exists(config_file):
        return

    data = None
    with open(config_file) as f:
        data = json.load(f)

    if "ignore_files" in data:
        default_ignore_files = data["ignore_files"]

    if "ignore_paths" in data:
        default_ignore_paths = data["ignore_paths"]

    if "roots" in data:
        default_roots = [expanduser(path) for path in data["roots"]]


def apply_ignore(files, root, ignore_files, ignore_paths):
    """Mutates(!) files (simple file names from os.walk) so that entries matching
    ignore_files and ignore_paths are removed."""
    for f in files[:]:
        removed = False
        for ign_file in ignore_files:
            if fnmatch.fnmatch(f, ign_file):
                files.remove(f)
                removed = True
                break
        if removed:
            continue
        for ign_path in ignore_paths:
            if fnmatch.fnmatch(join(root, f), ign_path):
                files.remove(f)


FileSpec = namedtuple("FileSpec", ["name", "mtime", "size"])
DirContent = namedtuple("DirContent", ["path", "filespecs"])
FileTree = namedtuple("FileTree", ["rootdir", "dircontents"])
FileDiff = namedtuple("FileDiff", ["rootdir_a", "rootdir_b", "only_in_a", "only_in_b", "changed"])

def file_spec(f, root):
    fstat = stat(join(root, f))
    return FileSpec(f, fstat.st_mtime, fstat.st_size)

def record_file_stats(rootdirs: List[str], ignore_files: List[str], ignore_paths: List[str]) -> List[FileTree]:
    """Recursively walks the file_system starting at basedir and for each rootdir
    creates a list of director / file dict tuples like
    [(dir, {file_name1: (mtime, size)})]
    """
    result = []
    for rootdir in rootdirs:
        dircontents = []
        for parentdir, dirs, files in dir_walk(rootdir):
            apply_ignore(files, parentdir, ignore_files, ignore_paths)
            apply_ignore(dirs, parentdir, ignore_files, ignore_paths)
            specs = [file_spec(f, parentdir) for f in files if exists(join(parentdir, f))]
            specs.append(file_spec(".", parentdir))
            dircontents.append(DirContent(relpath(parentdir, rootdir), specs))
        result.append(FileTree(rootdir, dircontents))

    return result

def remote_command(ssh_remote, cmd):
    p = Popen(["ssh", ssh_remote, cmd], stdout=PIPE, stderr=PIPE)
    out = p.stdout.read().decode("utf8")
    err = p.stderr.read().decode("utf8")
    if len(err) > 0:
        raise Exception("Error on remote: ", err)
    return out


def record_file_stats_remote(ssh_remote, rootdirs: List[str]) -> List[FileTree]:
    """Copies this script to the remote host, runs it, and sends back a serialized
    file index."""
    # print(remote_command(ssh_remote, "PATH=/usr/local/opt/pyenv/versions/3.6.3/bin:/usr/local/bin:$PATH python --version"))

    with open(__file__, "r+b") as code_file:
        cmd = "export PATH=/usr/local/opt/pyenv/versions/3.6.3/bin:/usr/local/bin:$PATH; cat - > $TMPDIR/{0} && python3 $TMPDIR/{0} --print-index --roots {1}".format(
            basename(__file__), " ".join(rootdirs))
        p = Popen(["ssh", ssh_remote, cmd], stdin=PIPE, stdout=PIPE, stderr=PIPE)
        out, err = p.communicate(input=code_file.read())
        if len(err) > 0:
            raise Exception("Error on remote: ", err)
        return pickle.loads(out)

def dump_file_stats(rootdirs: List[str], ignore_files: List[str], ignore_paths: List[str]) -> None:
    """serializes dict produced by record_file_stats"""
    data = record_file_stats(rootdirs, ignore_files, ignore_paths)
    out = cast(IO[bytes], sys.stdout.buffer if hasattr(sys.stdout, "buffer") else sys.stdout)
    pickle.dump(data, out)

def diff_file_remote(filename, basedir, ssh_remote, remote_base_dir):
    """Runs the diff command on contents of filename, locally and remotely."""
    remote_diff = subprocess.run(
        ["ssh", ssh_remote, "cat '{}'".format(join(remote_base_dir, filename))],
        stdout=PIPE, stderr=PIPE)
    if len(remote_diff.stderr) > 0:
        raise Exception("Error on remote while fetching content: ", remote_diff.stderr)
    diff = Popen(["diff", "-u", join(basedir, filename), "-"], stdin=PIPE, stdout=PIPE, stderr=PIPE)
    out, err = diff.communicate(input=remote_diff.stdout)
    if len(err) > 0:
        raise Exception("Error in diff: ", err)
    return out.decode("utf-8")



# record_file_stats("/Users/robert/org/website/content", [], [])
# 123

def diff_file_list(filetrees_a: List[FileTree], filetrees_b: List[FileTree]) -> List[FileDiff]:
    """builds a dict {
      only_in_a: {filename: (mtime, size)},
      only_in_b: {filename:, (mtime, size))},
      changed: {filename: ((mtimea, sizea), (mtimeb, sizeb))}
    }"""
    filediffs: list = []

    # each file tree contains the file specs starting from a root
    # directory. each item in filetrees_a and filetrees_b at the same index
    # correspond to each other: they might not have the same rootdir but the
    # files in them are to be diffed
    for (rootdir_a, dirs_a), (rootdir_b, dirs_b) in zip(filetrees_a, filetrees_b):
        only_in_a: dict = {}
        only_in_b: dict = {}
        changed: dict = {}
        seen_dirs: set = set()

        # for all files in a...
        for path, files_a in dirs_a:
            seen_dirs.add(path)

            # ... is a parent directory to be known to be only in filetree_a? If so, ignore this dir.
            parent_excluded = next((True for only_in_a_dir in only_in_a if path.startswith(only_in_a_dir)), None)
            if parent_excluded:
                continue

            # if we don't find a dir with the same relative path in b, we add
            # just the path to this dir to only_in_a and ignore all the files
            dir_b = next((dir_b for dir_b in dirs_b if dir_b.path == path), None)
            if not dir_b:
                only_in_a[path + "/"] = next(filespec for filespec in files_a if filespec.name == ".")

            else:
                # dir with the same relative path exists in a and in b. we have
                # to compare the individual files
                seen_files: set = set()
                files_in_a: dict = {}
                files_in_b: dict = {}
                changed_files: dict = {}
                path_b, files_b = dir_b
                for file_a in files_a:
                    if file_a.name == ".":
                        continue
                    seen_files.add(file_a.name)

                    # do we find two files with the same name?
                    # no: mark file as only in a
                    # yes: compare size and record in changed if it differs
                    file_b = next((filespec for filespec in files_b if filespec.name == file_a.name), None)
                    if not file_b:
                        files_in_a[join(path, file_a.name)] = file_a
                    elif file_a.size != file_b.size:
                        changed_files[join(path, file_a.name)] = (file_a, file_b)

                # housekeeping
                files_in_b.update([(join(path, file_b.name), file_b)
                                   for file_b in files_b
                                   if file_b.name not in seen_files and file_b.name != "."])
                only_in_a.update(files_in_a)
                only_in_b.update(files_in_b)
                changed.update(changed_files)

        # we looked at all directories in a. time to record directories in b
        # that don't exist in a
        for path, files_b in dirs_b:
            parent_excluded = next((True for only_in_b_dir in only_in_b if path.startswith(only_in_b_dir)), None)
            if parent_excluded:
                continue

            if path not in seen_dirs:
                only_in_b[path + "/"] = next(filespec for filespec in files_b if filespec.name == ".")

        filediffs.append(FileDiff(rootdir_a, rootdir_b, only_in_a, only_in_b, changed))

    return filediffs


def print_diff(diffed: List[FileDiff], print_ediff=False, print_content_diff=False, ssh_remote="???") -> None:
    def prin_time(t):
        return strftime("%Y-%m-%d %H:%M:%S", gmtime(t))

    def print_aligned(prefix, items, str_col_0_fn, str_col_1_fn):
        lines = []
        times = [None] * (len(prefix.splitlines()) - 1)
        max_len = 0
        lines.append(prefix)
        for item in items:
            col0 = str_col_0_fn(item)
            lines.append(col0)
            max_len = max(max_len, len(col0))
            times.append(str_col_1_fn(item))
        for i, line in enumerate(lines):
            if times[i] is None:
                continue
            lines[i] = lines[i].ljust(max_len + 1, " ") + "| " + times[i]
        return lines

    hostname = uname().nodename
    lines = []

    for diff in diffed:
        lines.extend(print_aligned(
            ">>> The following files are only present in {}:{}\n ".format(hostname, diff.rootdir_a),
            diff.only_in_a.items(),
            lambda item: item[0],
            lambda item: prin_time(item[1].mtime)))
        lines.append("\n")

    lines.append("\n")

    for diff in diffed:
        lines.extend(print_aligned(
            "<<< The following files are only present in {}:\n ".format(ssh_remote, diff.rootdir_b),
            diff.only_in_b.items(),
            lambda item: item[0],
            lambda item: prin_time(item[1].mtime)))
        lines.append("\n")

    lines.append("\n")

    for diff in diffed:
        lines.extend(print_aligned(
            "=== The following files are changed {}:{} <=> {}:{}\n ".format(hostname, diff.rootdir_a, ssh_remote, diff.rootdir_b),
            diff.changed.items(),
            lambda item: item[0],
            lambda item: "{} | {} | {}".format(
                "A" if item[1][0].mtime > item[1][1].mtime else "B",
                prin_time(item[1][0].mtime), prin_time(item[1][1].mtime))))
        lines.append("\n")

    if print_ediff:
        lines.append("\n=== ediff ===")
        for diff in diffed:
            lines.append("\n")
            lines.extend([
                "(let ((f1 \"{0}\") (f2 \"{1}\")) (ediff-files f1 (concat \"/ssh:{2}:\" f2)))".format(
                    join(diff.rootdir_a, file), join(diff.rootdir_b, file), ssh_remote)
                for file in diff.changed])

    # if print_content_diff:
    #     lines.append("\n")
    #     lines.extend([diff_file_remote(file, basedir, ssh_remote, remote_basedir)
    #                   for file in diffed["changed"]])

    print("\n".join(lines))


if __name__ == "__main__":
    read_default_config()

    parser = argparse.ArgumentParser(description='Compare file trees remotely')
    parser.add_argument('--roots', type=str, nargs='+', help='Base directories to operate from. Seperate multiple directories via spaces. If a directory string contains a ":" then the left part of the string is the local directory, the right side the directory of the remote.', default=default_roots)
    parser.add_argument('--print-index', action="store_true", help='Build and print the index of files. Not meant for direct usage but for remote communication via ssh. Will print a pickled index to stdout.')
    parser.add_argument('--ignore-files', type=str, nargs='+', help='file names and patterns to ignore', default=default_ignore_files)
    parser.add_argument('--ignore-paths', type=str, nargs='+', help='paths and path patterns to ignore', default=default_ignore_paths)
    parser.add_argument('--ssh-remote', type=str, help='passed to ssh. Typically user@host of remote.')
    parser.add_argument('--print-ediff-commands', action="store_true", help='Print ediff function calls for changed files. For copy and paste into emacs.')
    parser.add_argument('--print-content-diff', action="store_true", help='Do a full unified diff of all changed files.')
    args = parser.parse_args()

    localdirs = [dir.split(":")[0] if ":" in dir else dir for dir in args.roots]
    remotedirs = [dir.split(":")[1] if ":" in dir else dir for dir in args.roots]

    if args.print_index:
        dump_file_stats(localdirs, args.ignore_files, args.ignore_paths)

    elif args.ssh_remote:
        files_a = record_file_stats(localdirs, args.ignore_files, args.ignore_paths)
        files_b = record_file_stats_remote(args.ssh_remote, remotedirs)
        diffed = diff_file_list(files_a, files_b)
        print_diff(diffed,
                   print_ediff=args.print_ediff_commands,
                   print_content_diff=args.print_content_diff,
                   ssh_remote=args.ssh_remote)

    else:
        parser.print_help()
