#!/usr/bin/env python3
"""
闲鱼扫码登录工具
基于API接口实现二维码生成和Cookie获取（参照myfish-main项目）
"""

import asyncio
import time
import uuid
import json
import re
from random import random
from urllib.parse import quote
from typing import Optional, Dict, Any
import httpx
import qrcode
import qrcode.constants
from loguru import logger


def generate_headers():
    """生成请求头"""
    return {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
        'Sec-Fetch-Dest': 'empty',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'same-origin',
    }


class GetLoginParamsError(Exception):
    """获取登录参数错误"""


class GetLoginQRCodeError(Exception):
    """获取登录二维码失败"""


class NotLoginError(Exception):
    """未登录错误"""


class QRLoginSession:
    """二维码登录会话"""

    def __init__(self, session_id: str, owner_user_id: Optional[int] = None):
        self.session_id = session_id
        self.owner_user_id = owner_user_id
        self.status = 'waiting'  # waiting, scanned, success, expired, cancelled, verification_required
        self.qr_code_url = None
        self.qr_content = None
        self.cookies = {}
        self.unb = None
        self.created_time = time.time()
        self.expire_time = 300  # 5分钟过期
        self.params = {}  # 存储登录参数
        self.verification_url = None  # 风控验证URL
        self.message = None
        self.account_info = None

    def is_expired(self) -> bool:
        """检查是否过期"""
        return time.time() - self.created_time > self.expire_time

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'session_id': self.session_id,
            'status': self.status,
            'qr_code_url': self.qr_code_url,
            'created_time': self.created_time,
            'is_expired': self.is_expired()
        }


class QRLoginManager:
    """二维码登录管理器"""

    def __init__(self):
        self.sessions: Dict[str, QRLoginSession] = {}
        self.headers = generate_headers()
        self.host = "https://passport.goofish.com"
        self.api_mini_login = f"{self.host}/mini_login.htm"
        self.api_generate_qr = f"{self.host}/newlogin/qrcode/generate.do"
        self.api_scan_status = f"{self.host}/newlogin/qrcode/query.do"
        self.api_login_by_token = f"{self.host}/login_token/login.do"
        self.api_h5_tk = "https://h5api.m.goofish.com/h5/mtop.gaia.nodejs.gaia.idle.data.gw.v2.index.get/1.0/"
        self.api_h5_feed = "https://h5api.m.goofish.com/h5/mtop.taobao.idlehome.home.webpc.feed/1.0/"
        self.api_h5_nav = "https://h5api.m.goofish.com/h5/mtop.idle.web.user.page.nav/1.0/"
        self.api_mmstat = "https://log.mmstat.com/eg.js"

    def _merge_client_cookies(self, session: QRLoginSession, cookies):
        """Merge response/client cookies into the QR session without logging values."""
        cookie_jar = getattr(cookies, "jar", None)
        if cookie_jar is not None:
            for cookie in cookie_jar:
                session.cookies[cookie.name] = cookie.value
            return

        for key, value in cookies.items():
            session.cookies[key] = value

    async def _request(self, session: QRLoginSession, method: str, url: str, **kwargs) -> httpx.Response:
        """Issue a request while carrying the QR session cookie jar forward."""
        headers = kwargs.pop("headers", self.headers)
        timeout = kwargs.pop("timeout", 15)
        async with httpx.AsyncClient(
            follow_redirects=True,
            cookies=session.cookies,
            timeout=timeout,
        ) as client:
            resp = await client.request(method, url, headers=headers, **kwargs)
            self._merge_client_cookies(session, client.cookies)
            self._merge_client_cookies(session, resp.cookies)
            return resp

    def _cookie_marshal(self, cookies: dict) -> str:
        """将Cookie字典转换为字符串"""
        return "; ".join([f"{k}={v}" for k, v in cookies.items()])

    def _passport_common_params(self, session: QRLoginSession) -> Dict[str, Any]:
        """Common passport QR params derived from the current cookie jar."""
        csrf_token = session.cookies.get('XSRF-TOKEN', '')
        cookie2 = session.cookies.get('cookie2', '')
        return {
            "appName": "xianyu",
            "fromSite": "77",
            "appEntrance": "web",
            "_csrf_token": csrf_token,
            "umidToken": "",
            "hsiz": cookie2,
            "bizParams": f"taobaoBizLoginFrom=web&renderRefer={quote('https://www.goofish.com/')}",
            "mainPage": "false",
            "isMobile": "false",
            "lang": "zh_CN",
            "returnUrl": "",
            "umidTag": "SERVER",
        }

    async def _prime_mtop_cookie(self, session: QRLoginSession, api_url: str, api_name: str):
        params = {
            "jsv": "2.7.2",
            "appKey": "34839810",
            "t": str(int(time.time() * 1000)),
            "sign": "",
            "v": "1.0",
            "type": "originaljson",
            "accountSite": "xianyu",
            "dataType": "json",
            "timeout": "20000",
            "api": api_name,
            "sessionOption": "AutoLoginOnly",
            "spm_cnt": "a21ybx.home.0.0",
        }
        headers = {
            **self.headers,
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://www.goofish.com",
            "Referer": "https://www.goofish.com/",
            "Sec-Fetch-Site": "same-site",
        }
        await self._request(session, "POST", api_url, params=params, data="data=%7B%7D", headers=headers)

    async def _get_mh5tk(self, session: QRLoginSession) -> dict:
        """获取m_h5_tk和m_h5_tk_enc"""
        await self._request(session, "GET", self.api_mmstat, headers={**self.headers, "Referer": "https://www.goofish.com/"})
        await self._prime_mtop_cookie(session, self.api_h5_feed, "mtop.taobao.idlehome.home.webpc.feed")
        await self._prime_mtop_cookie(session, self.api_h5_tk, "mtop.gaia.nodejs.gaia.idle.data.gw.v2.index.get")
        return session.cookies

    async def _get_login_params(self, session: QRLoginSession) -> dict:
        """获取二维码登录时需要的表单参数"""
        params = {
            "lang": "zh_cn",
            "appName": "xianyu",
            "appEntrance": "web",
            "styleType": "vertical",
            "bizParams": "",
            "notLoadSsoView": False,
            "notKeepLogin": False,
            "isMobile": False,
            "qrCodeFirst": False,
            "stie": 77,
            "rnd": random(),
        }

        headers = {
            **self.headers,
            "Referer": "https://www.goofish.com/",
            "Sec-Fetch-Site": "same-site",
            "Sec-Fetch-Dest": "iframe",
            "Sec-Fetch-Mode": "navigate",
        }
        resp = await self._request(session, "GET", self.api_mini_login, params=params, headers=headers)

        # 正则匹配需要的json数据。页面结构变化时，回退到基于Cookie拼出的参数。
        pattern = r"window\.viewData\s*=\s*(\{.*?\})\s*;"
        match = re.search(pattern, resp.text, re.S)
        if match:
            json_string = match.group(1)
            view_data = json.loads(json_string)
            data = view_data.get("loginFormData")
            if data:
                data["umidTag"] = "SERVER"
                session.params.update(data)
                return data

        fallback = self._passport_common_params(session)
        if fallback.get("_csrf_token") or fallback.get("hsiz"):
            session.params.update(fallback)
            return fallback

        raise GetLoginParamsError("获取登录参数失败")
    
    async def generate_qr_code(self, owner_user_id: Optional[int] = None) -> Dict[str, Any]:
        """生成二维码"""
        try:
            # 创建新的会话
            session_id = str(uuid.uuid4())
            session = QRLoginSession(session_id, owner_user_id)

            # 1. 获取m_h5_tk
            await self._get_mh5tk(session)
            logger.info(f"获取m_h5_tk成功: {session_id}")

            # 2. 获取登录参数
            login_params = await self._get_login_params(session)
            logger.info(f"获取登录参数成功: {session_id}")

            # 3. 生成二维码
            headers = {
                **self.headers,
                "Referer": "https://passport.goofish.com/mini_login.htm",
            }
            resp = await self._request(session, "GET", self.api_generate_qr, params=login_params, headers=headers)
            results = resp.json()

            if results.get("content", {}).get("success") == True:
                qr_data = results["content"]["data"]
                # 更新会话参数
                session.params.update({
                    "t": qr_data["t"],
                    "ck": qr_data["ck"],
                })

                # 获取二维码内容
                qr_content = qr_data["codeContent"]
                session.qr_content = qr_content

                # 生成二维码图片（base64格式）
                qr = qrcode.QRCode(
                    version=5,
                    error_correction=qrcode.constants.ERROR_CORRECT_L,
                    box_size=10,
                    border=2,
                )
                qr.add_data(qr_content)
                qr.make()

                # 将二维码转换为base64
                from io import BytesIO
                import base64

                qr_img = qr.make_image()
                buffer = BytesIO()
                qr_img.save(buffer, format='PNG')
                qr_base64 = base64.b64encode(buffer.getvalue()).decode()
                qr_data_url = f"data:image/png;base64,{qr_base64}"

                session.qr_code_url = qr_data_url
                session.status = 'waiting'

                # 保存会话
                self.sessions[session_id] = session

                # 启动状态检查任务
                asyncio.create_task(self._monitor_qr_status(session_id))

                logger.info(f"二维码生成成功: {session_id}")
                return {
                    'success': True,
                    'session_id': session_id,
                    'qr_code_url': qr_data_url
                }
            else:
                raise GetLoginQRCodeError(results.get("content", {}).get("message") or "获取登录二维码失败")

        except Exception as e:
            logger.error(f"生成二维码失败: {e}")
            return {'success': False, 'message': f'生成二维码失败: {str(e)}'}
    
    async def _poll_qrcode_status(self, session: QRLoginSession) -> httpx.Response:
        """获取二维码扫描状态"""
        base_params = self._passport_common_params(session)
        base_params.update({
            "navlanguage": "zh-CN",
            "navUserAgent": self.headers["User-Agent"],
            "navPlatform": "Win32",
            "isIframe": "true",
            "documentReferer": "https://www.goofish.com/",
            "defaultView": "sms",
            "deviceId": session.cookies.get("cna", ""),
            "t": str(session.params.get("t", "")),
            "ck": session.params.get("ck", ""),
        })
        headers = {
            **self.headers,
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://passport.goofish.com",
            "Referer": "https://passport.goofish.com/mini_login.htm",
        }
        return await self._request(
            session,
            "POST",
            f"{self.api_scan_status}?appName=xianyu&fromSite=77",
            data=base_params,
            headers=headers,
        )

    async def _complete_login_with_token(self, session: QRLoginSession, login_token: str):
        """Use the QR login token to complete passport login."""
        headers = {
            **self.headers,
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://passport.goofish.com",
            "Referer": "https://passport.goofish.com/mini_login.htm",
        }
        await self._request(
            session,
            "POST",
            self.api_login_by_token,
            params={
                "token": login_token,
                "subFlow": "DIALOG_CHECK_LOGIN_RPC",
                "nextCode": "0018",
                "bizScene": "qrcode",
                "confirm": "true",
            },
            data={"deviceId": session.cookies.get("cna", "")},
            headers=headers,
        )

    async def _refresh_login_mtop_cookie(self, session: QRLoginSession):
        """Refresh logged-in mtop cookies after passport login completes."""
        await self._prime_mtop_cookie(session, self.api_h5_nav, "mtop.idle.web.user.page.nav")

    async def _finalize_confirmed_login(self, session: QRLoginSession, qrcode_data: Dict[str, Any]):
        login_token = qrcode_data.get("token") or qrcode_data.get("lgToken") or qrcode_data.get("loginToken")
        if login_token:
            await self._complete_login_with_token(session, login_token)

        await self._refresh_login_mtop_cookie(session)
        session.unb = session.cookies.get("unb")
        if session.unb:
            session.status = 'success'
            logger.info(f"扫码登录成功: {session.session_id}, UNB: {session.unb}")
        else:
            session.status = 'verification_required'
            session.message = "扫码已确认，但未获取到完整登录Cookie，请完成风控验证后重试"
            logger.warning(f"扫码已确认但Cookie不完整: {session.session_id}")

    async def _monitor_qr_status(self, session_id: str):
        """监控二维码状态"""
        try:
            session = self.sessions.get(session_id)
            if not session:
                return

            logger.info(f"开始监控二维码状态: {session_id}")

            # 监控登录状态
            max_wait_time = 300  # 5分钟
            start_time = time.time()

            while time.time() - start_time < max_wait_time:
                try:
                    # 检查会话是否还存在
                    if session_id not in self.sessions:
                        break

                    # 轮询二维码状态
                    resp = await self._poll_qrcode_status(session)
                    qrcode_data = resp.json().get("content", {}).get("data", {})
                    qrcode_status = qrcode_data.get("qrCodeStatus")

                    if qrcode_status == "CONFIRMED":
                        # 登录确认
                        if (
                            qrcode_data.get("iframeRedirect")
                            is True
                        ):
                            # 账号被风控，需要手机验证
                            session.status = 'verification_required'
                            iframe_url = qrcode_data.get("iframeRedirectUrl")
                            session.verification_url = iframe_url
                            logger.warning(f"账号被风控，需要手机验证: {session_id}")
                            break
                        else:
                            await self._finalize_confirmed_login(session, qrcode_data)
                            break

                    elif qrcode_status == "NEW":
                        # 二维码未被扫描，继续轮询
                        continue

                    elif qrcode_status == "EXPIRED":
                        # 二维码已过期
                        session.status = 'expired'
                        logger.info(f"二维码已过期: {session_id}")
                        break

                    elif qrcode_status in ("SCANED", "SCANNED"):
                        # 二维码已被扫描，等待确认
                        if session.status == 'waiting':
                            session.status = 'scanned'
                            logger.info(f"二维码已扫描，等待确认: {session_id}")
                    else:
                        # 用户取消确认
                        session.status = 'cancelled'
                        logger.info(f"用户取消登录: {session_id}")
                        break

                    await asyncio.sleep(0.8)  # 每0.8秒检查一次

                except Exception as e:
                    logger.error(f"监控二维码状态异常: {e}")
                    await asyncio.sleep(2)

            # 超时处理
            if session.status not in ['success', 'expired', 'cancelled', 'verification_required']:
                session.status = 'expired'
                logger.info(f"二维码监控超时，标记为过期: {session_id}")

        except Exception as e:
            logger.error(f"监控二维码状态失败: {e}")
            if session_id in self.sessions:
                self.sessions[session_id].status = 'expired'
    
    def get_session_status(self, session_id: str, owner_user_id: Optional[int] = None) -> Dict[str, Any]:
        """获取会话状态"""
        session = self.sessions.get(session_id)
        if not session:
            return {'status': 'not_found'}
        if session.owner_user_id is not None and owner_user_id is not None and session.owner_user_id != owner_user_id:
            return {'status': 'not_found'}

        if session.is_expired() and session.status != 'success':
            session.status = 'expired'

        result = {
            'status': session.status,
            'session_id': session_id
        }

        # 如果需要验证，返回验证URL
        if session.status == 'verification_required' and session.verification_url:
            result['verification_url'] = session.verification_url
            result['message'] = '账号被风控，需要手机验证'
        elif session.message:
            result['message'] = session.message

        if session.status == 'success' and session.account_info:
            result['account_info'] = session.account_info

        return result

    def get_session_account_info(self, session_id: str) -> Optional[Dict[str, Any]]:
        """获取已处理过的账号保存结果"""
        session = self.sessions.get(session_id)
        if session and session.status == 'success':
            return session.account_info
        return None

    def set_session_account_info(self, session_id: str, account_info: Dict[str, Any]) -> None:
        """记录扫码成功后的账号保存结果，避免前端轮询重复保存。"""
        session = self.sessions.get(session_id)
        if session:
            session.account_info = account_info

    def cleanup_expired_sessions(self):
        """清理过期会话"""
        expired_sessions = []
        for session_id, session in self.sessions.items():
            if session.is_expired():
                expired_sessions.append(session_id)

        for session_id in expired_sessions:
            del self.sessions[session_id]
            logger.info(f"清理过期会话: {session_id}")

    def get_session_cookies(self, session_id: str) -> Optional[Dict[str, str]]:
        """获取会话Cookie"""
        session = self.sessions.get(session_id)
        if session and session.status == 'success':
            return {
                'cookies': self._cookie_marshal(session.cookies),
                'unb': session.unb
            }
        return None


# 全局二维码登录管理器实例
qr_login_manager = QRLoginManager()
