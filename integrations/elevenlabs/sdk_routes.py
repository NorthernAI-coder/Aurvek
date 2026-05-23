from fastapi import APIRouter, Response

from integrations.elevenlabs.sdk_proxy import ElevenLabsSDKProxy


router = APIRouter()


@router.get("/sdk/elevenlabs-client.js")
async def serve_elevenlabs_sdk():
    content = await ElevenLabsSDKProxy.get_sdk()
    return Response(content, media_type="application/javascript")


@router.get("/sdk/elevenlabs-client.js.map")
async def serve_elevenlabs_sourcemap():
    content = await ElevenLabsSDKProxy.get_sourcemap()
    return Response(content, media_type="application/json")


@router.get("/sdk/lib.umd.js")
async def serve_elevenlabs_alias():
    content = await ElevenLabsSDKProxy.get_sdk()
    return Response(content, media_type="application/javascript")


@router.get("/sdk/lib.umd.js.map")
async def serve_elevenlabs_alias_map():
    content = await ElevenLabsSDKProxy.get_sourcemap()
    return Response(content, media_type="application/json")
