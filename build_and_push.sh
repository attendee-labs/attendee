#!/bin/bash
set -e

IMAGE_NAME="gitlabregistry.priv.sewan.fr/rd/attendee-docker-imagre-registry"
IMAGE_TAG="${IMAGE_NAME}:latest"

echo "Building Docker image: $IMAGE_TAG"
export DOCKER_BUILDKIT=1
sudo -E docker build -t "$IMAGE_TAG" .

echo "Pushing Docker image to registry..."
sudo docker push "$IMAGE_TAG"

echo "Build and push completed successfully!"
