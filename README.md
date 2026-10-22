# platform-gitops

Owner: Platform/release.

Desired deployment state for dev, QA and production, including application releases and platform add-ons. The first PR adds the service chart and capacity profiles, not live Argo CD Applications.

Project guide: [DOC.md](https://github.com/Narx-Space-Prod/platform-infrastructure/blob/main/DOC.md).

## Current state

The shared chart and capacity profiles are merged. `local/compose.yaml` now provides
a developer preview of the migrated application with PostgreSQL, Redis and an explicit
local payment simulator. It is not the Kubernetes dev/QA deployment or an AWS environment.
All changes after repository initialization go through feature branches and pull requests.
These private repositories use GitHub Free; review enforcement requires the planned Team upgrade.

## Chart contract

`charts/commerce-service` provides digest-only images, probes, requests/limits, non-root
containers, rolling updates, optional CPU HPA and an optional PodDisruptionBudget.
Applications must implement its runtime/health contract before deploying through it.
The production profile needs metrics-server. HPA and PDB do not provide AZ resilience
or add worker-node capacity automatically.

`environments/*/values.yaml` contains capacity settings only. No deployable image references
are present. Validation injects a dummy image into rendered output; it is never committed as a release.
Argo CD registration, secrets, ingress, monitoring and real release records are pending.

## Local preview and integration tests

Clone the three application repositories beside this repository, generate `local/.env`
using DOC.md, then run:

```bash
docker compose --parallel 1 --env-file local/.env -f local/compose.yaml build
docker compose --env-file local/.env -f local/compose.yaml up --no-build -d --wait
```

Open `http://localhost:18080`. Backend ports and database ports are not published.
All payments in this preview use the local simulator; no money is charged. PostgreSQL
data persists in a named volume. Carts and simulator state are disposable.

The opt-in test creates throwaway accounts and performs a local SQL role promotion:

```bash
NARX_LOCAL_TEST=1 NARX_COMPOSE_PROJECT=narx-commerce python3 -m unittest discover -s tests -v
```

It checks authentication, admin restrictions, INR prices, cart/stock, idempotent checkout,
payment verification, customer order isolation, product concurrency checks, order status,
refund retries and sales reporting. Run it only against this disposable preview.
