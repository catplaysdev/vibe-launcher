# AGENTS.md

Vibe Launcher: a desktop app (PySide6; Windows today, Linux / Ubuntu planned) that opens the official Codex / Claude CLIs on this PC or on SSH hosts
and manages several accounts and projects. The user writes in Traditional Chinese; reply in Traditional Chinese.
Code, comments, README.md and this file are English. UI text: English source in `locales/en.json` plus `zh-TW.json`;
README also has `README.zh-TW.md` and `README.zh-CN.md`.

## Goals
- Sign in once per account; every account has its own profile and never touches the user's `~/.codex` / `~/.claude`.
- Projects are not bound to accounts. Subscription sign-ins live in their environment; API accounts work anywhere.
- SSH work runs in tmux: it survives closing the terminal and can be re-attached, stopped or recalled.
- A workspace is a named desktop containing terminals from multiple projects; switching desktops keeps their processes and layouts.
- A project can have many sessions; the project drawer and the Workspace page both show and recover them.

## Method (`src/vibe_launcher/`)
- UI: `ui.py` main window (+ mixins `ui_sessions.py`, `ui_integrations.py`), `workspace_page.py` terminals and quick
  open, `kit.py` widgets, `theme.py` tokens, `terminal.py` + `conpty.py` + `assets/` built-in terminal (xterm.js).
- Data and jobs: `store.py` SQLite (exports carry no secrets), `service.py` builds jobs, `runner.py` runs them in a
  terminal and writes status back, `env_state.py` read-only status and recall plans. View preferences go in `prefs.py`
  (`ui-state.json`), CLI settings in `settings.py` (`settings.json`).
- **`runtime.py` is sent whole to SSH hosts as one script**: remote logic lives there, it must not import other package
  modules, and it must run on older Python (guard `tomllib` etc.).
- Isolation: `CODEX_HOME` / `CLAUDE_CONFIG_DIR` point to `<data>/profiles/<tool>/<account_id>`; environments pass
  through `isolated_env`. Each launch refreshes native user extensions and conversations; project discovery stays native.
  Claude plugins use current user/project settings; Codex can use the native SQLite state directory. Credentials stay isolated.
- Self-start and packaging: `selfcmd.py` (the only place that builds helper command lines), `entry.py`, `updates.py`,
  `packaging/`; read *Packaging, updates and licence* before changing them.
- UI strings go through `i18n.tr()`; run `scripts/check_translations.py --sync`, then fill in `zh-TW.json`.

## Workflow
1. Check the branch first (see *Version control*). Read the relevant code and tests; match the surrounding style.
2. Run the tests. For UI changes, check layout with `widget.grab()`
   (offscreen text renders as boxes unless `QT_QPA_FONTDIR=C:/Windows/Fonts` is set).
3. New behaviour gets tests in `tests/`. User-visible features: update the README feature list (all three languages)
   and, for details, `docs/guide.zh-TW.md`; add a line to `CHANGELOG.md` `[Unreleased]`.
   If a pictured page changes visibly, rerun `scripts/readme_screenshots.py` (offscreen, made-up data, no brand logos)
   and commit the updated `docs/images/`.
4. Report what was verified and what was not (especially real SSH / tmux / sign-in flows).

```
QT_QPA_PLATFORM=offscreen .venv/Scripts/python.exe -m pytest -q --basetemp=<temp>/bt -p no:cacheprovider -o faulthandler_timeout=90
```
- Set `--basetemp`: the default `%TEMP%\pytest-of-<user>` may be inaccessible.
- For the full suite, use a dedicated temporary directory outside this checkout so Git tests cannot discover the
  launcher's parent repository (for example `--basetemp="$env:TEMP/vibe-launcher-tests"` in PowerShell).
- Never add `-p no:faulthandler` (a native crash then opens a Windows dialog and hangs the run).
- Tests must not open real modals (`ConfirmDialog` …): patch seams like `view._confirm`, `view._notice` (conftest guards this).
  Tests that draw (`QPixmap`, `QImage` rendering) need a `qapp` / `qtbot` fixture.
- Windows with terminals: `qtbot.addWidget(w, before_close_func=lambda w: w.workspace.shutdown())`.
- Use `kit.dispose_widget` and `kit.later(owner, ms, fn)`; no `setParent(None)` + `deleteLater()`, no `QTimer.singleShot`
  bound to widget methods.
- Bash heredocs break on `'''`; write patch scripts with the Write tool.

## Watch out
- Secrets: never read, store or display passwords, API keys or OAuth tokens; the GUI never reads saved SSH passwords.
  Launcher data paths go through `assert_private_path` (rejects symlinks, reparse points, hard links).
- Terminal output is never a status source; remote results come from supervisor exit codes
  (78 not started, 79 ended, 80 background, 81 failed). Unknown remote work is never treated as ended.
- Development stage: nothing is kept for older builds. No migrations, legacy fields, old file formats or
  newer-build preservation; the store drops fields it does not know and `ui-state.json` / `settings.json` fall back to
  defaults for values they cannot use. Before changing the database schema, check (read-only) that the user's own
  database still opens, or tell them what would have to be reset; never rewrite their data without asking.
- Deletions ask first and go through `store.delete`'s release flow (no foreign-key dead ends).
- Keep readable contrast for secondary text.

## Boundaries
- Windows is the only supported desktop platform today; Linux (Ubuntu first) is planned. Keep Windows-specific code
  (ConPTY, Credential Manager, DWM title bar, `ssh.exe`) behind small platform seams so a Linux backend can be added
  without breaking Windows. Do not reimplement or proxy Codex / Claude sign-in; it happens in the official CLI / browser.
- No pywinpty on Windows (own ConPTY layer for CJK); on Linux use the stdlib `pty` first; xterm.js is vendored (MIT).
- Never capture the real screen (`QScreen.grabWindow`); use `widget.grab()`.
- Without being asked, do not change the user's `~/.codex`, `~/.claude` or SSH config, and do not connect to their real servers.
- `docs/private/` holds real-host records and security reviews; it is not committed.

## Packaging, updates and licence
Load the `package-release` skill and read this before touching `selfcmd.py`, `entry.py`, `updates.py`, `packaging/`, `.github/workflows/release.yml`, or anything
that starts a process or reads a package file. Do not let it be forgotten: the packaged build breaks silently otherwise.

**Licence.** GPL-3.0-or-later (`LICENSE`, `license` in `pyproject.toml`): anyone who uses or distributes the program or a
changed version must also publish the source under the same licence. New dependencies and bundled files must be
GPL-compatible (no proprietary, non-commercial or SSPL-style terms). When a bundled component is added or removed,
update `THIRD_PARTY_NOTICES.md`. Keep the build `onedir` (the Qt libraries must stay replaceable files for the LGPL).
Copied code keeps its own notice. No third-party brand assets in the repository or in builds.

**Two ways to run.** From source (`python -m vibe_launcher.ui`) and as a PyInstaller build (`Vibe Launcher.exe` for the
window, `vibe-launcher-cli.exe` for helpers; one folder, one set of files). There is no `python` in a packaged build, so:
- Only `selfcmd.py` builds helper command lines (`command()`, `gui_launch()`, `helper_environment()`) and reads
  `runtime.py` for SSH hosts (`runtime_source()`). Never write `[sys.executable, "-I", "-m", "vibe_launcher.x"]` anywhere
  else (`tests/test_packaging.py` fails if you do). A new helper goes into `selfcmd.HELPERS`, `entry.py` reaches it, and the
  spec lists it in `hiddenimports`.
- Package files are read through `Path(__file__).with_name(...)`, so every data file the code reads must be listed in
  `datas` of `packaging/vibe-launcher.spec`. Adding an asset, locale or script file means adding it there too.
- `vibe-launcher-cli --selftest` (also run by `packaging/build.ps1`) checks a built folder: version, data files, locales,
  the Qt WebEngine terminal, the GPU primer, the main window. Extend it when a packaged-only failure could hide.

**Build and release procedure: use the `package-release` skill** (`.agents/skills/package-release/SKILL.md`; it is tracked
in git and maintained by Codex; Claude Code may read it but edits go through Codex). In short: `powershell -File packaging/build.ps1` builds `dist-app/Vibe Launcher/` (about 370 MB
on disk, idle about 95 MB of memory, measured offscreen without a terminal), self-tests it and, with Inno Setup 6, builds the
installer and `SHA256SUMS.txt`. Keep it small and quiet: no UPX, no `collect_all`, no extra Qt modules without a reason,
Chromium language packs cut to the shipped languages, no tray icon, no background service, no updater process. Resource
budget for anything new: nothing runs between user actions except the update check (one small HTTPS request, at most every
six hours, can be switched off), and downloads are streamed to disk in 256 KB pieces, never held in memory.

**Updates.** Two channels behind one report format (`updates.py`, shown in Settings › About and updates):
- git checkout: tags plus `CHANGELOG.md`, applied with `git pull --ff-only` and `uv sync`.
- packaged build (`selfcmd.frozen()`): GitHub Releases of `updates.RELEASE_REPO` (the repository must be public; private
  shows "unavailable"). A release is installable only if it has `Vibe-Launcher-Setup-<version>.exe` and `SHA256SUMS.txt` and
  its body is that version's CHANGELOG section. `apply-release` backs up user data, downloads, verifies the SHA-256 against
  `SHA256SUMS.txt`, deletes earlier downloads, then starts the installer silently (`/SILENT /CLOSEAPPLICATIONS`); the
  installer (per user, no administrator rights) closes the launcher and starts the new version. Hosts are limited to
  GitHub over https. There is no code signing yet, so Windows SmartScreen may warn; integrity rests on GitHub's TLS and the
  checksum. Never embed tokens; never run an installer whose checksum did not match.
- Data in `%LOCALAPPDATA%\VibeLauncher` is never written inside the install folder and survives install, update and
  uninstall; both channels back it up before updating.

**Releasing.** On `develop` or `hotfix`: bump `version`, turn `[Unreleased]` into `## [x.y.z] - date`; merge the PR
into `main`, tag `vx.y.z` on `main`;
`.github/workflows/release.yml` tests, builds, self-tests and publishes the installer, checksum and notes (full steps in the
skill). A release without both assets is invisible to the updater. The v0.1.0 program folder build and packaged self-test
passed locally. Check each GitHub Release for both assets and matching notes. Installer installation and a real in-app
update remain unverified (no Inno Setup on the development PC); check the first release by hand and record what you learn
here and in the skill.

Linux (planned): a packaged build will need its own `selfcmd` branch (no `.exe` names), a PTY layer, and an AppImage or
`.deb` channel in `updates.py`; keep Windows-only calls behind `selfcmd.py` / the existing platform seams.

## Version control and collaboration
- **Version policy.** Use `major.minor.patch` (first release: `v0.1.0`). Increment major for substantial changes that
  require users to update; minor for optional new features; patch for small changes, usually bug fixes. Reset lower
  numbers to zero when incrementing major or minor. This is the release policy; the launcher currently asks users
  before applying updates and does not enforce mandatory updates. Keep `pyproject.toml`, CHANGELOG and the tag consistent.
- **Branches** (Git Flow without release branches, merged through GitHub PRs):

  | Branch | Role | Branches from | Merges into | On GitHub |
  | --- | --- | --- | --- | --- |
  | `main` | Released version; every merge is a release candidate, tagged `vX.Y.Z` | — | — | yes |
  | `develop` | Test version: finished features are merged and tested here | `main` | `main` (PR) | yes |
  | `hotfix` | Test version for fixes to the released `main` | `main` | `main` (PR), then `develop` | yes |
  | `feat/dev_<module>_<function>` | One feature, extension, docs or refactor (e.g. `feat/dev_workspace_restore`) | `develop` | `develop` (local merge) | **never** |
  | `fix_<module>_<function>[N]` | One fix to the released version (e.g. `fix_runtime_sync1`) | `hotfix` | `hotfix` (local merge) | **never** |

  `<module>` is the source module or area (`workspace`, `runtime`, `store`, `docs` …), `<function>` the feature or
  function, lower case with underscores. `.githooks/pre-push` refuses to push anything but `main`, `develop`, `hotfix`
  and `v*` tags; enable it once per clone with `git config core.hooksPath .githooks`.
- **Branch first.** Before implementing, run `git status` and `git branch --show-current`. A feature starts from the
  latest `develop` (`git switch develop`, `git pull --ff-only`, `git switch -c feat/dev_<module>_<function>`); a fix to
  the released version starts from `hotfix` (create it from `origin/main` when it does not exist:
  `git switch -c hotfix origin/main`). Uncommitted changes in the tree belong to the user: never discard them; ask
  whether to carry them to the new branch. Only stay on the current branch when the user explicitly says so.
- **Finish cleanly.** Run the full tests on the work branch, merge it into its parent with `git merge --no-ff`, run the
  full tests again there, push only `develop` / `hotfix`, and delete the merged work branch (`git branch -d`).
  `main` changes only by merging a `develop` or `hotfix` PR after CI (`.github/workflows/ci.yml`, Windows + offscreen
  Qt) passes. After a `hotfix` PR is merged: merge `main` back into `develop`, delete `hotfix` (local and remote) and
  create it again from `main` when the next fix comes.
- Commit, merge, push, open or merge PRs only when the user asks. One-line commit messages that say what changed;
  PR descriptions follow the template (verified / not verified).
- Release: see *Packaging, updates and licence*. On `develop` (or `hotfix`) bump `version` and turn `[Unreleased]` into
  `## [x.y.z] - date`, open the PR into `main`, merge after CI, tag `vX.Y.Z` on `main`; the release workflow builds and
  publishes, and the launcher updates from that release. Then merge `main` back into `develop`.
- Not committed (see `.gitignore`): `docs/private/`, `src/vibe_launcher/assets/brand/`, `.local-test/`, databases,
  keys, `.env`. Check the ignore rules before adding such files.
- Only own code and compatibly licensed third-party files (keep their LICENSE); no third-party trademarks, fonts or screenshots.
- Examples, tests and docs use generic values only (`user`, `user@server`, `example.com`, `192.0.2.x`), never real
  names, accounts, emails, host aliases or paths.
- This is the only agent guide; `CLAUDE.md` just references it.
