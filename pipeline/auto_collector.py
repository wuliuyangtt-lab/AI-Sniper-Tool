#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全平台采集指挥官 (Industrial v2.0)
实现被动网络拦截 + 主动滚动触发架构，支持 24h+ 无人值守、百万级数据采集

依赖:
    - playwright>=1.40.0
    - playwright-stealth>=1.0.0
    - asyncio (Python 3.7+)
    - pybloom-live>=1.2.0 (可选，用于大数据量去重)
    - structlog>=23.0.0 (可选，用于结构化日志)
"""

import os
import re
import sys
import time
import json
import random
import asyncio
import tempfile
import traceback
import shutil
import logging
from pathlib import Path
from typing import List, Optional, Dict, Set, Any
from urllib.parse import urlparse
from datetime import datetime
from dataclasses import dataclass, field
from enum import Enum

# 导入 UI 组件
try:
    from pathlib import Path as _P
    sys.path.insert(0, str(_P(__file__).parent / "downloader"))
    from cli.progress_display import ProgressDisplay
    UI_AVAILABLE = True
except Exception:
    UI_AVAILABLE = False
    print("[Warning] 未找到 ProgressDisplay 模块，将使用标准日志输出")

# 尝试导入可选依赖
try:
    from playwright.async_api import async_playwright, Page, Browser, BrowserContext, Route, Response
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    print("[Error] 未安装 playwright，请运行: pip install playwright")

try:
    from playwright_stealth import Stealth
    STEALTH_AVAILABLE = True
except ImportError:
    STEALTH_AVAILABLE = False
    print("[Warning] 未安装 playwright-stealth，请运行: pip install playwright-stealth")

try:
    from pybloom_live import ScalableBloomFilter
    BLOOM_AVAILABLE = True
except ImportError:
    BLOOM_AVAILABLE = False

try:
    import structlog
    STRUCTLOG_AVAILABLE = True
except ImportError:
    STRUCTLOG_AVAILABLE = False

# ================= 配置与路径 =================
WORKDIR = Path(__file__).parent.absolute()
HISTORY_FILE = WORKDIR / "processed_history.txt"
STORAGE_STATE_PATH = WORKDIR / "storage_state.json"
USER_DATA_DIR = WORKDIR / "pw_chrome_profile"

# ================= 配置类 =================
class CollectMode(Enum):
    """采集模式"""
    FULL = "full"          # 全量模式：滚动到页面底部
    INCREMENTAL = "incremental"  # 增量模式：遇到已处理内容即停止

@dataclass
class CollectorConfig:
    """采集器配置"""
    mode: CollectMode = CollectMode.INCREMENTAL
    max_items: int = 0
    stealth_level: int = 2
    
    # 滚动配置
    scroll_min: int = 800
    scroll_max: int = 1500
    scroll_interval_min: float = 1.8
    scroll_interval_max: float = 3.8
    
    # 网络与空闲检测
    network_idle_timeout: int = 10000  # 毫秒
    data_timeout: int = 30  # 秒，无新数据超时
    max_empty_rounds: int = 10  # 最大空转次数
    max_idle_time: int = 300  # 秒，最大总空闲时间（考虑到登录/验证）
    
    # 内存控制
    max_items_in_memory: int = 5000  # 内存中最大保存条数
    history_threshold: int = 5000000  # 历史记录阈值，超过则使用BloomFilter
    
    # 浏览器配置
    headless: bool = False
    user_agent: str = ""
    viewport_width: int = 1366
    viewport_height: int = 850
    proxy_server: str = ""
    
    # 重试配置
    max_retries: int = 3
    retry_delay: float = 5.0
    
    # 平台配置
    platform: str = "douyin"  # douyin, x
    
    def __post_init__(self):
        if not self.user_agent:
            self.user_agent = self._get_random_user_agent()
    
    def _get_random_user_agent(self) -> str:
        user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        ]
        return random.choice(user_agents)

# ================= 日志系统 =================
def setup_logging(level="INFO"):
    if STRUCTLOG_AVAILABLE:
        structlog.configure(
            processors=[
                structlog.stdlib.add_log_level,
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.JSONRenderer()
            ],
            context_class=dict,
            logger_factory=structlog.PrintLoggerFactory(),
        )
        return structlog.get_logger()
    else:
        logging.basicConfig(
            level=getattr(logging, level),
            format='%(asctime)s [%(levelname)s] %(message)s'
        )
        return logging.getLogger("collector")

logger = setup_logging()

# ================= 历史记录管理器 =================
class HistoryManager:
    """历史记录管理器，支持百万级 O(1) 去重"""
    
    def __init__(self, history_file: Path, threshold: int = 5000000):
        self.history_file = history_file
        self.threshold = threshold
        self._use_bloom = False
        self._history_set: Set[str] = set()
        self._bloom_filter: Optional[Any] = None
        self._lock = asyncio.Lock()
        self._load_history()
    
    def _load_history(self):
        if not self.history_file.exists():
            return
        
        try:
            with open(self.history_file, 'r', encoding='utf-8') as f:
                lines = [line.strip() for line in f if line.strip()]
            
            if len(lines) > self.threshold and BLOOM_AVAILABLE:
                self._use_bloom = True
                self._bloom_filter = ScalableBloomFilter(initial_capacity=len(lines)*2, error_rate=0.001)
                for line in lines:
                    self._bloom_filter.add(line)
                logger.info(f"history_loaded_bloom: count={len(lines)}")
            else:
                self._history_set = set(lines)
                logger.info(f"history_loaded_set: count={len(lines)}")
        except Exception as e:
            logger.error(f"history_load_failed: error={e}")

    async def contains(self, item_id: str) -> bool:
        async with self._lock:
            if self._use_bloom:
                return item_id in self._bloom_filter
            return item_id in self._history_set

    async def add(self, item_id: str):
        async with self._lock:
            if self._use_bloom:
                self._bloom_filter.add(item_id)
            else:
                self._history_set.add(item_id)

    async def save_history(self, new_ids: List[str]):
        if not new_ids:
            return
        async with self._lock:
            temp_file = self.history_file.with_suffix('.tmp')
            try:
                # 增量追加到文件
                with open(self.history_file, 'a', encoding='utf-8') as f:
                    for item_id in new_ids:
                        f.write(f"{item_id}\n")
                logger.info(f"history_saved: count={len(new_ids)}")
            except Exception as e:
                logger.error(f"history_save_failed: error={e}")

# ================= 网络拦截器 =================
class NetworkInterceptor:
    """健壮的网络拦截器：page.route + page.on('response')"""
    
    def __init__(self, config: CollectorConfig):
        self.config = config
        self.intercepted_items: List[Dict] = []
        self.seen_ids: Set[str] = set()
        self._lock = asyncio.Lock()
        self.last_data_time = time.time()
        self.total_intercepted = 0
        
        # 拦截模式配置
        self.api_patterns = {
            "douyin": [r'.*/aweme/v\d+/.*', r'.*/note/v\d+/.*', r'.*/feed/.*'],
            "x": [r'.*/graphql/.*', r'.*/api/v\d+/.*']
        }

    async def route_handler(self, route: Route):
        """必须继续请求，绝不阻塞"""
        try:
            await route.continue_()
        except Exception as e:
            logger.debug(f"route_continue_failed: error={e}")

    async def response_handler(self, response: Response):
        """处理响应数据"""
        try:
            # 基础过滤
            if response.status != 200:
                return
            
            url = response.url
            platform_patterns = self.api_patterns.get(self.config.platform, [])
            if not any(re.match(p, url) for p in platform_patterns):
                return
            
            # JSON 解析
            try:
                content_type = response.headers.get('content-type', '')
                if 'application/json' not in content_type:
                    return
                data = await response.json()
            except Exception:
                return # 跳过加密或非预期响应

            # 数据提取
            await self._parse_and_store(data, url)
            
        except Exception as e:
            logger.error(f"response_handler_error: error={e}")

    async def _parse_and_store(self, data: Dict, url: str):
        items = []
        if self.config.platform == "douyin":
            items = data.get('aweme_list', []) or data.get('items', [])
        elif self.config.platform == "x":
            # 深度查找 tweet 数据
            items = self._deep_extract_x_tweets(data)

        if not items:
            return

        async with self._lock:
            new_batch = []
            for item in items:
                item_id = str(item.get('aweme_id') or item.get('id') or item.get('tweet_id', ''))
                if not item_id or item_id in self.seen_ids:
                    continue
                
                self.seen_ids.add(item_id)
                processed_item = self._format_item(item, item_id)
                new_batch.append(processed_item)
                self.intercepted_items.append(processed_item)

            if new_batch:
                self.last_data_time = time.time()
                self.total_intercepted += len(new_batch)
                logger.info(f"intercepted_new_data: count={len(new_batch)}, total={self.total_intercepted}")

    def _format_item(self, item: Dict, item_id: str) -> Dict:
        if self.config.platform == "douyin":
            aweme_type = item.get('aweme_type', 0)
            share_url = item.get('share_url', '')
            if aweme_type in (68, 69) or '/note/' in share_url:
                ctype, link = 'article', f"https://www.douyin.com/note/{item_id}"
            elif aweme_type in (2, 51):
                ctype, link = 'image_suite', f"https://www.douyin.com/video/{item_id}"
            else:
                ctype, link = 'video', f"https://www.douyin.com/video/{item_id}"
            return {'id': item_id, 'url': link, 'type': ctype, 'desc': item.get('desc', '')}
        else:
            return {'id': item_id, 'url': f"https://x.com/i/status/{item_id}", 'type': 'x_post'}

    def _deep_extract_x_tweets(self, data: Any) -> List[Dict]:
        """递归提取 X 平台的 tweet 数据"""
        tweets = []
        if isinstance(data, dict):
            if data.get('__typename') in ('Tweet', 'TweetWithVisibilityResults'):
                return [data]
            if 'legacy' in data and 'id_str' in data.get('legacy', {}):
                return [data['legacy']]
            for v in data.values():
                tweets.extend(self._deep_extract_x_tweets(v))
        elif isinstance(data, list):
            for i in data:
                tweets.extend(self._deep_extract_x_tweets(i))
        return tweets

# ================= 采集指挥官 =================
class IndustrialCollector:
    """工业级采集器：被动拦截 + 主动触发"""
    
    def __init__(self, config: CollectorConfig):
        self.config = config
        self.history = HistoryManager(HISTORY_FILE, config.history_threshold)
        self.interceptor = NetworkInterceptor(config)
        self.accepted_items: List[Dict[str, Any]] = []
        
        self.pw = None
        self.browser = None
        self.context = None
        self.page = None
        self._stop_event = asyncio.Event()
        self.total_processed = 0
        self.scroll_y = 0
        
        # 初始化 UI
        self.ui = ProgressDisplay() if UI_AVAILABLE else None
        if self.ui:
            self.ui.show_banner()

    async def start(self, target_url: str):
        logger.info(f"collector_start: url={target_url}, mode={self.config.mode.value}")
        self._target_url = target_url
        
        if self.ui:
            self.ui.start_download_session(1)
            self.ui.start_url(1, 1, target_url)
            self.ui.advance_step("初始化", "正在启动浏览器...")
        
        while not self._stop_event.is_set():
            try:
                await self._setup_browser()
                
                # 初始跳转与恢复
                try:
                    await self.page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
                    
                    # 立即同步一次 Cookie
                    await self._sync_cookies()
                    
                    # 确保处于正确的页签
                    await self._ensure_correct_tab(target_url)
                    
                    if self.scroll_y > 0:
                        await self.page.evaluate(f"window.scrollTo(0, {self.scroll_y})")
                    
                    # 再次同步
                    await self._sync_cookies()
                except Exception as e:
                    if "Execution context was destroyed" in str(e):
                        logger.warning("initial_navigation_interrupted: retrying_in_loop")
                    else:
                        raise e
                
                await self._main_loop()
                break # 正常结束
            except Exception as e:
                logger.error(f"collector_crash: error={traceback.format_exc()}")
                await self._cleanup()
                await asyncio.sleep(self.config.retry_delay)
                logger.info(f"restarting_browser: scroll_y={self.scroll_y}")

    async def _setup_browser(self):
        self.pw = await async_playwright().start()
        
        args = [
            '--disable-blink-features=AutomationControlled',
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-dev-shm-usage',
            '--disable-extensions'
        ]
        
        launch_kwargs = {
            "headless": self.config.headless,
            "args": args,
        }
        if self.config.proxy_server:
            launch_kwargs["proxy"] = {"server": self.config.proxy_server}

        self.browser = None
        self.context = None
        self.page = None

        channels: List[str] = []
        env_channel = (os.environ.get("PW_CHANNEL") or "").strip().lower()
        if env_channel:
            channels.append(env_channel)
        channels.extend(["chrome", "msedge", ""])

        last_err: Optional[BaseException] = None

        USER_DATA_DIR.mkdir(exist_ok=True)
        for ch in channels:
            try:
                kwargs = dict(launch_kwargs)
                if ch:
                    kwargs["channel"] = ch
                self.context = await self.pw.chromium.launch_persistent_context(
                    user_data_dir=str(USER_DATA_DIR),
                    user_agent=self.config.user_agent,
                    viewport={'width': self.config.viewport_width, 'height': self.config.viewport_height},
                    ignore_https_errors=True,
                    **kwargs
                )
                break
            except Exception as e:
                last_err = e
                if "Target page, context or browser has been closed" in str(e) or "TargetClosedError" in str(type(e)):
                    continue
                break

        if self.context is None:
            for ch in channels:
                try:
                    kwargs = dict(launch_kwargs)
                    if ch:
                        kwargs["channel"] = ch
                    self.browser = await self.pw.chromium.launch(**kwargs)
                    context_kwargs = {
                        "user_agent": self.config.user_agent,
                        "viewport": {'width': self.config.viewport_width, 'height': self.config.viewport_height},
                        "ignore_https_errors": True,
                    }
                    if STORAGE_STATE_PATH.exists() and STORAGE_STATE_PATH.stat().st_size > 0:
                        context_kwargs["storage_state"] = str(STORAGE_STATE_PATH)
                    self.context = await self.browser.new_context(**context_kwargs)
                    break
                except Exception as e:
                    last_err = e
                    continue

        if self.context is None:
            raise last_err if last_err else RuntimeError("Failed to launch browser context")

        self.page = self.context.pages[0] if getattr(self.context, "pages", None) else None
        if not self.page:
            self.page = await self.context.new_page()
        if STEALTH_AVAILABLE:
            await Stealth().apply_stealth_async(self.page)
        
        # 挂载拦截器
        patterns = self.interceptor.api_patterns.get(self.config.platform, ["**/api/**"])
        for p in patterns:
            await self.page.route(p, self.interceptor.route_handler)
        self.page.on("response", self.interceptor.response_handler)
        
        # 监听 Cookie 变化并同步 (更加频繁地同步)
        self.page.on("loadstate", lambda _: asyncio.create_task(self._sync_cookies()))
        self.context.on("requestfinished", lambda _: asyncio.create_task(self._sync_cookies()))

    async def _sync_cookies(self):
        """同步浏览器 Cookie 到 Pipeline 和 Downloader"""
        try:
            # 增加一个内部标志，防止并发执行导致的文件竞争
            if hasattr(self, '_is_syncing') and self._is_syncing:
                return
            self._is_syncing = True
            
            from cookie_manager import EnhancedCookieManager
            now = time.time()
            last_attempt = getattr(self, "_last_cookie_sync_attempt_time", 0.0)
            if now - last_attempt < 2.5:
                self._is_syncing = False
                return
            self._last_cookie_sync_attempt_time = now

            cm = EnhancedCookieManager(WORKDIR, quiet=True)
            playwright_cookies = await self.context.cookies()
            
            if not playwright_cookies:
                self._is_syncing = False
                return

            auth_keys = {
                "sessionid",
                "sessionid_ss",
                "sid_guard",
                "sid_tt",
                "passport_csrf_token",
                "msToken",
                "ttwid",
                "odin_tt",
                "n_mh",
            }
            cookie_map = {c.get("name", ""): c.get("value", "") for c in playwright_cookies if isinstance(c, dict)}
            fp = tuple(sorted((k, cookie_map.get(k, "")) for k in auth_keys))
            last_fp = getattr(self, "_last_cookie_fingerprint", None)
            last_io = getattr(self, "_last_cookie_io_sync_time", 0.0)
            changed = (fp != last_fp)
            if not changed and now - last_io < 30:
                self._is_syncing = False
                return

            self._last_cookie_fingerprint = fp
            self._last_cookie_io_sync_time = now

            cm.save_playwright_cookies(playwright_cookies)
            cm.update_downloader_config()

            last_storage = getattr(self, "_last_storage_state_time", 0.0)
            if changed or now - last_storage > 120:
                await self.context.storage_state(path=str(STORAGE_STATE_PATH))
                self._last_storage_state_time = now
            
            # 只在第一次或关键时刻打日志，避免刷屏
            if not hasattr(self, '_last_sync_time') or time.time() - self._last_sync_time > 60:
                logger.info(f"cookies_synced: count={len(playwright_cookies)}")
                self._last_sync_time = time.time()
            
            self._is_syncing = False
        except Exception as e:
            self._is_syncing = False
            # 忽略一些常见的非致命错误
            msg = str(e)
            if "Target closed" not in msg and "TargetClosedError" not in msg:
                logger.error(f"cookie_sync_failed: error={e}")

    async def _ensure_correct_tab(self, target_url: str):
        """确保处于正确的页签（针对抖音收藏）"""
        if "douyin.com" in target_url:
            try:
                # 等待页面主体加载完成
                await self.page.wait_for_selector(".tab-content, .user-info", timeout=10000)
                
                # 如果是收藏相关链接，强制点击收藏页签
                if "showTab=favorite_collection" in target_url or "showTab=collect" in target_url:
                    # 使用更通用的选择器，抖音收藏页签通常包含 "收藏" 文本
                    collect_tab = self.page.locator("text=收藏").first
                    
                    if await collect_tab.count() > 0:
                        # 检查是否已经被选中（通过是否有特定 active 类来判断，如果没有就点一下）
                        class_name = await collect_tab.get_attribute("class") or ""
                        if "active" not in class_name.lower():
                            await collect_tab.click()
                            await self.page.wait_for_timeout(2000)
                            logger.info("tab_navigation: forced_click_collect_tab")
                    else:
                        logger.warning("tab_navigation: collect_tab_not_found")
            except Exception as e:
                logger.debug(f"tab_navigation_failed: {e}")

    async def _smart_scroll_once(self, dy: int) -> Dict[str, Any]:
        try:
            try:
                await self.page.evaluate("document.activeElement && document.activeElement.blur && document.activeElement.blur()")
            except Exception:
                pass
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass
            await self.page.mouse.wheel(0, dy)
            state = await self.page.evaluate(
                """(dy) => {
                    const root = document.scrollingElement || document.documentElement || document.body;
                    const candidates = [];
                    if (root) candidates.push(root);
                    const nodes = Array.from(document.querySelectorAll('div'));
                    for (const el of nodes) {
                        if (!el) continue;
                        const sh = el.scrollHeight || 0;
                        const ch = el.clientHeight || 0;
                        if (sh <= ch + 200) continue;
                        const r = el.getBoundingClientRect();
                        if (!r || r.height < 350 || r.width < 350) continue;
                        candidates.push(el);
                    }
                    let best = root;
                    let bestScore = 0;
                    for (const el of candidates) {
                        const sh = el.scrollHeight || 0;
                        const ch = el.clientHeight || 0;
                        const score = Math.max(0, sh - ch);
                        if (score > bestScore) {
                            bestScore = score;
                            best = el;
                        }
                    }
                    const before = best ? (best.scrollTop || 0) : 0;
                    if (best) {
                        if (best.scrollBy) {
                            best.scrollBy(0, dy);
                        } else {
                            best.scrollTop = before + dy;
                        }
                    } else {
                        window.scrollBy(0, dy);
                    }
                    const y = best ? (best.scrollTop || 0) : (window.scrollY || 0);
                    const vh = best ? (best.clientHeight || 0) : (window.innerHeight || 0);
                    const h = best ? (best.scrollHeight || 0) : (document.body ? (document.body.scrollHeight || 0) : 0);
                    const maxY = Math.max(0, h - vh);
                    const moved = Math.abs(y - before) >= 2;
                    const use = (best === root) ? "root" : "custom";
                    return { moved, y, h, vh, maxY, use };
                }""",
                dy,
            )
            return state if isinstance(state, dict) else {"moved": True, "y": self.scroll_y, "h": 0, "vh": 0, "maxY": 0, "use": "unknown"}
        except Exception as e:
            msg = str(e)
            if "Execution context was destroyed" in msg or "Navigation" in msg:
                return {"moved": True, "y": self.scroll_y, "h": 0, "vh": 0, "maxY": 0, "use": "nav"}
            raise

    async def _main_loop(self):
        empty_rounds = 0
        last_count = 0
        stuck_rounds = 0
        last_progress_log = 0.0
        
        while not self._stop_event.is_set():
            try:
                if self.config.platform == "douyin":
                    try:
                        u = (self.page.url or "").lower()
                    except Exception:
                        u = ""
                    if "douyin.com/video/" in u or "douyin.com/note/" in u:
                        logger.warning(f"detail_page_detected: url={self.page.url}")
                        try:
                            await self.page.goto(getattr(self, "_target_url", "https://www.douyin.com/user/self"), wait_until="domcontentloaded", timeout=60000)
                            await self._ensure_correct_tab(getattr(self, "_target_url", ""))
                            await self.page.wait_for_timeout(1200)
                        except Exception:
                            pass
                        continue

                # 0. 检查页面是否稳定（处理验证码/跳转）
                if await self._is_verification_page():
                    logger.info("verification_detected: pausing_automation")
                    await self.page.wait_for_timeout(5000)
                    continue

                # 1. 主动滚动
                dy = random.randint(self.config.scroll_min, self.config.scroll_max)
                
                # 如果连续多轮没有新数据或卡住，尝试往回滚一点以触发重绘
                if empty_rounds > 3 or stuck_rounds > 3:
                    if self.ui:
                        self.ui.update_step("滚动扫描", f"检测到卡顿，正在尝试回滚机动... (y={self.scroll_y})")
                    await self._smart_scroll_once(-dy // 2)
                    await self.page.wait_for_timeout(1000)
                    # 模拟鼠标移动
                    await self.page.mouse.move(random.randint(100, 800), random.randint(100, 600))
                
                scroll_state = await self._smart_scroll_once(dy)
                self.scroll_y = int(scroll_state.get("y") or 0)
                
                if self.ui:
                    self.ui.update_step("滚动扫描", f"已捕获: {self.interceptor.total_intercepted}, 已处理: {self.total_processed}, y={self.scroll_y}")
                
                if scroll_state.get("moved") is False:
                    stuck_rounds += 1
                else:
                    stuck_rounds = 0
                
                # 2. 自适应等待
                await self._adaptive_sleep()
                
                # 3. 处理数据
                async with self.interceptor._lock:
                    current_items = list(self.interceptor.intercepted_items)
                
                if len(current_items) > last_count:
                    # 4. 提取新数据
                    new_items = current_items[last_count:]
                    # 无论是否有有效数据，都必须推进 last_count，防止死循环
                    last_count = len(current_items)
                    
                    # 精准增量截断与过滤
                    cutoff_items, reached_history = await self._apply_cutoff(new_items)
                    
                    if cutoff_items:
                        await self._save_and_clean(cutoff_items)
                        self.total_processed += len(cutoff_items)
                        # 发现新数据时同步一次 Cookie，因为可能携带了新鲜的鉴权信息
                        await self._sync_cookies()
                    
                    if reached_history and self.config.mode == CollectMode.INCREMENTAL:
                        logger.info("incremental_cutoff_reached")
                        break
                    
                    empty_rounds = 0
                else:
                    empty_rounds += 1

                # 5. 防假触底与空闲检测
                idle_time = time.time() - self.interceptor.last_data_time
                now = time.time()
                if now - last_progress_log > 20:
                    logger.info(
                        "scan_progress: total_intercepted=%d, processed=%d, empty_rounds=%d, stuck_rounds=%d, idle_time=%.1f, scroll_y=%s, scroll_h=%s, use=%s",
                        self.interceptor.total_intercepted,
                        self.total_processed,
                        empty_rounds,
                        stuck_rounds,
                        idle_time,
                        scroll_state.get("y"),
                        scroll_state.get("h"),
                        scroll_state.get("use"),
                    )
                    last_progress_log = now
                
                if idle_time > self.config.data_timeout and empty_rounds >= self.config.max_empty_rounds:
                    if self.config.mode == CollectMode.FULL:
                        y = float(scroll_state.get("y") or 0)
                        maxY = float(scroll_state.get("maxY") or 0)
                        at_bottom = (maxY <= 0) or (y >= maxY - 80)
                        if at_bottom or stuck_rounds >= max(12, self.config.max_empty_rounds):
                            logger.info(f"page_end_detected: idle_time={idle_time:.2f}, rounds={empty_rounds}")
                            break
                    else:
                        if self.total_processed > 0:
                            logger.info(f"page_end_detected: idle_time={idle_time:.2f}, rounds={empty_rounds}")
                            break
                
                if idle_time > self.config.max_idle_time:
                    # 如果已经拦截到一些数据，不视为超时失败，而是正常结束
                    if self.interceptor.total_intercepted > 0:
                        logger.info(f"collection_finished: total_intercepted={self.interceptor.total_intercepted}, max_idle_reached")
                    else:
                        logger.info(f"max_idle_timeout_reached: total_processed={self.total_processed}, idle_time={idle_time:.2f}s")
                    break

                # 内存控制
                if len(self.interceptor.seen_ids) > self.config.max_items_in_memory:
                    async with self.interceptor._lock:
                        self.interceptor.intercepted_items = self.interceptor.intercepted_items[-100:]
                        last_count = len(self.interceptor.intercepted_items)
                        self.interceptor.seen_ids.clear()
            
            except Exception as e:
                error_msg = str(e)
                if "Target page, context or browser has been closed" in error_msg or "TargetClosedError" in error_msg:
                    logger.warning("browser_closed_detected: stopping_collector")
                    self._stop_event.set()
                    break
                
                if "Execution context was destroyed" in error_msg or "Navigation" in error_msg:
                    logger.warning("navigation_detected: waiting_for_stable")
                    try:
                        await self.page.wait_for_timeout(3000)
                    except:
                        pass
                    continue
                else:
                    raise e

    async def _is_verification_page(self) -> bool:
        """识别是否处于验证码或登录跳转页"""
        try:
            url = self.page.url.lower()
            verify_keywords = ['captcha', 'verify', 'login', 'punish', 'risk']
            return any(k in url for k in verify_keywords)
        except:
            return False

    async def _adaptive_sleep(self):
        """根据数据频率调整滚动间隔"""
        try:
            idle_time = time.time() - self.interceptor.last_data_time
            if idle_time < 5: # 数据多，慢点滚
                sleep_time = random.uniform(3.0, 5.0)
            else: # 数据少，快点找
                sleep_time = random.uniform(1.8, 3.8)
            
            await self.page.wait_for_timeout(int(sleep_time * 1000))
            try:
                await self.page.wait_for_load_state("networkidle", timeout=2000)
            except:
                pass
        except Exception as e:
            if "TargetClosedError" not in str(type(e)) and "Target page, context or browser has been closed" not in str(e):
                logger.debug(f"adaptive_sleep_failed: {e}")
            raise e

    async def _apply_cutoff(self, items: List[Dict]) -> (List[Dict], bool):
        """应用增量截断或全量过滤"""
        valid = []
        if self.config.mode == CollectMode.FULL:
            # 全量模式：仅过滤已处理的，但不截断后续数据
            for item in items:
                if not await self.history.contains(item['id']):
                    valid.append(item)
            return valid, False
        else:
            # 增量模式：遇到第一个已处理的 ID 就立即截断并标记结束
            for item in items:
                if await self.history.contains(item['id']):
                    return valid, True
                valid.append(item)
            return valid, False

    async def _save_and_clean(self, items: List[Dict]):
        ids = [i['id'] for i in items]
        await self.history.save_history(ids)
        self.accepted_items.extend(items)
        last_url = items[-1].get("url") if items else ""
        logger.info(f"tasks_accepted: batch={len(items)}, total_accepted={len(self.accepted_items)}, last={last_url}")

    async def _cleanup(self):
        try:
            if self.page: await self.page.close()
            if self.context: await self.context.close()
            if self.browser: await self.browser.close()
            if self.pw: await self.pw.stop()
        except:
            pass

    async def stop(self):
        self._stop_event.set()
        await self._cleanup()

# ================= 任务路由器 =================
class TaskRouter:
    """任务路由器"""
    def __init__(self):
        try:
            from pipeline import VideoProcessor, ImageSuiteProcessor, ArticleProcessor, XTextImageProcessor
            self.processors = {
                'video': VideoProcessor(),
                'image_suite': ImageSuiteProcessor(),
                'article': ArticleProcessor(),
                'x_text_image': XTextImageProcessor(),
            }
        except Exception as e:
            logger.error(f"load_processors_failed: error={e}")
            self.processors = {}
    
    def get_processor(self, content_type: str):
        return self.processors.get(content_type)
    
    def route_by_url(self, url: str) -> str:
        domain = urlparse(url).netloc.lower()
        if "x.com" in domain or "twitter.com" in domain:
            return 'x_text_image'
        elif "douyin.com" in domain or "iesdouyin.com" in domain:
            if "/note/" in url or "/photo/" in url:
                return 'image_suite'
            elif "/article/" in url:
                return 'article'
            # 明确返回 video，避免混淆
            return 'video'
        return 'video'

# 导入配置
import sys
import os
# 确保先导入根目录的config.py
sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))
# 清除可能存在的config模块缓存
if 'config' in sys.modules:
    del sys.modules['config']
from config import ALLM_URL, ALLM_API_KEY, ALLM_WORKSPACE

# ================= AnythingLLM 闭环 =================
class AnythingLLMClient:
    def __init__(self):
        self.base_url = ALLM_URL.rstrip("/api/v1")
        self.api_key = ALLM_API_KEY
        self.workspace_name = ALLM_WORKSPACE
        self.enabled = bool(self.api_key)
        import requests
        self.session = requests.Session()
        if self.enabled:
            self.session.headers.update({"Authorization": f"Bearer {self.api_key}"})

    def find_workspace_slug(self) -> str:
        if not self.enabled: return self.workspace_name
        try:
            r = self.session.get(f"{self.base_url}/api/v1/workspaces", timeout=10)
            if r.status_code == 200:
                workspaces = r.json().get("workspaces", [])
                for w in workspaces:
                    if w.get("name") == self.workspace_name:
                        return w.get("slug")
        except: pass
        return self.workspace_name

    def upload_markdown(self, md_path: str) -> str:
        if not self.enabled: return ""
        try:
            import requests
            with open(md_path, "rb") as f:
                files = {"file": (os.path.basename(md_path), f, "text/markdown")}
                r = self.session.post(f"{self.base_url}/api/v1/document/upload", files=files, timeout=60)
                if r.status_code == 200:
                    data = r.json()
                    if "documents" in data and data["documents"]:
                        return data["documents"][0].get("id")
        except: pass
        return ""

    def move_to_workspace(self, doc_id: str, slug: str):
        if not doc_id or not self.enabled: return
        try:
            self.session.post(f"{self.base_url}/api/v1/workspace/{slug}/update", json={"adds": [doc_id]}, timeout=15)
        except: pass

# ================= 启动逻辑 =================
async def run_collector(url: str, mode: str = "incremental"):
    config = CollectorConfig(
        mode=CollectMode.INCREMENTAL if mode == "incremental" else CollectMode.FULL,
        platform="x" if "x.com" in url or "twitter.com" in url else "douyin",
        headless=False
    )
    
    if config.platform == "x":
        config.proxy_server = os.environ.get("PW_PROXY") or os.environ.get("HTTPS_PROXY") or ""
    
    collector = IndustrialCollector(config)
    try:
        await collector.start(url)
        # 获取最终结果并移交 pipeline
        items = list(getattr(collector, "accepted_items", []) or [])
        if not items:
            async with collector.interceptor._lock:
                items = list(collector.interceptor.intercepted_items)
        
        if items:
            await process_collected_items(items, getattr(collector, "ui", None))
        return items
    finally:
        await collector.stop()

async def process_collected_items(items: List[Dict], ui: Optional['ProgressDisplay'] = None):
    """处理收集到的 items - 调用 pipeline"""
    logger.info(f"processing_collected_items: count={len(items)}")
    
    if ui:
        ui.start_download_session(len(items))
    
    # 动态加载 pipeline
    try:
        import pipeline
        pipeline_fn = getattr(pipeline, "run_pipeline", None)
    except Exception as e:
        logger.error(f"load_pipeline_failed: error={e}")
        return

    llm_client = AnythingLLMClient()
    workspace_slug = llm_client.find_workspace_slug()

    for idx, item in enumerate(items, 1):
        url = item['url']
        logger.info(f"processing_item: url={url}")
        
        if ui:
            ui.start_url(idx, len(items), url)
            
        try:
            # 运行 Pipeline (现在是异步的)
            md_path = await pipeline_fn(url, progress_display=ui)
            
            if ui:
                ui.complete_url()
            
            if md_path and os.path.exists(str(md_path)) and llm_client.enabled:
                doc_id = llm_client.upload_markdown(str(md_path))
                if doc_id:
                    llm_client.move_to_workspace(doc_id, workspace_slug)
                    logger.info(f"knowledge_base_synced: url={url}")
        except Exception as e:
            logger.error(f"item_process_failed: url={url}, error={e}")
            if ui:
                ui.fail_url(str(e)[:160])
    
    if ui:
        ui.stop_download_session()

if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("TARGET_URL", "https://www.douyin.com/user/self?from_tab_name=main&showTab=favorite_collection")
    
    # 修正 URL 中可能由于 shell 转义引入的错误字符（如 ^&）
    target = target.replace('^', '').strip()
    
    # 从环境变量获取采集模式
    mode_env = os.environ.get("COLLECT_MODE", "1")
    mode_str = "full" if mode_env == "2" else "incremental"
    asyncio.run(run_collector(target, mode=mode_str))
