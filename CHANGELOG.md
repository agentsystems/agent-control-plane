# Changelog

All notable changes to the Agent Control Plane will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Type hints throughout the codebase for better IDE support and type safety
- Comprehensive docstrings for all functions
- `.coveragerc` configuration for test coverage reporting
- Auto-start functionality for stopped agent containers
- Proper agent naming consistency (preserving full agent names like "hello-world-agent")

### Changed
- Refactored monolithic `main.py` into modular components:
  - `database.py` - Database operations and connection pooling
  - `docker_discovery.py` - Docker container discovery and management
  - `egress.py` - Egress allowlist management
  - `exceptions.py` - Common exception patterns
  - `lifecycle.py` - Container lifecycle and idle timeout management
  - `models.py` - Pydantic data models
  - `proxy.py` - HTTP CONNECT proxy server
- Improved test coverage configuration in `pytest.ini`

### Fixed
- Agent proxy IP mapping not recognizing agents correctly
- Test import errors after modularization
- Agent naming inconsistency between configuration and runtime

### Security
- Deferred non-root user implementation to avoid breaking existing deployments

## [TBD] - Previous versions

- Version history to be documented
