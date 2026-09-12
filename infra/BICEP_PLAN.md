# DR Command Center — Bicep Plan (Dev / Test)

**Status:** Canonical companion (regenerated 2026-09-12, D-220). Implemented in BUILD-22; validated by BUILD-23 security gates.
**Decisions applied:** D-217 (Blob), D-239 (session cookie, same-origin `/api`), D-241/D-242 (Celery, Redis pub/sub), D-244 (Chromium in worker), D-245 (Defender for Storage), D-246 (Checkov), D-248 (Container Apps, Azure Database for PostgreSQL, Key Vault, Log Analytics/App Insights, Azure Managed Redis; Dev/Test separate resource groups; outside the protected workload failure domain), D-260 (PITR 35 d prod, budgets, structured logs), FROZEN §3.
**Scope:** Dev and Test resource groups. Production is a separate, later phase (D-206); §9 records the production posture that Dev/Test must not paint themselves into.

---

## 1. Topology

```text
Subscription (POC)
├── rg-drcc-dev-<loc>            e.g. rg-drcc-dev-wus2
│   ├── id-drcc-dev              user-assigned managed identity (shared by web/api/worker)
│   ├── log-drcc-dev             Log Analytics workspace
│   ├── appi-drcc-dev            Application Insights (workspace-based)
│   ├── kv-drcc-dev-<suffix>     Key Vault (RBAC mode) — secrets + package KEK
│   ├── crdrccdev<suffix>        Container Registry (Basic)
│   ├── stdrccdev<suffix>        Storage account — evidence / documents / packages / quarantine
│   ├── evgt-drcc-dev-scan       Event Grid topic — Defender malware scan results
│   ├── psql-drcc-dev-<suffix>   PostgreSQL Flexible Server 16 (+ vector, pgcrypto, pg_stat_statements)
│   ├── redis-drcc-dev-<suffix>  Azure Managed Redis (Balanced B0/B1)
│   ├── vnet-drcc-dev            VNet (infra subnet for Container Apps; pe subnet for private endpoints)
│   ├── cae-drcc-dev             Container Apps environment (workload profiles: Consumption)
│   │   ├── ca-drcc-dev-web      Next.js — external ingress (the only public hostname)
│   │   ├── ca-drcc-dev-api      FastAPI — internal ingress; sticky sessions optional
│   │   ├── ca-drcc-dev-worker   Celery worker — no ingress; KEDA redis scaler
│   │   └── ca-drcc-dev-beat     Celery beat — no ingress; exactly 1 replica
│   ├── budget + action group + metric/log alerts
│   └── (test only) private endpoints + private DNS zones for psql / storage / kv / redis
└── rg-drcc-test-<loc>           identical module set, `env = test` parameters
```

Region: `location` parameter, default **`westus2`**. The coordinated workloads run East US 2 → Central US; the Command Center must sit outside both (FROZEN §3.1), so neither of those regions is allowed for `location` (enforced by an `@allowed` list in `main.bicep`).

Traffic: browser → `ca-…-web` (HTTPS, public) → Next.js proxies `/api/*` (HTTP + WebSocket upgrade) → `ca-…-api` over the environment's internal DNS (`http://ca-drcc-dev-api`). One origin, one cookie (D-239). `api` is never public in Dev/Test; Playwright/k6 in CI go through `web`.

Network mode:
- **Dev** (`networkMode: 'public'`): PaaS data services keep public endpoints with firewall rules limited to the Container Apps environment's outbound IPs and a `developerIpAllowlist` parameter. POC public ingress is explicitly allowed (FROZEN §3.3).
- **Test** (`networkMode: 'private'`): data services have `publicNetworkAccess: Disabled` and private endpoints in `snet-pe`; Container Apps environment is VNet-injected into `snet-infra` (`/23`); ingress on `web` stays external so testers can reach it. This rehearses the production posture (§9) before production exists.

---

## 2. Module files

All under `infra/bicep/`. Resource-group scope throughout (`az deployment group …`). Naming follows CAF abbreviations; `<suffix>` = `uniqueString(resourceGroup().id)` truncated to 6 characters for globally-unique names.

| File | Purpose | Key parameters | Outputs |
|---|---|---|---|
| `main.bicep` | Orchestrator; wires modules in dependency order; applies `tags` (`project=drcc`, `env`, `costCenter`, `owner`) | `env` (`dev`\|`test`), `location` (`@allowed`, excludes `eastus2`, `centralus`), `networkMode` (`public`\|`private`), `imageTag`, `entraTenantId`, `entraClientId`, `postgresAdminPassword` (`@secure`), `postgresAppPassword` (`@secure`), `postgresRoPassword` (`@secure`), `sessionSecret` (`@secure`), `csrfSecret` (`@secure`), `developerIpAllowlist` (array), `budgetAmount`, `alertEmails` (array), `apiStickySessions` (bool, default `false`), `smtpHost`, `smtpPort`, `smtpFromAddress` | `webFqdn`, `apiInternalFqdn`, `keyVaultName`, `storageAccountName`, `postgresFqdn`, `redisHostName`, `containerRegistryLoginServer`, `appInsightsConnectionString` (secure output not exposed; stored in KV), `managedIdentityClientId` |
| `modules/identity.bicep` | User-assigned managed identity + role assignments | `name`, `location`, `keyVaultId`, `storageAccountId`, `containerRegistryId` | `identityId`, `principalId`, `clientId` |
| `modules/monitoring.bicep` | Log Analytics workspace (30 d dev / 90 d test retention), workspace-based App Insights, diagnostic settings helper | `name`, `location`, `retentionDays` | `workspaceId`, `workspaceCustomerId`, `appInsightsId`, `appInsightsConnectionString` (secure) |
| `modules/keyvault.bicep` | Key Vault, RBAC authorization, soft delete 90 d, purge protection on; creates the package KEK; writes non-generated secrets passed from parameters | `name`, `location`, `secrets` (secure object), `networkMode`, `peSubnetId`, `logWorkspaceId` | `keyVaultId`, `keyVaultUri`, `packageKekId` (`https://…/keys/drcc-package-kek`) |
| `modules/registry.bicep` | Azure Container Registry (`Basic` dev, `Standard` test), admin user disabled, MI pull | `name`, `location`, `sku` | `registryId`, `loginServer` |
| `modules/storage.bicep` | Storage account (StorageV2, `Standard_LRS` dev / `Standard_ZRS` test), HTTPS only, TLS 1.2, shared-key access **disabled** (MI + user-delegation SAS only), blob public access disabled, soft delete 14 d, versioning on; containers `evidence`, `documents`, `packages`, `quarantine`; Event Grid topic + Defender for Storage (`onUpload` malware scanning, `capGBPerMonth` 1000 dev / 5000 test, `scanResultsEventGridTopicResourceId`); event subscription → `api` webhook `/api/v1/internal/malware-result` | `name`, `location`, `sku`, `networkMode`, `peSubnetId`, `allowedIps`, `containerAppsOutboundIps`, `apiWebhookUrl`, `logWorkspaceId` | `storageAccountId`, `blobEndpoint`, `scanTopicId` |
| `modules/postgres.bicep` | PostgreSQL Flexible Server, version `16`; `Standard_B2ms` (dev) / `Standard_D2ds_v5` (test); 128 GiB storage, auto-grow; backup `retentionDays` 7 (dev) / 35 (test, rehearsing prod); `geoRedundantBackup` `Enabled` on test; HA `Disabled` dev / `ZoneRedundant` test; password auth + Entra auth enabled; server parameters `azure.extensions = VECTOR,PGCRYPTO,PG_STAT_STATEMENTS`, `shared_preload_libraries = pg_stat_statements`, `log_min_duration_statement = 500`, `max_connections = 200`; database `drcc`; firewall (public mode) or delegated subnet + private DNS zone (private mode); diagnostic settings → Log Analytics | `name`, `location`, `sku`, `tier`, `storageGb`, `backupRetentionDays`, `geoRedundantBackup`, `highAvailability`, `adminLogin`, `adminPassword` (`@secure`), `networkMode`, `delegatedSubnetId`, `privateDnsZoneId`, `allowedIps`, `containerAppsOutboundIps`, `logWorkspaceId` | `serverId`, `fqdn`, `databaseName` |
| `modules/redis.bicep` | Azure Managed Redis (`Microsoft.Cache/redisEnterprise`), SKU `Balanced_B0` dev / `Balanced_B1` test; database on port 10000, `clientProtocol: Encrypted`, `clusteringPolicy: EnterpriseCluster` (single endpoint, standard clients, **DB 0 only** — hence `.env.example` uses key prefixes), `evictionPolicy: NoEviction` (sessions and Celery must not be evicted), AOF persistence 1 s; access key written to Key Vault as `redis-url` (`rediss://:<key>@<host>:10000/0`); private endpoint in private mode | `name`, `location`, `sku`, `networkMode`, `peSubnetId`, `keyVaultName`, `logWorkspaceId` | `hostName`, `redisUrlSecretUri` |
| `modules/network.bicep` | VNet `10.40.0.0/16`; `snet-infra` `/23` (Container Apps, delegated `Microsoft.App/environments`), `snet-pe` `/24` (private endpoints), `snet-psql` `/24` (Flexible Server delegation, private mode); private DNS zones `privatelink.postgres.database.azure.com`, `privatelink.blob.core.windows.net`, `privatelink.vaultcore.azure.net`, `privatelink.redis.azure.net` linked to the VNet; NSG on `snet-infra` allowing 443 inbound only | `name`, `location`, `networkMode` | `vnetId`, `infraSubnetId`, `peSubnetId`, `psqlSubnetId`, `privateDnsZoneIds` (object) |
| `modules/container-apps-env.bicep` | Container Apps environment, workload profile `Consumption`, VNet-injected (`infrastructureSubnetId`), `internal: false` (web needs a public hostname; api is internal at app level), Log Analytics destination, Dapr off, zone redundancy on test | `name`, `location`, `infraSubnetId`, `logWorkspaceId`, `logWorkspaceCustomerId`, `logWorkspaceSharedKey` (`@secure`), `zoneRedundant` | `environmentId`, `defaultDomain`, `staticIp`, `outboundIps` |
| `modules/container-app.bicep` | Generic app: image from ACR via MI, secrets as **Key Vault references** (`keyVaultUrl` + `identity`), env vars (plain + `secretRef`), ingress (external/internal/none, transport `auto` so WebSockets pass, sticky sessions optional), probes (`/healthz` web, `/api/v1/health` api), scale rules, CPU/memory, `revisionMode: Single` | `name`, `location`, `environmentId`, `identityId`, `image`, `registryServer`, `ingress` (object or null), `secrets` (array of `{name, keyVaultUrl}`), `env` (array), `probes`, `scale` (`{min, max, rules}`), `cpu`, `memory`, `command` (array, optional) | `fqdn`, `appId`, `latestRevisionName` |
| `modules/budget.bicep` | `Microsoft.Consumption/budgets` at RG scope; monthly; notifications Actual 80 % and Forecasted 100 % → action group | `name`, `amount`, `actionGroupId`, `contactEmails`, `startDate` | `budgetId` |
| `modules/alerts.bicep` | Action group (email) + metric alerts + scheduled-query alerts (§6) | `name`, `location`, `contactEmails`, `apiAppId`, `webAppId`, `workerAppId`, `postgresId`, `redisId`, `storageId`, `appInsightsId`, `workspaceId` | `actionGroupId` |
| `params/dev.bicepparam` | `using 'main.bicep'`; dev values; secrets read from environment at deploy time (`readEnvironmentVariable('DRCC_PG_ADMIN_PASSWORD')`, …) | — | — |
| `params/test.bicepparam` | test values (`networkMode: 'private'`, `backupRetentionDays: 35`, `geoRedundantBackup: 'Enabled'`, `highAvailability: 'ZoneRedundant'`, ZRS storage) | — | — |
| `bicepconfig.json` | Linter: `use-recent-api-versions`, `secure-secrets-in-params`, `no-hardcoded-env-urls`, `outputs-should-not-contain-secrets`, `use-secure-value-for-secure-inputs` all `error`; `experimentalFeaturesEnabled` off | — | — |

Container Apps per environment (all from `modules/container-app.bicep`):

| App | Image | Ingress | Scale | CPU / Mem | Notes |
|---|---|---|---|---|---|
| `ca-drcc-<env>-web` | `crdrcc…/drcc-web:<imageTag>` | external, 3000, transport `auto` | 1–3 (HTTP concurrency 50) | 0.5 / 1 Gi | env `API_INTERNAL_URL=http://ca-drcc-<env>-api`, `NEXT_PUBLIC_API_BASE=/api` |
| `ca-drcc-<env>-api` | `crdrcc…/drcc-api:<imageTag>` | internal, 8000, transport `auto`, `stickySessions.affinity: apiStickySessions ? 'sticky' : 'none'` | 1–4 (HTTP concurrency 100) | 1 / 2 Gi | runs `alembic upgrade head` as an init container before serving; correctness never depends on affinity (D-242) |
| `ca-drcc-<env>-worker` | `crdrcc…/drcc-worker:<imageTag>` | none | 1–4, KEDA `redis` scaler on Celery queue length (`listName: celery`, `listLength: 20`) | 2 / 4 Gi | Chromium + sentence-transformers + faster-whisper; model cache on Azure Files volume `models` (env storage mount) |
| `ca-drcc-<env>-beat` | `crdrcc…/drcc-worker:<imageTag>` | none | min = max = 1 | 0.25 / 0.5 Gi | `command: celery -A app.jobs.celery_app beat`; single scheduler (D-241) |

Secrets referenced by the apps (Key Vault secret names → env var):

| Secret | Env var | Consumer |
|---|---|---|
| `session-secret` | `SESSION_SECRET` | api |
| `csrf-secret` | `CSRF_SECRET` | api |
| `postgres-app-url` | `DATABASE_URL` | api, worker, beat |
| `postgres-ro-url` | `DATABASE_URL_RO` | api |
| `redis-url` | `REDIS_URL`, `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND` | api, worker, beat |
| `entra-client-secret` | `ENTRA_CLIENT_SECRET` | api |
| `smtp-password` | `SMTP_PASSWORD` | api, worker |
| `synthetic-api-key` | `SYNTHETIC_API_KEY` | api, worker |
| `anthropic-api-key` | `ANTHROPIC_API_KEY` | api, worker |
| `appinsights-connection-string` | `APPLICATIONINSIGHTS_CONNECTION_STRING` | all |
| `malware-webhook-secret` | `MALWARE_WEBHOOK_SECRET` | api |
| key `drcc-package-kek` (RSA-3072, `wrapKey`/`unwrapKey`) | `DRCC_PACKAGE_KEK_ID` (URI, not a secret) | api, worker (D-230) |

Non-secret env in Azure differs from `.env.example` only in: `DRCC_ENV=dev|test`, `DRCC_TENANT_ID=<Entra tenant GUID>`, `AZURE_STORAGE_ACCOUNT_URL=https://stdrcc…blob.core.windows.net` (MI, no connection string), `MALWARE_SCANNER=defender`, `LOG_FORMAT=json`, `OTEL_EXPORTER_OTLP_ENDPOINT` empty (App Insights exporter used instead), `SMTP_*` from parameters.

---

## 3. Identity and RBAC

One user-assigned managed identity per environment (`id-drcc-<env>`), assigned to web/api/worker/beat. Role assignments (scoped as tightly as Bicep allows):

| Scope | Role | Why |
|---|---|---|
| Key Vault | `Key Vault Secrets User` | Container Apps secret references |
| Key Vault | `Key Vault Crypto User` | `wrapKey`/`unwrapKey` on `drcc-package-kek` (D-230) |
| Storage account | `Storage Blob Data Contributor` | evidence/documents/packages read/write, quarantine move |
| Storage account | `Storage Blob Delegator` | user-delegation SAS for time-limited download URLs (SECURITY_REVIEW §6) |
| Container Registry | `AcrPull` | image pull |
| Event Grid topic | (none) | Defender publishes; api receives webhook validated by HMAC |

The deploying principal (CI service principal or the engineer) needs `Contributor` + `User Access Administrator` on the RG (role assignments) and `Key Vault Secrets Officer` to write the initial secrets. PostgreSQL application roles (`drcc`, `drcc_readonly`) are created after deployment by `scripts/azure/postgres-bootstrap.sh` (runs the same statements as `infra/docker/postgres-init/01-extensions.sql` against the Flexible Server; `CREATE EXTENSION vector` succeeds only because `azure.extensions` allowlists it).

---

## 4. Observability

- Container Apps environment → Log Analytics (`ContainerAppConsoleLogs_CL`, `ContainerAppSystemLogs_CL`).
- App Insights connection string injected into every app; api/worker use the OpenTelemetry Azure Monitor exporter; correlation id propagated as `traceparent` (SECURITY_REVIEW §4).
- Diagnostic settings on PostgreSQL (`PostgreSQLLogs`, metrics), Storage (blob read/write/delete, metrics), Key Vault (`AuditEvent`), Redis (metrics), Event Grid (delivery failures) → same workspace.
- Audit/security log tables are Sentinel-ready (structured JSON, correlation id); Sentinel onboarding is later (D-260).

---

## 5. Budgets

| Env | Monthly budget (USD) | Notifications |
|---|---|---|
| dev | `budgetAmount` default 300 | Actual ≥ 80 %, Forecasted ≥ 100 % → `alertEmails` |
| test | `budgetAmount` default 600 | same |

Cost is tracked per service through the `project`/`env` tags (OPERATIONS.md FinOps). AI provider spend is outside Azure and reported by the app's usage metrics.

---

## 6. Alerts (`modules/alerts.bicep`)

| Alert | Signal | Threshold | Severity |
|---|---|---|---|
| API 5xx ratio | App Insights `requests/failed` / `requests/count` | > 5 % over 5 min | 1 |
| API p95 latency | App Insights `requests/duration` p95 | > 500 ms over 15 min (FROZEN §16.3) | 2 |
| API replica restarts | Container Apps `RestartCount` | ≥ 3 in 10 min | 2 |
| Worker queue backlog | Log query: Celery queue length gauge emitted by beat | > 500 for 10 min | 2 |
| Outbox lag | Log query: `outbox_publish_lag_seconds` max | > 10 s for 5 min (D-259) | 1 |
| PostgreSQL CPU | `cpu_percent` | > 80 % over 15 min | 2 |
| PostgreSQL storage | `storage_percent` | > 85 % | 1 |
| PostgreSQL connections | `active_connections` | > 160 (of 200) | 2 |
| Redis memory | `usedmemorypercentage` | > 80 % over 10 min | 2 |
| Malware detected | Event Grid delivery of `Microsoft.Security.MalwareScanningResult` with verdict `Malicious` (log query on webhook audit) | ≥ 1 | 1 |
| Key Vault access denied | `AuditEvent` with `ResultType != Success` | ≥ 5 in 5 min | 2 |
| Auth anomalies | Log query: failed logins / denied high-risk ops (app audit log) | ≥ 20 in 5 min | 2 |
| Audit pipeline failure | Log query: `audit_write_failed` | ≥ 1 | 0 |

All alerts route to one action group (email now; Teams/Slack channels are V2 per D-256).

---

## 7. Deployment commands

Prerequisites: `az` ≥ 2.65 with Bicep ≥ 0.30 (`az bicep upgrade`), `checkov`, a subscription where `Microsoft.App`, `Microsoft.DBforPostgreSQL`, `Microsoft.Cache`, `Microsoft.Security`, `Microsoft.EventGrid`, `Microsoft.OperationalInsights`, `Microsoft.Insights`, `Microsoft.KeyVault`, `Microsoft.ContainerRegistry`, `Microsoft.Storage`, `Microsoft.Network` are registered, and an Entra app registration (`entraClientId`) whose redirect URI is `https://<webFqdn>/api/v1/auth/entra/callback` (added after the first deploy prints `webFqdn`).

```bash
# 0. Select subscription and export secrets for this shell only (never commit)
az account set --subscription "<POC subscription id>"
export DRCC_PG_ADMIN_PASSWORD="$(openssl rand -base64 24)"
export DRCC_PG_APP_PASSWORD="$(openssl rand -base64 24)"
export DRCC_PG_RO_PASSWORD="$(openssl rand -base64 24)"
export DRCC_SESSION_SECRET="$(openssl rand -hex 32)"
export DRCC_CSRF_SECRET="$(openssl rand -hex 32)"

# 1. Lint + build + IaC scan (also run by `make security` / CI)
az bicep lint --file infra/bicep/main.bicep
az bicep build --file infra/bicep/main.bicep --stdout > /dev/null
checkov -d infra/bicep --framework bicep --quiet

# 2. Resource groups (one per environment; FROZEN §3.2)
az group create --name rg-drcc-dev-wus2  --location westus2 --tags project=drcc env=dev  owner=<alias>
az group create --name rg-drcc-test-wus2 --location westus2 --tags project=drcc env=test owner=<alias>

# 3. Dev — preview, then deploy
az deployment group what-if \
  --resource-group rg-drcc-dev-wus2 \
  --template-file infra/bicep/main.bicep \
  --parameters infra/bicep/params/dev.bicepparam \
  --parameters imageTag="$(git rev-parse --short HEAD)" \
  --result-format FullResourcePayloads

az deployment group create \
  --name "drcc-dev-$(date -u +%Y%m%d%H%M%S)" \
  --resource-group rg-drcc-dev-wus2 \
  --template-file infra/bicep/main.bicep \
  --parameters infra/bicep/params/dev.bicepparam \
  --parameters imageTag="$(git rev-parse --short HEAD)" \
  --query "properties.outputs" --output json | tee .deploy/dev-outputs.json

# 4. Test — same, private network mode, 35-day PITR
az deployment group what-if \
  --resource-group rg-drcc-test-wus2 \
  --template-file infra/bicep/main.bicep \
  --parameters infra/bicep/params/test.bicepparam \
  --parameters imageTag="$(git rev-parse --short HEAD)" \
  --result-format FullResourcePayloads

az deployment group create \
  --name "drcc-test-$(date -u +%Y%m%d%H%M%S)" \
  --resource-group rg-drcc-test-wus2 \
  --template-file infra/bicep/main.bicep \
  --parameters infra/bicep/params/test.bicepparam \
  --parameters imageTag="$(git rev-parse --short HEAD)" \
  --query "properties.outputs" --output json | tee .deploy/test-outputs.json

# 5. One-time post-deploy per environment
sh scripts/azure/postgres-bootstrap.sh rg-drcc-dev-wus2        # extensions, drcc + drcc_readonly roles, writes postgres-*-url secrets
az keyvault secret set --vault-name "$(jq -r .keyVaultName.value .deploy/dev-outputs.json)" --name entra-client-secret --value "<from Entra app registration>"
az keyvault secret set --vault-name "$(jq -r .keyVaultName.value .deploy/dev-outputs.json)" --name smtp-password --value "<smtp>"
# synthetic-api-key / anthropic-api-key only when AI is enabled for that environment (D-249)

# 6. Image build/push (CI does this on every merged BUILD tag)
az acr login --name "$(jq -r .containerRegistryLoginServer.value .deploy/dev-outputs.json | cut -d. -f1)"
docker build -t "<loginServer>/drcc-api:$(git rev-parse --short HEAD)"    apps/api
docker build -t "<loginServer>/drcc-worker:$(git rev-parse --short HEAD)" -f apps/api/Dockerfile.worker apps/api
docker build -t "<loginServer>/drcc-web:$(git rev-parse --short HEAD)"    -f apps/web/Dockerfile .
docker push "<loginServer>/drcc-api:$(git rev-parse --short HEAD)"
docker push "<loginServer>/drcc-worker:$(git rev-parse --short HEAD)"
docker push "<loginServer>/drcc-web:$(git rev-parse --short HEAD)"
# then re-run step 3/4 `create` with the new imageTag (revision roll; previous revision is the rollback target)

# 7. Teardown (dev only, when idle)
az group delete --name rg-drcc-dev-wus2 --yes --no-wait
```

`what-if` is mandatory before every `create` in CI (the workflow fails if what-if reports `Delete` on a data resource — PostgreSQL, Storage, Key Vault, Redis — without an explicit `ALLOW_DATA_DELETE=1`).

Rollback: `az containerapp revision activate` on the previous revision of `api`/`web`/`worker` (immutable images, OPERATIONS.md Rollback); schema changes follow expand/migrate/contract, so a code rollback does not require a downgrade.

---

## 8. Verification (BUILD-22 acceptance)

| Check | Command / evidence |
|---|---|
| Lint clean | `az bicep lint` exit 0 |
| Checkov clean (no Critical/High) | `make security` |
| what-if shows no drift on a second run | `az deployment group what-if …` → "No change" |
| `webFqdn` serves the app; `/api/v1/health` 200 via web proxy; WebSocket upgrade succeeds through `web` | `curl`, Playwright `@realtime` against `E2E_BASE_URL=https://<webFqdn>` |
| Entra login round-trip | manual, recorded |
| `api` not reachable publicly | `curl https://ca-drcc-dev-api.<domain>` → no DNS / 404 |
| Postgres extensions | `select extname from pg_extension` shows `vector`, `pgcrypto`, `pg_stat_statements` |
| PITR retention | `az postgres flexible-server show … backup.backupRetentionDays` = 7 (dev) / 35 (test) |
| Defender scan | upload EICAR to `evidence` → Event Grid → webhook → `malware_scan_status = INFECTED`, object moved to `quarantine` |
| Key Vault references resolve | app revision `provisioningState = Succeeded`; no secret value in `az containerapp show` output |
| Shared-key access disabled | `az storage account show … allowSharedKeyAccess` = false |
| Budget + alerts exist | `az consumption budget list`, `az monitor metrics alert list` |
| Logs flowing | KQL `ContainerAppConsoleLogs_CL | take 10`; App Insights live metrics |

---

## 9. Production posture (documented now, built later)

Not deployed in this plan; Dev/Test parameters must not contradict it.

- **Private ingress:** Container Apps environment `internal: true`; Azure Front Door Premium (WAF, Private Link origin to the environment) or Application Gateway v2 with WAF in front; `web` has no public hostname; corporate access via Front Door + Entra Conditional Access or via ExpressRoute/VPN to the internal load balancer.
- **Data plane:** `publicNetworkAccess: Disabled` everywhere; private endpoints + private DNS (already exercised in Test); Storage and Key Vault firewalls default-deny; Defender for Storage on with sensitive-data discovery.
- **PostgreSQL:** `Standard_D4ds_v5` or larger, zone-redundant HA, PITR 35 days, geo-redundant backup, customer-managed key optional; Entra-only auth for humans, password auth for the app until MI auth is validated.
- **Redis:** `Balanced_B3`+ with zone redundancy; persistence on.
- **Isolation:** separate subscription or at least separate RG + separate Key Vault + separate identity; no shared ACR with Dev/Test (or image promotion with signed digests only).
- **Command Center failure domain:** production region ≠ any protected workload region; region pair documented; backup/restore drill proves RTO 60 min / RPO 15 min (FROZEN §3.5–8).
- **Ops:** monthly maintenance window, patch SLA 7/30/60/90, Sentinel connector on the Log Analytics workspace, privileged deploy approval per organisational release process (OPERATIONS.md Release process).
