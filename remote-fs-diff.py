#!/usr/bin/env python

"""
A simple file comparison utility for listing file difference remotely via SSH.
Copyright Robert Krahn 2018
"""

import sys
from os import uname, stat, walk as dir_walk
from os.path import join, exists, relpath, realpath, basename
import fnmatch
from time import gmtime, strftime
import argparse
import pickle
from subprocess import PIPE, Popen
import subprocess


default_ignore_files = [
    ".DS_Store",
    "objects.sqlite",
    "node_modules",
    "*.pid",
    "*.tmp",
    "*.cache",
    "combined.js*",
    ".git",
    "lively.next-node_modules",
    ".*~",
    "*~",
    ".#*",
    "#*",
    "*.pyc",
    ".mypy_cache",
    "__pycache__",
    ".module_cache"
]

default_ignore_paths = [
    "*/Dropbox/configs/unison/*",
    "*/Dropbox/configs/gnupg/gpg-agent.log",
    "*/Dropbox/configs/gnupg/S.gpg-agent",
    "*/Dropbox/configs/gnupg/random_seed",
    "*/opencv-test/build*",
    "*/Projects/old_projects*"
]

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

def file_item(f, root):
    fstat = stat(join(root, f))
    return (f, (fstat.st_mtime, fstat.st_size))

def record_file_stats(basedir, ignore_files, ignore_paths):
    """Recursively walks the file_system starting at basedir and creates a dict
    {dir1: {file_name1: (mtime, size)}, ...}"""
    file_dict = {}
    for root, dirs, files in dir_walk(basedir):
        apply_ignore(files, root, ignore_files, ignore_paths)
        apply_ignore(dirs, root, ignore_files, ignore_paths)
        items = [file_item(f, root) for f in files if exists(join(root, f))]
        items.append(file_item(".", root))
        file_dict[relpath(root, basedir)] = dict(items)

    return file_dict

def record_file_stats_remote(ssh_remote, remote_basedir):
    """Copies this script to the remote host, runs it, and sends back a serialized
    file index."""
    with open(__file__, "r+b") as code_file:
        cmd = "cat - > $TMPDIR/{0} && python $TMPDIR/{0} --print-index --basedir '{1}'".format(
            basename(__file__), remote_basedir)
        p = Popen(["ssh", ssh_remote, cmd], stdin=PIPE, stdout=PIPE, stderr=PIPE)
        out, err = p.communicate(input=code_file.read())
        if len(err) > 0:
            raise Exception("Error on remote: ", err)
        return pickle.loads(out)

def dump_file_stats(basedir, ignore_files, ignore_paths):
    """serializes dict produced by record_file_stats"""
    data = record_file_stats(basedir, ignore_files, ignore_paths)
    out = sys.stdout.buffer if hasattr(sys.stdout, "buffer") else sys.stdout
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

def diff_file_list(file_dict_a, file_dict_b):
    """builds a dict {
      only_in_a: {filename: (mtime, size)},
      only_in_b: {filename:, (mtime, size))},
      changed: {filename: ((mtimea, sizea), (mtimeb, sizeb))}
    }"""
    seen_dirs = set()
    only_in_a = {}
    only_in_b = {}
    dirs_only_in_a = []
    dirs_only_in_b = []
    changed = {}

    for dir, files_a in sorted(file_dict_a.items()):
        seen_dirs.add(dir)
        parent_excluded = next((True for only_in_a_dir in only_in_a if dir.startswith(only_in_a_dir)), None)
        if parent_excluded:
            continue
        if dir not in file_dict_b:
            only_in_a[dir + "/"] = files_a["."]
            dirs_only_in_a.append(dir)
        else:
            seen_files = set()
            files_in_a = {}
            files_in_b = {}
            changed_files = {}
            files_b = file_dict_b[dir]
            for f in files_a:
                if f == ".":
                    continue
                seen_files.add(f)
                if f not in files_b:
                    files_in_a[join(dir, f)] = files_a[f]
                else:
                    time_a, size_a = files_a[f]
                    time_b, size_b = files_b[f]
                    if size_a != size_b:
                        changed_files[join(dir, f)] = (files_a[f], files_b[f])

            files_in_b.update([(join(dir, f), files_b[f])
                               for f in files_b
                               if f not in seen_files and f != "."])
            only_in_a.update(files_in_a)
            only_in_b.update(files_in_b)
            changed.update(changed_files)

    for dir, files_b in sorted(file_dict_b.items()):
        parent_excluded = next((True for only_in_b_dir in only_in_b if dir.startswith(only_in_b_dir)), None)
        if parent_excluded:
            continue

        if dir not in seen_dirs:
            # only_in_b.update([(join(dir, f), files_b[f]) for f in files_b])
            only_in_b[dir + "/"] = files_b["."]
            dirs_only_in_b.append(dir)

    return {"only_in_a": only_in_a, "only_in_b": only_in_b, "changed": changed}


def print_diff(diffed, basedir,
               print_ediff=False, print_content_diff=False,
               ssh_remote="???", remote_basedir=""):
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
    lines = ["Comparing\n  {} {}\nand\n  {} {}\n".format(
        hostname, basedir, ssh_remote, remote_basedir or basedir)]

    lines.extend(print_aligned(
        ">>> The following files are only present in {}:\n ".format(hostname),
        diffed["only_in_a"].items(),
        lambda item: item[0],
        lambda item: prin_time(item[1][0])))

    lines.append("\n")

    lines.extend(print_aligned(
        "<<< The following files are only present in {}:\n ".format(ssh_remote),
        diffed["only_in_b"].items(),
        lambda item: item[0],
        lambda item: prin_time(item[1][0])))

    lines.append("\n")

    lines.extend(print_aligned(
        "=== The following files are changed:\n ",
        diffed["changed"].items(),
        lambda item: item[0],
        lambda item: "{} | {} | {}".format(
            "A" if item[1][0][0] > item[1][1][0] else "B",
            prin_time(item[1][0][0]), prin_time(item[1][1][0]))))

    if print_ediff:
        lines.append("\n")
        lines.extend([
            "(let ((f1 \"{0}\") (f2 \"{1}\")) (ediff-files f1 (concat \"/ssh:{2}:\" f2)))".format(
                join(basedir, file), join(remote_basedir, file), ssh_remote)
            for file in diffed["changed"]])

    if print_content_diff:
        lines.append("\n")
        lines.extend([diff_file_remote(file, basedir, ssh_remote, remote_basedir)
                      for file in diffed["changed"]])

    print("\n".join(lines))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Compare file trees remotely')
    parser.add_argument('--basedir', type=str, help='base directory to operate from', required=True)
    parser.add_argument('--print-index', action="store_true", help='Build and print the index of files. Not meant for direct usage but for remote communication via ssh. Will print a pickled index to stdout.')
    parser.add_argument('--ignore-files', type=str, nargs='+', help='file names and patterns to ignore', default=default_ignore_files)
    parser.add_argument('--ignore-paths', type=str, nargs='+', help='paths and path patterns to ignore', default=default_ignore_paths)
    parser.add_argument('--ssh-remote', type=str, help='user@host of remote. If followed by : like user@host:foo/bar, specified basedir on remote host. If not specified, basedir is taken')
    parser.add_argument('--print-ediff-commands', action="store_true", help='Print ediff function calls for changed files. For copy and paste into emacs.')
    parser.add_argument('--print-content-diff', action="store_true", help='Do a full unified diff of all changed files.')
    args = parser.parse_args()

    basedir = realpath(args.basedir)

    if args.print_index:
        dump_file_stats(basedir, args.ignore_files, args.ignore_paths)

    elif args.ssh_remote:
        ssh_remote = args.ssh_remote
        remote_basedir = basedir
        if ":" in ssh_remote:
            ssh_remote, remote_basedir = ssh_remote.split(":")
        files_a = record_file_stats(basedir, args.ignore_files, args.ignore_paths)
        files_b = record_file_stats_remote(ssh_remote, remote_basedir)
        diffed = diff_file_list(files_a, files_b)
        print_diff(diffed, basedir,
                   print_ediff=args.print_ediff_commands,
                   print_content_diff=args.print_content_diff,
                   ssh_remote=args.ssh_remote,
                   remote_basedir=remote_basedir)

    else:
        parser.print_help()
