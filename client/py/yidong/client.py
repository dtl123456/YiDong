import inspect
import mimetypes
import os
from datetime import datetime
from time import sleep
from typing import Iterable, get_args

import httpx
import rich
from jsonargparse import CLI
from pydantic import TypeAdapter, ValidationError
from yidong.config import CONFIG
from yidong.exception import YDError
from yidong.model import (
    Chapter,
    GenScriptElement,
    GenScriptTask,
    GenScriptTaskResult,
    Pagination,
    PingTask,
    PingTaskResult,
    Reply,
    Resource,
    ResourceUploadResponse,
    T,
    Task,
    TaskContainer,
    TaskInfo,
    VideoMashupTask,
    VideoMashupTaskResult,
    VideoSummaryTask,
    VideoSummaryTaskResult,
)
from yidong.util import PaginationIter, TaskRef


class YiDong:
    _client: httpx.Client

    def __init__(
        self, api_key: str = CONFIG.api_key, base_url: str = CONFIG.base_url
    ) -> None:
        """Initialize the Client

        Args:
            base_url: The base url of the server.
            api_key: The api key for authentication.
        """
        self._client = httpx.Client(
            base_url=base_url, headers={CONFIG.api_key_header: api_key}
        )

    def _request(
        self,
        T: type[T],
        method: str,
        path: str,
        *,
        params: dict | None = None,
        payload: dict | None = None,
        headers: dict | None = None,
        content: str | bytes | Iterable[bytes] | None = None,
    ) -> T:
        resp = self._client.request(
            method=method,
            url=path,
            params=params,
            json=payload,
            headers=headers,
            content=content,
        )
        try:
            payload = resp.json()
            reply = Reply[T].parse_obj(payload)
        except ValidationError as e:
            raise YDError(1, str(e), payload)

        if reply.code != 0:
            raise YDError(reply.code, reply.message, reply.data)
        return reply.data

    def add_resource(
        self, file: str | None = None, content_type: str | None = None
    ) -> str:
        """Add a resource to the server. A resource id will be returned.
        If `file` is not provided, a pre-signed url for uploading will be
        returned.

        Args:
            file: It can be either a local file path or a URL. If nothing is
                provided, a pre-signed url will be generated which you can use
                to upload the file later with the HTTP `PUT` request. Note that
                the `Content-Type` header should be set the same as the
                `content_type` parameter when uploading. Even if you do not
                provide the `content_type` parameter here, you should still set
                the `Content-Type` header as the default value of `content_type`
                of `application/octet-stream`.
            content_type: The mime content type of the file. If not provided, it
                will first try to guess the content type from the file
                extension. If it fails, it will be set to
                `application/octet-stream` by default. You can still update it
                later with the `update_resource` method.
        """
        if file is None:
            r = self._client.put(
                f"/resource",
                headers={"Content-Type": content_type or "application/octet-stream"},
            )
            if r.status_code == 307:
                return r.headers["Location"]
            else:
                raise YDError(1, "Failed to get pre-signed url", r.text)
        elif os.path.exists(file):
            headers = {
                "Content-Type": content_type
                or mimetypes.guess_type(file)[0]
                or "application/octet-stream"
            }
            r = self._client.put(
                f"/resource",
                headers=headers,
                params={"file": file},
            )
            if r.status_code == 307:
                with open(file, "rb") as f:
                    r = self._client.put(
                        r.headers["Location"],
                        content=f,
                        headers=headers,
                    )
                    res = Reply[ResourceUploadResponse].parse_obj(r.json())
                    return res.data.id
            else:
                raise YDError(1, "Failed to get pre-signed url", r.text)
        else:
            raise FileNotFoundError(f"File not found: {file}")

    def update_resource(
        self, id: str, name: str | None = None, mime: str | None = None
    ) -> Resource:
        """Update the resource with the given id.

        Args:
            id: The resource id.
            name: The file name of the resource.
            mime: The mime content type of the resource.
        """
        return self._request(
            Resource, "patch", f"/resource/{id}", payload={"name": name, "mime": mime}
        )

    def list_resource(
        self,
        page: int = 1,
        page_size: int = 10,
        source: list[str] = ["local_upload", "remote_download"],
        ids: list[str] | None = None,
    ) -> Pagination[Resource]:
        """Retrieve resources in `page` based on filters of `source`. See also `list_resource_iter`.

        Args:
            page: The page number, starting from 1.
            page_size: The number of resources per page.
            source: The source of the resources.
        """
        params = {"page": page, "page_size": page_size, "source": source}
        if ids:
            params["ids"] = ids
        return self._request(
            Pagination[Resource],
            "get",
            "/resource",
            params=params,
        )

    def list_resource_iter(self, **kwargs) -> PaginationIter[Resource]:
        return PaginationIter[Resource](lambda p: self.list_resource(page=p, **kwargs))

    def get_resource(self, id: str) -> Resource:
        return self._request(Resource, "get", f"/resource/{id}")

    def delete_resource(self, id: str) -> None:
        self._request(bool, "delete", f"/resource/{id}")

    #####

    def list_task(
        self,
        page: int = 1,
        page_size: int = 10,
        ids: list[str] | None = None,
    ) -> Pagination[TaskContainer]:
        params = {"page": page, "page_size": page_size}
        if ids:
            params["ids"] = ids
        return self._request(Pagination[TaskContainer], "get", "/task", params=params)

    def list_task_iter(self, **kwargs) -> PaginationIter[TaskContainer]:
        return PaginationIter[TaskContainer](lambda p: self.list_task(page=p, **kwargs))

    def _get_task(self, id: str) -> TaskContainer:
        return self._request(TaskContainer, "get", f"/task/{id}")

    def get_task(
        self,
        id: str,
        block: bool = True,
        poll_interval: float = 1.0,
        timeout: float = 0,
    ) -> TaskContainer:
        """Get the task detail with the given task id.

        Args:
            id: The task id. block: Whether to block the request until the task is completed.
            poll_interval: The interval to poll the task status.
            timeout: The maximum time to wait for the task to finish. By default it will wait infinitely.
        """
        if block:
            start = datetime.now()
            while True:
                t = self._get_task(id)
                if t.is_done():
                    return t
                else:
                    if t.records:
                        print(
                            f"{id}\t{t.records[-1].time}\t{t.records[-1].type.value}\t{t.records[-1].message}"
                        )
                now = datetime.now()
                if timeout > 0 and (now - start).total_seconds() > timeout:
                    raise TimeoutError(
                        f"failed to fetch task [{id}] result within {timeout} seconds"
                    )
                sleep(poll_interval)
        else:
            return self._get_task(id)

    def delete_task(self, tid: str) -> bool:
        return self._request(bool, "delete", f"/task/{tid}")

    def _submit_task(self, payload: dict) -> TaskRef:
        caller = inspect.currentframe().f_back.f_code.co_name
        task_type, task_result_type = get_args(
            inspect.signature(getattr(self, caller)).return_annotation
        )
        payload = payload | {"type": caller}
        res = self._request(
            TaskInfo,
            "post",
            "/task",
            payload=TypeAdapter(Task).validate_python(payload).dict(),
        )
        return TaskRef[task_type, task_result_type](self, res.id)

    def ping(self) -> TaskRef[PingTask, PingTaskResult]:
        """A simple task to test the health of the server."""
        return self._submit_task(locals())

    def video_summary(
        self,
        video_id: str,
        prompt: str | None = None,
        chapter_prompt: str | None = None,
        chapters: list[Chapter] | None = None,
    ) -> TaskRef[VideoSummaryTask, VideoSummaryTaskResult]:
        """
        Summarize a video with the given video id. By default, the video will be
        split into chapters. The summary of each chapter together with the summary of the whole video will be returned.

        Args:
            video_id: The video id.
            prompt: The prompt for the video summary. If not set, a builtin prompt will be used here.
            chapter_prompt: The prompt for the chapter summary. If not set, it will be the same as `prompt`.
            chapters: The list of video chapters. If not set, the `chapters` will be extracted automatically.
        """
        return self._submit_task(locals())

    def video_script(
        self,
        collection: list[GenScriptElement],
        remix_s1_prompt: str,
        remix_s2_prompt: str,
    ) -> TaskRef[GenScriptTask, GenScriptTaskResult]:
        """
        Generate scripts based on a collection of video summarizations.
        """
        return self._submit_task(locals())

    def video_mashup(
        self,
        video_ids: list[str],
        voice_overs: list[str],
        bgm_id: str,
        voice_style_id: str,
        chapters: list[Chapter] | None = None,
    ) -> TaskRef[VideoMashupTask, VideoMashupTaskResult]:
        """Create a new video based on the given videos and other elements.

        Args:
            video_ids: The list of video ids.
            chapters: The list of chapters. If not provided, the whole video will be used.
            voice_overs: The list of voice over texts.
            bgm_id: The background music id. Make sure it exists first.
            voice_style_id: The voice style id. TODO: enumerate all available styles here.
        """
        return self._submit_task(locals())


def main():
    rich.print(CLI(YiDong))


if __name__ == "__main__":
    main()
