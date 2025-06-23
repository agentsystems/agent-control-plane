# Contributing to `agent-control-plane`

This document explains **how to cut a new Docker image release** for the Gateway / core services.  All user-facing docs stay in the top-level `README.md`; the steps below are for contributors and maintainers.

---

## 1 – Prerequisites

| Requirement | Why it is needed |
|-------------|-----------------|
| Docker ≥ 24 with **Buildx / `buildx bake`** | multi-architecture (`linux/amd64` + `linux/arm64`) image build |
| QEMU static emulation binaries (`docker run --privileged tonistiigi/binfmt`) | cross-build support for arm64 on amd64 hosts |
| Write access to **Docker Hub** repo `agentsystems/agent-control-plane` | push images |
| Local clone with a clean **`main`** branch | the script tags the current commit |
| Git configured with push rights (or PAT) | pushes Git tags |

---

## 2 – Versioning Rules

* **Semantic Versioning** – `MAJOR.MINOR.PATCH`.
* Git tag **must be prefixed** with `v` (e.g. `v0.4.0`).
* Docker tag is the same number **without** the `v` (e.g. `0.4.0`).
* The script also tags every release as `latest`.
* Versions must be **monotonically increasing** – the script aborts if a higher tag already exists.

---

## 3 – Standard Release (one-liner)

```bash
./build_and_release.sh --version 0.4.0 --push
```

What happens:

1. Checks that **`v0.4.0`** Git tag doesn’t exist.
2. Builds multi-arch image using Buildx.
3. Pushes both `0.4.0` and `latest` tags to Docker Hub.
4. Automatically creates and pushes Git tag **`v0.4.0`** pointing to the current commit.

> Tip – Omit `--push` to build locally without pushing / tagging.

---

## 4 – Verify the image

```bash
docker run --rm -p 8080:8080 agentsystems/agent-control-plane:0.4.0
# then open http://localhost:8080/docs
```

Check logs for `Application startup complete` and ensure the Swagger UI loads.

---

## 5 – Post-release tasks

* The **local Docker Compose / Helm deployments** track the `:latest` tag, so no manifest change is required.
* If you *pin* versions in an env (e.g. staging), update the tag there.
* Add an entry to `CHANGELOG.md`.

---

## 6 – Continuous Delivery (future)

A GitHub Action can call the same script on **tag push**:

```yaml
name: Release
on:
  push:
    tags: ["v*.*.*"]
jobs:
  release:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v2
      - name: QEMU
        uses: docker/setup-qemu-action@v2
      - name: Build & push
        run: ./build_and_release.sh --version ${GITHUB_REF#refs/tags/v} --push
        env:
          DOCKERHUB_USERNAME: ${{ secrets.DOCKERHUB_USERNAME }}
          DOCKERHUB_TOKEN: ${{ secrets.DOCKERHUB_TOKEN }}
```

---

## 7 – Local Development Loop

Need an iterative build without pushing to Hub?

```bash
./build_and_release.sh --version dev --no-cache
# or: docker build -t agent-control-plane:dev .
```

Then run `docker compose up gateway` from the deployments repo and point the service to `agent-control-plane:dev`.

---

### Why no `--git-tag` flag?

Tagging is mandatory for every pushed image, ensuring a one-to-one mapping between Docker images and source commits. This prevents “orphan” images and keeps release history clean.

Happy shipping! 🚀
