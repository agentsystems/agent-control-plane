# Release Process for Agent Control Plane

## Release Workflow

We use GitHub Actions for automated Docker image releases to GitHub Container Registry (GHCR).

### 1. Prepare Release

Create a release branch and bump version:
```bash
git checkout -b release/X.Y.Z
# Update version in any relevant files (e.g., docker-compose.yml, docs)
git commit -am "chore: bump version to X.Y.Z"
git push -u origin release/X.Y.Z
```

### 2. Test Build

**Manual test via GitHub Actions** (from Actions tab):
1. Go to Actions → "Build and Release to GHCR"
2. Click "Run workflow"
3. Select branch: `release/X.Y.Z`
4. Version: `X.Y.Z` (without 'v' prefix)
5. Push: `false` (dry run first)
6. Verify build succeeds

### 3. Create Pull Request

Create PR from `release/X.Y.Z` to `main`:
- Wait for CI checks to pass
- Review Dockerfile changes if any
- Verify license compliance is working

### 4. Test Release

**Push test image** (from Actions tab):
1. Run workflow again on `release/X.Y.Z`
2. Version: `X.Y.Z`
3. Push: `true`
4. This creates `ghcr.io/agentsystems/agent-control-plane:X.Y.Z`

**Test the image**:
```bash
# Pull and test
docker pull ghcr.io/agentsystems/agent-control-plane:X.Y.Z
docker run --rm ghcr.io/agentsystems/agent-control-plane:X.Y.Z --version

# Integration test
docker run -d \
  --name test-control-plane \
  -p 8080:8080 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  ghcr.io/agentsystems/agent-control-plane:X.Y.Z

# Verify it's running
curl http://localhost:8080/health
docker logs test-control-plane
docker stop test-control-plane && docker rm test-control-plane
```

### 5. Final Release

If tests pass:

1. **Merge the PR** to main

2. **Create and push version tag**:
   ```bash
   git checkout main
   git pull origin main
   git tag -a vX.Y.Z -m "Release vX.Y.Z"
   git push origin vX.Y.Z
   ```

3. **Automatic release** happens on tag push:
   - Creates `ghcr.io/agentsystems/agent-control-plane:X.Y.Z`
   - Updates `ghcr.io/agentsystems/agent-control-plane:latest`
   - Creates GitHub Release with notes

### 6. Verify Production Release

```bash
# Verify latest tag updated
docker pull ghcr.io/agentsystems/agent-control-plane:latest
docker inspect ghcr.io/agentsystems/agent-control-plane:latest | grep -i version

# Verify specific version
docker pull ghcr.io/agentsystems/agent-control-plane:X.Y.Z
```

## Version Numbering

Follow semantic versioning:
- **MAJOR.MINOR.PATCH** (e.g., 0.3.0)
- **MAJOR**: Breaking API changes
- **MINOR**: New features, backward compatible
- **PATCH**: Bug fixes, backward compatible

## Quick Release (for maintainers)

For a quick patch release:
```bash
# On main branch
git tag -a vX.Y.Z -m "Release vX.Y.Z"
git push origin vX.Y.Z
# Workflow automatically builds and pushes to GHCR
```

## Rollback

If a release has issues:
```bash
# Point latest back to previous version
docker pull ghcr.io/agentsystems/agent-control-plane:PREVIOUS_VERSION
docker tag ghcr.io/agentsystems/agent-control-plane:PREVIOUS_VERSION \
          ghcr.io/agentsystems/agent-control-plane:latest
docker push ghcr.io/agentsystems/agent-control-plane:latest
```

## Notes

- The `latest` tag only updates on stable releases (version tags)
- The `main` tag always reflects the latest commit on main branch
- All images include full license compliance in `/app/licenses/`
- Multi-platform images support both linux/amd64 and linux/arm64
