# NOC_Beam project notes

## Build output destination

There are two zip targets — do not confuse them:

- **`E:\NOC_Beam\NOC_Beam.zip`** — the canonical PRODUCTION artifact. Only a
  release build (a tagged version on `main`) is written here. The boss demo
  machine and the user's launch path both look at this path (the user unzips
  it back into `E:\NOC_Beam\NOC_Beam\` to run).
- **`E:\NOC_Beam\NOC_BEAM_TEST.zip`** — the TEST/preview artifact, uploaded to
  the GitHub `NOC_BEAM_TEST` release for the team to try. All in-progress /
  branch builds go here. NEVER let a preview build overwrite `NOC_Beam.zip`.

Production release flow (on `main`, version bumped + tagged):

```powershell
cd E:\NOC_Beam\Eyebeam\python-app
pyinstaller --clean --noconfirm build\noc_beam.spec
Compress-Archive -Path dist\NOC_Beam\* -DestinationPath E:\NOC_Beam\NOC_Beam.zip -Force
```

The standard build flow on this machine:

```powershell
cd E:\NOC_Beam\Eyebeam\python-app
pyinstaller --clean --noconfirm build\noc_beam.spec
Compress-Archive -Path dist\NOC_Beam\* -DestinationPath E:\NOC_Beam\NOC_Beam.zip -Force
```

The `-Force` overwrites the existing zip in place — that's the intent.
