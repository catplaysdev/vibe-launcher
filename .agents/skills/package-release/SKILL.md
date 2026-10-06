---
name: package-release
description: Build, check and release the packaged Windows version of Vibe Launcher (PyInstaller folder, Inno Setup installer, GitHub Release, in-app update). Use when asked to package, build an exe or installer, cut or publish a release, bump a version, change how the program updates itself, or when touching selfcmd.py, entry.py, updates.py, packaging/ or .github/workflows/release.yml.
---

# Package and release Vibe Launcher

The rules that must never be forgotten live in `AGENTS.md` → *Packaging, updates and licence*. This skill is the procedure.
Reply to the user in Traditional Chinese; code, comments and docs stay English (CHANGELOG bullets and `docs/guide.zh-TW.md`
are Traditional Chinese).

## 0. Before anything

1. `git status` and `git branch --show-current`. Work on a topic branch (`feat/…`, `fix/…`); never commit, push, tag or
   publish unless the user asked for that step. Uncommitted changes belong to the user.
2. The launcher may be running (it hosts this very session): `uv sync` fails on the locked `.venv\Scripts\vibe-launcher.exe`.
   Install build tools with `uv pip install --python .venv/Scripts/python.exe "pyinstaller>=6.10,<7"` instead.
3. Scratch work goes to the session scratchpad, not the repo. `dist-app/`, `build-app/` and `packaging/*.ico` are git-ignored.

## 1. Build and check the program folder (about 4 minutes)

```
powershell -NoProfile -File packaging/build.ps1 -SkipInstaller
```

It makes the icon, runs `pyinstaller packaging/vibe-launcher.spec`, then runs `vibe-launcher-cli.exe --selftest` (version,
data files, locales, Qt WebEngine terminal, GPU primer, main window) and fails if any check fails. Expect about 370 MB in
`dist-app/Vibe Launcher/`. Afterwards report size and, if the spec changed, the idle memory: start `Vibe Launcher.exe` with
`QT_QPA_PLATFORM=offscreen`, wait 12 s, sum the working set (baseline about 95 MB, one process, about 1 s CPU).

If the self-test fails:
- missing file/data → add it to `datas` in `packaging/vibe-launcher.spec` (package files are read through `__file__`);
- Qt DLL load error → the `wanted()` filter in the spec removed too much; relax that pattern only;
- helper not found → `selfcmd.HELPERS`, `entry.py` and `hiddenimports` must agree;
- `version` is `0.0.0` → `copy_metadata("vibe-launcher")` found no installed metadata (install the project into `.venv`).

## 2. Installer and checksums (needs Inno Setup 6)

The same script builds `dist-app/installer/Vibe-Launcher-Setup-<version>.exe` and `SHA256SUMS.txt` (`<hash>  <file name>`)
when `iscc` is found. Without Inno Setup say so plainly: the installer script (`packaging/installer.iss`) has **not** been run
on this PC. Do not install system software without asking the user. Names are a contract with `updates.py`
(`Vibe-Launcher-Setup-<version>.exe`, `SHA256SUMS.txt`); `tests/test_packaging.py` checks they agree.

## 3. Cut a release (only when the user asks)

1. Topic branch: bump `version` in `pyproject.toml`; turn `[Unreleased]` in `CHANGELOG.md` into `## [x.y.z] - date`.
   Anything the release removes or changes in `ui-state.json`, `settings.json` or the database goes under `### 設定變更`
   as `- 移除: key` / `- 變更: key（說明）`; run `.venv/Scripts/python.exe tests/test_settings_compat.py --update-snapshot`
   after adding settings. `python scripts/release_notes.py check vX.Y.Z` must pass.
2. Full tests, then PR → CI → merge into `main` (the user merges).
3. Tag `vX.Y.Z` on `main` and push the tag (the user, or you when asked). `.github/workflows/release.yml` tests, builds,
   self-tests and runs `gh release create` with the installer, `SHA256SUMS.txt` and the CHANGELOG section as notes.
4. Verify on GitHub: both assets are attached, the notes show the CHANGELOG section, the repository is public (a private
   repository makes every installed copy show "unavailable"). A release without both assets is invisible to the updater.
5. Install the published installer on a clean user profile or VM if one is available; then publish a second, newer
   release and confirm Settings › About and updates updates the first one in place (backup made, checksum checked,
   launcher restarted). This end-to-end path is **not verified yet**; record what you learn in AGENTS.md.

## 4. Changing the update mechanism or anything that starts a process

- Build helper command lines only through `selfcmd.py`; `tests/test_packaging.py` fails otherwise.
- The updater only trusts https GitHub hosts, verifies SHA-256 before running anything, streams downloads in 256 KB chunks,
  and never stores tokens. Keep that. Keep the update check to one small request at most every six hours.
- New data file read by the code → spec `datas`. New helper → `HELPERS` + `hiddenimports`. New bundled component →
  `THIRD_PARTY_NOTICES.md` (and check its licence is GPL-compatible). Keep the build `onedir`.
- Re-run step 1 after every such change; extend `selftest.py` when a failure could hide only in the packaged build.

## 5. Report

Say what was verified (tests, build, self-test, size, memory) and what was not (installer, release upload, real update
from a published release, SmartScreen behaviour without code signing).
