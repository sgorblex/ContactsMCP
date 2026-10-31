# Changelog

## [0.2.0](https://github.com/sgorblex/ContactsMCP/compare/v0.1.0...v0.2.0) (2026-10-31)


### Features

* add a settings page to manage linked accounts from the browser ([#20](https://github.com/sgorblex/ContactsMCP/issues/20)) ([e730bf1](https://github.com/sgorblex/ContactsMCP/commit/e730bf117275e956598726c78397c2f131398820))
* link and re-link Google accounts from the settings page ([#21](https://github.com/sgorblex/ContactsMCP/issues/21)) ([f57323b](https://github.com/sgorblex/ContactsMCP/commit/f57323b725910daad3caf27976717f09facdc093))

## 0.1.0 (2026-10-30)


### Features

* add configuration, errors and JSON logging ([#2](https://github.com/sgorblex/ContactsMCP/issues/2)) ([9d44459](https://github.com/sgorblex/ContactsMCP/commit/9d444591b0c5fb2d062d011b50f3ac6bffd0871f))
* add contact models and People API normalization ([#4](https://github.com/sgorblex/ContactsMCP/issues/4)) ([71173eb](https://github.com/sgorblex/ContactsMCP/commit/71173eb7a400d779c014c554e077e86139bc824e))
* add non-destructive write and group tools ([#7](https://github.com/sgorblex/ContactsMCP/issues/7)) ([4a30149](https://github.com/sgorblex/ContactsMCP/commit/4a30149ad387b717a469b06ba24f5c8ca9356b71))
* add read tools for contacts and other contacts ([#6](https://github.com/sgorblex/ContactsMCP/issues/6)) ([d0d45a7](https://github.com/sgorblex/ContactsMCP/commit/d0d45a7b23bea9d082a06ca23be7e8964e6e4972))
* add the People API client with token refresh and backoff ([#3](https://github.com/sgorblex/ContactsMCP/issues/3)) ([f60921a](https://github.com/sgorblex/ContactsMCP/commit/f60921aec19e4e0970163309fc0451bc28fad1aa))
* link Google accounts with the auth CLI ([#5](https://github.com/sgorblex/ContactsMCP/issues/5)) ([7f1cb4e](https://github.com/sgorblex/ContactsMCP/commit/7f1cb4ebb31bfe86d015516e800887252c04fe02))
* serve the tools over stdio and over HTTP behind OIDC with a user allowlist ([#8](https://github.com/sgorblex/ContactsMCP/issues/8)) ([91b9d67](https://github.com/sgorblex/ContactsMCP/commit/91b9d6738e9cfc5700f24279f9472a3326e1847e))


### Bug Fixes

* change the type when adding an existing value with a new one ([#12](https://github.com/sgorblex/ContactsMCP/issues/12)) ([79b1ce2](https://github.com/sgorblex/ContactsMCP/commit/79b1ce22da08db9c66f2c8b656d211cdd6fa2076))
* report only real group membership changes ([#13](https://github.com/sgorblex/ContactsMCP/issues/13)) ([36b648f](https://github.com/sgorblex/ContactsMCP/commit/36b648ffca76750003f1d389bcb6fcc196acae52))


### Performance Improvements

* shrink tool schemas to cut context cost ([#14](https://github.com/sgorblex/ContactsMCP/issues/14)) ([35445e5](https://github.com/sgorblex/ContactsMCP/commit/35445e5fd234b910a78dd5e70319362ab1be8739))
