# Advisor Map — remote access request for the systems administrator

**Requested by:** Bo Ladyman
**What it is:** an internal web application — static HTML/JavaScript plus about
175 MB of JSON data files. No database, no server-side code, no installer. It
runs on the internal network today, served by a small Python process.

**What we need:** the seven-person external sales team must reach it from
laptops and phones, signed in with their existing company accounts, with MFA.

**Why Bo can't do this himself:** his account has no Azure subscription, no
Azure role assignments, and app registration is blocked in Entra ("You don't
have access"). Everything below needs directory and/or subscription rights.

---

## Two viable options — we prefer Option 1, but you may disagree

Both give Microsoft Entra sign-in, MFA, Conditional Access, and a custom domain
such as `advisors.eicatlanta.com`. Those are **not** differentiators.

### Option 1 — Azure Static Web App *(preferred)*

Host the site in Azure behind Entra ID sign-in.

- Served from a **global CDN** — materially faster on a phone, which is the
  main use case
- **No dependency on our office being online.** The field team is never in the
  office; making their tool depend on our internet link is a strange coupling
- Includes **Azure Functions**, which is where the call logging and CRM
  write-back we're planning next would live
- Nothing for us to keep running
- **Costs:** ~$9/month, and needs an Azure subscription — we don't currently
  have one
- **Trade-off:** the data would be stored in Azure rather than here

### Option 2 — Entra application proxy *(fallback)*

Publish the existing internal site through application proxy.

- **The data never leaves our network**
- **No new spend** — Microsoft 365 E3 includes Entra ID P1, which is what
  application proxy requires
- **No Azure subscription needed**
- No inbound firewall ports; the connector connects outbound only
- **Trade-offs:** every byte routes through Microsoft's edge to a connector on
  our server, so it's slower; and it requires a Windows server, the connector,
  and the Python process all to stay running

**If the subscription or a decision about data leaving the building is going to
take weeks, Option 2 gets the field team working now and can be retired later.**
The two aren't exclusive.

---

## Part 1 — Three questions, whichever option we choose

1. **Are there Conditional Access policies that would apply to a new enterprise
   application — and do they permit personal or unmanaged iPhones?**

   Please answer this first. The whole purpose is field access from phones. If
   policy requires a compliant or Intune-managed device, we need to know before
   anything is built.

2. **Do we already have an Azure subscription?** Bo sees none, but he'd only see
   subscriptions he's assigned to. Please check tenant-wide, and confirm the
   global subscription filter isn't hiding one.

3. **Which option do you prefer, and why?** You know our environment and our
   constraints better than we do. If you'd rather not have this data in Azure,
   say so and we'll do Option 2.

---

# OPTION 1 — Azure Static Web App

## Step 1 — Azure access

1. Identify or create an **Azure subscription**.
2. Create a **resource group**, e.g. `rg-advisor-map`, region **East US 2**.
3. Grant Bo **Contributor on that resource group only** — not the subscription:
   `portal.azure.com` → Resource groups → `rg-advisor-map` →
   **Access control (IAM)** → **Add** → **Add role assignment** →
   Role **Contributor** → Member: Bo → Review + assign.

Resource-group scope is deliberate: it lets Bo deploy data refreshes without
coming back to you each time, and grants nothing elsewhere in Azure.

## Step 2 — Bo creates the app *(no action from you)*

Bo creates the Static Web App in that resource group and sends you the hostname
it generates, of the form `https://<something>.azurestaticapps.net`.

**Steps 3 and 4 need that hostname**, which is why this is a two-part job rather
than one sitting. The sign-in redirect URL can't be registered before the site
exists.

## Step 3 — Register the application in Entra

`entra.microsoft.com` → Identity → Applications → **App registrations** →
**New registration**

| Field | Value |
|---|---|
| Name | `EIC Advisor Map` |
| Supported account types | **Accounts in this organizational directory only (single tenant)** |
| Redirect URI — platform | **Web** |
| Redirect URI — value | `https://<hostname-from-step-2>/.auth/login/aad/callback` |

From the Overview page, copy the **Application (client) ID** and the
**Directory (tenant) ID**.

Then `Certificates & secrets` → **Client secrets** → **New client secret**
(description `advisor-map`, expiry 12 or 24 months).

**Copy the secret VALUE immediately** — Entra shows it once. Copy the *Value*
column, not the *Secret ID*.

> **Please note the expiry date and set a calendar reminder.** When a client
> secret expires, sign-in stops working with no other warning. This is the most
> common way setups like this break a year later.

## Step 4 — Restrict who can sign in

Without this, **every account in our tenant can open the application.** It
contains our CRM data, including which EIC employee owns each client
relationship.

1. Create a security group, e.g. `EIC Advisor Map Users`, with the field sales
   team and anyone else who should have access
2. **Enterprise applications → EIC Advisor Map → Properties** →
   **Assignment required? = Yes** → Save
3. **Users and groups → Add user/group** → assign the group

## Step 5 — Custom domain (when we're ready)

If we point `advisors.eicatlanta.com` at the app, its callback URL must be added
to the same app registration
(`https://advisors.eicatlanta.com/.auth/login/aad/callback`). Sign-in breaks on
the new domain until that's added.

## Hand back to Bo

- **Application (client) ID**
- **Directory (tenant) ID**
- **Client secret value** — via a password manager or in person, **not by email
  or chat**. It's equivalent to a password for the application.
- Resource group and subscription names
- Answers to Part 1

---

# OPTION 2 — Entra application proxy

Only if we're not doing Option 1.

**Prerequisites:** Entra ID P1 (included in our M365 E3), an Application
Administrator account, and synced identities — Entra Connect is already enabled,
so that's satisfied.

## Step 1 — Install the connector

Now called the **Microsoft Entra private network connector** (shared with Entra
Private Access, so docs may use either name). Install on a Windows server that
stays running, with outbound HTTPS (443). No inbound ports.

## Step 2 — Publish the app

`entra.microsoft.com` → **Entra ID → Enterprise apps → New application** →
**Add an on-premises application** (about halfway down, under *On-premises
applications*).

| Field | Value |
|---|---|
| Name | `EIC Advisor Map` |
| Internal URL | e.g. `http://mapserver.yourdomain.local:8781/` |
| External URL | default `*.msappproxy.net`, or a custom domain |
| Pre Authentication | **Microsoft Entra ID** (the default — please keep it) |
| Connector Group | Default |

Leave *Additional settings* at defaults.

**URL rules that cause most first-attempt failures:** both URLs must start with
`http`/`https`, must **end with a trailing `/`**, and must be domain names, not
IP addresses. If the map server is currently reached by IP, we need an internal
DNS record.

## Step 3 — Grant admin consent *(new requirement, easy to miss)*

Since **30 June 2026**, application proxy no longer grants this automatically
for newly created apps, and sign-in will not work without it.

**Enterprise applications → EIC Advisor Map → Permissions →
Grant admin consent for [tenant] → Accept.** Confirm **User.Read** appears
under Admin consent, type *Delegated*.

This applies only to newly created apps, so it won't match older setups you may
have configured before.

## Step 4 — Restrict who can sign in

Same as Option 1, Step 4.

## Step 5 — Test

The application's **Application Proxy** page has a **Test Application** button
that runs a diagnostic. Worth running before anyone gets the URL.

---

## Operational note — applies to Option 2 only

The map server is currently a Python process someone starts by hand; it won't
survive a reboot. If we publish it via application proxy, it should run as a
Windows service or scheduled task that starts automatically. Bo can supply the
command — we just need to agree where it lives.

---

## One separate item, unrelated to remote access

The application currently runs internally with **no authentication at all** —
anyone who can reach the port can read the whole dataset, including the CRM
layer. Both options above fix this for external users; neither fixes it
internally. Worth addressing on its own merits.
