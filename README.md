# platform-gitops

Owner: Platform/release.

Desired deployment state for dev, QA and production, including application releases and platform add-ons. A shared service chart and commerce umbrella chart provide digest-only deployment. Argo Applications and release automation are prepared; registration and AWS publishing remain disabled.

Project guide: [DOC.md](https://github.com/Narx-Space-Prod/platform-infrastructure/blob/main/DOC.md).

## Current state

The shared chart and capacity profiles are merged. `local/compose.yaml` now provides
a developer preview of the migrated application with PostgreSQL, Redis and an explicit
local payment simulator, persistent ElasticMQ queue/DLQ, outbox publisher and receipt
worker. The background roles reuse the orders image; there are no extra image builds.
It is not the Kubernetes dev/QA deployment or an AWS environment.
All changes after repository initialization go through feature branches and pull requests.
These repositories are public on GitHub Free. Protected `main` requires a CODEOWNER
approval, passing CI checks and resolved conversations; administrators follow the same rules.

## Chart contract

`charts/commerce-service` provides digest-only images, probes, requests/limits, non-root
containers, rolling updates, optional CPU HPA and an optional PodDisruptionBudget.
Applications must implement its runtime/health contract before deploying through it.
The production profile needs metrics-server. HPA and PDB do not provide AZ resilience
or add worker-node capacity automatically.

`environments/*/values.yaml` contains capacity settings only. No deployable image references
are present. Validation injects a dummy image into rendered output; it is never committed as a release.
`environments/*/runtime.yaml` configures the complete stack; `release.json` is the
canonical image bundle, initially non-deployable. `applications/local/commerce.json`
registers dev/QA only after operator bootstrap; `applications/aws/commerce.json` targets
the future registered EKS cluster. `applications/notifications.json` is a data-only
merge patch, not a standalone manifest; use the infrastructure helper to install it.

The reconciler verifies signed main-build records before proposing release/promotion
PRs. The trusted candidate verifier never executes PR code. Dev, QA and production
all retain human review. Failed QA/production rollouts can propose reviewed rollback
PRs; persistent data and SQL migrations are not rolled back. See DOC.md for exact App,
OIDC, credential-refresh and activation steps. No new Argo Applications are live yet.

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
NARX_LOCAL_TEST=1 NARX_COMPOSE_PROJECT=narx-commerce python3 -m unittest discover -s tests -v
```

It checks authentication, admin restrictions, INR prices, cart/stock, idempotent checkout,
payment verification, customer order isolation, product concurrency checks, order status,
refund retries and sales reporting. Run it only against this disposable preview.

For the separate queue failure exercise, follow DOC.md. Set `NARX_QUEUE_TEST=1` only
on a fresh disposable project: it stops/restarts that project's queue/worker and
injects database failures to verify transaction rollback, retries, duplicates and DLQ
recovery. Regular customer-flow tests do not stop services or inject these constraints.
Real AWS queue policies, IAM, monitoring alarms and production redrive remain pending.
