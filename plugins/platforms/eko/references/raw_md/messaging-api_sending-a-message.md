# Sending a Message

## Sending a text message to a user

<mark style="color:green;">`POST`</mark> `https://customer-h1.ekoapp.com/bot/v1/direct/message`

#### Headers

| Name          | Type   | Description      |
| ------------- | ------ | ---------------- |
| Content-type  | string | application/json |
| Authorization | string | API Key          |

#### Request Body

| Name    | Type   | Description                              |
| ------- | ------ | ---------------------------------------- |
| uid     | string | user id of the user to be sent a message |
| message | object | object of message                        |
| type    | string | text                                     |
| data    | string | message to be sent                       |

{% tabs %}
{% tab title="200 " %}

```
{
    "status": "success"
}
```

{% endtab %}
{% endtabs %}

```
curl -X POST \
  https://app-h1.sea.ekoapp.com/bot/v1/direct/message \
  -H 'Authorization: Bearer 835c1bc124ca99be1c6a8a175018252c84b22bbf' \
  -H 'Content-Type: application/json' \
  -d '{
	"uid" : "5d8c318c22fba500114ed1c5",
	"message": {
		"type": "text",
		"data": "hi"
	}
}'
```

## Sending a text message to a group chat

<mark style="color:green;">`POST`</mark> `https://customer-h1.ekoapp.com/bot/v1/group/message`

#### Headers

| Name          | Type   | Description      |
| ------------- | ------ | ---------------- |
| Authorization | string | API Key          |
| Content-type  | string | application/json |

#### Request Body

| Name    | Type   | Description                   |
| ------- | ------ | ----------------------------- |
| gid     | string | group id to be sent a message |
| tid     | string | topic id to be sent a message |
| message | string | object of message             |
| type    | string | text                          |
| data    | string | message to be sent            |

{% tabs %}
{% tab title="200 " %}

```
{
    "status": "success"
}
```

{% endtab %}
{% endtabs %}

```
curl -X POST \
  https://app-h1.sea.ekoapp.com/bot/v1/group/message \
  -H 'Authorization: Bearer 835c1bc124ca99be1c6a8a175018252c84b22bbf' \
  -H 'Content-Type: application/json' \
  -d '{
	"gid" : "5d8af2ff8a63093716267ecf",
	"tid" : "5d8af2ff8a6309492e267ed1",
	"message": {
		"type": "text",
		"data": "hi"
	}
}'
```

## Sending a picture to a user

<mark style="color:green;">`POST`</mark> `https://customer-h1.ekoapp.com/bot/v1/direct/picture`

#### Headers

| Name          | Type   | Description         |
| ------------- | ------ | ------------------- |
| Authorization | string | API Key             |
| Content-type  | string | multipart/from-data |

#### Request Body

| Name    | Type   | Description                              |
| ------- | ------ | ---------------------------------------- |
| uid     | string | user id of the user to be sent a picture |
| file    | string | image to be sent                         |
| caption | string | caption of the picture                   |

{% tabs %}
{% tab title="200 " %}

```
{
    "status": "success"
}
```

{% endtab %}
{% endtabs %}

```
curl -X POST \
  https://app-h1.sea.ekoapp.com/bot/v1/direct/picture \
  -H 'Authorization: Bearer 835c1bc124ca99be1c6a8a175018252c84b22bbf' \
  -H 'Content-Type: multipart/form-data' \
  -F uid=5d8af2ff164176ecaec49e5c \
  -F caption=test \
  -F file=@1mb.jpg
```

## Sending a picture to a group chat

<mark style="color:green;">`POST`</mark> `https://customer-h1.ekoapp.com/bot/v1/group/picture`

#### Headers

| Name          | Type   | Description         |
| ------------- | ------ | ------------------- |
| Authorization | string | API Key             |
| Content-type  | string | multipart/form-data |

#### Request Body

| Name    | Type   | Description                   |
| ------- | ------ | ----------------------------- |
| caption | string | caption of the picture        |
| gid     | string | group id to be sent a picture |
| tid     | string | topic id to be sent a picture |
| file    | object | picture to be sent            |

{% tabs %}
{% tab title="200 " %}

```
{
    "status": "success"
}
```

{% endtab %}
{% endtabs %}

```
curl -X POST \
  https://app-h1.sea.ekoapp.com/bot/v1/group/picture \
  -H 'Authorization: Bearer 835c1bc124ca99be1c6a8a175018252c84b22bbf' \
  -H 'Content-Type: multipart/form-data' \
  -F gid=5d8af2ff164176ecaec49e5c \
  -F tid=d8a42f164176ecwec4sda95c \
  -F caption=test \
  -F file=@1mb.jpg
```

## Sending a file to a user

<mark style="color:green;">`POST`</mark> `https://customer-h1.ekoapp.com/bot/v1/direct/file`

#### Headers

| Name          | Type   | Description         |
| ------------- | ------ | ------------------- |
| Authorization | string | API Key             |
| Content-type  | string | multipart/form-data |

#### Request Body

| Name | Type   | Description                           |
| ---- | ------ | ------------------------------------- |
| uid  | string | user id of the user to be sent a file |
| file | string | file to be sent                       |

{% tabs %}
{% tab title="200 " %}

```
{
    "status": "success"
}
```

{% endtab %}
{% endtabs %}

```
curl -X POST \
  https://app-h1.sea.ekoapp.com/bot/v1/direct/file \
  -H 'Authorization: Bearer 835c1bc124ca99be1c6a8a175018252c84b22bbf' \
  -H 'Content-Type: multipart/form-data' \
  -F uid=5d8af2ff164176ecaec49e5c \
  -F file=@1mb.pdf
```

## Sending a file to a group chat

<mark style="color:green;">`POST`</mark> `https://customer-h1.ekoapp.com/bot/v1/group/file`

#### Headers

| Name          | Type   | Description         |
| ------------- | ------ | ------------------- |
| Authorization | string | API Key             |
| Content-type  | string | multipart/form-data |

#### Request Body

| Name | Type   | Description                |
| ---- | ------ | -------------------------- |
| gid  | string | group id to be sent a file |
| tid  | string | topic id to be sent a file |
| file | string | file to be sent            |

{% tabs %}
{% tab title="200 " %}

```
{
    "status": "success"
}
```

{% endtab %}
{% endtabs %}

```
curl -X POST \
  https://app-h1.sea.ekoapp.com/bot/v1/group/file \
  -H 'Authorization: Bearer 835c1bc124ca99be1c6a8a175018252c84b22bbf' \
  -H 'Content-Type: multipart/form-data' \
  -F gid=5d8af2ff164176ecaec49e5c \
  -F tid=d8a42f164176ecwec4sda95c \
  -F file=@1mb.pdf
```
