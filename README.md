# Garmin MCP connector

A personal, read-only [MCP](https://modelcontextprotocol.io) server that lets an AI
assistant read **my own** Garmin data through the official Garmin Connect Personal API.
Single user, no database, no stored history.

It is a standard remote MCP server (Streamable HTTP with MCP-spec authorization), so it
is not tied to one assistant: any compliant client can connect.

> **Status.** Phases 1 and 2 are implemented and covered by automated tests that use
> fakes. Not yet verified against the real Garmin API, real GitHub login, or real
> clients. Sections marked _(not yet run)_ contain commands that have not been
> executed for this project yet.

## Tools

| Tool | Returns |
|---|---|
| `list_activities` | Activities between two dates, optional type filter, paginated |
| `get_activity` | Details of one activity, optionally its workouts |
| `get_daily_health` | One day: daily summary, sleep and HRV |
| `get_health_range` | Daily summaries, sleep and HRV over a date range |

Every response has the same shape:

```json
{ "source": "Garmin Connect Personal API", "garmin_data": {}, "own_calculations": null, "warnings": [] }
```

`garmin_data` holds values exactly as Garmin reports them. `own_calculations` is for
anything this connector computes; it is `null` today. The two are never mixed.

Only endpoints documented in the
[official Swagger](https://developer.garmin.com/gc-developer-program/personal/swagger-docs/)
are used.

## How it is built

```
src/garmin_mcp/
  config.py        settings from environment variables
  garmin.py        official Garmin client + token renewal, cache, safe errors
  tools.py         tool logic (plain functions, no MCP)
  server.py        exposes the tools over MCP
  app.py           assembles the web app (MCP endpoint + authorization)
  auth/
    provider.py    OAuth logic the MCP SDK calls into
    clients.py     client registration and client metadata documents
    tokens.py      signed, self-contained tokens
    github.py      GitHub login and consent screen
```

Design rules:

- **Nothing is stored.** The container scales to zero, so tokens, client registrations
  and login state are signed tokens, not rows in a database.
- **No health data in logs.** Logs contain tool name, outcome and duration only.
- **Small cache.** In memory, 5 minutes, at most 64 entries.
- **The Garmin key is never in the code or the repository.**

## Authorization

The server is both the OAuth resource server (`/mcp`) and the authorization server.

1. A client calls `/mcp` without a token and gets `401` with a pointer to
   `/.well-known/oauth-protected-resource/mcp`.
2. From there it discovers `/authorize`, `/token` and `/register`.
3. The browser is sent to GitHub. Only the account whose numeric id equals
   `GITHUB_ALLOWED_USER_ID` is accepted.
4. A consent screen shows which application is asking and where access would be sent.
   Nothing is granted until **Allow** is pressed.
5. The client receives an access token (1 hour) and a refresh token (30 days, renewed
   on each use).

Limits worth knowing:

- A single client cannot be revoked. Rotating `GARMIN_MCP_SIGNING_KEY` logs out every
  client at once.
- Security rests on the GitHub account. **Enable two-factor authentication on it.**

## Running locally

Requires Python 3.12+.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
cp .env.example .env      # then fill it in
python -m garmin_mcp
```

The endpoint is `http://localhost:8000/mcp`. Test it with
`npx @modelcontextprotocol/inspector` (transport: Streamable HTTP).

To fill in `.env`:

1. **Garmin key**: Garmin Connect Web > Personal API Key > Create Key. It is shown once.
2. **Signing key**: `python -c "import secrets; print(secrets.token_urlsafe(48))"`
3. **GitHub OAuth App**: GitHub > Settings > Developer settings > OAuth Apps > New.
   Callback URL: `http://localhost:8000/auth/github/callback`. Copy the client id and
   generate a client secret.
4. **`GITHUB_ALLOWED_USER_ID`**: the `id` field of `https://api.github.com/users/<username>`.

To try the tools without any login, set `GARMIN_MCP_AUTH_DISABLED=1`. The server then
only accepts `127.0.0.1` and refuses to start on any other host.

## Deploying to Azure _(not yet run)_

Target: Azure Container Apps, Consumption plan, scale to zero, at most one replica.
Image in GitHub Container Registry, deployed by the manual **Deploy** workflow.

Cost: the Consumption plan includes a monthly free grant per subscription (180,000
vCPU-seconds, 360,000 GiB-seconds and 2 million requests, per Microsoft's pricing page
at the time of writing). A connector used by one person and scaled to zero is expected
to stay inside it. That is a quota, not a spending cap, hence the budget alert below.

### One-time setup

```bash
RG=rg-garmin-mcp
ENVIRONMENT=cae-garmin-mcp
APP=garmin-mcp
LOCATION=westeurope

az group create --name $RG --location $LOCATION
az containerapp env create --name $ENVIRONMENT --resource-group $RG --location $LOCATION

# The public URL is known before the app exists: <app>.<environment default domain>
DOMAIN=$(az containerapp env show --name $ENVIRONMENT --resource-group $RG --query properties.defaultDomain -o tsv)
PUBLIC_URL="https://$APP.$DOMAIN"
echo $PUBLIC_URL
```

Create a **second** GitHub OAuth App for production, with callback
`<PUBLIC_URL>/auth/github/callback` (an OAuth App allows a single callback URL).

Run the **Deploy** workflow once so the image exists (its deploy job will fail until the
app is created; that is expected). Then create a GitHub personal access token with only
`read:packages`, so Azure can pull the private image, and create the app:

```bash
az containerapp create \
  --name $APP --resource-group $RG --environment $ENVIRONMENT \
  --image ghcr.io/victoriacarbonell/garmin-mcp-connector:latest \
  --registry-server ghcr.io --registry-username victoriacarbonell --registry-password <READ_PACKAGES_TOKEN> \
  --ingress external --target-port 8000 \
  --min-replicas 0 --max-replicas 1 --cpu 0.25 --memory 0.5Gi \
  --secrets garmin-key=<GARMIN_KEY> signing-key=<SIGNING_KEY> github-client-secret=<GITHUB_CLIENT_SECRET> \
  --env-vars \
    GARMIN_PERSONAL_API_KEY=secretref:garmin-key \
    GARMIN_MCP_SIGNING_KEY=secretref:signing-key \
    GITHUB_CLIENT_SECRET=secretref:github-client-secret \
    GITHUB_CLIENT_ID=<GITHUB_CLIENT_ID> \
    GITHUB_ALLOWED_USER_ID=<NUMERIC_ID> \
    GARMIN_MCP_PUBLIC_URL=$PUBLIC_URL
```

Type the secrets in a terminal you trust and clear the shell history afterwards, or set
them in the Azure Portal (Container App > Secrets) instead.

`--min-replicas 0 --max-replicas 1` is what keeps the cost at zero when idle and bounded
when busy. One replica is also required by the design: authorization codes are tracked
in memory for their 60-second lifetime.

For later deployments, let GitHub Actions log in to Azure without a stored password
(OIDC): create an app registration with a federated credential for this repository,
give it the Contributor role on the resource group, and add to the repository:

- Secrets: `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`
- Variables: `AZURE_CONTAINER_APP`, `AZURE_RESOURCE_GROUP`

### Budget alert

Azure Portal > Cost Management > Budgets > Add. Scope: the subscription. Amount: 1 EUR,
monthly. Alerts at 50 % and 100 % of actual cost, sent to your email. The alert only
notifies; it does not stop spending.

### Connecting a client

Give the client the URL `<PUBLIC_URL>/mcp`. It discovers the rest and opens the GitHub
login and the consent screen in a browser. In Claude this is done under Settings >
Connectors > Add custom connector. Steps for other clients have not been verified here.

## Maintenance

### The Garmin key expires _(not yet run)_

Garmin emails about a week before the key expires (30, 90, 180 days or 1 year, as chosen
when creating it). Symptom if missed: every tool fails with
`Garmin returned HTTP 401 ... The Garmin key may have expired`.

1. Garmin Connect Web > Personal API Key: delete the old key and create a new one.
2. Replace the secret and restart:

```bash
az containerapp secret set --name garmin-mcp --resource-group rg-garmin-mcp --secrets garmin-key=<NEW_KEY>
az containerapp revision restart --name garmin-mcp --resource-group rg-garmin-mcp \
  --revision $(az containerapp revision list --name garmin-mcp --resource-group rg-garmin-mcp --query "[0].name" -o tsv)
```

Clients stay connected: the Garmin key and the clients' tokens are independent.
Locally, just edit `.env` and restart.

### Logging every client out

Replace `signing-key` the same way as above with a newly generated value. Every client
must authorize again. Do this if a device with a connected client is lost.

### Changing the allowed GitHub account

Update `GITHUB_ALLOWED_USER_ID`. Tokens issued to the previous account stop working
immediately.

### Updating dependencies

```bash
pip install -U -e ".[dev]" && pytest
```

The MCP SDK and the Garmin client are pinned to a major version in `pyproject.toml`.
Read their release notes before raising those bounds; the authorization code relies on
SDK internals described in `app.py`.

### Reading the logs

```bash
az containerapp logs show --name garmin-mcp --resource-group rg-garmin-mcp --follow
```

Expect lines such as `tool=list_activities outcome=ok ms=412`. Health data never
appears there; if it ever does, treat it as a bug.

## Terms of use

The Garmin Connect Personal API is for personal access to your own data, not for
applications serving other users. Read the terms shown when creating the key before
using this with any AI assistant; using a connector sends the returned data to that
assistant's provider.

## License and disclaimer

© Victoria Carbonell Sánchez. All rights reserved.

This repository is public as a sample of my work. It is a personal project that I run
for myself with my own Garmin key: it is not a product or a service, no hosted instance
is offered to anyone else, and no permission is granted to use, modify or distribute
the code.

Not affiliated with, endorsed by or sponsored by Garmin. "Garmin" and "Garmin Connect"
are trademarks of Garmin Ltd. or its subsidiaries.
