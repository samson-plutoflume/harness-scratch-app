import asyncio
import functools
from functools import partial
import logging
import typing
from typing import Literal
import uuid

from fastapi import FastAPI, Depends, WebSocket
from featureflags.client import CfClient
from featureflags.config import (
    with_base_url,
    with_analytics_enabled,
    with_stream_enabled,
    with_events_url,
)
from featureflags.evaluations.auth_target import Target
from starlette.websockets import WebSocketDisconnect
import structlog
from featureflags.util import log as harness_logger
from pydantic import BaseSettings, BaseModel, Field
import uvicorn

WEBSOCKET_MAX_CONNECTION_TIME = 60 * 30  # 30 minutes
WEBSOCKET_PING_SECONDS = 30


def configure_logging(log_level: int = logging.INFO) -> None:
    harness_logger.setLevel(logging.WARNING)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
    )


class Settings(BaseSettings):
    class Config:
        env_prefix = "HARNESS_SCRATCH_"
        case_sensitive = True

    RELAY_BASE_URL: str = ""
    RELAY_EVENTS_URL: str = ""
    API_KEY: str = ""


logger = structlog.get_logger(__name__)
settings = Settings()


@functools.lru_cache()
def get_client() -> CfClient:
    client = CfClient(
        settings.API_KEY,
        with_base_url(settings.RELAY_BASE_URL),
        with_events_url(settings.RELAY_EVENTS_URL),
        with_stream_enabled(True),
        with_analytics_enabled(True),
    )
    client.authenticate()
    return client


app = FastAPI()
configure_logging()
get_client()  # try to initialise the client on startup


class FlagRequest(BaseModel):
    name: str | None = None
    variation_type: Literal["string", "boolean", "number"] = "string"
    target_attributes: dict = Field(default_factory=dict)


@app.get("/health")
def healthcheck():
    return {}


@app.on_event("shutdown")
def shutdown_event(client: CfClient = Depends(get_client)):
    client.close()
    logger.info("Closed Harness client")


def _build_target(target_id: str, details: FlagRequest | None) -> Target:
    if details:
        return Target(
            identifier=target_id,
            name=details.name or target_id,
            attributes=details.target_attributes,
        )
    else:
        return Target(identifier=target_id, name=target_id)


def _get_variation_callable(details: FlagRequest | None, client: CfClient) -> partial:
    result_info = {
        "string": ("", client.string_variation),
        "boolean": (False, client.bool_variation),
        "number": (0, client.number_variation),
    }
    default, variation_callable = (
        result_info.get(details.variation_type, result_info["string"])
        if details
        else result_info["string"]
    )
    return functools.partial(variation_callable, default=default)


class FlagValueResponse(BaseModel):
    flag_id: str
    flag_value: typing.Any
    target_id: str


@app.get("/{flag_id}/{target_id}", response_model=FlagValueResponse)
async def get_feature_flag(
    flag_id: str, target_id: str, client: CfClient = Depends(get_client)
) -> FlagValueResponse:
    target = _build_target(target_id, None)
    variation_callable = _get_variation_callable(None, client)

    result = variation_callable(flag_id, target)
    logger.info(
        "Evaluated feature flag",
        flag_id=flag_id,
        flag_value=result,
        target_id=target_id,
    )
    return FlagValueResponse(flag_id=flag_id, flag_value=result, target_id=target_id)


@app.post("/{flag_id}/{target_id}", response_model=FlagValueResponse)
async def post_feature_flag(
    flag_id: str,
    target_id: str,
    details: FlagRequest | None = None,
    client: CfClient = Depends(get_client),
) -> FlagValueResponse:
    target = _build_target(target_id, details)
    variation_callable = _get_variation_callable(details, client)

    result = variation_callable(flag_id, target)
    logger.info(
        "Evaluated feature flag",
        flag_id=flag_id,
        flag_value=result,
        target_id=target_id,
        target_attributes=details.target_attributes if details else None,
    )
    return FlagValueResponse(flag_id=flag_id, flag_value=result, target_id=target_id)


@app.post("/reauthenticate")
async def force_reauth(client: CfClient = Depends(get_client)):
    client.authenticate()
    return {}


class FlagState(BaseModel):
    flag_id: str
    flag_value: typing.Any
    target_id: str
    target_attributes: dict


class FlagWatchMessage(BaseModel):
    message: str
    connection_id: str
    state: FlagState
    previous_state: FlagState | None = None


@app.websocket("/{flag_id}/{target_id}/watch")
async def watch_changes(
    flag_id: str,
    target_id: str,
    websocket: WebSocket,
    client: CfClient = Depends(get_client),
):
    await websocket.accept()
    data = await websocket.receive_json()
    details = FlagRequest(**data)

    target = _build_target(target_id, details)
    variation_callable = _get_variation_callable(details, client)
    target_attributes = details.target_attributes if details else None

    connection_id = uuid.uuid4()
    result = variation_callable(flag_id, target)

    with structlog.contextvars.bound_contextvars(
        connection_id=str(connection_id),
        flag_id=flag_id,
        target_id=target_id,
        target_attributes=target_attributes,
    ):

        logger.info("Starting watch on feature flag", flag_value=result)

        async def send_update(
            message: str,
            *,
            current_value: typing.Any,
            previous_value: typing.Any | None = None
        ) -> None:
            logger.info(
                "Sending update to watched feature flag",
                flag_value=current_value,
                previous_flag_value=previous_value,
            )
            await websocket.send_json(
                FlagWatchMessage(
                    message=message,
                    connection_id=str(connection_id),
                    state=FlagState(
                        flag_id=flag_id,
                        flag_value=current_value,
                        target_id=target_id,
                        target_attributes=target_attributes,
                    ),
                    previous_state=FlagState(
                        flag_id=flag_id,
                        flag_value=previous_value,
                        target_id=target_id,
                        target_attributes=target_attributes,
                    )
                    if previous_value
                    else None,
                ).dict(exclude_none=True)
            )

        await send_update("Initiated connection to watch flag", current_value=result)
        second_counter = 0
        try:
            while second_counter < WEBSOCKET_MAX_CONNECTION_TIME:
                await asyncio.sleep(1)
                second_counter += 1
                next_result = variation_callable(flag_id, target)
                if next_result != result:
                    await send_update(
                        "Flag changed", current_value=next_result, previous_value=result
                    )
                    result = next_result

                if second_counter % WEBSOCKET_PING_SECONDS == 0:
                    logger.debug(
                        "Checking connection",
                        application_state=websocket.application_state,
                        client_state=websocket.client_state,
                    )
                    await websocket.send_json({"type": "ping"})
        except WebSocketDisconnect:
            logger.info("Closing connection to watched feature flag")
        else:
            await websocket.close(reason="timed out")
            logger.warning("Closed connection due to timeout.")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
