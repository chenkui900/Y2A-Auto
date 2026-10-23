#!/usr/bin/env python
# -*- coding: utf-8 -*-

import enum
import os
import tempfile
from urllib.parse import parse_qs, urlparse

import qrcode

from .exceptions import ArgsException
from .utils.utils import get_api, raise_for_statement
from .utils.network import Credential, HEADERS, get_client
from .utils.picture import Picture

API = get_api("login")


class QrCodeLoginEvents(enum.Enum):
    SCAN = "scan"
    CONF = "confirm"
    TIMEOUT = "timeout"
    DONE = "done"


class QrCodeLogin:
    def __init__(self) -> None:
        self.__qr_link = ""
        self.__qr_picture = None
        self.__qr_key = ""
        self.__credential = None
        self.__login_cookies = {}

    def has_qrcode(self) -> bool:
        return self.__qr_link != ""

    def has_done(self) -> bool:
        return bool(self.__credential)

    def get_credential(self) -> Credential:
        raise_for_statement(self.has_done())
        return self.__credential

    def get_qrcode_picture(self) -> Picture:
        return self.__qr_picture

    async def generate_qrcode(self) -> None:
        api = API["qrcode"]["web"]["get_qrcode_and_token"]
        client = get_client()
        resp = await client.request(
            method=api["method"],
            url=api["url"],
            headers=HEADERS.copy(),
            cookies={},
        )
        if resp.code != 200:
            raise ArgsException(f"二维码生成失败，HTTP 状态码：{resp.code}")
        payload = resp.json()
        if payload.get("code") != 0:
            raise ArgsException(payload.get("message") or payload.get("msg") or "二维码生成失败")
        self.__login_cookies.update(resp.cookies or {})
        data = payload.get("data") or {}
        self.__qr_link = data["url"]
        self.__qr_key = data["qrcode_key"]

        qr = qrcode.QRCode()
        qr.add_data(self.__qr_link)
        img = qr.make_image()
        img_path = os.path.join(tempfile.gettempdir(), "y2a_bilibili_qrcode.png")
        img.save(img_path)
        self.__qr_picture = Picture.from_file(img_path)

    async def check_state(self) -> QrCodeLoginEvents:
        api = API["qrcode"]["web"]["get_events"]
        params = {"qrcode_key": self.__qr_key, "source": "main-fe-header"}
        client = get_client()
        resp = await client.request(
            method=api["method"],
            url=api["url"],
            params=params,
            headers=HEADERS.copy(),
            cookies=self.__login_cookies,
        )
        if resp.code != 200:
            raise ArgsException(f"二维码登录轮询失败，HTTP 状态码：{resp.code}")
        payload = resp.json()
        if payload.get("code") != 0:
            raise ArgsException(payload.get("message") or payload.get("msg") or "二维码登录轮询失败")
        events = payload.get("data") or {}
        code = events["code"]
        if code == 86101:
            return QrCodeLoginEvents.SCAN
        if code == 86090:
            return QrCodeLoginEvents.CONF
        if code == 86038:
            return QrCodeLoginEvents.TIMEOUT

        query = parse_qs(urlparse(events["url"]).query)
        cookies = dict(self.__login_cookies)
        cookies.update(resp.cookies or {})
        self.__login_cookies = cookies
        sessdata = cookies.get("SESSDATA") or (query.get("SESSDATA") or [""])[0]
        bili_jct = cookies.get("bili_jct") or (query.get("bili_jct") or [""])[0]
        dedeuserid = cookies.get("DedeUserID") or (query.get("DedeUserID") or [""])[0]
        extra_cookies = {}
        for key, values in query.items():
            if key not in {"SESSDATA", "bili_jct", "DedeUserID"} and values:
                extra_cookies[key] = values[0]
        for key, value in cookies.items():
            if key not in {"SESSDATA", "bili_jct", "DedeUserID", "buvid3", "BUVID3", "buvid4", "BUVID4"}:
                extra_cookies[key] = value
        self.__credential = Credential(
            sessdata=sessdata,
            bili_jct=bili_jct,
            dedeuserid=dedeuserid,
            buvid3=cookies.get("buvid3") or cookies.get("BUVID3"),
            buvid4=cookies.get("buvid4") or cookies.get("BUVID4"),
            ac_time_value=events.get("refresh_token"),
            **extra_cookies,
        )
        return QrCodeLoginEvents.DONE
