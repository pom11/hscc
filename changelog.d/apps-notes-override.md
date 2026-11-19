kind: Fixed
task: apps-1.3.0

- **`scripts/release_apps.sh` takes `HSCC_APPS_NOTES_FILE`.** A CLI release archives every `changelog.d/` fragment, so an apps release cut after it rendered "No app-facing changes recorded." The variable supplies the notes file directly.
