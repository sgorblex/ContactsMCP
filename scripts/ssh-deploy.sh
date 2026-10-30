#!/usr/bin/env bash
# Pull :production on the server and restart the unit, over SSH. Called by
# .woodpecker/release.yaml after release-push.sh. The remote commands are fixed, so a restricted
# deploy user can be allowed exactly these three in sudoers (README, "Releasing to your own
# registry"). A pre-release moves no :production tag, so there is nothing to deploy.
set -euo pipefail

: "${CI_COMMIT_TAG:?CI_COMMIT_TAG must be set (v<version>)}"
if [[ "${CI_COMMIT_TAG#v}" == *-* ]]; then
  echo "ssh-deploy: $CI_COMMIT_TAG is a pre-release, not deploying"
  exit 0
fi

: "${REGISTRY:?REGISTRY must be set}"
: "${REGISTRY_USER:?REGISTRY_USER must be set}"
: "${REGISTRY_PASSWORD:?REGISTRY_PASSWORD must be set}"
: "${REGISTRY_IMAGE:?REGISTRY_IMAGE must be set}"
: "${DEPLOY_HOST:?DEPLOY_HOST must be set (user@host)}"
: "${DEPLOY_SSH_KEY:?DEPLOY_SSH_KEY must be set (private key contents)}"
: "${DEPLOY_KNOWN_HOSTS:?DEPLOY_KNOWN_HOSTS must be set (the host key line)}"
: "${DEPLOY_UNIT:?DEPLOY_UNIT must be set (systemd unit to restart)}"

keyfile="$(mktemp)"
knownhosts="$(mktemp)"
trap 'rm -f "$keyfile" "$knownhosts"' EXIT

printf '%s\n' "$DEPLOY_SSH_KEY" > "$keyfile"
chmod 600 "$keyfile"
printf '%s\n' "$DEPLOY_KNOWN_HOSTS" > "$knownhosts"

remote_cmd="sudo podman login '${REGISTRY}' -u '${REGISTRY_USER}' --password-stdin \
  && sudo podman pull '${REGISTRY}/${REGISTRY_IMAGE}:production' \
  && sudo systemctl restart '${DEPLOY_UNIT}' \
  && echo 'ssh-deploy: ${DEPLOY_UNIT} restarted'"

# The registry password goes over stdin, never on a command line.
ssh -i "$keyfile" -o UserKnownHostsFile="$knownhosts" -o StrictHostKeyChecking=yes \
  "$DEPLOY_HOST" "$remote_cmd" <<< "$REGISTRY_PASSWORD"
