# Getting Started

Eko OpenID enables Single Sign On (SSO) with their Eko Account for a third party web application. The Eko user can access any third party web application integrated with Eko OpenID after signing in with their Eko account

## How Eko OpenID works

Eko OpenID authentication conforms to [OpenID Connect 1.0](http://openid.net/specs/openid-connect-core-1_0.html). The flow can be broken down into 4 steps including:

1. Redirect users to authenticate to Eko
2. Obtain authentication code from Eko
3. Request tokens using authentication code
4. Get user profile using access token (optional)

We're also providing Eko SSO with OpenID SDK for PHP and Javascript. You can download the SDK as the following link:

PHP: [SDK](https://github.com/EkoCommunications/EkoOAuthSDK-PHP)\
Javascript: [SDK](https://github.com/EkoCommunications/EkoOAuthSDK-Java)

If you're not using PHP or Javascript, we also provide SSO api in the topic: &#x20;

{% content-ref url="/pages/-LaZ3XQHyFN6HfAotMlz" %}
[Eko OpenID Flow](/api/eko-openid/untitled-1.md)
{% endcontent-ref %}

## How to integrate with Eko OpenID

To integrate a third-party web application with Eko, it consists of these steps:

1. Register third party web application on Eko admin panel.
2. Use Eko OpenID SDK or Implement your own API.
3. Customize your web server configuration
