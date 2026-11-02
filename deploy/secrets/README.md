# Server secrets

Files here are mounted read-only into the Airflow scheduler and `jobs` containers at
`/app/secrets/`. Git ignores everything in this folder except this README.

- `google-sheets.json`: the Google service account key that lets the pipeline update the
  dashboard Sheet (RUNBOOK section T). Make it readable only by you: `chmod 600`.

Never commit, paste, or email these files.
