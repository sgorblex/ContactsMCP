#!/usr/bin/env bash
# Build the image and push :<version>, plus :production for a final release (a pre-release such
# as 1.2.0-rc.1 gets its version tag only). Called by .woodpecker/release.yaml: a script rather
# than inline commands, because Woodpecker substitutes ${...} in pipeline files itself.
set -euo pipefail

: "${REGISTRY:?REGISTRY must be set (registry host)}"
: "${REGISTRY_IMAGE:?REGISTRY_IMAGE must be set (image path in the registry)}"
: "${CI_COMMIT_TAG:?CI_COMMIT_TAG must be set (v<version>)}"
: "${IMAGE_PLATFORM:?IMAGE_PLATFORM must be set (e.g. linux/arm64, as the server runs)}"
version="${CI_COMMIT_TAG#v}"
revision="${CI_COMMIT_SHA:-$(git rev-parse HEAD)}"
image="$REGISTRY/$REGISTRY_IMAGE"

tags=(-t "$image:$version")
if [[ "$version" != *-* ]]; then
  tags+=(-t "$image:production")
fi

# --load: the CLI builds through buildx's container driver, which otherwise keeps the result in
# its cache and leaves nothing for docker push. --platform: without it the image gets the agent's
# architecture, which need not be the server's. An agent that cannot build for it fails here,
# before anything is pushed.
docker build --load --platform "$IMAGE_PLATFORM" -f Containerfile \
  --label org.opencontainers.image.version="$version" \
  --label org.opencontainers.image.revision="$revision" \
  "${tags[@]}" .

docker push "$image:$version"
if [[ "$version" != *-* ]]; then
  docker push "$image:production"
fi
