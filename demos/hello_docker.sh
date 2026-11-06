#!/usr/bin/env bash
# Can this codespace run containers of its own?
#
#   bash demos/hello_docker.sh
#
# Downloads and runs Docker's tiny hello-world image, then deletes the container.

if docker run --rm hello-world; then
  echo "✅ Docker works."
else
  echo "❌ Docker could not run a container. Post the output above on Zulip."
  exit 1
fi
