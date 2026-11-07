# platform-gitops

Owner: Platform/release.

Desired deployment state for dev, QA and production, including application releases and platform add-ons. A shared service chart and commerce umbrella chart provide digest-only deployment. Argo CD deploys all three environments on the dedicated Narx cluster, using signed private GHCR images.

Project guide: [DOC.md](https://github.com/Narx-Space-Prod/platform-infrastructure/blob/main/DOC.md).

## Current state

The shared chart and capacity profiles are merged. `local/compose.yaml` now provides
a developer preview of the migrated application with PostgreSQL, Redis and an explicit
local payment simulator, persistent ElasticMQ queue/DLQ, outbox publisher and receipt
worker. The background roles reuse the orders image; there are no extra image builds.
It is not the Kubernetes dev/QA deployment or an AWS environment.
All changes after repository initialization go through feature branches and pull requests.
These repositories are public on GitHub Free. Protected `main` requires a CODEOWNER
approval from the designated reviewer team for protected file paths, passing CI checks and resolved conversations; administrators follow the same rules. Only Dev/QA release JSON files are unowned, with trusted verification requiring the dedicated release App as author.

## Chart contract

`charts/commerce-service` provides digest-only images, probes, requests/limits, non-root
containers, rolling updates, optional CPU HPA and an optional PodDisruptionBudget.
Applications must implement its runtime/health contract before deploying through it.
The production profile needs metrics-server. HPA and PDB do not provide AZ resilience
or add worker-node capacity automatically.

`environments/*/values.yaml` contains capacity settings only. No deployable image references
are present. Validation injects a dummy image into rendered output; it is never committed as a release.
`environments/*/runtime.yaml` configures the complete stack; `release.json` is the
canonical immutable image bundle. `applications/local/commerce.json`
registers dev/QA/production on the dedicated `narx-local` cluster after operator bootstrap.
Argo CD runs in the dedicated Narx cluster; the old management cluster is paused. AWS production manifests are retired. `applications/notifications.json` is a data-only
merge patch, not a standalone manifest; use the infrastructure helper to install it.

The reconciler verifies signed main-build records before proposing release/promotion
PRs. The trusted candidate verifier never executes PR code. Dev and QA release PRs merge
automatically after required checks. Production requires code-owner approval and explicit
merge. Failed QA rollouts can propose verified automatic rollback PRs; Production
rollback PRs retain human review; persistent data and SQL migrations are not rolled back. See DOC.md for exact App,
OIDC, credential-refresh and activation steps. All three environments have running workloads. After this policy PR is reviewed and merged, apply `policy/review-settings.json` to the GitOps main branch review settings. The blanket count is zero, but that alone does not enforce code-owner review. The active native ruleset in `policy/review-ruleset.json` requires one approval from the reviewer team for all paths except the two Dev/QA release files. Keep this ruleset active before applying the zero-count branch setting; never rely on CODEOWNERS alone at count zero. No user or App has a ruleset bypass. Service repository review settings are unchanged.

## Local preview and integration tests

Clone the three application repositories beside this repository, generate `local/.env`
using DOC.md, then run:

```bash
docker compose --parallel 1 --env-file local/.env -f local/compose.yaml build
docker compose --env-file local/.env -f local/compose.yaml up --no-build -d --wait
```

Open `http://localhost:18080`. Backend ports and database ports are not published.
All payments in this preview use the local simulator; no money is charged. PostgreSQL
and queue data persist in named volumes. Carts and simulator state are disposable.

The opt-in test creates throwaway accounts and performs a local SQL role promotion:

```bash
NARX_LOCAL_TEST=1 NARX_COMPOSE_PROJECT=narx-commerce python3 -m unittest discover -s tests -p test_local_stack.py -v
```

It checks authentication, admin restrictions, INR prices, cart/stock, idempotent checkout,
payment verification, customer order isolation, product concurrency checks, order status,
refund retries and sales reporting. Run it only against this disposable preview.

For the separate queue failure exercise, follow DOC.md. Set `NARX_QUEUE_TEST=1` only
on a fresh disposable project: it stops/restarts that project's queue/worker and
injects database failures to verify transaction rollback, retries, duplicates and DLQ
recovery. Regular customer-flow tests do not stop services or inject these constraints.
Real AWS queue policies, IAM, monitoring alarms and production redrive remain pending.

## Required release-policy check

`narx/release-verification` is reported automatically on each PR by the trusted base
workflow. It fetches candidate release JSON as data, verifies source and environment
evidence, and never executes candidate scripts. Changes without release JSON still
receive a successful policy status after the trusted inspection. Deployable release
changes remain blocked until GHCR verification is activated. Main branch protection
requires this status once its first live PR run has been verified.

## Dedicated local cluster and shared Argo CD

The local ApplicationSet generates dev/QA/production Applications in `narx-local`,
restricted by a dedicated AppProject to this repository and three commerce namespaces.
Only Argo CD is shared with the older Boutique project. See the infrastructure guide
for kind creation, namespace bootstrap, scoped cluster registration and CRD installation.
The destination manifest is operator-applied; merging does not retarget live Applications.

Production now uses local PostgreSQL, Redis and ElasticMQ with no AWS Pod Identity.
Its frontend keeps two replicas and blue-green Rollouts; backends use the chart's
single-replica defaults for laptop capacity. HTTP/loopback preview is a lab access mode.
The retired AWS Application must not also manage `commerce-production`.

`monitoring/local-values.yaml` is retained from the earlier shared monitoring integration.
An independent Narx monitoring deployment and removal of the old Boutique integration
remain pending. Do not apply Boutique's monitoring Application as Narx bootstrap.

Real runtime activation is still blocked: publishing/verification use GHCR and remain disabled pending service caller merges, private package access and release/observer App setup. Empty
release files are not working app deployments. Existing policy and platform tests remain
part of CI; no mutable or fictional images should be committed to activate the stack.

## GHCR activation

Release facts accept only `ghcr.io/narx-space-prod/commerce-COMPONENT` with a digest.
Verification binds the signed digest, source repository/main commit, successful caller
CI and an explicitly reviewed shared builder SHA; promotion does not rebuild images.
Registry authentication uses GitHub's ephemeral token with packages:read, not AWS roles.
Private packages must grant this GitOps repository Actions read access, and the cluster
needs its own ghcr-pull secret. The infrastructure helper provisions that separately.

The shared publisher is merged at ba41b7532a626ba47e9bf7b74ba56c4a6808228a;
policy/builders.json trusts that reviewed revision. Service callers must pin it before activation. Existing ECR release facts are rejected; current states
are empty bootstraps, so no live ECR releases require migration.

Release App credentials belong in the main-only gitops-release environment. Read-only
trusted PR verification uses only its ephemeral packages:read token; it does not bind
the release-key environment or execute candidate scripts. Remove repository-level App
key copies after the reviewed workflow/caller cutover so branch workflows cannot read
them. Empty bootstrap Applications cannot report a successful deployed-image status.

Local QA and production use a namespaced CiliumNetworkPolicy to let only the Rollouts controller reach the kube-apiserver identity on TCP 443/6443. CIDR policies do not select node-hosted API endpoints under Cilium defaults. The existing CI custom-resource schema lookup is schemas/argo; ciliumnetworkpolicy.json there is extracted from the v1.20.4 Cilium CRD at https://raw.githubusercontent.com/cilium/cilium/v1.20.4/pkg/k8s/apis/cilium.io/client/crds/v2/ciliumnetworkpolicies.yaml so CI validates the policy without skipping unknown kinds.

## Automated dev and QA promotion

Source code still needs human review in its source repository. CI builds each image once, scans it and publishes signed provenance and an SBOM. The release reconciler creates digest-only dev and QA release PRs and enables native GitHub auto-merge. Required checks, strict branch freshness, trusted release verification, and conversation resolution remain enforced. QA advances only after dev has current successful rollout and smoke evidence. Every release PR requires code-owner approval; dev and QA can merge automatically after approval and checks. Production is merged explicitly.

CODEOWNERS owns every file and main requires one approval, including the most recent push. A live GitHub check showed that setting the generic approval count to zero did not enforce code-owner review, so that configuration was withdrawn. Fully unattended dev/QA promotions are not enabled under the current repository-wide review boundary. Native auto-merge waits for human review and required checks; it is not an approval bot or a bypass. Source repository protections remain unchanged.

After this change is reviewed and merged, apply its tracked review configuration and enable native auto-merge from this repository checkout:

```bash
gh api --method PATCH repos/Narx-Space-Prod/platform-gitops/branches/main/protection/required_pull_request_reviews --input policy/review-settings.json
gh repo edit Narx-Space-Prod/platform-gitops --enable-auto-merge
```

These commands change only the GitOps review settings and auto-merge option. Required checks, admin enforcement, linear history and force-push protection are retained. Verify the resulting branch protection and CODEOWNERS errors endpoint before relying on automation.

Promotion evidence follows the environment configuration history, not every unrelated GitOps commit. Reconciliation searches main history back to the latest change in shared charts, that environment directory, or the local Argo application definitions. It uses the newest deployment status within that window. Pending/failure/forged status blocks advancement; a runtime change invalidates older success. The search is bounded to 50 revisions and missing evidence fails closed.

## Bounded frontend autoscaling

The metrics API has been verified on the Narx cluster. QA frontend HPA is bounded to 1–2 replicas; production to 2–3, targeting 70% CPU request utilization. Scale-up adds at most one pod per minute, and scale-down waits five minutes before removing at most one per minute. Dev and the other services keep fixed replicas. This is a laptop capacity profile, not a production throughput claim.

HPA scales the Rollout through its scale subresource. Helm omits replicas while HPA is enabled, and the local ApplicationSet ignores only the named frontend Rollout replica field with RespectIgnoreDifferences enabled. Image and pod-template drift remain enforced. After reviewed activation, verify HPA conditions/metrics, observe a bounded QA-only load exercise, then confirm scale-down and that Argo remains Synced. The initial configuration is not evidence that this live test passed.

Probe budgets explicitly allow five seconds for health checks and five minutes for application/controller startup. This prevents brief host stalls from immediately becoming restart cascades; it does not add capacity or hide HTTP health failures. The first 500-attempt/s QA exercise scaled 1→2→1 but had failed requests and host-wide pressure/restarts, so it did not pass cleanly. The follow-up QA fixture is capped at 200 attempts/s/four concurrent requests. Reserve host headroom before repeating it. Probe changes roll affected pods, including single-replica data services; apply in a maintenance window.

Service updates use Argo sync waves: Orders API → cart → catalogue → publisher → worker → frontend, after the existing data/controller waves. Orders readiness depends on the database/queue; catalogue and frontend wait on upstream APIs. This limits concurrent Deployment surge to one role and avoids dependent previews consuming the quota needed by their upstream replacement. The frontend retains blue-green preview and active analysis. Rollout progress allows ten minutes, longer than the five-minute startup allowance. This orders GitOps changes; a laptop reboot still starts existing pods together, and host capacity must still be reserved.

Live checkpoint, 2026-11-06: the reviewed 200-attempt/s QA fixture completed 15,401 requests with zero failures; HPA scaled 1 → 2 → 1 and no application/monitoring container restart increases were observed during the exercise. Dev application clients were temporarily paused with operator approval to reserve laptop capacity; data services stayed running, and Dev was restored afterward. This verifies bounded autoscaling mechanics and that availability check, not simultaneous three-environment peak capacity or rollback recovery.


The opt-in Rollouts recovery fixture can reuse an already verified, immutable frontend artifact cached on **every** node of the selected kind cluster:

```bash
NARX_ROLLOUT_TEST=1 NARX_KUBE_CONTEXT=kind-narx-commerce \
NARX_ROLLOUT_IMAGE=ghcr.io/narx-space-prod/commerce-frontend@sha256:d6390f4248ee46d583f8b9fef273ed5f02cca5a567a8df3047d656f4f772513d \
python3 -m unittest discover -s tests -p test_rollout_local.py -v
```

The harness refuses mutable registry tags, missing cached images and an existing `narx-rollout-validation` namespace. It creates a fresh restricted namespace, uses the artifact only as a Node runtime for a synthetic service, exercises preview rejection, post-switch traffic recovery and healthy promotion, then removes the namespace. It does not modify Dev/QA/production releases or claim to verify full GitOps bundle rollback, application checkout or payment recovery. Reserve host headroom first. The local `narx-local/frontend:dev` path remains available for the original developer fixture.

Recovery mechanics checkpoint, 2026-11-06: the isolated fixture passed preview rejection with stable traffic retained, observed the bad post-switch candidate serving before automatic recovery to stable, and promoted a healthy replacement through both analyses. The run completed in 342 seconds and its namespace was confirmed deleted. Dev clients were temporarily paused for capacity; QA and production releases were unchanged. This is direct Rollouts traffic-recovery evidence, separate from end-to-end GitOps bundle rollback.

## Public storefront origins

The local profiles use `global.publicAccess.origin` and `global.publicAccess.secureCookies` for the Cloudflare HTTPS entry points: `https://narx-dev.narx.space`, `https://narx-qa.narx.space`, and `https://narx-prod.narx.space`. Those values render into commerce-settings and their checksum changes only Frontend pods, so updated origin checks and cookie flags take effect without Java service restarts. Older profiles without publicAccess retain the existing top-level values. After enabling secure cookies, use the public HTTPS URL for login and checkout. See platform-infrastructure/local/access.json and its systemd template for persistent loopback origin ports. The origin and DNS route must agree exactly for state-changing API requests.

## Event-driven promotion and recovery

Protected source CI emits release-published; a signed GitHub push webhook refreshes Argo when GitOps changes merge. The observer posts deployment evidence on the revision actually synced. Only success/failure status events from the trusted observer App and the three deployment contexts can trigger promotion; required health, signature and review gates still apply. Scheduling remains a fallback. Configure the webhook using the reviewed infrastructure bootstrap helper and permit only Argo's signed `/api/webhook` endpoint through any interactive Cloudflare Access policy. Slack uses the named webhook subscription (`.slack: ""`), with its credential kept in the existing Kubernetes Secret. The release bot waits for GitHub to expose its expected PR head and retries the exact-head auto-merge operation within a bounded window; it never accepts another head or auto-merges production.

## Delivery timing and bounded capacity

Java Orders, Publisher and Worker stay in separate sync waves. Their CPU ceilings
are one core, with the existing 448/320/320 MiB memory limits preserved. This uses
available CPU during startup rather than overlapping Java memory peaks. Cart and
Catalogue share a lightweight wave; the render tests check aggregate surge memory
against namespace quotas, including maximum frontend HPA replicas. Readiness,
startup, smoke and progressive-delivery checks remain enabled.

Image provenance and SBOM success can be reused within one reconciliation process
only for the identical digest, source/run identity and reviewed builder policy.
The cache is in memory, stores only successful checks and is discarded between
invocations. Changed sources, digests or builder policies require verification
again. No persisted verification cache or skipped environment health evidence is
introduced. Preserve old reviewed builders for existing releases and rollbacks.

## Operator pipeline entrypoints

Application repositories expose **01 - Application CI**, a single call to the
shared `platform-ci/application.yml` wrapper. Open its graph to follow source
policy → test/coverage/image scan → Sonar → signed publication → delivery handoff.
Each source repository still owns its PR checks; the shared wrapper removes
repeated orchestration rather than coupling unrelated source changes.

Open **[02 - Commerce Delivery](https://github.com/Narx-Space-Prod/platform-gitops/actions/workflows/delivery.yml)**
to follow the central release graph:

```mermaid
flowchart LR
    CI[Application CI succeeds] --> Intake[Verify signed releases / prepare Dev]
    Intake --> Dev[Dev rollout and smoke proof]
    Dev --> QA[QA promotion PR]
    QA --> QACheck[QA rollout and smoke proof]
    QACheck --> Approval[Production PR / Code-S-12 review]
    Approval --> Prod[Production rollout and smoke proof]
```

The Dev and QA jobs wait for the exact immutable bundle and successful smoke
statuses from the dedicated observer App. They do not use a fixed sleep as proof,
do not call Argo sync manually, and have no cluster credentials. Promotion jobs
reuse the existing canonical-bundle, source-CI and signed-attestation policy.
Source dispatches include their CI run ID; intake waits for completed successful
CI before selecting releases. Queued intakes coalesce and read fresh state. An
hourly fallback catches missed source events without generating five-minute
normal-delivery runs or a new delivery run for every deployment status.

The current Production PR review gate remains unchanged. The automatic graph
ends by requesting that PR; its body links back to the central delivery graph and
lists source CI runs. After review and merge, **the same Commerce Delivery
workflow** opens a Production continuation run and checks the actual deployment.
The summary distinguishes requesting approval from successful deployment. This
avoids an idle runner while waiting for human review. A single uninterrupted
run would require deliberately moving the approval to a protected GitHub
Production environment; that is a separate approval-policy choice.

**[03 - Recovery](https://github.com/Narx-Space-Prod/platform-gitops/actions/workflows/recovery.yml)**
is the manual rollback entrypoint. It also responds only to trusted failed QA or
Production deployment statuses, and rechecks the current failure before proposing
a rollback. Dev/QA PR auto-merge and Production code-owner review still apply.
Normal delivery and recovery share one reusable privileged release-stage setup;
read-only deployment waits cannot mint release tokens. An observed timeout,
failure or superseded upstream cannot authorize promotion.

Platform/library PR validation and the trusted base-code release verifier remain
supporting checks. They are distinct security responsibilities, not separate
operator deployment entrypoints. Workflow names and the CI/PR summary links are
the navigation starting points; individual reusable files are implementation
building blocks.

## Main pipeline: Commerce CI/CD

Open **Actions → Commerce CI/CD** in this repository. This is the operator entry point from application CI through Production. Each main-branch application change starts a run titled with its repository and commit.

The graph verifies the actual source unit/integration/security jobs, waits for Sonar, confirms signed publication and complete source CI, then prepares and verifies Dev, QA and Production. Source CI runs remain execution details linked from the master gate summaries; this is cross-repository orchestration, not duplicated tests or a second image build. PR checks remain in the service repository before merge.

Production still requires an independently reviewed GitOps release PR. The same master run observes its merge and trusted rollout/smoke evidence for up to one hour. A timeout is a failed observation, never permission to merge or a deployment success. Recovery and the existing Production merge observer remain available afterward. Read-only observer jobs receive no App secrets or Kubernetes credentials.

Releases are serialized using the existing `release-operations` concurrency group. GitHub retains the latest pending request; intake verifies that its source release has not been superseded. The graph fails visibly if its source release is no longer selected. A master can include other components' latest verified releases; the immutable bundle is identical across Dev, QA and Production.

Activation order: merge the master first, merge the shared CI intake next, then pin service callers to the final reviewed shared revision. Older callers continue to use Commerce Delivery until migrated. Do not call the new view active before the callers are migrated and the end-to-end run is verified.
