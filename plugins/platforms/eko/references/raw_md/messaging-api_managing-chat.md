# Managing Chat

## Get specific users

<mark style="color:blue;">`GET`</mark> `https://customer-h1.ekoapp.com/bot/v1/users`

#### Query Parameters

| Name     | Type   | Description                    |
| -------- | ------ | ------------------------------ |
| username | string | username of user to be queried |

#### Headers

| Name          | Type   | Description |
| ------------- | ------ | ----------- |
| Authorization | string | API Key     |

{% tabs %}
{% tab title="200 " %}

```
{
    "users": [
        {
            "_id": "5d8af2ff164176ecaec49e5c",
            "username": "test01@ekoapp.com",
            "email": "test01@ekoapp.com",
            "firstname": "Test",
            "lastname": "01",
            "deleted": false
        }
    ]
}
```

{% endtab %}
{% endtabs %}

```
curl -X GET \
  'https://customer-h1.ekoapp.com/bot/v1/users?username=test01@ekoapp.com' \
  -H 'Authorization: Bearer 6a2d844924de1b396dcb58af4684fa769bf8984b' \

```

### Get Group ID and Topic ID

The group id and topic can be found on the URL. Once you click on group chat, the url will include group id of the group chat and topic id of general topic

> Screenshot replacement: Eko group chat URL containing the group ID and the general topic ID.

```text
.../<group_id>/<topic_id>
```


If you want a specific topic id, just click on the topic you want and the topic id will show on the URL.

> Screenshot replacement: Selecting a specific topic updates the URL to that topic ID.

```mermaid
flowchart LR
    A[Open group chat] --> B[Read group ID from URL]
    B --> C[Click target topic]
    C --> D[Read topic ID from URL]
```


## Create a group chat

<mark style="color:green;">`POST`</mark> `https://customer-h1.ekoapp.com/bot/v1/groups`

#### Headers

| Name          | Type   | Description        |
| ------------- | ------ | ------------------ |
| Content-type  | string | multipart/formdata |
| Authorization | string | API Key            |

#### Request Body

| Name | Type   | Description                                    |
| ---- | ------ | ---------------------------------------------- |
| uids | array  | array of user id to be the member of the group |
| name | string | name of group chat                             |
| file | string | picture of group chat                          |

{% tabs %}
{% tab title="200 " %}

```
{
    "group": {
        "_id": "5d91c6017755dba7a80a93c6",
        "name": "My first group",
        "type": "group_chatv2",
        "settings": {
            "isReadOnly": false,
            "isAutoManagedMember": false,
            "isAdminManaged": true,
            "isCreatorManaged": true,
            "isMemberManaged": false,
            "isPrivate": false
        },
        "userCount": 3
    }
}
```

{% endtab %}
{% endtabs %}

```
curl -X POST \
  https://app-h1.sea.ekoapp.com/bot/v1/groups \
  -H 'Authorization: Bearer 8fadfb49d0d87e0ac24e25a2468b09650470310d' \
  -H 'Content-Type: multipart/form-data' \
  -F 'uids=["5d8c318c22fba500114ed1c5","5d8af2ff164176ecaec49e5c"]' \
  -F 'name=My first group' \
  -F file=@4.jpg
```

## Create a topic in a group chat

<mark style="color:green;">`POST`</mark> `https://customer-h1.ekoapp.com/bot/v1/groups/group_id/topics`

Replace group\_id with the group id you want to create a topic.

#### Headers

| Name          | Type   | Description      |
| ------------- | ------ | ---------------- |
| Authorization | string | API Key          |
| Content-type  | string | Application/json |

#### Request Body

| Name | Type   | Description                 |
| ---- | ------ | --------------------------- |
| name | string | name of topic to be created |

{% tabs %}
{% tab title="200 " %}

```
{
    "topic": {
        "_id": "5d91c6d350088a9246ad9256",
        "name": "My first Topic",
        "gid": "5d91c6017755dba7a80a93c6"
    }
}
```

{% endtab %}
{% endtabs %}

```
curl -X POST \
  https://app-h1.sea.ekoapp.com/bot/v1/groups/5d91c6017755dba7a80a93c6/topics \
  -H 'Authorization: Bearer 8fadfb49d0d87e0ac24e25a2468b09650470310d' \
  -H 'Content-Type: application/json' \
  -d '{
	"name": "My first Topic"
}'
```
