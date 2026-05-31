# Replying Message

Response to be sent to Eko’s server from the webhook is sent using POST method with the parameter below.

## Reply text message to user

<mark style="color:green;">`POST`</mark> `https://customer-h1.ekoapp.com/bot/v1/message/text`

#### Headers

| Name          | Type   | Description        |
| ------------- | ------ | ------------------ |
| Content-type  | string | multipart/formdata |
| Authorization | string | API Key            |

#### Request Body

| Name       | Type   | Description                                        |
| ---------- | ------ | -------------------------------------------------- |
| message    | string | reply message to user                              |
| replyToken | string | reply token from request that came from Eko server |

{% tabs %}
{% tab title="200 " %}

```
```

{% endtab %}
{% endtabs %}

```
#example command for replying text message to user.
  curl -X POST \
  https://customer-api.ekoapp.com/bot/v1/message/text \
  -H 'Autorization: Bearer 53a0295873d08e6bd21a9c8f27d0f13acba5d62f' \
  -H 'Content-Type: multipart/form-data' \
  -F 'message=Hello There' \
  -F replyToken=8350939af2afb69a969649e1e8a9436669da9e83
```

## Reply picture to user

<mark style="color:green;">`POST`</mark> `https://customer-h1.ekoapp.com/bot/v1/message/picture`

#### Headers

| Name          | Type   | Description        |
| ------------- | ------ | ------------------ |
| Content-type  | string | multipart/formdata |
| Authorization | string | API Key            |

#### Request Body

| Name       | Type   | Description                                        |
| ---------- | ------ | -------------------------------------------------- |
| file       | string | reply picture to user                              |
| replyToken | string | reply token from request that came from Eko server |

{% tabs %}
{% tab title="200 " %}

```
```

{% endtab %}
{% endtabs %}

```
#example command for replying picture to user.  
  curl -X POST \
  https://customer-api.ekoapp.com/bot/v1/message/picture \
  -H 'Autorization: Bearer 53a0295873d08e6bd21a9c8f27d0f13acba5d62f' \
  -H 'Content-Type: multipart/form-data' \
  -F file=@/Users/don/Desktop/api/1mb.jpg \
  -F replyToken=8350939af2afb69a969649e1e8a9436669da9e83
```

## Sending quick reply

<mark style="color:green;">`POST`</mark> `https://customer-h1.ekoapp.com/bot/v1/message/quickreply`

#### Headers

| Name          | Type   | Description      |
| ------------- | ------ | ---------------- |
| Authorization | string | API Key          |
| Content-type  | string | application/json |

#### Request Body

| Name       | Type   | Description                                        |
| ---------- | ------ | -------------------------------------------------- |
| replyToken | string | reply token from request that came from eko server |
| meta       | string | array of quick reply                               |

{% tabs %}
{% tab title="200 " %}

```
```

{% endtab %}
{% endtabs %}

```
curl -X POST \
  https://app-h1.sea.ekoapp.com/bot/v1/message/quickreply \
  -H 'Authorization: Bearer 835c1bc124ca99be1c6a8a175018252c84b22bbf' \
  -H 'Content-Type: application/json' \
  -d '{
	"replyToken": "d2247696a330525b20bc858c84b0997477a8aacf",
	"message": {
		"data": "What do you want?",
		"meta": {
			"quickreply": {
				"template": "default",
				"items": [{
					"data": {
						"text": "I want coffee."
					},
					"type": "label",
					"value": "I want coffee."
				}, {
					"data": {
						"text": "I want food."
					},
					"type": "label",
					"value": "I want food."
				}, {
					"data": {
						"text": "I want travel."
					},
					"type": "label",
					"value": "I want travel."
				}]
			}
		}
	}
}
'
```
