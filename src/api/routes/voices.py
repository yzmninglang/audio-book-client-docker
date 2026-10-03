"""音色库(`/api/voices`)：列表 / 新建 / 编辑 / 重命名 / 删除 / 试听。

上传字段名兼容两处契约:`doc/api/design.md` §8.4 用 `audio`,
前端 `src/web/src/api/client.ts` 用 `file` —— 二者均接受。

`prompt_text` 需区分「省略」与「空串」(省略=不改动,空串=清除,§8.4)——
FastAPI 的 `Form(str | None)` 会把空串折叠成 `None`,无法区分,
故此处手动解析 `request.form()`。
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Request, Response
from starlette.datastructures import FormData, UploadFile

from ...errors import InvalidInputError
from ...voice import VoiceEntry
from ..deps import VoicesDep
from ..schemas import VoiceEntryOut

router = APIRouter(prefix="/api/voices", tags=["voices"])


def _entry_out(entry: VoiceEntry) -> VoiceEntryOut:
    return VoiceEntryOut(**asdict(entry))


def _str_field(form: FormData, key: str) -> str | None:
    value = form.get(key)
    return value if isinstance(value, str) else None


async def _read_upload(form: FormData, *keys: str) -> bytes | None:
    for key in keys:
        value = form.get(key)
        if isinstance(value, UploadFile):
            data = await value.read()
            if not data:
                raise InvalidInputError("参考音频为空")
            return data
    return None


@router.get("", response_model=list[VoiceEntryOut])
def list_voices(voices: VoicesDep) -> list[VoiceEntryOut]:
    return [_entry_out(entry) for entry in voices.list()]


@router.post("", response_model=VoiceEntryOut)
async def create_voice(request: Request, voices: VoicesDep) -> VoiceEntryOut:
    """新建音色(上传音频 + 参考文本),入库时标准化。"""
    form = await request.form()
    name = _str_field(form, "name")
    if not name:
        raise InvalidInputError("缺少音色名 name")
    data = await _read_upload(form, "audio", "file")
    if data is None:
        raise InvalidInputError("缺少参考音频文件")
    entry = voices.create(name, data, _str_field(form, "prompt_text"))
    return _entry_out(entry)


@router.put("/{name}", response_model=VoiceEntryOut)
async def update_voice(name: str, request: Request, voices: VoicesDep) -> VoiceEntryOut:
    """编辑音色:`audio`/`prompt_text`/`rename` **均可选**(§8.4)。

    `prompt_text` 省略=不改动,空串=清除。
    """
    form = await request.form()
    rename = _str_field(form, "rename")
    prompt_text = _str_field(form, "prompt_text")

    target = name
    if rename and rename != name:
        voices.rename(name, rename)
        target = rename

    data = await _read_upload(form, "audio", "file")
    if data is not None or prompt_text is not None:
        entry = voices.update(target, audio=data, prompt_text=prompt_text)
    else:
        entry = voices.get(target)
    return _entry_out(entry)


@router.delete("/{name}", status_code=204)
def delete_voice(name: str, voices: VoicesDep) -> Response:
    voices.delete(name)
    return Response(status_code=204)


@router.get("/{name}/preview")
def voice_preview(name: str, voices: VoicesDep) -> Response:
    """音色试听 —— **资源型 GET**,返回 `audio/wav` 字节流(design §5)。"""
    data = voices.reference_audio(name)
    return Response(content=data, media_type="audio/wav")
