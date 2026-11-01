import os
import io
import tempfile
from typing import Any
from dataclasses import dataclass

from yarl import URL
from PIL import Image

from .network import Credential, get_client, BiliAPIFile


@dataclass
class Picture:
    """
    (@dataclasses.dataclass)

    图片类，包含图片链接、尺寸以及下载操作。

    Args:
        height    (int)  : 高度

        imageType (str)  : 格式，例如: png

        size      (Any)  : 大小。单位 KB

        url       (str)  : 图片链接

        width     (int)  : 宽度

        content   (bytes): 图片内容

    可以不实例化，用 `load_url`, `from_content` 或 `from_file` 加载图片。
    """

    height: int = -1
    imageType: str = ""
    size: Any = ""
    url: str = ""
    width: int = -1
    content: bytes = b""

    def __str__(self) -> str:
        return f"Picture(height='{self.height}', width='{self.width}', imageType='{self.imageType}', size={self.size}, url='{self.url}')"

    def __repr__(self) -> str:
        # no content...
        return f"Picture(height='{self.height}', width='{self.width}', imageType='{self.imageType}', size={self.size}, url='{self.url}')"

    def __set_picture_meta_from_bytes(self, imgtype: str) -> None:
        # 直接在内存中解析图片元数据，避免写固定名临时文件导致并发互踩
        with Image.open(io.BytesIO(self.content)) as img:
            self.width = img.width
            self.height = img.height
        self.imageType = imgtype
        self.size = int(round(len(self.content) / 1024, 0))

    @staticmethod
    async def load_url(url: str) -> "Picture":
        """
        加载网络图片。(async 方法)

        Args:
            url (str): 图片链接

        Returns:
            Picture: 加载后的图片对象
        """
        if URL(url).scheme == "":
            url = "https:" + url
        obj = Picture()
        session = get_client()
        resp = await session.request(
            method="GET",
            url=url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36 Edg/116.0.1938.54",
                "Referer": url,
            },
        )
        obj.content = resp.raw
        obj.url = url
        obj.__set_picture_meta_from_bytes(
            url.split("/")[-1].split(".")[-1].split("?")[0]
        )
        return obj

    @staticmethod
    def from_file(path: str) -> "Picture":
        """
        加载本地图片。

        Args:
            path (str): 图片地址

        Returns:
            Picture: 加载后的图片对象
        """
        obj = Picture()
        with open(path, "rb") as file:
            obj.content = file.read()
        obj.url = "file://" + path
        obj.__set_picture_meta_from_bytes(os.path.basename(path).split(".")[-1])
        return obj

    @staticmethod
    def from_content(content: bytes, format: str) -> "Picture":
        """
        加载字节数据

        Args:
            content (str): 图片内容

            format  (str): 图片后缀名，如 `webp`, `jpg`, `ico`

        Returns:
            Picture: 加载后的图片对象
        """
        obj = Picture()
        obj.content = content
        obj.url = "bytes://" + content.decode("utf-8", errors="ignore")
        obj.__set_picture_meta_from_bytes(format)
        return obj

    def _to_biliapifile(self) -> BiliAPIFile:
        # 使用唯一临时文件名，避免并发上传时互相覆盖同一个 test.xxx
        fd, img_path = tempfile.mkstemp(prefix="y2a_cover_", suffix="." + self.imageType)
        with os.fdopen(fd, "wb") as file:
            file.write(self.content)
        with Image.open(io.BytesIO(self.content)) as img:
            mime_type = img.get_format_mimetype()
        return BiliAPIFile(path=img_path, mime_type=mime_type)

    async def upload(self, credential: Credential) -> "Picture":
        """
        上传图片至 B 站。

        Args:
            credential (Credential): 凭据类。

        Returns:
            Picture: `self`
        """
        from ..dynamic import upload_image

        res = await upload_image(self, credential)
        self.height = res["image_height"]
        self.width = res["image_width"]
        self.url = res["image_url"]
        self.size = res["img_size"]
        self.content = (await self.load_url(self.url)).content
        return self

    async def upload_by_note(self, credential: Credential) -> "Picture":
        """
        通过笔记接口上传图片至 B 站。

        Args:
            credential (Credential): 凭据类。

        Returns:
            Picture: `self`
        """
        from ..note import upload_image

        res = await upload_image(self, credential)
        self = await self.load_url("https:" + res["location"])
        return self

    def convert_format(self, new_format: str) -> "Picture":
        """
        将图片转换为另一种格式。

        Args:
            new_format (str): 新的格式。例：`png`, `ico`, `webp`.

        Returns:
            Picture: `self`
        """
        save_format = "JPEG" if new_format.lower() in ("jpg", "jpeg") else new_format.upper()
        with Image.open(io.BytesIO(self.content)) as img:
            buf = io.BytesIO()
            img.save(buf, format=save_format)
            self.content = buf.getvalue()
        self.__set_picture_meta_from_bytes(new_format)
        return self

    def resize(self, width: int, height: int) -> "Picture":
        """
        调整大小

        Args:
            width  (int): 宽度
            height (int): 高度

        Returns:
            Picture: `self`
        """
        save_format = "JPEG" if self.imageType.lower() in ("jpg", "jpeg") else self.imageType.upper()
        with Image.open(io.BytesIO(self.content)) as img:
            img = img.resize((width, height))
            buf = io.BytesIO()
            img.save(buf, format=save_format)
            self.content = buf.getvalue()
        self.__set_picture_meta_from_bytes(self.imageType)
        return self

    def to_file(self, path: str) -> "Picture":
        """
        下载图片至本地。

        Args:
            path (str): 下载地址。

        Returns:
            Picture: `self`
        """
        with Image.open(io.BytesIO(self.content)) as img:
            save_all = self.imageType in ("webp", "gif")
            img.save(path, save_all=save_all)
        self.url = "file://" + path
        return self

    def to_json(self) -> dict:
        """
        转换为 bilibili api 中的 json 格式，提供图片链接/长宽/大小

        Returns:
            dict: 图片链接/长宽/大小
        """
        return [
            {
                "img_src": self.url,
                "img_width": self.width,
                "img_height": self.height,
                "img_size": self.size,
            }
        ]
