# Changelog

All notable changes to this project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

<!-- version list -->

## v1.3.0 (2026-03-31)

### Chores

- **deps**: Bump github/codeql-action from 4.33.0 to 4.34.1 in the github-actions group
  ([#33](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/33),
  [`f610cc5`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/f610cc5b7a3fd8816cdbc97edcf682abe0e6f396))

### Features

- Remove module path plus add YAML export option
  ([#34](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/34),
  [`66fbf7b`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/66fbf7b5ddb9f97b4c3ec2ba8f54d2e6800f132e))


## v1.2.3 (2026-03-21)

### Bug Fixes

- Harden engine, views, and signals from code review
  ([#32](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/32),
  [`1dfe0fc`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/1dfe0fc63f857b57ec9a272d4b3a4fa3f57234d7))

### Chores

- **deps**: Bump the github-actions group with 3 updates
  ([#31](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/31),
  [`157c3d5`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/157c3d50253fa15943749c758fac3289f409627d))

- **deps**: Bump the github-actions group with 3 updates
  ([#29](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/29),
  [`5962490`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/5962490147391056e7e3f10f7ff02307577c7d1e))

- **deps-dev**: Update django requirement from <6.0,>=5.1 to >=5.1,<7.0
  ([#30](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/30),
  [`65496da`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/65496dad3e7b12765e9952f7b757ea42c50289dd))


## v1.2.2 (2026-03-12)

### Bug Fixes

- Added pyproject updates / ruff updates
  ([#28](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/28),
  [`cb526ca`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/cb526ca42c917a59c8503af8550a08eb2e412499))

### Chores

- Update dependabot
  ([`6fba7f2`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/6fba7f2ab227da5d669a3bd384141fe8f7d9aaa0))

- Update pyproject/__init__.py
  ([`5c78985`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/5c7898582f8e542979b6f820f611df72504ba606))

- **deps**: Bump actions/upload-artifact from 6.0.0 to 7.0.0
  ([#22](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/22),
  [`386a38d`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/386a38ddf352f2308ab5f3a53d5419ab15142f98))

- **deps**: Bump amannn/action-semantic-pull-request from 5.5.3 to 6.1.1
  ([#23](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/23),
  [`56ee2e4`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/56ee2e4d41361f37e93dc0a6c5cc2dc9c834a82d))

- **deps**: Bump the github-actions group with 4 updates
  ([#27](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/27),
  [`022ca6d`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/022ca6d4b947f4af7cbc41056264f8ca92fcdb35))

### Testing

- Add targeted coverage tests to reach 97%
  ([#21](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/21),
  [`5ca1ce6`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/5ca1ce6b79371df877f78b5f70050600c2a7e6f2))


## v1.2.1 (2026-03-01)

### Bug Fixes

- Exempt standalone E2E/data-loading scripts and forced bump
  ([#20](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/20),
  [`b0ecd29`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/b0ecd291c5b3125fa719b440d67d7199687fcdf3))

### Continuous Integration

- Add Codecov PR comments and conventional commit PR title check
  ([#19](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/19),
  [`ccb6e38`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/ccb6e3822fd19df6aebceca1d69f5a4497273594))


## v1.2.0 (2026-03-01)

### Documentation

- Add DeepWiki link ([#16](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/16),
  [`7a8df9e`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/7a8df9e702b703aa510d1c6caee37018066ead50))

### Features

- Add VC support ([#18](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/18),
  [`922391d`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/922391def56f93f9be81d76f9a28b9a8d624b96b))


## v1.1.2 (2026-02-24)

### Bug Fixes

- **ci**: Use PAT for semantic-release, decouple publish workflow
  ([`3197110`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/31971104e6c004f2d635ceb81195a8a7c15cf7ae))

- **docs**: Pin mkdocs <2 to avoid Material incompatibility
  ([`971900b`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/971900b4f17f5673c948b6c2445e1a9f5c887a0a))


## v1.1.1 (2026-02-24)

### Bug Fixes

- **ci**: Fetch tags explicitly after checkout
  ([`b89dfa0`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/b89dfa022010b25456bfd1ddd9fb89c073de1a02))

### Chores

- **deps**: Bump actions/upload-artifact from 4.6.2 to 6.0.0
  ([`5f890b5`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/5f890b533ccb5c43949926b7de1b5b29acbfdd8e))


## v1.0.0 (2026-02-24)

- Initial Release

## v1.0.0 (2026-02-20)

- Initial Release
