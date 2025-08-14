# Release Process for Agent Control Plane

## Release Workflow

We use GitHub Actions for manual Docker image releases to GitHub Container Registry (GHCR).
This follows the same manual release pattern as the agentsystems-sdk.

### 1. Prepare Release

Create a release branch:
```bash
git checkout -b release/X.Y.Z
# Update version in any relevant files if needed (e.g., docker-compose.yml, docs)
git commit -am "chore: prepare release X.Y.Z" # only if files were changed
git push -u origin release/X.Y.Z
```

### 2. Test Build (Dry Run)

**Manual test via GitHub Actions** (from Actions tab):
1. Go to Actions → "Build and Release to GHCR"
2. Click "Run workflow"
3. Use workflow from: `Branch: release/X.Y.Z`
4. Version: `X.Y.Z` (without 'v' prefix)
5. Push to registry: `false`
6. Create git tag and GitHub release: `false`
7. Run workflow and verify build succeeds

### 3. Create Pull Request

Create PR from `release/X.Y.Z` to `main`:
- Wait for CI checks to pass
- Review changes if any

### 4. Test Push to Registry

**Push test image** (from Actions tab):
1. Go to Actions → "Build and Release to GHCR"
2. Click "Run workflow"
3. Use workflow from: `Branch: release/X.Y.Z`
4. Version: `X.Y.Z`
5. Push to registry: `true`
6. Create git tag and GitHub release: `false`
7. This creates `ghcr.io/agentsystems/agent-control-plane:X.Y.Z` (but not latest yet)

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

2. **Final release from main** (from Actions tab):
   1. Go to Actions → "Build and Release to GHCR"
   2. Click "Run workflow"
   3. Use workflow from: `Branch: main`
   4. Version: `X.Y.Z`
   5. Push to registry: `true`
   6. Create git tag and GitHub release: `true`
   7. This will:
      - Create `ghcr.io/agentsystems/agent-control-plane:X.Y.Z`
      - Update `ghcr.io/agentsystems/agent-control-plane:latest`
      - Create git tag `vX.Y.Z`
      - Create GitHub Release with notes

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

For a quick patch release directly from main:
1. Go to Actions → "Build and Release to GHCR"
2. Run workflow from `main` branch
3. Set version, push: `true`, tag_release: `true`

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

- The `latest` tag only updates when `tag_release` is set to `true`
- All releases are manual through GitHub Actions UI
- All images include full license compliance in `/app/licenses/`
- Multi-platform images support both linux/amd64 and linux/arm64
- This follows the same manual release pattern as agentsystems-sdk
