#!/usr/bin/env python3
"""
本地浏览器扫码登录并导入闲鱼 Cookie。

该模块只在本机读取浏览器 Cookie jar，并由后端保存到数据库；
不会把明文 Cookie 返回给前端。
"""

import asyncio
import ctypes
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Optional, Dict, Any

from loguru import logger

try:
    from playwright.async_api import async_playwright, BrowserContext
except Exception:
    async_playwright = None
    BrowserContext = Any


GOOFISH_PERSONAL_URL = "https://www.goofish.com/personal?spm=a21ybx.home.nav.1.4c053da6J3DDgP"
COOKIE_URLS = [
    "https://www.goofish.com",
    "https://goofish.com",
    "https://h5api.m.goofish.com",
    "https://passport.goofish.com",
    "https://taobao.com",
    "https://login.taobao.com",
]


class BrowserCookieLoginSession:
    def __init__(self, session_id: str, owner_user_id: Optional[int] = None):
        self.session_id = session_id
        self.owner_user_id = owner_user_id
        self.status = "starting"  # starting, waiting, success, expired, cancelled, error
        self.message = "正在启动本地浏览器"
        self.created_time = time.time()
        self.expire_time = 600
        self.cookies = None
        self.unb = None
        self.account_info = None
        self.profile_dir: Optional[Path] = None
        self.task: Optional[asyncio.Task] = None
        self.context: Optional[BrowserContext] = None
        self.playwright = None

    def is_expired(self) -> bool:
        return time.time() - self.created_time > self.expire_time


class BrowserCookieLoginManager:
    def __init__(self):
        self.sessions: Dict[str, BrowserCookieLoginSession] = {}
        self.profile_root = Path("browser_profiles/goofish_login").resolve()

    async def start_login(self, owner_user_id: Optional[int] = None) -> Dict[str, Any]:
        if async_playwright is None:
            return {
                "success": False,
                "message": "Playwright 未安装，无法启动本地浏览器",
            }

        session_id = str(uuid.uuid4())
        session = BrowserCookieLoginSession(session_id, owner_user_id)
        self.sessions[session_id] = session
        session.task = asyncio.create_task(self._run_browser_login(session))

        return {
            "success": True,
            "session_id": session_id,
            "message": "已启动本地浏览器，请在浏览器中扫码登录闲鱼",
        }

    async def _launch_context(self, session: BrowserCookieLoginSession):
        session.profile_dir = self.profile_root / session.session_id
        session.profile_dir.mkdir(parents=True, exist_ok=True)
        browser_args = [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            "--start-maximized",
            "--window-position=60,40",
            "--window-size=1280,900",
        ]
        context_kwargs = {
            "headless": False,
            "args": browser_args,
            "no_viewport": True,
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/138.0.0.0 Safari/537.36"
            ),
        }

        session.playwright = await async_playwright().start()
        try:
            return await session.playwright.chromium.launch_persistent_context(
                str(session.profile_dir),
                channel="chrome",
                **context_kwargs,
            )
        except Exception as chrome_error:
            logger.warning(f"启动系统 Chrome 失败，尝试使用 Playwright Chromium: {chrome_error}")
            return await session.playwright.chromium.launch_persistent_context(
                str(session.profile_dir),
                **context_kwargs,
            )

    async def _run_browser_login(self, session: BrowserCookieLoginSession):
        try:
            session.context = await self._launch_context(session)
            page = await session.context.new_page()
            session.status = "waiting"
            session.message = "浏览器已打开，请在浏览器中扫码登录闲鱼"

            try:
                await page.set_content(
                    """
                    <html>
                    <head><title>闲鱼扫码登录 - 正在打开</title></head>
                    <body style="font-family: sans-serif; padding: 32px;">
                    <h2>正在打开闲鱼登录页面...</h2>
                    <p>如果页面没有自动跳转，请保持这个窗口打开。</p>
                    </body>
                    </html>
                    """
                )
                await page.bring_to_front()
                await page.goto(GOOFISH_PERSONAL_URL, wait_until="domcontentloaded", timeout=45000)
                await page.bring_to_front()
                await page.evaluate("window.moveTo(60, 40); window.resizeTo(1280, 900); window.focus();")
                self._raise_browser_window(session)

                for old_page in list(session.context.pages):
                    if old_page != page and old_page.url == "about:blank":
                        await old_page.close()
            except Exception as goto_error:
                session.status = "error"
                session.message = "本地浏览器已启动，但打开闲鱼页面失败，请查看后端日志"
                logger.warning(f"打开闲鱼个人页失败: {goto_error}")
                await self._close_session_browser(session)
                return

            while not session.is_expired() and session.status not in ("cancelled", "success"):
                cookie_string, unb = await self._read_cookie_string(session.context)
                if unb and cookie_string:
                    await asyncio.sleep(2)
                    cookie_string, unb = await self._read_cookie_string(session.context)
                    session.cookies = cookie_string
                    session.unb = unb
                    session.status = "success"
                    session.message = "浏览器登录成功，已获取 Cookie"
                    logger.info(f"本地浏览器扫码登录成功: {session.session_id}, UNB: {unb}")
                    await self._close_session_browser(session)
                    return

                await asyncio.sleep(2)

            if session.status == "cancelled":
                await self._close_session_browser(session)
                return

            if session.status != "success":
                session.status = "expired"
                session.message = "浏览器登录超时，请重新发起导入"
                await self._close_session_browser(session)

        except Exception as e:
            session.status = "error"
            session.message = self._friendly_error_message(str(e))
            logger.error(f"本地浏览器扫码登录失败: {session.session_id}, {e}")
            await self._close_session_browser(session)

    async def _read_cookie_string(self, context: BrowserContext):
        cookies = await context.cookies(COOKIE_URLS)
        cookie_map = {}
        for cookie in cookies:
            domain = cookie.get("domain", "")
            if "goofish.com" not in domain and "taobao.com" not in domain:
                continue
            name = cookie.get("name")
            value = cookie.get("value")
            if name and value:
                cookie_map[name] = value

        unb = cookie_map.get("unb")
        cookie_string = "; ".join(f"{key}={value}" for key, value in cookie_map.items())
        return cookie_string, unb

    def _friendly_error_message(self, message: str) -> str:
        if "Executable doesn't exist" in message or "playwright install" in message:
            return "Playwright 浏览器未安装，请执行: .venv/bin/python -m playwright install chromium"
        if "Missing X server" in message or "looks like you launched a headed browser" in message:
            return "当前环境无法打开图形浏览器，请在桌面环境运行，或配置 DISPLAY"
        return "本地浏览器登录失败，请查看后端日志"

    def _raise_browser_window(self, session: BrowserCookieLoginSession) -> None:
        """Ask the X11 window manager to activate and keep the login window above."""
        if not session.profile_dir or not os.environ.get("DISPLAY"):
            return

        window_id = self._find_browser_window_id(session.profile_dir)
        if not window_id:
            logger.warning(f"未找到本地浏览器窗口，profile: {session.profile_dir}")
            return

        try:
            self._x11_activate_window(window_id)
            logger.info(f"已置顶本地浏览器扫码窗口: {hex(window_id)}")
        except Exception as e:
            logger.warning(f"置顶本地浏览器窗口失败: {e}")

    def _find_browser_window_id(self, profile_dir: Path) -> Optional[int]:
        try:
            result = subprocess.run(
                ["xwininfo", "-root", "-tree"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
        except Exception as e:
            logger.warning(f"读取X11窗口列表失败: {e}")
            return None

        profile_text = str(profile_dir)
        for line in result.stdout.splitlines():
            if profile_text not in line:
                continue
            match = re.search(r"0x[0-9a-fA-F]+", line)
            if match:
                return int(match.group(0), 16)
        return None

    def _x11_activate_window(self, window_id: int) -> None:
        class XClientMessageData(ctypes.Union):
            _fields_ = [
                ("b", ctypes.c_char * 20),
                ("s", ctypes.c_short * 10),
                ("l", ctypes.c_long * 5),
            ]

        class XClientMessageEvent(ctypes.Structure):
            _fields_ = [
                ("type", ctypes.c_int),
                ("serial", ctypes.c_ulong),
                ("send_event", ctypes.c_int),
                ("display", ctypes.c_void_p),
                ("window", ctypes.c_ulong),
                ("message_type", ctypes.c_ulong),
                ("format", ctypes.c_int),
                ("data", XClientMessageData),
            ]

        class XEvent(ctypes.Union):
            _fields_ = [
                ("xclient", XClientMessageEvent),
                ("pad", ctypes.c_long * 24),
            ]

        libx11 = ctypes.cdll.LoadLibrary("libX11.so.6")
        libx11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        libx11.XOpenDisplay.restype = ctypes.c_void_p
        libx11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        libx11.XDefaultRootWindow.restype = ctypes.c_ulong
        libx11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        libx11.XInternAtom.restype = ctypes.c_ulong
        libx11.XSendEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.c_long,
            ctypes.POINTER(XEvent),
        ]
        libx11.XMapRaised.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        libx11.XRaiseWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        libx11.XFlush.argtypes = [ctypes.c_void_p]

        display = libx11.XOpenDisplay(None)
        if not display:
            raise RuntimeError("无法连接X11 DISPLAY")

        root = libx11.XDefaultRootWindow(display)
        substructure_redirect_mask = 1 << 20
        substructure_notify_mask = 1 << 19
        client_message = 33

        def atom(name: str) -> int:
            return libx11.XInternAtom(display, name.encode("utf-8"), 0)

        messages = [
            ("_NET_WM_STATE", [1, atom("_NET_WM_STATE_ABOVE"), 0, 1, 0]),
            ("_NET_ACTIVE_WINDOW", [1, 0, 0, 0, 0]),
        ]
        for message_type, data in messages:
            event = XEvent()
            event.xclient.type = client_message
            event.xclient.display = display
            event.xclient.window = window_id
            event.xclient.message_type = atom(message_type)
            event.xclient.format = 32
            for index, value in enumerate(data):
                event.xclient.data.l[index] = value
            libx11.XSendEvent(
                display,
                root,
                0,
                substructure_redirect_mask | substructure_notify_mask,
                ctypes.byref(event),
            )

        libx11.XMapRaised(display, window_id)
        libx11.XRaiseWindow(display, window_id)
        libx11.XFlush(display)

    def get_session_status(self, session_id: str, owner_user_id: Optional[int] = None) -> Dict[str, Any]:
        session = self.sessions.get(session_id)
        if not session:
            return {"status": "not_found", "message": "浏览器登录会话不存在"}
        if session.owner_user_id is not None and owner_user_id is not None and session.owner_user_id != owner_user_id:
            return {"status": "not_found", "message": "浏览器登录会话不存在"}

        if session.is_expired() and session.status not in ("success", "error", "cancelled"):
            session.status = "expired"
            session.message = "浏览器登录超时，请重新发起导入"

        result = {
            "status": session.status,
            "session_id": session.session_id,
            "message": session.message,
        }
        if session.account_info:
            result["account_info"] = session.account_info
        return result

    def get_session_cookies(self, session_id: str) -> Optional[Dict[str, str]]:
        session = self.sessions.get(session_id)
        if session and session.status == "success" and session.cookies and session.unb:
            return {
                "cookies": session.cookies,
                "unb": session.unb,
            }
        return None

    def get_session_account_info(self, session_id: str) -> Optional[Dict[str, Any]]:
        session = self.sessions.get(session_id)
        if session and session.status == "success":
            return session.account_info
        return None

    def set_session_account_info(self, session_id: str, account_info: Dict[str, Any]) -> None:
        session = self.sessions.get(session_id)
        if session:
            session.account_info = account_info

    async def cancel_session(self, session_id: str, owner_user_id: Optional[int] = None) -> Dict[str, Any]:
        session = self.sessions.get(session_id)
        if not session:
            return {"success": False, "message": "浏览器登录会话不存在"}
        if session.owner_user_id is not None and owner_user_id is not None and session.owner_user_id != owner_user_id:
            return {"success": False, "message": "浏览器登录会话不存在"}

        session.status = "cancelled"
        session.message = "已取消浏览器登录"
        if session.task and not session.task.done():
            session.task.cancel()
        await self._close_session_browser(session)
        return {"success": True, "message": "已取消浏览器登录"}

    async def _close_session_browser(self, session: BrowserCookieLoginSession):
        try:
            if session.context:
                await session.context.close()
                session.context = None
        except Exception as e:
            logger.warning(f"关闭浏览器上下文失败: {e}")

        try:
            if session.playwright:
                await session.playwright.stop()
                session.playwright = None
        except Exception as e:
            logger.warning(f"停止 Playwright 失败: {e}")

        if session.profile_dir:
            try:
                shutil.rmtree(session.profile_dir, ignore_errors=True)
            except Exception as e:
                logger.warning(f"清理浏览器临时目录失败: {e}")
            finally:
                session.profile_dir = None

    def cleanup_expired_sessions(self):
        expired = []
        for session_id, session in self.sessions.items():
            if session.is_expired() and session.status in ("expired", "error", "cancelled", "success"):
                expired.append(session_id)
        for session_id in expired:
            del self.sessions[session_id]


browser_cookie_login_manager = BrowserCookieLoginManager()
