# Microsoft 365 email workflow

## What was added

The map now uses its existing Entra identity, Azure Functions, Azure Table
Storage, and server-synced call lists for an application-native email composer.
The Graph boundary is delegated: every batch is permanently bound to the Entra
object id and Microsoft 365 mailbox verified through Graph `/me`. There is no
`/users/{someone-else}` send path.

Each recipient has an independent application record. Common edits regenerate
only messages without an individual override. The final subject, sanitized HTML
body, generated corporate signature, recipient, approved-attachment snapshots,
validation results, Graph identifiers, timestamps, and failures are stored as
structured records. OAuth artifacts are never included in audit events.

The send path is always:

1. validate and explicitly approve;
2. create a Graph draft with `Prefer: IdType="ImmutableId"` and an EIC extended
   property;
3. persist the immutable id;
4. add or resume approved attachments;
5. stop for draft-only mode, or enqueue the known draft after the cancellation
   window;
6. pace through the durable queue and reconcile the immutable id in Sent Items.

Graph documents this immutable-id send workflow explicitly. Draft creation uses
`Mail.ReadWrite`; draft submission uses `Mail.Send`. Files under 3 MB use the
simple attachment endpoint and larger files use upload sessions. The application
limit defaults to 15 MB per attachment and 20 MB estimated total even though
Graph documents support up to 150 MB, because the Exchange tenant's actual
message limit can be lower.

References:

- [Create a message draft](https://learn.microsoft.com/en-us/graph/api/user-post-messages?view=graph-rest-1.0)
- [Send a draft message](https://learn.microsoft.com/en-us/graph/api/message-send?view=graph-rest-1.0)
- [Immutable Outlook identifiers](https://learn.microsoft.com/en-us/graph/outlook-immutable-id)
- [Large Outlook attachments](https://learn.microsoft.com/en-us/graph/outlook-large-attachments)
- [Outlook throttling limits](https://learn.microsoft.com/en-us/graph/throttling-limits#outlook-service-limits)

## Required Entra configuration

Use the existing single-tenant `EIC Advisor Map` app registration unless the
administrator intentionally prefers a separate registration.

1. Add a **Web** redirect URI for every deployed hostname:
   `https://<hostname>/api/email-auth`.
2. Add delegated Microsoft Graph permissions:
   `User.Read`, `Mail.ReadWrite`, and `Mail.Send`.
3. Grant tenant-wide administrator consent. Employees still authorize a
   delegated session, but they do not configure an integration or separately
   consent to scopes.
4. Keep assignment required on the enterprise application and keep the existing
   sales-team group assignment.
5. Optionally define an app role named `EmailAdministrator`. Only that role can
   operate the application-level direct-send kill switch through
   `POST /api/email?op=policy` with `{ "killed": true, "reason": "..." }`.

`offline_access`, `openid`, and `profile` are protocol scopes requested during
the authorization-code flow; they are not broad Graph application permissions.
The persisted per-user MSAL cache is AES-256-GCM encrypted and is used so a
durable worker can finish after the browser closes. Microsoft recommends a
distributed, encrypted, per-user cache for server web applications:
[MSAL Node token caching](https://learn.microsoft.com/en-us/entra/msal/javascript/node/caching).

## Required Function App deployment

The pre-existing managed Static Web App Functions deployment supports HTTP
triggers only. The new durable sender uses an Azure Storage queue trigger, so
deploy `api/` to a standalone Function App and link it to the Static Web App
using the Standard-plan **bring your own Functions** feature. Set the Static Web
Apps workflow `api_location` to an empty string when the linked Function App is
used. Microsoft documents that linked Functions support all triggers while the
Static Web App continues to proxy `/api`:
[Bring your own Functions](https://learn.microsoft.com/en-us/azure/static-web-apps/functions-bring-your-own).

Prefer a Linux **Flex Consumption** Function App with Node.js 22, On Demand,
and zero Always Ready instances. Flex is Microsoft's recommended serverless
plan for new Function Apps and uses a regional memory quota separate from the
classic Consumption/App Service Total VMs meter. Before changing the live API,
run **az staticwebapp backends validate** with the Function App resource ID and
region; link it only after validation succeeds. This validation is important
because Microsoft's Static Web Apps overview says an existing Function App can
use any plan while its older plan comparison table does not explicitly list
Flex.

`src/deploy_swa.py` enforces this split. After the Function App exists and is
linked to the Static Web App, install Azure Functions Core Tools v4 and sign
in with Azure CLI or Azure PowerShell, then run:

~~~powershell
python src/deploy_swa.py --test --function-app <FUNCTION_APP_NAME>
python src/deploy_swa.py --full --function-app <FUNCTION_APP_NAME>
~~~

`AZURE_FUNCTION_APP_NAME` may be set instead of repeating `--function-app`.
Use `--static-only` only when the linked Function App has already been deployed
from the same revision. The deployment script deliberately refuses the old
managed-API upload when it detects `email-worker`'s queue trigger.

Set these Function App settings (values belong in Azure configuration or Key
Vault references, never source control):

| Setting | Required / initial value |
|---|---|
| `AZURE_STORAGE_CONNECTION_STRING` | Existing storage account; used for Tables, queue, and approved-document blobs |
| `GRAPH_CLIENT_ID` | Existing Entra application client id |
| `GRAPH_CLIENT_SECRET` | Entra secret or Key Vault reference |
| `GRAPH_TENANT_ID` | `c1efc78d-a7d7-4998-98b6-08e90af5661f` |
| `GRAPH_REDIRECT_URI` | Exact `https://<hostname>/api/email-auth` URI |
| `EMAIL_TOKEN_ENCRYPTION_KEY` | Base64-encoded 32 random bytes; rotate by reconnecting users |
| `NODE_ENV` | `production` only in production |
| `EMAIL_DIRECT_SEND_ENABLED` | Start at `0`; set `1` only after tenant testing |
| `EMAIL_DIRECT_SEND_KILL_SWITCH` | `1` is an environment-level emergency stop; default to `1` for initial deployment |
| `EMAIL_INTERNAL_DOMAINS` | Comma-separated internal domains, initially `eicatlanta.com` |
| `EMAIL_TEST_ADDRESS_ALLOWLIST` | Comma-separated test addresses during production smoke testing |
| `EMAIL_DOCUMENT_CONTAINER` | Optional; defaults to `email-documents` |
| `EMAIL_SIGNATURE_*` | Company name, address, website, and disclosure used by the modular signature generator |

Configurable policy defaults are 30 seconds cancellation, one submission every
5 seconds per mailbox across batches, 250 direct recipients per batch, 5,000
external recipients in a rolling 24 hours, 15 MB per approved attachment, and
20 MB estimated total message size. The 15,000-recipient campaign hard stop is
not configurable downward in code and cannot be bypassed by slowing a batch.

The Function host is configured for one queue item per batch. A separate
optimistic Table-storage mailbox gate enforces pacing across scale-out workers,
and the 24-hour reservation uses a short database mutex so concurrent approvals
cannot race past the limit.

## Approved templates and documents

Templates and documents are versioned rows in `EmailTemplates` and
`EmailDocuments`; file bytes live in the private `email-documents` blob
container. Messages snapshot document name, size, type, version, hash, and blob
identity at batch creation, then verify blob size again before upload.

From `api/`, with storage credentials supplied only in the environment:

```powershell
npm run email:provision -- document strategy-overview C:\approved\overview.pdf "Strategy overview"
npm run email:provision -- template meeting-v2 C:\approved\meeting-template.json
```

Template JSON contains `name`, `subject`, `bodyText`, and optional
`defaultAttachmentIds`. Provisioning rejects merge fields outside
`first_name`, `last_name`, and `company_name`.

## Development safety and release sequence

`serve.py` implements the complete composer contract with a local Graph mock.
It never holds Microsoft credentials and returns 403 for every direct-send
attempt. Automated worker tests inject a mocked Graph boundary. In the deployed
API, direct sending requires all three conditions: `NODE_ENV=production`,
`EMAIL_DIRECT_SEND_ENABLED=1`, and neither environment nor administrator kill
switch active. When a test allowlist is non-empty, every direct recipient must
be on it.

Recommended smoke test:

1. deploy with both kill switches on and the allowlist containing only an
   internal test mailbox;
2. connect one employee and confirm Graph `/me` matches their Static Web Apps
   object id;
3. create one draft without an attachment, then one with a sub-3 MB attachment,
   then one using an upload session;
4. verify immutable-id lookup after manually sending a draft;
5. enable direct send while retaining the allowlist and send one message;
6. test cancel during the 30-second window, pause/resume, a forced 429 with
   `Retry-After`, worker restart, and an ambiguous mocked timeout;
7. confirm the audit rows and only then widen the allowlist or remove it.

## NDR / bounce boundary

The current release includes the suppression store, permanent/temporary/policy/
unknown classifications in the data model, and pre-send suppression validation.
It deliberately does **not** call a successful Graph submission “delivered”; the
UI says “Sent · no known failure.”

Mailbox NDR ingestion remains the next isolated increment. It requires a public
anonymous webhook validation route, secret `clientState` verification, durable
notification enqueueing within three seconds, delegated per-user subscription
creation and renewal, lifecycle handling, and NDR correlation back to the stored
`internetMessageId`. Those pieces should be deployed together; weakening the
existing authenticated `/api/*` route merely to accept callbacks is not an
acceptable shortcut. Microsoft documents retry and validation behavior here:
[Graph webhook delivery](https://learn.microsoft.com/en-us/graph/change-notifications-delivery-webhooks)
and Outlook message subscriptions here:
[Outlook change notifications](https://learn.microsoft.com/en-us/graph/outlook-change-notifications-overview).

When implemented, the webhook worker should mark only permanent failures in
`EmailSuppressions`; temporary failures must remain retryable and policy/security
rejections must stay a distinct audit classification.
