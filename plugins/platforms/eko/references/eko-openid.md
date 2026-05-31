# Eko OpenID Reference

Quick local summary of the stable Eko OpenID / SSO docs used by the Hermes Eko platform.

For full plain-markdown copies, see:

- `raw_md/sso_oidc_getting-started.md`
- `raw_md/sso_oidc_register-webapp.md`
- `raw_md/sso_oidc_openid-flow.md`

## Scope

This reference covers Eko SSO via OpenID Connect 1.0.

## What it is

- Lets users sign in to third-party web apps with their Eko account.
- Follows the OpenID Connect 1.0 authorization-code flow.
- Supports SDKs for PHP and JavaScript.
- Also documents a direct API flow for non-SDK integrations.

## Register WebApp

To use Eko as an identity provider, register the third-party app in the Eko Admin Panel:

1. Log in to the Admin Panel.
2. Open **PORTAL**.
3. Add a new webview.
4. Enter name and website URL.
5. Use an **HTTPS** website URL; non-HTTPS sites are not supported.
6. Create or select an OAuth client profile.
7. Enter app name and redirect URL.
8. The redirect URL must match the website URL / registered redirect URL.
9. Save, then open the site details to get `client_id` and `client_secret`.

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

## Scope and claim differences

- `openid` returns an ID token with claims such as `iss`, `sub`, `aud`, `exp`, `firstname`, `lastname`, `email`, and `iat`.
- `openid profile` returns an ID token with claims such as `iss`, `sub`, `aud`, `exp`, `name`, `email`, and `iat`; the access token can also be used to get additional user info.

## Token exchange

- The token request uses HTTP Basic auth.
- The Basic credential is `base64(client_id:client_secret)`.
- `grant_type` must be `authorization_code`.
- `redirect_uri` must exactly match the registered redirect.
- The returned payload includes:
  - `access_token`
  - `token_type`
  - `expires_in`
  - `refresh_token`
  - `scope`
  - `id_token`

## ID token notes

- The ID token is a JWT signed with the client secret.
- Default token lifetime is one hour.
- Validate:
  - `iss`
  - `aud`
  - `exp`
  - `iat`

## Optional user profile

- Use the access token to query user info via `/userinfo`.
- Returned profile fields may include:
  - `_id`
  - `nid`
  - `username`
  - `firstname`
  - `lastname`
  - `email`
  - `avatar`
  - `position`
  - `status`
  - `extras`

Profile picture URL pattern:

```text
https://customer-h1.ekoapp.com/file/view/avatar_id?size=large&access_token=<access_token>
```

## Source pages

- Getting Started: https://eko.gitbook.io/api/eko-openid/getting-started
- Register WebApp: https://eko.gitbook.io/api/eko-openid/register-webapp
- OpenID Flow: https://eko.gitbook.io/api/eko-openid/untitled-1
