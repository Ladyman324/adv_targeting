# Advisor Map — Phase 2 admin requests (Outlook email feature)

**Requested by:** Bo Ladyman
**Status of Phase 1:** complete. The Static Web App is live, Entra sign-in
works, and the field team is using it.

**What Phase 2 adds:** the sales team can send templated, personalised emails to
advisors from inside the tool, through their own Outlook mailbox, using Microsoft
Graph. Every message is written by, sent from, and stored in the rep's own
mailbox — this is not a bulk-mail service and it does not send on behalf of a
shared or service account.

**Two admin asks remain — Ask 2 and Ask 3.** Ask 1 is resolved and is kept below
only as a record of what was tried.

| | Ask | Status |
|---|---|---|
| 1 | Function hosting / quota | **Resolved** — Flex Consumption worked |
| 2 | Entra app registration for Graph mail | **Outstanding — this is the blocker** |
| 3 | Restrict who can open the application | **Outstanding — security gap today** |

---

## Ask 1 — Function hosting *(RESOLVED — no action needed)*

> **Resolved on 17 Aug 2026.** After `Microsoft.App` was registered, a **Flex
> Consumption** Function App (`eic-advisors-api`, Linux, Node 22, East US 2)
> created without hitting the quota wall, deployed successfully, and is linked
> to the Static Web App. The `email-worker` queue trigger is running.
>
> **Please do not file the App Service quota support ticket.** It is no longer
> needed. Everything below is retained as a record of the diagnosis, in case
> the classic Consumption meter matters for something else later.

### Original symptom

### The symptom

Creating a **classic Consumption (Windows/Dynamic) Function App** fails
preflight validation:

```
Code:  SubscriptionIsOverQuotaForSku
Resource provider: Microsoft.Web/serverFarms (2024-11-01)
Current Limit (Total VMs): 0
Current Usage: 0
Amount required for this deployment (Total VMs): 1
Location:  <empty>
```

| Fact | Value |
|---|---|
| Subscription | `1664043a-7b8e-4cf5-b82b-31effe4d4d44` |
| Resource group | `rg-advisor-map` |
| Tracking id | `3f1cfbd6-d240-4cff-96b5-2cd012135fb2` |
| Deployment name | `Microsoft.Web-FunctionApp-Portal-81177bd7-9e99` |

### What the evidence establishes

- **Not a resource provider registration problem.** `Microsoft.Compute` was
  unregistered and has now been registered. The error persists.
- **Not isolated to one tested region.** It reproduces in **East US** and
  **East US 2**. The empty Location field prevents us from proving whether
  the blocked meter is regional or subscription-wide, so the memo should not
  claim more than the error establishes.
- **Not an Azure Policy denial.** A policy block returns
  `RequestDisallowedByPolicy` naming the policy. This is the quota subsystem.
- **Not a general compute restriction.** Compute quotas are at normal defaults
  (Total Regional vCPUs 0 of 10). Only App Service is zeroed.
- **Not self-serviceable.** Usage + quotas → App Service → **Total Regional
  VMs** shows **0 of 0**, the adjust button is greyed out, and the portal states
  *"provided for informational purposes only; quota need not be requested for
  this SKU."*

That last point matters: repeatedly trying to edit that greyed-out quota is not
a productive path. If classic Consumption remains necessary, it needs a
Microsoft support request or, for a CSP/reseller subscription, action by the
partner that owns the billing relationship.

### Recommended path now: Flex Consumption

Flex Consumption is Microsoft's recommended serverless Functions plan for new
applications. It is Linux-based, supports Node.js 22, and supports the Storage
Queue trigger used by the durable email sender. It has its own regional memory
quota; Microsoft documents a default of 250 cores and says new apps can still
be created when scaling is constrained by that quota. The zero classic App
Service Total VMs meter therefore does not, by itself, show that Flex is
blocked.

Use **On Demand** with **zero Always Ready instances**. That scales to zero and
has no idle compute charge. Current paid-consumption pricing includes a monthly
free grant for on-demand Flex executions, although storage and normal Azure
charges remain separate:
[Flex Consumption hosting](https://learn.microsoft.com/en-us/azure/azure-functions/flex-consumption-plan)
and [Functions pricing](https://azure.microsoft.com/en-us/pricing/details/functions/).

### What we need from you

**A. Try creating this resource in the portal:**

- Create a resource → Function App → **Flex Consumption**
- Resource group: **rg-advisor-map**
- Runtime: **Node.js 22**
- Operating system: **Linux** (Flex is Linux-only)
- Instance size: default
- Always Ready instances: **0**
- Storage: create a new general-purpose storage account in the same region
- Application Insights: enabled

The equivalent Cloud Shell commands are:

~~~bash
az functionapp list-flexconsumption-locations --query "sort_by(@, &name)[].{Region:name}" -o table

az storage account create --name <new-runtime-storage-name> --location <supported-region> --resource-group rg-advisor-map --sku Standard_LRS --allow-blob-public-access false

az functionapp create --resource-group rg-advisor-map --name <function-app-name> --storage-account <new-runtime-storage-name> --flexconsumption-location <supported-region> --runtime node --runtime-version 22
~~~

These are Microsoft's current creation parameters:
[Create a Flex Consumption app](https://learn.microsoft.com/en-us/azure/azure-functions/flex-consumption-how-to).

**B. Validate the linked backend before changing the live site.**

Microsoft's Static Web Apps overview says an existing Function App can be from
any plan, but its older comparison table still enumerates only Consumption,
Premium, and Dedicated. Because those two statements are inconsistent, do not
cut over the API based on an assumption. Run the GA validation command first:

~~~bash
FUNCTION_ID=$(az functionapp show --resource-group rg-advisor-map --name <function-app-name> --query id -o tsv)

az staticwebapp backends validate --name <static-web-app-name> --resource-group rg-advisor-map --backend-resource-id "$FUNCTION_ID" --backend-region <supported-region>
~~~

The Static Web App must be on the **Standard** plan. If validation succeeds,
send Bo the Function App name and region; he will configure, deploy, smoke-test,
and then link it. Do not remove or replace the current managed API first.
[Static Web Apps backend validation](https://learn.microsoft.com/en-us/cli/azure/staticwebapp/backends#az-staticwebapp-backends-validate).

### If Flex creation or validation fails

**C. Confirm who controls the subscription.** Under Subscription → Overview,
identify the **Offer ID** and **billing account**. If this is reseller- or
CSP-managed, Microsoft may redirect the request to the partner. Bo cannot see
these fields because his role is resource-scope only.

**D. File or continue the support request for classic Consumption.** Include
the complete deployment operation JSON, not only the portal summary.

> **Help + support → New support request**
> **Issue type:** Service and subscription limits (quotas)
> **Quota type:** App Service
>
> Creating a Consumption (Dynamic, Windows) Function App fails preflight
> validation with `SubscriptionIsOverQuotaForSku` on
> `Microsoft.Web/serverFarms`. Current Limit (Total VMs) is 0 with an **empty
> Location field**, and it reproduces identically in East US and East US 2.
> Please determine whether this is a subscription restriction, offer
> restriction, or a regional quota that the portal is failing to identify.
>
> Usage + quotas → App Service → Total Regional VMs shows 0 of 0 and is not
> adjustable ("provided for informational purposes only"), so there is no
> self-service path.
>
> Please identify and remove the restriction that prevents creation of a
> Windows Dynamic/Consumption Function App, or enable sufficient App Service
> Consumption capacity for this subscription. The quota meter is not
> self-serviceable in the portal.
>
> Subscription: `1664043a-7b8e-4cf5-b82b-31effe4d4d44`
> Tracking id: `3f1cfbd6-d240-4cff-96b5-2cd012135fb2`
> Deployment: `Microsoft.Web-FunctionApp-Portal-81177bd7-9e99`

### Cost clarification

Neither Consumption option means that EIC manages a VM. Azure's Total VMs
message is a platform capacity meter, not a request for us to provision an IaaS
virtual machine. Classic Consumption and Flex Consumption have different
execution grants and meters. At this workload, on-demand Flex should be very
low cost, but the invoice—not the error wording—is the source of truth.

---

## Ask 2 — Entra app registration for Microsoft Graph mail

**This is now the only thing standing between the sales team and a working
feature.** All the infrastructure is built, deployed, and tested. Nothing else
is outstanding on our side.

### What it does

The application signs each rep in individually and acts **as that rep**
(delegated permissions, not application permissions). It cannot read or send
mail for anyone who has not personally signed in and consented. There is no
service account with tenant-wide mailbox access.

Day one, the feature is configured to **create drafts only** — messages land in
the rep's Outlook Drafts folder and a human presses Send. Direct sending is
gated behind three separate settings that will stay off until the drafts
workflow has been running cleanly.

### Step 1 — Create the registration

Go to **entra.microsoft.com** → Identity → Applications → **App registrations**
→ **+ New registration**.

| Field | Value |
|---|---|
| Name | `EIC Advisor Map — Email` |
| Supported account types | **Accounts in this organizational directory only (Single tenant)** |

Leave the Redirect URI blank on this screen — it is added in step 2, where the
platform type can be set correctly. Click **Register**.

> **Why a second registration rather than reusing the Phase 1 sign-in app.**
> Technically the existing `EIC Advisor Map` registration could carry both —
> a second redirect URI and the mail permissions added alongside. Sign-in
> would not break. We are asking for a separate one for three reasons:
>
> - **Secret blast radius.** Each registration has its own client secret with
>   its own expiry. Combined, one expired secret takes down sign-in *and*
>   email at once — and sign-in going down locks the field team out of the
>   application entirely.
> - **Audit clarity.** Anyone reviewing the tenant later sees a login app that
>   holds `Mail.Send`, with no way to tell from the object why. Separate
>   registrations state the purpose plainly.
> - **Independent revocation.** If the email feature ever needs switching off
>   or its consent withdrawn, that should not touch the ability to sign in.
>
> If you would rather run one registration, the application supports it — set
> `GRAPH_CLIENT_ID` and `GRAPH_CLIENT_SECRET` to the same values as the
> sign-in app, and add `https://<APP-HOSTNAME>/api/email-auth` as an
> additional redirect URI on it. Tell Bo which way you have gone, because the
> app settings differ.

### Step 2 — Add the redirect URI

**Authentication → + Add a platform → Web**

| Field | Value |
|---|---|
| Redirect URI | `https://<APP-HOSTNAME>/api/email-auth` |
| Front-channel logout URL | leave blank |
| Implicit grant checkboxes | leave **unchecked** |

> **Bo will supply `<APP-HOSTNAME>` — please use his exact value.** It must
> match character for character, including `https://` and with no trailing
> slash. A mismatch here is the single most common cause of the connect flow
> failing, and it fails with an unhelpful error.

Click **Configure**.

### Step 3 — Add delegated permissions

**API permissions → + Add a permission → Microsoft Graph → Delegated
permissions.** Search for and tick each:

| Permission | Why it is needed |
|---|---|
| `User.Read` | read the signed-in rep's own name, title, and phone for the signature |
| `Mail.ReadWrite` | create the draft in that rep's own Drafts folder |
| `Mail.Send` | send it, once direct send is enabled later |
| `offline_access` | refresh tokens, so reps do not have to reconnect daily |
| `openid` | standard sign-in |
| `profile` | standard sign-in |

Click **Add permissions**.

**Delegated, not Application.** This distinction is the whole security model.
Delegated means the app can only ever act as a person who has personally signed
in and consented, limited to that person's own mailbox. Application permissions
would grant tenant-wide mailbox access — we are explicitly not asking for that,
and the code cannot use it.

### Step 4 — Grant admin consent

On the same API permissions page, click **Grant admin consent for
[tenant name]** → **Yes**.

Confirm every row then shows **Granted for [tenant]** in the Status column.

Without this, reps hit a consent prompt they have no rights to approve, and the
feature does nothing.

### Step 5 — Create a client secret

**Certificates & secrets → Client secrets → + New client secret**

| Field | Value |
|---|---|
| Description | `advisor-email` |
| Expires | **24 months** |

**Copy the Value immediately.** Entra displays it exactly once and it cannot be
retrieved afterwards. Copy the **Value** column — not the *Secret ID*, which is
a different string and a common mix-up.

> **Please record the expiry date and set a calendar reminder for one month
> before.** When a client secret expires, email stops working with no other
> warning, and the failure looks like a bug rather than an expiry.

### Step 6 — Restrict who can use it

**Entra ID → Enterprise applications → EIC Advisor Map — Email → Properties**

- **Assignment required?** → **Yes** → Save

Then **Users and groups → + Add user/group** → assign the sales security group
(the same one used in Ask 3).

### Step 7 — Hand back to Bo

From the registration's **Overview** page:

- **Application (client) ID**
- **Directory (tenant) ID**

Plus:

- **Client secret value** — via a password manager or in person, **not by email
  or chat**. It is equivalent to a password for the application.

---

## Ask 3 — Restrict who can open the application *(security gap today)*

**Right now, every account in the EIC tenant can open the application and read
the whole dataset**, including CRM data and which employee owns each client
relationship. This was intended to be restricted during Phase 1 and evidently
was not applied, or was applied to the wrong object.

### Why the application cannot fix this itself

The Static Web App's route rules require the role `authenticated`, which means
"signed in successfully with a tenant account." Static Web Apps has no built-in
way to read Entra group membership, so **the restriction has to be enforced in
Entra**, on the Phase 1 sign-in application.

### The fix

1. **Confirm the security group exists** with the correct members — the field
   sales team plus anyone else who should have access. Create it if the earlier
   attempt did not complete.

2. **Entra ID → Enterprise applications** → find **`EIC Advisor Map`** (the
   Phase 1 *sign-in* app, not the new email one) → **Properties**

   - **Assignment required?** → **Yes** → **Save**

   This is the setting that actually enforces it. With it set to *No*, users and
   groups can be assigned and the assignment is simply ignored — which would
   explain the symptom exactly.

3. **Users and groups → + Add user/group** → assign the security group.

### Please verify rather than assume

Sign in as, or ask, an EIC employee who is **not** in the group, using a private
browser window. They should be refused at sign-in. Existing sessions may persist
until they expire, so a fresh private window matters.

If a non-member still gets in after Assignment required is Yes, likely causes
are: the group is assigned to a different app object; the person holds a
directory role that bypasses assignment; or nested group membership, which
Entra app assignment does not expand.

### A note on scope

Both applications should be restricted — Ask 2 step 6 covers the email one. They
are separate objects and each needs its own setting.

---

## What happens once Ask 2 is delivered

Steps 1 to 4 below are already done. Bo does the remainder; no further admin
involvement needed.

1. ~~Create the Flex Function App~~ — done
2. ~~Deploy the API with a Linux remote build~~ — done
3. ~~Link it to the Static Web App as the backend~~ — done, calling verified
4. ~~Deploy the updated front end~~ — done, composer is live
5. Set the four `GRAPH_*` application settings and restart — ~10 min
6. Each rep connects their own mailbox once, in the app — ~2 min each
7. Test with a single allowlisted recipient, in drafts mode, before anyone
   touches a real advisor contact

**About twenty minutes of work** once the client ID, tenant ID, and secret are
in hand.

---

## If both Function hosting paths remain blocked

There is a fallback that needs no additional Azure compute: a drafts-only build
that runs entirely on the existing managed Static Web App API. It can provide
templated, merged Outlook drafts, but direct sending remains disabled because
managed Static Web Apps Functions cannot run the durable queue trigger. That
fallback requires a small application change and should be treated as the
interim plan, not as a reason to weaken queue reliability.

**Ask 2 is still required for that fallback.** The Graph registration and admin
consent are unavoidable regardless of which path we take, which is why it should
start now.
