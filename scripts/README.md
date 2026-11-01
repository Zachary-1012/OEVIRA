# Development commands

Run these scripts from any working directory using their absolute paths. They
resolve the repository from their own location. The only development root is
`D:\寰意`; commands do not copy or migrate data from another workspace.

These commands prepare the development environment. Publishing them does not
publish or complete the product implementation. Installation, builds and startup
require the corresponding product sources and dependency manifests in the
selected checkout. A specification-only checkout reports the missing inputs
before changing an environment or starting a product process.

## Read-only entry checks

```powershell
& 'D:\寰意\scripts\setup.ps1' -CheckOnly
& 'D:\寰意\scripts\start.ps1' -Client None -CheckOnly
& 'D:\寰意\scripts\start.ps1' -Client Web -CheckOnly
& 'D:\寰意\scripts\start.ps1' -Client Windows -CheckOnly
& 'D:\寰意\scripts\stop.ps1' -CheckOnly
```

`-CheckOnly` uses the same required-file preflight as normal setup/start, then
resolves paths and checks the selected interpreter/client artifact.
It does not install, build, create data directories, read runtime receipts or
credentials, start/stop services, or prove that the product works. A missing
selected client artifact is an error. The stop check resolves the target only;
it does not inspect whether a service is running. Interpreter checks use isolated
Python and an in-memory SQLite connection to read the actual library version
and source ID; no product modules or business databases are opened.

Setup requires the server requirements and constraints files. Client builds also
require the Flutter manifest/lockfile, Dart entry, Web index and Windows CMake/runner
entries. Startup requires the backend package entry and its direct startup modules,
even with `-Client None`. The check lists these required files in its output. This
is a prerequisite check; it does not validate every transitive import, dependency
compatibility, build input or runtime behavior.

## Prepare the existing environment

```powershell
& 'D:\寰意\scripts\setup.ps1'
# Prepare backend dependencies without building either client:
& 'D:\寰意\scripts\setup.ps1' -SkipClientBuild
```

Preparation requires Python **3.13** and preserves an existing `.venv`. Its
version must match; an incompatible or incomplete environment is reported
without replacement. If no environment exists, the script first selects the
verified project runtime at `.runtime/tools/python-3.13.15-sqlite-3.51.3` when
present, then checks the Python launcher and available Python executables.
`-Python` may select an
existing 3.13 executable or command; it is validated even when `.venv` exists.
No Python installation or global PATH change occurs.

The project runtime is local, ignored by Git and not shipped in a clone. Setup
selects and validates an existing runtime; it does not download, build or patch
Python or SQLite. If that runtime is absent, supply another existing compatible
Python 3.13 interpreter. An unsupported interpreter must be repaired or replaced
separately before using these commands.

Setup and startup share `check_python_runtime.py`: the loaded SQLite must contain
the [official WAL-reset fix](https://www.sqlite.org/wal.html#walresetbug), using
3.51.3 or a subsequent valid release, or the fixed 3.50.7/3.44.6 backports.
Withdrawn 3.52.0 is rejected. An unsupported explicit selection fails without
changing the environment or starting a service; it is never silently replaced.
The newly created venv is checked again. A project runtime referenced by
`.venv/pyvenv.cfg` is an active dependency and must not be deleted as a cache.
Preserve its license, source/build identity and verified rollback material.

Without `-CheckOnly`, setup installs the pinned server dependencies and, unless
`-SkipClientBuild` is supplied, resolves the locked Flutter dependencies and
builds Web and Windows. `-Flutter` selects the existing Flutter command. These
actions can access dependency registries; preparation is not release acceptance.

## Start and stop local development

```powershell
& 'D:\寰意\scripts\start.ps1' -Client Web
& 'D:\寰意\scripts\start.ps1' -Client Windows
& 'D:\寰意\scripts\start.ps1' -Client None
& 'D:\寰意\scripts\stop.ps1'
```

Select one startup command for the intended client. The default port is `8317`;
the service listens on loopback. The default data root is
`D:\寰意\data\development`, which Git ignores. Relative `-DataRoot` values are
resolved against the repository, not the caller's working directory. An
explicit absolute data path remains supported; use the same value for start
and stop. Drive-relative paths such as `D:work` are rejected. Existing external
data is not moved, copied or selected automatically.

```powershell
& 'D:\寰意\scripts\start.ps1' -Client None -DataRoot 'data\local-check' -CheckOnly
& 'D:\寰意\scripts\stop.ps1' -DataRoot 'data\local-check' -CheckOnly
```

Web requires `app\build\web\index.html` and serves that build. Windows requires
`app\build\windows\x64\runner\Release\oevira.exe`. `None` starts only the API
and Worker; Windows and None do not require or serve a Web build. A running
managed service with different root, data, port, interpreter or Web-root
configuration is rejected rather than silently reconfigured. Stop it explicitly
before changing that configuration. No unrelated port owner is stopped.

The stop command verifies the recorded process identity and then **forcibly
terminates** it. There is no graceful-shutdown protocol in this script. Save
work before using it; recovery of interrupted work requires separate validation.
Data and receipts are retained. Startup confirms API availability, not Worker
behavior, client usability or completed user work. Monitoring requires the
service to remain running.

## Existing verification

From `D:\寰意`, use the project interpreter rather than a different global Python:

```powershell
& '.\.venv\Scripts\python.exe' scripts/verify_repository.py
& '.\.venv\Scripts\python.exe' -m unittest discover -s tests -v
```

Only when the corresponding product source and test dependencies are present:

```powershell
$env:PYTHONPATH = 'server'
& '.\.venv\Scripts\python.exe' -m unittest discover -s server/tests -v
```

`verify_repository.py` checks specification, structure and the exact permitted
environment script set; it does not admit product source through this gate.
Product tests, builds,
real operations and release acceptance remain separate. `--require-product`
retains the full-product release gate. These commands do not authorize account
access, collection, training, deployment or external business effects.

The tracked [Codex launcher](../.codex/launch.ps1) starts from the current root
without changing the user's model/provider configuration. This workstation also
has a local `lcodex` convenience shim under the user's Codex `bin`; that personal
shim is not included in a repository clone.
