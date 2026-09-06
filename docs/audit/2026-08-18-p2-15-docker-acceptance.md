# P2-15 Docker Profile Acceptance

Date: 2026-08-18

## Network Recovery

Docker Desktop was restarted with `docker desktop start`. The earlier failure was a direct
connection timeout from the Docker engine to Docker Hub, not an authentication failure. The
anonymous registry endpoint correctly returned HTTP 401 for an unauthenticated API request, and
anonymous image pulls then succeeded. A Docker Hub login was not required.

## Builds

The pinned `python:3.12.14-slim-bookworm` manifest was pulled and both profiles built from the
same Dockerfile:

- Base: `noyra:p2-15-base`, image index
  `sha256:a2dc94f906391da9db9ca86bf03e4c16e40e68072c9ed287fa2ebafee49f9101`.
- Cloud: `noyra:p2-15-cloud`, image index
  `sha256:06738299a63e1e3f9e70fba6a887cb4827ae9f642f758489d7e15981bcb20a31`.

Both builds completed with hash-locked installation. The base image imported `noyra`; the cloud
image imported `boto3==1.43.72` successfully. The focused profile contract suite passed (`2
passed`), and the previously recorded Ubuntu base/cloud installer and fail-closed dependency
checks remain green.

P2-15 is therefore `verified` in the acceptance matrix.
