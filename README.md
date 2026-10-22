# platform-gitops

Owner: Platform/release.

Desired deployment state for dev, QA and production, including application releases and platform add-ons. The first PR adds the service chart and capacity profiles, not live Argo CD Applications.

Project guide: [DOC.md](https://github.com/Narx-Space-Prod/platform-infrastructure/blob/main/DOC.md).
The guide is introduced in the infrastructure foundation PR and becomes available on main after review.

## Current state

Foundation only. Application code, cloud infrastructure and end-to-end delivery are not deployed.
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
