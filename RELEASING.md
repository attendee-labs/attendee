# Fork releases

Release Please creates service-pack releases from Conventional Commit titles:

```text
upstream 1.79.4 -> v1.79.4-sp.1 -> v1.79.4-sp.2
```

Merging its release PR creates the GitHub release and publishes the matching
image tag to:

```text
607374883620.dkr.ecr.us-west-2.amazonaws.com/tricorder/attendee
```

GitHub Actions must have a `CI_CD_IAM_ROLE` secret containing an OIDC role ARN
that can push to that ECR repository.

The workflow reads the upstream version from `version.json` and finds the next
available `sp.N` Git tag automatically. After syncing a newer upstream release,
the next fork release restarts at `sp.1` without manual version maintenance.
