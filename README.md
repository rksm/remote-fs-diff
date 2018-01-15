# about

The code implements a file system diff tool in vanilla Python 3, it's a simple
shell script. The use case is the following: When working on multiple computers
and shared files, tools like rsync and unison are sometimes not enough.. rsync
is not bi-directional and unison messes up git repositories. For this purpose
this little script gives me a quick overview of local and remote file system
changes and allows me to discover if merges are needed (so I don't loose changes
when rsyncing).

It works by creating an index of files and modification dates locally and
remotely (by copying itself to the remote host and sending a serialized index
back).

It is invoked like:
  `remote-fs-diff.py --basedir ~/Projects/python --ssh-remote robert@10.0.1.8`

The output looks like:

```
Comparing
  Roberts-MacBook-Air.local /Users/robert/Projects/python
and
  robert@10.0.1.8 /Users/robert/Projects/python/
>>> The following files are only present in Roberts-MacBook-Air.local:
./ws_server.py  | 2018-01-03 12:39:51
<<< The following files are only present in robert@10.0.1.8:
./eval_server.py | 2018-01-03 01:04:47
=== The following files are changed:
./code_formatting.py    | A | 2018-01-03 12:30:28 | 2018-01-03 02:46:32
```

# License

Copyright 2018 Robert Krahn
