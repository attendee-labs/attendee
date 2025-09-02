import os
import uuid
from typing import Dict, Optional

from kubernetes import client, config

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

    def create_bot_pod(
        self,
        bot_id: int,
        bot_name: Optional[str] = None,
        bot_cpu_request: Optional[int] = None,
    ) -> Dict:
        """
        Create a bot pod with configuration from environment.
        
        Args:
            bot_id: Integer ID of the bot to run
            bot_name: Optional name for the bot (will generate if not provided)
        """
        if bot_name is None:
            bot_name = f"bot-{bot_id}-{uuid.uuid4().hex[:8]}"

        if bot_cpu_request is None:
            bot_cpu_request = os.getenv("BOT_CPU_REQUEST", "4")

        # Set the command based on bot_id
        # Run entrypoint script first, then the bot command
        bot_cmd = f"python manage.py run_bot --botid {bot_id}"
        command = ["/bin/bash", "-c", f"/opt/bin/entrypoint.sh && {bot_cmd}"]

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

        pod = client.V1Pod(
            metadata=client.V1ObjectMeta(
                name=bot_name,
                namespace=self.namespace,
                labels=labels,
                annotations=annotations
            ),
            spec=client.V1PodSpec(
                containers=[
                    client.V1Container(
                        name="bot-proc",
                        image=self.image,
                        image_pull_policy="Always",
                        command=command,
                        resources=client.V1ResourceRequirements(
                            requests={
                                "cpu": bot_cpu_request,
                                "memory": os.getenv("BOT_MEMORY_REQUEST", "4Gi"),
                                "ephemeral-storage": os.getenv("BOT_EPHEMERAL_STORAGE_REQUEST", "10Gi")
                            },
                            limits={
                                "memory": os.getenv("BOT_MEMORY_LIMIT", "4Gi"),
                                "ephemeral-storage": os.getenv("BOT_EPHEMERAL_STORAGE_LIMIT", "10Gi")
                            }
                        ),
                        env_from=[
                            # environment variables for the bot
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
                        env=[]
                    )
                ],
                restart_policy="Never",
                image_pull_secrets=[
                    client.V1LocalObjectReference(
                        name="regcred"
                    )
                ],
                termination_grace_period_seconds=60,
                # Add tolerations to allow pods to be scheduled on nodes with specific taints
                # This can help with scheduling during autoscaling events
                tolerations=[
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
            )
        )

        try:
            api_response = self.v1.create_namespaced_pod(
                namespace=self.namespace,
                body=pod
            )
            
            return {
                "name": api_response.metadata.name,
                "status": api_response.status.phase,
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
