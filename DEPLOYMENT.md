# Deploying self-hosted Attendee

Handover document for whoever operates this. It covers two supported production shapes:

| | **Path A — Docker + Celery** | **Path B — Kubernetes** |
|---|---|---|
| Where a bot runs | inside the Celery worker container | in its own pod, one per bot |
| Setup effort | low | moderate (RBAC, manifests) |
| Bot isolation | none — bots share a container | full |
| Effect of a worker restart | **every in-progress bot is killed** | app restarts don't touch running bots |
| Scaling | vertical, one host | horizontal, cluster autoscaler |
| Recommended for | pilots, low volume, single host | anything with real meeting load |

Both paths assume **PostgreSQL, Redis and an S3-compatible bucket already exist as separate
services**. Nothing in this repo provisions them.

Attendee is a Django app. Every deployment is the *same image* run with three different
commands — web, Celery worker, scheduler — plus, on Kubernetes, the bot pods the app creates
at runtime.

---

## 1. Prerequisites

### PostgreSQL
- Version 15 or newer.
- One dedicated database and role. The app owns its schema and runs its own migrations.
- Reachable over TLS. `POSTGRES_SSL_REQUIRE=true` is the default and should stay on.
- If you front it with **PgBouncer in transaction mode**, also set
  `DISABLE_SERVER_SIDE_CURSORS=true` — server-side cursors do not survive transaction pooling.

### Redis
- Used as the Celery broker *and* result backend, plus some direct key access by the scheduler.
- **Give Attendee its own database index.** If Mawrid points at the same Redis, Attendee must
  not use Mawrid's `CACHE_DB_INDEX`. Sharing an index means Attendee's Celery and Mawrid's
  taskiq write into one keyspace and will consume each other's jobs. Attendee's own convention
  is index `5`.
- The connection string is assembled from the platform's `CACHE_DB_*` values:
  ```
  REDIS_URL=redis://<CACHE_DB_USER>:<CACHE_DB_PASSWORD>@<CACHE_DB_HOST>:<CACHE_DB_PORT>/5
  ```
  With no username, keep the leading colon: `redis://:password@host:6379/5`.
  For TLS use `rediss://` and set `REDIS_SSL_REQUIREMENTS=required`.

### Object storage (S3-compatible)
- **The bucket must already exist.** Attendee never creates it.
- Credentials need `GetObject`, `PutObject`, `DeleteObject`, `ListBucket`, and multipart upload
  permissions on that bucket. Recordings are streamed up as multipart.
- Attendee reads `AWS_*` variable names. Map them from the platform's names:

  | Platform variable | Attendee variable |
  |---|---|
  | `CLOUD_BUCKET_ACCESS_KEY_ID` | `AWS_ACCESS_KEY_ID` |
  | `CLOUD_BUCKET_SECRET_ACCESS_KEY` | `AWS_SECRET_ACCESS_KEY` |
  | `CLOUD_BUCKET_REGION` | `AWS_DEFAULT_REGION` |
  | `CLOUD_BUCKET_ENDPOINT_URL` | `AWS_ENDPOINT_URL` |
  | `CLOUD_BUCKET_NAME` | `AWS_RECORDING_STORAGE_BUCKET_NAME` |

- **`AWS_S3_ADDRESSING_STYLE=path` is required for any non-AWS endpoint** (Oracle Object
  Storage's `compat` endpoint, rustfs, MinIO, Ceph). Botocore's default folds the bucket name
  into the hostname — `https://mybucket.<namespace>.compat.objectstorage...` — which those
  endpoints do not serve. This fork defaults to `path` whenever `AWS_ENDPOINT_URL` is set; the
  env file pins it explicitly so nobody has to rediscover it. Leave `AWS_ENDPOINT_URL` empty
  and drop this variable if the bucket is genuinely on AWS S3.

### Ingress / TLS
- Terminate TLS in front of the app and **forward `X-Forwarded-Proto`**. Django's
  `SECURE_PROXY_SSL_HEADER` depends on it; without it every request looks insecure and
  `SECURE_SSL_REDIRECT` puts clients into a redirect loop.
- Allow large request bodies (512 MB is a safe starting point) and a generous read timeout.

---

## 2. Build and publish the image

The Dockerfile pins `--platform=linux/amd64`. Building on an ARM machine (Apple Silicon) works
but emulates, and takes a long time — the image installs Chrome, the Zoom SDK, OpenCV and a
build toolchain. Build on an amd64 runner where you can.

```bash
export TAG=$(date +%Y%m%d)-$(git rev-parse --short HEAD)
docker build --platform=linux/amd64 -t registry.example.com/attendee:$TAG .
docker push registry.example.com/attendee:$TAG
```

Keep `$TAG` — both paths need it, and on Kubernetes it must also be written into
`CUBER_RELEASE_VERSION`.

Generate the two permanent secrets once, from any build of the image:

```bash
docker run --rm registry.example.com/attendee:$TAG python init_env.py
```

It prints `CREDENTIALS_ENCRYPTION_KEY` and `DJANGO_SECRET_KEY`.

> **`CREDENTIALS_ENCRYPTION_KEY` is not rotatable in place.** It is the Fernet key encrypting
> every stored Zoom / Google / Teams OAuth credential. If it changes, all of them become
> undecryptable and every integration has to be reconnected by hand. Back it up outside the
> cluster.

---

## 3. Path A — Docker + Celery

Bots run inside the Celery worker. Three containers, one image, external backing services.

### 3.1 Configure

```bash
cp .env.production.example .env
```

Fill in every `REPLACE_ME`. Use the `[PATH A]` block at the bottom; leave the whole `[PATH B]`
block commented out.

**Leave `LAUNCH_BOT_METHOD` unset.** The code defaults to Celery when it is absent. Only the
literal strings `kubernetes` and `docker-compose-multi-host` are recognised — setting it to
`celery` happens to work but only because it falls through to the default, so omitting it is
the honest expression of intent.

### 3.2 Migrate, then start

```bash
docker compose -f prod.docker-compose.yaml pull
docker compose -f prod.docker-compose.yaml run --rm attendee-web python manage.py migrate --noinput
docker compose -f prod.docker-compose.yaml up -d
```

`prod.docker-compose.yaml` defines exactly three services. If you would rather run them as
bare `docker run` units or under systemd, these are the commands:

| Role | Command | Entrypoint |
|---|---|---|
| **web** | `python manage.py collectstatic --noinput && gunicorn attendee.wsgi --bind 0.0.0.0:8000 --workers 3 --timeout 120 --access-logfile -` | `/tini --` |
| **worker** | `celery -A attendee worker -l INFO --concurrency 2` | **image default** |
| **scheduler** | `python manage.py run_scheduler` | `/tini --` |

The entrypoint column matters. The image's `ENTRYPOINT` is
`["/tini","--","/usr/local/bin/entrypoint.sh"]`, and `entrypoint.sh` boots PulseAudio.

- The **worker keeps the default entrypoint** — a bot with no audio device cannot join a
  meeting.
- Web and scheduler override it with `/tini --` so tini stays PID 1 (signal forwarding, zombie
  reaping) while the audio setup is skipped.

Give the worker container **`--shm-size=2g`**. Chrome crashes on Docker's 64 MB default.

### 3.3 Sizing

Concurrency is the number of bots one worker container can host at once.

- Budget **≈ 4 vCPU and 4 GB RAM per concurrent bot** — each is a full Chrome plus PulseAudio.
- Also budget disk: a bot records to local storage before upload, ~10 GB per concurrent bot.
- The app forces `CELERY_WORKER_MAX_TASKS_PER_CHILD=1`, so the worker process is recreated
  after every task. This is a deliberate workaround for a Zoom SDK segfault, not a
  misconfiguration — it means process startup cost is paid once per bot.

So `--concurrency 2` wants roughly an 8 vCPU / 16 GB host with 32 GB of free disk.

### 3.4 Known limitations of this path

Straight from the upstream README, and the reason Kubernetes is recommended at volume:

1. **Bots are killed on restart.** Any deploy, scale, or crash of the worker container
   terminates every bot currently sitting in a meeting. There is no drain.
2. **No isolation between bots.** Bots in one container share audio devices, so audio from
   separate meetings can bleed together.
3. Any infrastructure problem with that container affects every bot on it.

Plan deploys for quiet hours, or move to Path B.

---

## 4. Path B — Kubernetes

The app talks to the Kubernetes API and creates **one pod per bot**. App restarts no longer
touch running bots, and each bot is isolated.

Manifests are in [`k8s/`](k8s/). They are plain YAML with a `kustomization.yaml` wrapper.

### 4.1 What gets deployed

| Manifest | Contents |
|---|---|
| `00-namespaces.yaml` | `attendee`, `attendee-webpage-streamer` |
| `10-rbac.yaml` | `attendee-app` ServiceAccount + Roles; `attendee-bot` SA with no permissions |
| `20-configmap-env.yaml` | ConfigMap **`env`** — all non-secret configuration |
| `21-secret-app-secrets.yaml` | Secret **`app-secrets`** — template, do not commit filled in |
| `30-deployment-web.yaml` | gunicorn, 2 replicas |
| `31-deployment-worker.yaml` | Celery workers, 2 replicas |
| `32-deployment-scheduler.yaml` | scheduler + failed-launch corrector, 1 replica each |
| `40-service.yaml` | ClusterIP `attendee-web:8000` |
| `41-ingress.yaml` | TLS ingress (nginx + cert-manager example) |
| `50-job-migrate.yaml` | migration Job, run per release |
| `60-cronjobs.yaml` | pod cleanup + stalled-bot cleanup + optional data retention |

### 4.2 Three names that are load-bearing

The app builds each bot pod in code, and the pod spec references config by name. Get these
wrong and bots fail in ways that do not point at the cause.

1. **ConfigMap must be named `env`** and **Secret must be named `app-secrets`** — or you must
   set `BOT_POD_CONFIG_MAP_NAME` / `BOT_POD_SECRETS_NAME` to match. Bot pods are created with
   `envFrom` referencing them, and the reference is *not* marked optional, so a mismatch
   leaves every bot pod in `CreateContainerConfigError`.
2. **They must live in `BOT_POD_NAMESPACE`** — the same namespace the bot pods run in. That is
   why the app and its bots share the `attendee` namespace here.
3. **`CUBER_RELEASE_VERSION` must equal the image tag.** Bot pods are launched as
   `$BOT_POD_IMAGE:$CUBER_RELEASE_VERSION`. It is required — the app raises without it — and
   Kustomize's image rewriting does *not* update ConfigMap values, so it must be bumped by
   hand alongside `newTag` on every release. If they drift, the app runs one build and its
   bots run another.

### 4.3 RBAC

The `attendee-app` ServiceAccount gets a namespaced Role, not a ClusterRole. Every verb is
exercised by code in this repo:

| Resource | Verbs | Used by |
|---|---|---|
| `pods` | `create` | `bots/bot_pod_creator/bot_pod_creator.py` |
| `pods` | `get` | `bots/k8s_utils.py`, `bots/tasks/restart_bot_pod_task.py` |
| `pods` | `list` | `clean_up_completed_bot_pods` |
| `pods` | `delete` | pod cleanup, stalled-bot cleanup, `restart_bot_pod_task` |
| `events` | `list` | `bots/k8s_utils.py` — attaches pod events to a bot's failure record |
| `services` | `create` | streamer namespace only, for the webpage-streamer Service |

Bot pods themselves never call the Kubernetes API, so they run as `attendee-bot`, which has no
bindings and `automountServiceAccountToken: false`.

### 4.4 Deploy

```bash
# 1. Registry credentials, in both namespaces bot pods can land in.
kubectl create namespace attendee
kubectl create namespace attendee-webpage-streamer
for ns in attendee attendee-webpage-streamer; do
  kubectl -n $ns create secret docker-registry regcred \
    --docker-server=registry.example.com \
    --docker-username=REPLACE_ME \
    --docker-password=REPLACE_ME
done

# 2. Edit k8s/20-configmap-env.yaml and k8s/21-secret-app-secrets.yaml.
#    Replace every REPLACE_ME, REPLACE_WITH_REGISTRY and REPLACE_WITH_IMAGE_TAG,
#    and set the real hostname in the ConfigMap, the Ingress, and the web probes.

# 3. Apply everything.
kubectl apply -k k8s/

# 4. Migrate before the rollout completes serving traffic.
kubectl -n attendee delete job attendee-migrate --ignore-not-found
kubectl -n attendee apply -f k8s/50-job-migrate.yaml
kubectl -n attendee wait --for=condition=complete job/attendee-migrate --timeout=10m

# 5. Watch it come up.
kubectl -n attendee rollout status deployment/attendee-web
kubectl -n attendee get pods
```

If you prefer generating config from an env file rather than editing YAML:

```bash
kubectl -n attendee create configmap env      --from-env-file=attendee.env
kubectl -n attendee create secret generic app-secrets --from-env-file=attendee.secrets.env
```

### 4.5 Things that are easy to get wrong

- **The scheduler must stay at `replicas: 1`.** Two schedulers double-launch every scheduled
  bot. Its Deployment uses `strategy: Recreate` so a rollout never briefly runs two.
- **The web probes send an explicit `Host` header.** `ALLOWED_HOSTS` does not contain the pod
  IP, and kubelet uses the pod IP by default, which Django answers with `400 DisallowedHost`.
  Update that header whenever you change the hostname. (A `301` from `SECURE_SSL_REDIRECT` on
  the probe is expected and healthy — kubelet treats 2xx and 3xx as success.)
- **The cleanup CronJobs are not optional.** Without them, completed bot pods accumulate
  forever, and a bot whose pod dies mid-meeting is never marked failed — it hangs in
  `launching` and its slot counts against `CONCURRENT_BOTS_LIMIT`.
- **Bot pods need room to schedule.** Each requests 4 CPU / 4 Gi / 10 Gi ephemeral storage. On
  a cluster without headroom or a fast autoscaler, bots miss the start of meetings. If you run
  Karpenter, set `USING_KARPENTER=true` so it will not disrupt a pod mid-meeting.
- **`attendee-webpage-streamer` must exist** even if you never stream webpages. The pod-cleanup
  job sweeps both namespaces every run and will otherwise log errors continuously.

---

## 5. First run (both paths)

1. Browse to `https://<SITE_DOMAIN>/`.
2. Create the first account. Two options:
   - Temporarily set `DISABLE_SIGNUP=false`, register through the UI, then set it back to
     `true` and restart the web process. With `DISABLE_EMAIL=true` the confirmation link is
     printed to the web container's log — retrieve it there.
   - Or leave signup closed and run `python manage.py createsuperuser` in a one-off container.
3. Create a project, then generate an **API key** from the project page.
4. Copy the project's **webhook secret** (shown once).
5. Health endpoints for monitoring: `GET /health/` returns 200; `GET /version/` returns the
   app version and the `CUBER_RELEASE_VERSION` it is running.

### Wiring Mawrid to it

Set these in Mawrid's own `.env`:

```
ATTENDEE_HOST=https://attendee.example.com
ATTENDEE_API_KEY=<the API key from step 3>
ATTENDEE_WEBHOOK_SECRET=<the webhook secret from step 4>
ATTENDEE_TRANSCRIPTION_SECRET=<any strong shared secret you generate>
```

`ATTENDEE_TRANSCRIPTION_SECRET` is the bearer token Attendee's Custom Async transcription
provider presents when calling Mawrid's `POST /speech/attendee-transcribe`; configure the same
value on the Attendee side in the project's transcription settings.

Mawrid also supports `ATTENDEE_WEBHOOK_CALLBACK_HOST`, which overrides only the host of the
callback URL Mawrid registers with Attendee. Leave it unset in production — it exists for local
development, where the two stacks sit on separate Docker networks.

---

## 6. Upgrades

```bash
# Build and push the new tag, then:

# Path A
docker compose -f prod.docker-compose.yaml run --rm attendee-web python manage.py migrate --noinput
docker compose -f prod.docker-compose.yaml up -d

# Path B  (bump newTag in k8s/kustomization.yaml AND CUBER_RELEASE_VERSION in the ConfigMap)
kubectl apply -k k8s/
kubectl -n attendee delete job attendee-migrate --ignore-not-found
kubectl -n attendee apply -f k8s/50-job-migrate.yaml
kubectl -n attendee wait --for=condition=complete job/attendee-migrate --timeout=10m
kubectl -n attendee rollout restart deployment/attendee-web deployment/attendee-worker
```

Migrations run before the new code serves traffic. On Path A, remember that restarting the
worker kills bots that are currently in meetings.

---

## 7. Troubleshooting

| Symptom | Cause |
|---|---|
| `SignatureDoesNotMatch` on upload | Custom S3 endpoint negotiating SigV2. This fork pins s3v4 on all S3 clients; check `AWS_ENDPOINT_URL` is set so that code path is taken. |
| `NoSuchBucket` | The bucket does not exist. Attendee never creates it. |
| Uploads fail with a DNS error naming `<bucket>.<endpoint>` | `AWS_S3_ADDRESSING_STYLE` is not `path`. |
| `400 DisallowedHost` in web logs | Hostname missing from `ALLOWED_HOSTS`, or a probe sending the pod IP as `Host`. |
| Dashboard form POSTs rejected | `CSRF_TRUSTED_ORIGINS` missing the scheme, or not set. |
| Infinite HTTPS redirect loop | The proxy is not forwarding `X-Forwarded-Proto`. |
| Bot pods stuck `CreateContainerConfigError` | ConfigMap `env` or Secret `app-secrets` missing from `BOT_POD_NAMESPACE`, or renamed without updating `BOT_POD_CONFIG_MAP_NAME` / `BOT_POD_SECRETS_NAME`. |
| Bots never launch, app logs a `CUBER_RELEASE_VERSION` error | Unset. It is required in Kubernetes mode. |
| Bot pods `ImagePullBackOff` | `regcred` missing in the bot namespace, or `BOT_POD_IMAGE`/`CUBER_RELEASE_VERSION` do not name a real tag. |
| Bots stuck in `launching` forever | The stalled-bot cleanup CronJob is not running. |
| Scheduled bots joining twice | More than one scheduler replica. |
| Bot joins but records silence | Worker container is missing the default entrypoint, so PulseAudio never started. |
| Chrome crashes inside a bot | `/dev/shm` too small — set `--shm-size=2g` (Path A). |
| Attendee and Mawrid losing each other's jobs | Both pointed at the same Redis database index. |

Useful commands:

```bash
kubectl -n attendee logs -l app.kubernetes.io/component=web --tail=200
kubectl -n attendee get pods -l app=bot-proc            # live bot pods
kubectl -n attendee describe pod <bot-pod>              # why a bot pod will not start
kubectl -n attendee logs job/attendee-migrate
```

Attendee also records infrastructure detail against each bot when
`STORE_INFRASTRUCTURE_INFORMATION_IN_BOT_EVENT_METADATA=true`, and attaches the bot's own logs
to its final event when `SAVE_BOT_LOGS_TO_DASHBOARD=true`. Both are on in the supplied config
and are usually faster than digging through cluster logs.

---

## 8. Local development

Not production, but useful for reproducing a problem: `selfhost.docker-compose.yaml` runs the
whole stack — app, worker, scheduler, Postgres, Redis — plus **rustfs** as a local
S3-compatible store, so nothing has to reach a real bucket.

```bash
cp .env.selfhost.example .env
docker compose -f selfhost.docker-compose.yaml build
docker compose -f selfhost.docker-compose.yaml run --rm attendee-app python init_env.py
# paste the two generated keys into .env, then:
docker compose -f selfhost.docker-compose.yaml up -d
docker compose -f selfhost.docker-compose.yaml exec attendee-app python manage.py migrate
```

Host ports are offset so this can run alongside Mawrid's own compose stack, which already holds
8000, 5432, 6379, 9000 and 9001:

- App — <http://localhost:8100>
- rustfs S3 API — <http://localhost:9100>
- rustfs console — <http://localhost:9101>

Point Mawrid at it with `ATTENDEE_HOST=http://host.docker.internal:8100` and
`ATTENDEE_WEBHOOK_CALLBACK_HOST=http://host.docker.internal:8000` — both stacks are on separate
Docker networks, so neither can use `localhost` to reach the other.

---

## 9. Full environment variable reference

`A` = Path A only, `B` = Path B only, `both` = required either way. The complete upstream list,
including options not used here, is in [`docs/environment-variables.md`](docs/environment-variables.md).

### Required

| Variable | Path | Notes |
|---|---|---|
| `DJANGO_SECRET_KEY` | both | From `init_env.py`. |
| `CREDENTIALS_ENCRYPTION_KEY` | both | From `init_env.py`. **Not rotatable** — encrypts stored OAuth credentials. |
| `DJANGO_SETTINGS_MODULE` | both | `attendee.settings.production` |
| `DATABASE_URL` | both | `postgresql://user:pass@host:5432/db`, password percent-encoded. |
| `REDIS_URL` | both | Must use a DB index Mawrid does not use. |
| `SITE_DOMAIN` | both | Public hostname, no scheme. |
| `ALLOWED_HOSTS` | both | Comma-separated; must include `SITE_DOMAIN`. |
| `CSRF_TRUSTED_ORIGINS` | both | Comma-separated, **with** scheme. |
| `AWS_ACCESS_KEY_ID` | both | ← `CLOUD_BUCKET_ACCESS_KEY_ID` |
| `AWS_SECRET_ACCESS_KEY` | both | ← `CLOUD_BUCKET_SECRET_ACCESS_KEY` |
| `AWS_RECORDING_STORAGE_BUCKET_NAME` | both | ← `CLOUD_BUCKET_NAME`. Must already exist. |
| `ATTENDEE_IMAGE` | A | Image tag the compose file runs. |
| `LAUNCH_BOT_METHOD` | B | `kubernetes`. **Omit entirely on Path A.** |
| `CUBER_RELEASE_VERSION` | B | Must equal the image tag. App raises without it. |
| `BOT_POD_IMAGE` | B | Registry path, no tag. |

### Storage

| Variable | Default | Notes |
|---|---|---|
| `STORAGE_PROTOCOL` | `s3` | `s3` or `azure`. |
| `AWS_DEFAULT_REGION` | `us-east-1` | ← `CLOUD_BUCKET_REGION` |
| `AWS_ENDPOINT_URL` | — | ← `CLOUD_BUCKET_ENDPOINT_URL`. Empty for real AWS. |
| `AWS_S3_ADDRESSING_STYLE` | `path` when endpoint set | Required for non-AWS endpoints. |
| `AWS_AUDIO_CHUNK_STORAGE_BUCKET_NAME` | recording bucket | Optional split bucket. |
| `AWS_BOT_DEBUG_SCREENSHOT_STORAGE_BUCKET_NAME` | recording bucket | Optional split bucket. |
| `USE_REMOTE_STORAGE_FOR_AUDIO_CHUNKS` | `false` | `true` keeps audio out of Postgres. |
| `FALLBACK_TO_DB_STORAGE_FOR_AUDIO_CHUNKS_IF_REMOTE_STORAGE_FAILS` | `false` | Safety net for the above. |

### Database and Redis

| Variable | Default | Notes |
|---|---|---|
| `POSTGRES_SSL_REQUIRE` | `true` | Leave on in production. |
| `DISABLE_SERVER_SIDE_CURSORS` | `false` | `true` behind PgBouncer transaction pooling. |
| `REDIS_SSL_REQUIREMENTS` | — | `none`/`optional`/`required`, for `rediss://`. |

### Security and access

| Variable | Default | Notes |
|---|---|---|
| `DJANGO_SSL_REQUIRE` | `true` | Needs `X-Forwarded-Proto` from the proxy. |
| `DISABLE_SIGNUP` | `false` | **Set `true`** for a private deployment. |
| `DISABLE_RATE_LIMITING` | `false` | Leave off in production. |
| `PROJECT_POST_THROTTLE_RATE` | `3000/min` | Per-project API rate limit. |
| `TIME_ZONE` | `UTC` | |

### Bots — Path A

| Variable | Default | Notes |
|---|---|---|
| `CELERY_CONCURRENCY` | `2` | Simultaneous bots per worker container. ~4 vCPU + 4 GB each. |
| `CONCURRENT_BOTS_LIMIT` | `2500` | Deployment-wide ceiling. Lower it to match capacity. |

### Bots — Path B

| Variable | Default | Notes |
|---|---|---|
| `CUBER_APP_NAME` | `attendee` | Also used in pod labels. |
| `BOT_POD_IMAGE_PULL_POLICY` | `Always` | `IfNotPresent` with immutable tags. |
| `BOT_POD_NAMESPACE` | `attendee` | Must hold the ConfigMap and Secret. |
| `WEBPAGE_STREAMER_POD_NAMESPACE` | `attendee-webpage-streamer` | Must exist. |
| `BOT_POD_SERVICE_ACCOUNT_NAME` | `default` | Set to `attendee-bot`. |
| `BOT_POD_CONFIG_MAP_NAME` | `env` | Must name a real ConfigMap in the bot namespace. |
| `BOT_POD_SECRETS_NAME` | `app-secrets` | Must name a real Secret in the bot namespace. |
| `DISABLE_BOT_POD_IMAGE_PULL_SECRET` | `false` | `true` for a public registry. |
| `BOT_POD_IMAGE_PULL_SECRET_NAME` | `regcred` | |
| `BOT_CPU_REQUEST` | `4` | |
| `BOT_MEMORY_REQUEST` / `BOT_MEMORY_LIMIT` | `4Gi` | |
| `BOT_EPHEMERAL_STORAGE_REQUEST` | `10Gi` | Also the limit. |
| `INTERNAL_SITE_DOMAIN` | — | In-cluster callback address; bypasses the ingress. |
| `USING_KARPENTER` | `false` | `true` stops mid-meeting disruption. |
| `USE_GKE_EXTENDED_DURATION_FOR_BOT_PODS` | `false` | GKE equivalent. |
| `ENABLE_CHROME_SANDBOX` | `false` | Needs an Unconfined seccomp profile. |
| `BOT_POD_SPEC_DEFAULT` / `BOT_POD_SPEC_SCHEDULED` | — | JSON6902 patch applied to the generated pod spec, for node selectors, tolerations, etc. |

### Webhooks

| Variable | Default | Notes |
|---|---|---|
| `REQUIRE_HTTPS_WEBHOOKS` | `true` | `false` only for local development. |
| `MAX_WEBHOOK_DELIVERY_ATTEMPTS` | `3` | |
| `DELIVER_WEBHOOK_VERIFY_SSL` | `true` | |
| `GLOBAL_WEBHOOK_DELIVERIES_PER_SECOND_RATE_LIMIT` | — | Unset = no limit. |
| `EXTERNAL_WEBHOOK_SITE_DOMAIN` | `SITE_DOMAIN` | Only if outside services use a different hostname. |

### Email

| Variable | Default | Notes |
|---|---|---|
| `DISABLE_EMAIL` | `false` | `true` prints to the container log. |
| `EMAIL_HOST` | `smtp.mailgun.org` | Port 587 with TLS is hardcoded. |
| `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD` | — | Required unless email is disabled. |
| `DEFAULT_FROM_EMAIL` / `SERVER_EMAIL` | `noreply@mail.attendee.dev` | Change these. |
| `ERROR_REPORTS_RECEIVER_EMAIL_ADDRESS` | — | Receives Django error mail. |

### Observability

| Variable | Default | Notes |
|---|---|---|
| `ATTENDEE_LOG_LEVEL` | `INFO` | |
| `ATTENDEE_LOG_FORMAT` | — | `json` for structured logs. |
| `SAVE_BOT_LOGS_TO_DASHBOARD` | `false` | `true` — very useful for diagnosing bots. |
| `STORE_INFRASTRUCTURE_INFORMATION_IN_BOT_EVENT_METADATA` | `true` | Records pod status/events on failures. |
| `SCHEDULER_HEARTBEAT_FILE` | — | Enables the scheduler liveness probe. |
| `SENTRY_DSN` | — | Empty disables Sentry. |
| `SENTRY_ENVIRONMENT` | `production` | |
| `SENTRY_TRACES_SAMPLE_RATE` / `SENTRY_PROFILES_SAMPLE_RATE` | `0.1` | |
| `SENTRY_SEND_PII` | `false` | Leave off — transcripts are PII. |
| `MASK_TRANSCRIPT_IN_LOGS` | `false` | `true` if transcripts must not appear in logs. |
