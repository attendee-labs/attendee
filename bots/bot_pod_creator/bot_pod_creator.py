import json
import logging
import os
import uuid
from typing import Dict, Optional

import jsonpatch
from django.conf import settings
from kubernetes import client, config

from .bot_pod_spec import BotPodSpecType

logger = logging.getLogger(__name__)

# fmt: off

def apply_json6902_patch(json_to_patch: dict, patch_str: str) -> dict:
    """
    Apply a JSON6902 (RFC 6902) patch to a JSON object.

    Args:
        json_to_patch: The JSON object to patch
        patch_str: The JSON6902 patch string
    """
    if not patch_str:
        return json_to_patch

    try:
        patch_ops = json.loads(patch_str)
    except json.JSONDecodeError as e:
        logger.error("patch_str is not valid JSON: %s", e)
        return json_to_patch

    if not isinstance(patch_ops, list):
        logger.error(
            "patch_str must be a JSON array of JSON6902 operations; got %r",
            type(patch_ops),
        )
        return json_to_patch

    try:
        patch = jsonpatch.JsonPatch(patch_ops)
        patched = patch.apply(json_to_patch, in_place=False)
        return patched
    except Exception as e:
        logger.error("Failed to apply patch: %s", e)
        return json_to_patch

class BotPodCreator:
    def __init__(self):
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        
        self.v1 = client.CoreV1Api()
        self.api_client = client.ApiClient()
        self.namespace = settings.BOT_POD_NAMESPACE
        self.webpage_streamer_namespace = settings.WEBPAGE_STREAMER_POD_NAMESPACE
        
        # Get configuration from environment variables
        self.app_name = os.getenv('CUBER_APP_NAME', 'attendee')
        self.app_version = os.getenv('CUBER_RELEASE_VERSION')
        
        if not self.app_version:
            raise ValueError("CUBER_RELEASE_VERSION environment variable is required")
            
        # Parse instance from version (matches your pattern of {hash}-{timestamp})
        self.app_instance = f"{self.app_name}-{self.app_version.split('-')[-1]}"
        default_pod_image = f"nduncan{self.app_name}/{self.app_name}"
        self.image = f"{os.getenv('BOT_POD_IMAGE', default_pod_image)}:{self.app_version}"

    def get_bot_pod_volumes(self):
        """
        Use a generic ephemeral volume backed by PD so we can exceed
        the 10Gi Autopilot local ephemeral-storage cap.

        BOT_PERSISTENT_STORAGE_SIZE: e.g. "50Gi"
        """
        if not self.add_persistent_storage:
            return None
        
        size = os.getenv("BOT_PERSISTENT_STORAGE_SIZE", "50Gi")

        pvc_spec = client.V1PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"],
            resources=client.V1ResourceRequirements(
                requests={"storage": size}
            ),
        )

        pvc_template = client.V1PersistentVolumeClaimTemplate(
            metadata=client.V1ObjectMeta(
                labels={"app": self.app_name},
            ),
            spec=pvc_spec,
        )

        return [client.V1Volume(
            name="bot-persistent-storage",
            ephemeral=client.V1EphemeralVolumeSource(
                volume_claim_template=pvc_template
            ),
        ),]

    def get_bot_container_volume_mounts(self):
        if not self.add_persistent_storage:
            return None
        return [
            client.V1VolumeMount(name="bot-persistent-storage", mount_path="/bot-persistent-storage"),
        ]

    def get_bot_pod_security_context(self):
        if not self.add_persistent_storage:
            return None
        return client.V1PodSecurityContext(
            fs_group=1000,
        )

    def get_bot_container_security_context(self):

        # It's annoying but if we want chrome sandboxing, we need to use Unconfined seccomp profile 
        # because chrome with sandboxing needs some syscalls that are not allowed by the default profile
        if os.getenv("ENABLE_CHROME_SANDBOX", "false").lower() == "true":
            seccomp_profile = client.V1SeccompProfile(type="Unconfined")
        else:
            seccomp_profile = client.V1SeccompProfile(type="RuntimeDefault")

        return client.V1SecurityContext(
                            run_as_non_root=True,
                            run_as_user=1000,
                            run_as_group=1000,
                            allow_privilege_escalation=False,
                            capabilities=client.V1Capabilities(drop=["ALL"]),
                            seccomp_profile=seccomp_profile,
                            #read_only_root_filesystem=True,
                        )
    def get_webpage_streamer_container_security_context(self):
        return client.V1SecurityContext(
                            run_as_non_root=True,
                            run_as_user=1000,
                            run_as_group=1000,
                            allow_privilege_escalation=False,
                            capabilities=client.V1Capabilities(drop=["ALL"]),
                            seccomp_profile=client.V1SeccompProfile(type="Unconfined"),
                            read_only_root_filesystem=True,
                        )

    def get_webpage_streamer_volumes(self):
        # Writable /tmp (node-backed by default). Align size with your ephemeral-storage limit.
        tmp_size = os.getenv("WEBPAGE_STREAMER_TMP_SIZE_LIMIT", "1024Mi")
        tmp_medium = os.getenv("WEBPAGE_STREAMER_TMP_MEDIUM", "")  # "" or "Memory" for tmpfs
        tmp = client.V1Volume(
            name="tmp",
            empty_dir=client.V1EmptyDirVolumeSource(
                medium=tmp_medium if tmp_medium else None,
                size_limit=tmp_size
            )
        )

        # Optional: larger shared memory for Chromium (strongly recommended)
        shm_size = os.getenv("WEBPAGE_STREAMER_SHM_SIZE_LIMIT", "1024Mi")
        dshm = client.V1Volume(
            name="dshm",
            empty_dir=client.V1EmptyDirVolumeSource(
                medium="Memory",
                size_limit=shm_size
            )
        )

        # Allow writing to /home directory
        home = client.V1Volume(name="home", empty_dir=client.V1EmptyDirVolumeSource(size_limit="1024Mi"))
        return [tmp, dshm, home]

    def get_webpage_streamer_volume_mounts(self):
        return [
            client.V1VolumeMount(name="tmp", mount_path="/tmp"),
            client.V1VolumeMount(name="dshm", mount_path="/dev/shm"),
            client.V1VolumeMount(name="home", mount_path="/home/app"),
        ]

    def get_webpage_streamer_container(self):
        args = ["python", "bots/webpage_streamer/run_webpage_streamer.py", "--video-frame-size", os.getenv("WEBPAGE_STREAMER_VIDEO_FRAME_SIZE", "1280x720")]
        return client.V1Container(
                name="webpage-streamer",
                image=self.image,
                image_pull_policy="Always",
                args=args,
                resources=client.V1ResourceRequirements(
                    requests={
                        "cpu": os.getenv("WEBPAGE_STREAMING_CPU_REQUEST", "1"),
                        "memory": os.getenv("WEBPAGE_STREAMING_MEMORY_REQUEST", "4Gi"),
                        "ephemeral-storage": os.getenv("WEBPAGE_STREAMING_EPHEMERAL_STORAGE_REQUEST", "0.5Gi")
                    },
                    limits={
                        "memory": os.getenv("WEBPAGE_STREAMING_MEMORY_LIMIT", "4Gi"),
                        "ephemeral-storage": os.getenv("WEBPAGE_STREAMING_EPHEMERAL_STORAGE_LIMIT", "0.5Gi")
                    }
                ),
                env=[
                    client.V1EnvVar(name="ENABLE_CHROME_SANDBOX_FOR_WEBPAGE_STREAMER", value=os.getenv("ENABLE_CHROME_SANDBOX_FOR_WEBPAGE_STREAMER", "true")),
                ],
                security_context = self.get_webpage_streamer_container_security_context(),
                volume_mounts=self.get_webpage_streamer_volume_mounts()
            )  

    def get_bot_container(self):
        cpu_request = self.bot_cpu_request or os.getenv("BOT_CPU_REQUEST", "4")
        memory_request = os.getenv("BOT_MEMORY_REQUEST", "4Gi")
        memory_limit = os.getenv("BOT_MEMORY_LIMIT", "4Gi")
        ephemeral_storage_request = os.getenv("BOT_EPHEMERAL_STORAGE_REQUEST", "10Gi")

        args = ["python", "manage.py", "run_bot", "--botid", str(self.bot_id)]

        return client.V1Container(
                        name="bot-proc",
                        image=self.image,
                        image_pull_policy="Always",
                        args=args,
                        resources=client.V1ResourceRequirements(
                            requests={
                                "cpu": cpu_request,
                                "memory": memory_request,
                                "ephemeral-storage": ephemeral_storage_request
                            },
                            limits={
                                "memory": memory_limit,
                                "ephemeral-storage": ephemeral_storage_request
                            }
                        ),
                        env_from=[
                            # environment variables for the bot, pull from the same secrets the webserver can access
                            client.V1EnvFromSource(
                                config_map_ref=client.V1ConfigMapEnvSource(
                                    name=os.getenv("BOT_POD_CONFIG_MAP_NAME", "env")
                                )
                            ),
                            client.V1EnvFromSource(
                                secret_ref=client.V1SecretEnvSource(
                                    name=os.getenv("BOT_POD_SECRETS_NAME", "app-secrets")
                                )
                            )
                        ],
                        env=[],
                        security_context = self.get_bot_container_security_context(),
                        volume_mounts=self.get_bot_container_volume_mounts(),
                    )

    def get_pod_tolerations(self):
        return [
                    client.V1Toleration(
                        key="node.kubernetes.io/not-ready",
                        operator="Exists",
                        effect="NoExecute",
                        toleration_seconds=900  # Tolerate not-ready nodes for 15 minutes
                    ),
                    client.V1Toleration(
                        key="node.kubernetes.io/unreachable",
                        operator="Exists",
                        effect="NoExecute",
                        toleration_seconds=900  # Tolerate unreachable nodes for 15 minutes
                    )
                ]

    def get_pod_image_pull_secrets(self):
        if os.getenv("DISABLE_BOT_POD_IMAGE_PULL_SECRET", "false").lower() == "true":
            return []
        
        return [
            client.V1LocalObjectReference(
                name=os.getenv("BOT_POD_IMAGE_PULL_SECRET_NAME", "regcred")
            )
        ]

    def apply_spec_to_bot_pod(self, bot_pod: client.V1Pod) -> dict:
        bot_pod_spec_data = self.api_client.sanitize_for_serialization(bot_pod)
        return apply_json6902_patch(bot_pod_spec_data, self.bot_pod_spec)

    def create_bot_pod(
        self,
        bot_id: int,
        bot_name: Optional[str] = None,
        bot_cpu_request: Optional[int] = None,
        add_webpage_streamer: Optional[bool] = False,
        add_persistent_storage: Optional[bool] = False,
        bot_pod_spec_type: Optional[BotPodSpecType] = BotPodSpecType.DEFAULT,
    ) -> Dict:
        """
        Create a bot pod with configuration from environment.
        
        Args:
            bot_id: Integer ID of the bot to run
            bot_name: Optional name for the bot (will generate if not provided)
        """
        if bot_name is None:
            bot_name = f"bot-{bot_id}-{uuid.uuid4().hex[:8]}"

        self.bot_id = bot_id
        self.bot_cpu_request = bot_cpu_request
        self.add_persistent_storage = add_persistent_storage

        # Out of caution ensure bot_pod_spec_type is purely alphabetical and all uppercase
        if not bot_pod_spec_type.isalpha() or not bot_pod_spec_type.isupper():
            raise ValueError(f"bot_pod_spec_type must be purely alphabetical and all uppercase: {bot_pod_spec_type}")
        # Fetch bot pod spec from environment variable, falling back to default if not defined
        self.bot_pod_spec = os.getenv(f"BOT_POD_SPEC_{bot_pod_spec_type}") or os.getenv(f"BOT_POD_SPEC_{BotPodSpecType.DEFAULT}")

        # Metadata labels matching the deployment
        bot_pod_labels = {
            "app.kubernetes.io/name": self.app_name,
            "app.kubernetes.io/instance": self.app_instance,
            "app.kubernetes.io/version": self.app_version,
            "app.kubernetes.io/managed-by": "cuber",
            "app.kubernetes.io/component": "bot-proc",
            "app": "bot-proc",
        }
        if add_webpage_streamer:
            bot_pod_labels["network-role"] = "attendee-webpage-streamer-receiver"

        annotations = {}
        
        # Currently, experimenting with this flag to see if it helps with bot pod evictions
        # It makes the pod take longer to be provisioned, so not enabling by default.
        if os.getenv("USE_GKE_EXTENDED_DURATION_FOR_BOT_PODS", "false").lower() == "true":
            annotations["cluster-autoscaler.kubernetes.io/safe-to-evict"] = "false"

        if os.getenv("USING_KARPENTER", "false").lower() == "true":
            annotations["karpenter.sh/do-not-disrupt"] = "true"
            annotations["karpenter.sh/do-not-evict"] = "true"

        bot_pod = client.V1Pod(
            metadata=client.V1ObjectMeta(
                name=bot_name,
                namespace=self.namespace,
                labels=bot_pod_labels,
                annotations=annotations
            ),
            spec=client.V1PodSpec(
                containers=[self.get_bot_container()],
                security_context=self.get_bot_pod_security_context(),
                service_account_name=os.getenv("BOT_POD_SERVICE_ACCOUNT_NAME", "default"),
                restart_policy="Never",
                image_pull_secrets=self.get_pod_image_pull_secrets(),
                termination_grace_period_seconds=60,
                tolerations= self.get_pod_tolerations(),
                volumes=self.get_bot_pod_volumes(),
            )
        )

        bot_pod_spec_data = self.apply_spec_to_bot_pod(bot_pod)

        if add_webpage_streamer:
            # Create specific labels for the webpage streamer pod
            webpage_streamer_labels = {
                "app": "webpage-streamer",
                "bot-id": bot_name
            }
            
            webpage_streamer_pod = client.V1Pod(
                metadata=client.V1ObjectMeta(
                    name=f"{bot_name}-webpage-streamer",
                    namespace=self.webpage_streamer_namespace,
                    labels=webpage_streamer_labels,
                    annotations=annotations
                ),
                spec=client.V1PodSpec(
                    containers=[self.get_webpage_streamer_container()],
                    service_account_name=os.getenv("WEBPAGE_STREAMER_POD_SERVICE_ACCOUNT_NAME", "default"),
                    restart_policy="Never",
                    image_pull_secrets=self.get_pod_image_pull_secrets(),
                    termination_grace_period_seconds=60,
                    tolerations=self.get_pod_tolerations(),
                    volumes=self.get_webpage_streamer_volumes(),
                )
            )

        try:
            bot_pod_api_response = self.v1.create_namespaced_pod(
                namespace=self.namespace,
                body=bot_pod_spec_data
            )

            if add_webpage_streamer:
                webpage_streamer_pod_api_response = self.v1.create_namespaced_pod(
                    namespace=self.webpage_streamer_namespace,
                    body=webpage_streamer_pod
                )
                logger.info(f"Webpage streamer pod created: {webpage_streamer_pod_api_response}")
            
                # This is used so that when the streamer pod is deleted, the service is also deleted
                owner_ref = client.V1OwnerReference(
                    api_version="v1",
                    kind="Pod",
                    name=webpage_streamer_pod_api_response.metadata.name,
                    uid=webpage_streamer_pod_api_response.metadata.uid,
                    controller=False,
                    block_owner_deletion=False
                )
                # This service is used so that the bot pod can make requests to the streamer pod
                webpage_streamer_pod_service = client.V1Service(
                    metadata=client.V1ObjectMeta(
                        name=f"{webpage_streamer_pod_api_response.metadata.name}-service",
                        namespace=self.webpage_streamer_namespace,
                        owner_references=[owner_ref],
                    ),
                    spec=client.V1ServiceSpec(
                        # Selector is used to connect the service to the streaming pod
                        selector={"app": "webpage-streamer", "bot-id": bot_name},
                        ports=[client.V1ServicePort(name="http", port=8000, target_port=8000)],
                        type="ClusterIP",
                    ),
                )
                self.v1.create_namespaced_service(self.webpage_streamer_namespace, webpage_streamer_pod_service)

            return {
                "name": bot_pod_api_response.metadata.name,
                "status": bot_pod_api_response.status.phase,
                "created": True,
                "image": self.image,
                "app_instance": self.app_instance,
                "app_version": self.app_version
            }
            
        except client.ApiException as e:
            return {
                "name": bot_name,
                "status": "Error",
                "created": False,
                "error": str(e)
            }

    def delete_bot_pod(self, pod_name: str) -> Dict:
        try:
            self.v1.delete_namespaced_pod(
                name=pod_name,
                namespace=self.namespace,
                grace_period_seconds=60
            )
            return {"deleted": True}
        except client.ApiException as e:
            return {
                "deleted": False,
                "error": str(e)
            }

# fmt: on
