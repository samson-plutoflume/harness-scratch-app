# Harness Scratch App

Scratch app running in staging to demonstrate Harness feature flags

## Building and Running

```shell
docker build -t harness-scratch-app .
docker run -p 8000:8000 -e HARNESS_SCRATCH_API_KEY="$HARNESS_API_KEY" -t harness-scratch-app
```

Documentation is then available at http://localhost:8000/docs

## Usage

There are 2 main functionalities:
* Query a feature flag
* Watch a feature flag for changes

### Querying a flag

```http request
GET /{flag_id}/{target_id}
```

```http request
POST /{flag_id}/{target_id}
Content-Type: application/json

{"name": "{target_name}", "variation_type": "string", "target_attributes": {"attr_key": "attr_value"}}
```

Both these queries return data in the form

```json
{
  "flag_id": "{flag_id}",
  "flag_value": "{flag_value}",
  "target_id": "{target_id}"
}
```

### Watch for flag changes

You can use websockets to watch for flag changes.

There is a helpful website for this here: https://websocketking.com/

1. Set the connection as `ws://{host}:{port}/{flag_id}/{target_id}/watch`
2. Send the setup query which should be the same format as the POST request data.  
   For ease, you can send just and empty dict (`{}`) which will use the default variation type of `string`.
3. You should get an initial value for the flag, and then the connection will remain open for other updates when you update the flag in the Harness portal.
4. The `connection_id` can be used to search for the events in logs.
5. The connection will automatically close after 30 minutes.
