# Advisor Map training video

The introduction is internal media. It is not part of `webapp/`, the Static
Web App upload, or Git history.

## Runtime design

- Blob account: `eicadvisorlog`
- Private container: `training` (`publicAccess` must remain unset)
- Production origin: `https://lively-stone-05fce880f.7.azurestaticapps.net`
- API: authenticated `GET /api/training-video`
- Default media version: `1`

The endpoint verifies the Static Web Apps principal and returns read-only SAS
URLs valid for one hour. The browser streams the bytes directly from Blob
Storage. The Function never proxies the recording.

The storage CORS rule permits only the production origin and `GET`, `HEAD`, and
`OPTIONS`. The account itself has public Blob access disabled. A CORS rule is
not authorization; the short-lived blob-scoped SAS is authorization.

## Build the media

FFmpeg is an operator tool, not an application dependency. Preserve the source
recording and write release media only under ignored `dist/training/`.

```powershell
ffmpeg -i "Advisor Map - Introduction.mp4" `
  -map 0:v:0 -map 0:a:0 -c:v libx264 -preset medium -crf 21 `
  -tune stillimage -profile:v high -level 4.0 -pix_fmt yuv420p `
  -g 60 -keyint_min 60 -sc_threshold 0 -c:a aac -b:a 128k `
  -movflags +faststart dist/training/advisor-map-introduction-v1.mp4

ffmpeg -ss 120 -i dist/training/advisor-map-introduction-v1.mp4 `
  -frames:v 1 -q:v 2 dist/training/advisor-map-introduction-v1.jpg
```

Generate captions locally with the optional release tool. Its environment is
deliberately separate from application requirements.

```powershell
python scripts/transcribe_training_video.py `
  "Advisor Map - Introduction.mp4" `
  --output dist/training/advisor-map-introduction-v1.vtt `
  --model small.en
```

Review names, firms, acronyms, and product terminology in both the VTT and the
generated `*.transcript.json` before upload.

## Upload a version

Use immutable/versioned blob names and correct content types. Never upload the
source under an unversioned name and never enable anonymous container access.

```powershell
az storage blob upload --account-name eicadvisorlog --auth-mode login `
  --container-name training --name advisor-map-introduction-v1.mp4 `
  --file dist/training/advisor-map-introduction-v1.mp4 `
  --content-type video/mp4 --overwrite false

az storage blob upload --account-name eicadvisorlog --auth-mode login `
  --container-name training --name advisor-map-introduction-v1.jpg `
  --file dist/training/advisor-map-introduction-v1.jpg `
  --content-type image/jpeg --overwrite false

az storage blob upload --account-name eicadvisorlog --auth-mode login `
  --container-name training --name advisor-map-introduction-v1.vtt `
  --file dist/training/advisor-map-introduction-v1.vtt `
  --content-type text/vtt --overwrite false
```

To publish a materially different recording, increment the version in the blob
names, `api/training-video/index.js`, and `webapp/training.js`. The per-user
`introVideoSeenVersion` preference will then offer the updated guide once.
