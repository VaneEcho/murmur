#!/bin/bash
set -e
git pull
docker compose up -d --build
echo "Done. $(docker compose ps murmur | tail -1)"
