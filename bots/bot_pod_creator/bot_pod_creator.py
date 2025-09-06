import logging
import os
import uuid
from typing import Dict, Optional

from kubernetes import client, config

logger = logging.getLogger(__name__)

# fmt: off

class BotPodCreator:
    def __init__(self, namespace: str = "attendee"):
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        
        self.v1 = client.CoreV1Api()
        self.namespace = namespace
        
        # Get configuration from environment variables
        self.app_name = os.getenv('CUBER_APP_NAME', 'attendee')
        self.app_version = os.getenv('CUBER_RELEASE_VERSION')
        
        if not self.app_version:
            raise ValueError("CUBER_RELEASE_VERSION environment variable is required")
            
        # Parse instance from version (matches your pattern of {hash}-{timestamp})
        self.app_instance = f"{self.app_name}-{self.app_version.split('-')[-1]}"
        default_pod_image = f"nduncan{self.app_name}/{self.app_name}"
        self.image = f"{os.getenv('BOT_POD_IMAGE', default_pod_image)}:{self.app_version}"

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
        return self.get_bot_container_security_context()

    def get_webpage_streamer_container(self):
        args = ["python", "bots/webpage_streamer/run_webpage_streamer.py"]
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
                    client.V1EnvVar(
                        name="DJANGO_SETTINGS_MODULE",
                        value=os.getenv("DJANGO_SETTINGS_MODULE")
                    ),
                    client.V1EnvVar(name="ALSA_CONFIG_PATH", value="/tmp/asoundrc"),
                ],
                security_context = self.get_webpage_streamer_container_security_context()
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
                                    name="env"
                                )
                            ),
                            client.V1EnvFromSource(
                                secret_ref=client.V1SecretEnvSource(
                                    name="app-secrets"
                                )
                            )
                        ],
                        env=[],
                        security_context = self.get_bot_container_security_context()
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

    def create_bot_pod(
        self,
        bot_id: int,
        bot_name: Optional[str] = None,
        bot_cpu_request: Optional[int] = None,
        add_webpage_streamer: Optional[bool] = False,
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

        # Metadata labels matching the deployment
        labels = {
            "app.kubernetes.io/name": self.app_name,
            "app.kubernetes.io/instance": self.app_instance,
            "app.kubernetes.io/version": self.app_version,
            "app.kubernetes.io/managed-by": "cuber",
            "app": "bot-proc"
        }

        annotations = {}
        if os.getenv("USING_KARPENTER", "false").lower() == "true":
            annotations["karpenter.sh/do-not-disrupt"] = "true"
            annotations["karpenter.sh/do-not-evict"] = "true"

        bot_pod = client.V1Pod(
            metadata=client.V1ObjectMeta(
                name=bot_name,
                namespace=self.namespace,
                labels=labels,
                annotations=annotations
            ),
            spec=client.V1PodSpec(
                containers=[self.get_bot_container()],
                service_account_name=os.getenv("BOT_POD_SERVICE_ACCOUNT_NAME", "default"),
                restart_policy="Never",
                image_pull_secrets=[
                    client.V1LocalObjectReference(
                        name="regcred"
                    )
                ],
                termination_grace_period_seconds=60,
                tolerations= self.get_pod_tolerations()
            )
        )

        if add_webpage_streamer:
            # Create specific labels for the webpage streamer pod
            webpage_streamer_labels = labels.copy()
            webpage_streamer_labels.update({
                "app": "webpage-streamer",
                "bot-id": bot_name
            })
            
            webpage_streamer_pod = client.V1Pod(
                metadata=client.V1ObjectMeta(
                    name=f"{bot_name}-webpage-streamer",
                    namespace=self.namespace,
                    labels=webpage_streamer_labels,
                    annotations=annotations
                ),
                spec=client.V1PodSpec(
                    containers=[self.get_webpage_streamer_container()],
                    service_account_name=os.getenv("WEBPAGE_STREAMER_POD_SERVICE_ACCOUNT_NAME", "default"),
                    restart_policy="Never",
                    image_pull_secrets=[
                        client.V1LocalObjectReference(
                            name="regcred"
                        )
                    ],
                    termination_grace_period_seconds=60,
                    tolerations=self.get_pod_tolerations()
                )
            )

        try:
            bot_pod_api_response = self.v1.create_namespaced_pod(
                namespace=self.namespace,
                body=bot_pod
            )

            if add_webpage_streamer:
                webpage_streamer_pod_api_response = self.v1.create_namespaced_pod(
                    namespace=self.namespace,
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
                        namespace=self.namespace,
                        owner_references=[owner_ref],
                    ),
                    spec=client.V1ServiceSpec(
                        # Selector is used to connect the service to the streaming pod
                        selector={"app": "webpage-streamer", "bot-id": bot_name},
                        ports=[client.V1ServicePort(name="http", port=8000, target_port=8000)],
                        type="ClusterIP",
                    ),
                )
                self.v1.create_namespaced_service(self.namespace, webpage_streamer_pod_service)

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
