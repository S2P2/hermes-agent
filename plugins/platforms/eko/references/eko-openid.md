# Eko OpenID Reference

Local snapshot of the stable Eko OpenID / SSO docs used by the Hermes Eko platform.

## Scope

This reference covers Eko SSO via OpenID Connect 1.0.

## What it is

- Lets users sign in to third-party web apps with their Eko account.
- Follows the OpenID Connect 1.0 flow.
- Supports SDKs for PHP and JavaScript.
- Also documents a direct API flow for non-SDK integrations.

## Flow

1. Redirect the user to Eko for authentication.
2. Receive an authorization code.
3. Exchange the code for tokens.
4. Optionally fetch the user profile with the access token.

## Key concepts

- `response_type=code`
- `client_id`
- `client_secret`
- `redirect_uri` must exactly match registration
- `state` should be used for CSRF protection
- `scope` can be `openid` or `openid profile`

## Token exchange

- The token request uses HTTP Basic auth.
- The Basic credential is `base64(client_id:client_secret)`.
- The returned payload includes:
  - `access_token`
  - `token_type`
  - `expires_in`
  - `refresh_token`
  - `scope`
  - `id_token`

## ID token notes

- The ID token is a JWT signed with the client secret.
- Validate:
  - `iss`
  - `aud`
  - `exp`
  - `iat`

## Optional user profile

- Use the access token to query user info.
- Returned profile fields may include:
  - `_id`
  - `username`
  - `firstname`
  - `lastname`
  - `email`
  - `avatar`

## Source pages

- Getting Started: https://eko.gitbook.io/api/eko-openid/getting-started
- OpenID Flow: https://eko.gitbook.io/api/eko-openid/untitled-1
