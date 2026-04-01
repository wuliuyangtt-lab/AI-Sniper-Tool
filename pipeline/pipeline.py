"""
抖音全自动 Pipeline
抖音链接 → 下载 → 音频提取 → Whisper转录 → LLM洗稿 → Markdown输出 → AnythingLLM入库
"""

import re
import sys
import os
import json
import subprocess
import urllib.request
import urllib.error
import time
import asyncio
import hashlib
import shutil
from urllib.parse import urlparse
from datetime import datetime
from abc import ABC, abstractmethod
from pathlib import Path

# 导入配置
import sys
# 确保先导入根目录的config.py
sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))
# 清除可能存在的config模块缓存
if 'config' in sys.modules:
    del sys.modules['config']
from config import (
    COOKIE_FILE,
    FFMPEG_BIN,
    DOWNLOADER_SCRIPT,
    LM_STUDIO_URL,
    LM_STUDIO_MODEL,
    ALLM_URL,
    ALLM_API_KEY,
    ALLM_WORKSPACE,
    TIMEOUT,
    PROXY_SERVER,
    YTDLP_BIN,
    YTDLP_COOKIES
)

# 导入增强的Cookie管理器
try:
    from cookie_manager import EnhancedCookieManager
except ImportError:
    print("[Warning] 未找到cookie_manager模块，使用默认cookies管理")
    EnhancedCookieManager = None

# ============ 配置 ============
WORKDIR = Path(__file__).parent.absolute()


def ensure_cookies_updated():
    """确保cookies是最新的"""
    if EnhancedCookieManager:
        log("检查并更新cookies...")
        cookie_manager = EnhancedCookieManager(WORKDIR, quiet=True)
        # 验证cookies
        if not cookie_manager.validate_cookies():
            log("Cookies验证失败，需要重新登录")
        # 更新downloader配置
        cookie_manager.update_downloader_config()
    else:
        log("未使用增强的Cookie管理器")
# ==============================

_WIN_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


# ============ 处理器基类 ============
class BaseContentProcessor(ABC):
    """内容处理器抽象基类"""
    
    @abstractmethod
    async def process(self, item: dict, progress_display: 'ProgressDisplay' = None) -> dict:
        """处理内容并返回统一格式
        
        Args:
            item: 包含待处理内容信息的字典，至少包含 'url' 字段
            progress_display: 可选的 UI 进度显示对象
            
        Returns:
            包含处理结果的字典
        """
        pass


class VideoProcessor(BaseContentProcessor):
    """视频处理器"""
    
    async def process(self, item: dict, progress_display: 'ProgressDisplay' = None) -> dict:
        url = item.get('url')
        output_dir = item.get('output_dir', str(Path(__file__).parent.absolute() / "output"))
        
        log(f"视频处理器开始处理: {url}")

        platform, content_id = _content_key(url)
        transcript_cache = ""
        polished_cache = ""
        md_cache = ""
        if platform == "douyin" and content_id:
            cache_dir = _cache_dir(output_dir, platform, content_id)
            transcript_cache = str(Path(cache_dir) / "transcript.json")
            polished_cache = str(Path(cache_dir) / "polished.json")
            md_cache = _cached_md_path(output_dir, platform, content_id)
            if os.path.exists(md_cache) and os.path.getsize(md_cache) > 0:
                return {
                    'title': f'{platform}_{content_id}',
                    'content': '',
                    'media_paths': [md_cache],
                    'tags': ['视频', '抖音'],
                    'source_url': url,
                    'content_type': 'video'
                }
        
        # 1. 下载视频
        try:
            if progress_display:
                progress_display.advance_step("下载视频", f"正在请求: {url}")
            video_path = await download_video(url, output_dir)
        except Exception as e:
            msg = str(e)
            if "IS_NOTE_DETECTED" in msg:
                log("下载器反馈该内容为笔记而非视频，正在切换到图文处理器...")
                processor = ImageSuiteProcessor()
                return await processor.process(item, progress_display)
            if "IS_ARTICLE_DETECTED" in msg:
                log("下载器反馈该内容为长文而非视频，正在切换到长文处理器...")
                processor = ArticleProcessor()
                return await processor.process(item, progress_display)
                
            log(f"下载视频异常: {msg}，尝试切换到图文/长文处理器...")
            # 失败后，优先尝试图文下载
            try:
                processor = ImageSuiteProcessor()
                return await processor.process(item, progress_display)
            except:
                processor = ArticleProcessor()
                return await processor.process(item, progress_display)

        # 2. 检查下载文件的真实性与完整性
        # 如果配套的 _data.json 显示是图文，或者文件根本不是 MP4
        is_gallery = False
        json_path = video_path.replace(".mp4", "_data.json")
        metadata = {}
        if os.path.exists(json_path):
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    metadata = data
                # 抖音 API 中，图文笔记会有 image_post_info 或 images 字段
                if data.get("image_post_info") or data.get("images") or data.get("media_type") == "gallery":
                    is_gallery = True
            except:
                pass
        
        # 补充逻辑：如果 desc 长度极大且没有有效的视频流，也可能是长文
        desc_len = len(metadata.get("desc", ""))
        if desc_len > 1000 and not is_gallery:
            log(f"检测到描述文字超长 ({desc_len} 字)，可能是长文笔记...")
            # 这里不强制切换，但在后面提取音频失败时会作为依据
        
        if is_gallery or not video_path.lower().endswith(".mp4"):
            log("检测到实际内容为图文笔记，切换到图文处理器...")
            processor = ImageSuiteProcessor()
            return await processor.process(item, progress_display)

        # 3. 获取元数据 (用于后续 Markdown 生成)
        video_metadata = get_video_metadata(video_path, url)
        
        # 4. 提取音频
        if progress_display:
            progress_display.advance_step("提取音频", f"正在转换: {os.path.basename(video_path)}")
        audio_path = os.path.splitext(video_path)[0] + ".mp3"
        try:
            if os.path.exists(audio_path) and os.path.getsize(audio_path) > 0:
                pass
            else:
                audio_path = await asyncio.to_thread(extract_audio, video_path, audio_path)
        except Exception as e:
            log(f"音频提取失败 (可能不是有效视频): {e}")
            # 如果音频提取失败（如 moov atom not found），通常是因为它其实是个披着视频皮的图文或长文
            log("尝试切换到长文/图文处理器...")
            try:
                # 如果元数据里已经有不错的正文，优先尝试图文处理器（因为它会下载图片）
                if is_gallery or desc_len > 500:
                    processor = ImageSuiteProcessor()
                    return await processor.process(item, progress_display)
                else:
                    processor = ArticleProcessor()
                    return await processor.process(item, progress_display)
            except Exception as e2:
                log(f"切换处理器也失败: {e2}")
                raise e

        # 5. 转录
        if progress_display:
            progress_display.advance_step("语音转录", "正在进行 Whisper 识别...")
        transcript = _load_json(transcript_cache) if transcript_cache else None
        if not transcript:
            transcript = await asyncio.to_thread(transcribe, audio_path)
            if transcript_cache:
                _save_json(transcript_cache, transcript)
        
        # 6. LLM洗稿
        if progress_display:
            progress_display.advance_step("AI 处理", "正在进行洗稿...")
        polished = _load_json(polished_cache) if polished_cache else None
        if not polished:
            polished = await asyncio.to_thread(polish_text, transcript.get("full_text", ""))
            if polished_cache:
                _save_json(polished_cache, polished)
        
        # 7. 生成Markdown
        if progress_display:
            progress_display.advance_step("保存结果", "正在生成 Markdown 报告...")
        title_safe = sanitize_windows_filename(video_metadata.get("title", "未命名"), max_length=80)
        if platform == "douyin" and content_id:
            md_path = str(Path(output_dir) / f"{title_safe}_{content_id}.md")
        else:
            md_path = str(Path(output_dir) / f"{title_safe}.md")
        generate_markdown(video_metadata, transcript, polished, md_path)
        if md_cache:
            try:
                os.makedirs(os.path.dirname(md_cache), exist_ok=True)
                if os.path.exists(md_path) and os.path.getsize(md_path) > 0:
                    shutil.copyfile(md_path, md_cache)
            except Exception:
                pass
        
        # 构建返回结果
        return {
            'title': video_metadata.get('title', '未命名'),
            'content': polished.get('polished', transcript.get('full_text', '')),
            'media_paths': [p for p in [md_path, md_cache, video_path, audio_path] if p],
            'tags': ['视频', '抖音'],
            'source_url': url,
            'content_type': 'video'
        }


class ImageSuiteProcessor(BaseContentProcessor):
    """图集/笔记处理器 - 使用 dy-downloader 原生下载"""
    
    async def process(self, item: dict, progress_display: 'ProgressDisplay' = None) -> dict:
        return await asyncio.to_thread(self.process_sync, item, progress_display)
        
    def process_sync(self, item: dict, progress_display: 'ProgressDisplay' = None) -> dict:
        url = item.get('url')
        output_dir = item.get('output_dir', str(Path(__file__).parent.absolute() / "output"))
        
        log(f"使用下载器处理图集/笔记: {url}")
        if progress_display:
            progress_display.advance_step("下载图集", f"正在请求 API: {url}")
        
        # 提取 note_id
        note_id = extract_douyin_video_id(url)
            
        # 1. 下载图集
        try:
            metadata, image_paths = download_douyin_note(url, output_dir)
            
            if progress_display:
                progress_display.update_step("下载图集", f"已获取 {len(image_paths)} 张高清图片")
            
            title = metadata.get("title", metadata.get("desc", f"图文_{note_id}"))
            content = metadata.get("desc", "")
            
            # 生成 markdown
            if progress_display:
                progress_display.advance_step("生成 Markdown", f"标题: {title[:20]}...")
            title_safe = sanitize_windows_filename(title, max_length=80)
            md_path = str(Path(output_dir) / f"{title_safe}.md")
            
            md_content = f"# {title}\n\n"
            if content:
                md_content += f"{content}\n\n"
            
            # 在 markdown 中插入图片引用
            for img_path in image_paths:
                img_name = os.path.basename(img_path)
                # 使用相对路径或绝对路径
                md_content += f"![{img_name}]({img_name})\n\n"
                
            try:
                with open(md_path, "w", encoding="utf-8") as f:
                    f.write(md_content)
            except:
                pass
                
            log(f"图文处理完成: {title}, 图片数: {len(image_paths)}")

            platform, content_id = _content_key(url)
            if platform == "douyin" and content_id and os.path.exists(md_path) and os.path.getsize(md_path) > 0:
                cache_md = _cached_md_path(output_dir, platform, content_id)
                try:
                    os.makedirs(os.path.dirname(cache_md), exist_ok=True)
                    shutil.copyfile(md_path, cache_md)
                except Exception:
                    pass
            
            return {
                'title': title,
                'content': content,
                'media_paths': image_paths + [md_path] if image_paths else [],
                'tags': ['图集', '抖音'],
                'source_url': url,
                'content_type': 'image_suite'
            }
        except Exception as e:
            log(f"下载图集失败: {e}")
            return {
                'title': '处理失败 - 图集',
                'content': f'图集处理失败: {e}',
                'media_paths': [],
                'tags': ['图集'],
                'source_url': url,
                'content_type': 'image_suite'
            }



class ArticleProcessor(BaseContentProcessor):
    """长文笔记处理器 - 使用同步API避免asyncio冲突"""
    
    async def process(self, item: dict, progress_display: 'ProgressDisplay' = None) -> dict:
        """异步接口包装同步实现"""
        return await asyncio.to_thread(self.process_sync, item, progress_display)

    def process_sync(self, item: dict, progress_display: 'ProgressDisplay' = None) -> dict:
        """同步处理方法"""
        url = item.get('url')
        output_dir = item.get('output_dir', str(Path(__file__).parent.absolute() / "output"))
        is_article = item.get('is_article', False)
        
        log(f"处理抖音笔记: {url}")
        if progress_display:
            progress_display.advance_step("正在处理长文", f"请求页面: {url}")
        
        # 使用 Playwright 打开页面
        try:
            from playwright.sync_api import sync_playwright
        except Exception:
            return {
                'title': '未实现 - 长文笔记',
                'content': '缺少 Playwright 依赖',
                'media_paths': [],
                'tags': ['长文笔记'],
                'source_url': url,
                'content_type': 'article'
            }
        
        content = ""
        title = "未命名"
        images = []
        
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, channel="chrome")
            
            # 尝试加载存储状态（包含 cookies 和 localStorage）
            storage_state = None
            storage_state_path = str(Path(__file__).parent.absolute() / "storage_state.json")
            if os.path.exists(storage_state_path):
                storage_state = storage_state_path
                log(f"加载存储状态: {storage_state_path}")
            
            context_kwargs = {
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
                "locale": "zh-CN",
                "viewport": {"width": 1366, "height": 850},
                "timezone_id": "Asia/Shanghai",
                "ignore_https_errors": True,
            }
            if storage_state:
                context_kwargs["storage_state"] = storage_state
                
            context = browser.new_context(**context_kwargs)
            page = context.new_page()
            
            try:
                # 打开页面，使用domcontentloaded等待策略，避免networkidle超时
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                
                # 等待页面加载
                page.wait_for_timeout(8000)  # 增加等待时间到8秒
                
                # 处理可能的登录提示
                try:
                    # 尝试关闭登录提示
                    close_buttons = page.locator('button:has-text("关闭"), button:has-text("取消"), .close-button, .popup-close').all()
                    for btn in close_buttons:
                        if btn.is_visible():
                            btn.click()
                            page.wait_for_timeout(3000)
                            break
                except Exception:
                    pass
                
                # 滚动页面以加载更多内容
                log("滚动页面以加载更多内容...")
                
                # 智能滚动到页面底部
                last_scroll_height = 0
                scroll_count = 0
                max_scrolls = 30  # 增加最大滚动次数
                stable_count = 0
                max_stable = 3  # 连续稳定次数
                
                # 智能检测滚动容器
                try:
                    scroll_container = page.evaluate("""
                        () => {
                            try {
                                const candidates = [];
                                const root = document.scrollingElement || document.documentElement || document.body;
                                if (root) candidates.push(root);
                                document.querySelectorAll('div').forEach(el => {
                                    if (!el) return;
                                    const sh = el.scrollHeight || 0;
                                    const ch = el.clientHeight || 0;
                                    if (sh > ch + 200) {
                                        const r = el.getBoundingClientRect();
                                        if (r && r.height > 400) candidates.push(el);
                                    }
                                });
                                let best = candidates[0] || root;
                                let bestScore = 0;
                                candidates.forEach(el => {
                                    if (!el) return;
                                    const sh = el.scrollHeight || 0;
                                    const ch = el.clientHeight || 0;
                                    const score = (sh - ch);
                                    if (score > bestScore) {
                                        bestScore = score;
                                        best = el;
                                    }
                                });
                                return (best === document.scrollingElement || best === document.documentElement || best === document.body) ? 'body' : 'custom';
                            } catch (e) {
                                return 'body';
                            }
                        }
                    """)
                except Exception as e:
                    log(f"检测滚动容器失败，默认使用body: {e}")
                    scroll_container = 'body'
                
                log(f"使用滚动容器: {scroll_container}")
                
                while scroll_count < max_scrolls and stable_count < max_stable:
                    try:
                        # 滚动到当前页面底部
                        if scroll_container == 'body':
                            page.evaluate("if(document.body) { window.scrollTo(0, document.body.scrollHeight || 0); }")
                        else:
                            page.evaluate("""
                                () => {
                                    try {
                                        const candidates = [];
                                        const root = document.scrollingElement || document.documentElement || document.body;
                                        if (root) candidates.push(root);
                                        document.querySelectorAll('div').forEach(el => {
                                            if (!el) return;
                                            const sh = el.scrollHeight || 0;
                                            const ch = el.clientHeight || 0;
                                            if (sh > ch + 200) {
                                                const r = el.getBoundingClientRect();
                                                if (r && r.height > 400) candidates.push(el);
                                            }
                                        });
                                        let best = candidates[0] || root;
                                        let bestScore = 0;
                                        candidates.forEach(el => {
                                            if (!el) return;
                                            const sh = el.scrollHeight || 0;
                                            const ch = el.clientHeight || 0;
                                            const score = (sh - ch);
                                            if (score > bestScore) {
                                                bestScore = score;
                                                best = el;
                                            }
                                        });
                                        if (best) best.scrollTop = best.scrollHeight || 0;
                                    } catch (e) {}
                                }
                            """)
                        
                        # 增加等待时间，确保内容加载
                        page.wait_for_timeout(4000)  # 增加到4秒
                        
                        # 获取新的滚动高度
                        if scroll_container == 'body':
                            new_scroll_height = page.evaluate("document.body ? document.body.scrollHeight : 0")
                        else:
                            new_scroll_height = page.evaluate("""
                                () => {
                                    try {
                                        const candidates = [];
                                        const root = document.scrollingElement || document.documentElement || document.body;
                                        if (root) candidates.push(root);
                                        document.querySelectorAll('div').forEach(el => {
                                            if (!el) return;
                                            const sh = el.scrollHeight || 0;
                                            const ch = el.clientHeight || 0;
                                            if (sh > ch + 200) {
                                                const r = el.getBoundingClientRect();
                                                if (r && r.height > 400) candidates.push(el);
                                            }
                                        });
                                        let best = candidates[0] || root;
                                        let bestScore = 0;
                                        candidates.forEach(el => {
                                            if (!el) return;
                                            const sh = el.scrollHeight || 0;
                                            const ch = el.clientHeight || 0;
                                            const score = (sh - ch);
                                            if (score > bestScore) {
                                                bestScore = score;
                                                best = el;
                                            }
                                        });
                                        return best ? (best.scrollHeight || 0) : 0;
                                    } catch (e) {
                                        return 0;
                                    }
                                }
                            """)
                        
                        # 检查是否已经滚动到底部
                        if new_scroll_height == last_scroll_height:
                            stable_count += 1
                            log(f"页面高度稳定 {stable_count}/{max_stable}")
                        else:
                            stable_count = 0
                            last_scroll_height = new_scroll_height
                        
                        scroll_count += 1
                        log(f"滚动页面 {scroll_count}/{max_scrolls} 完成，当前高度: {new_scroll_height}")
                    except Exception as e:
                        log(f"滚动失败: {e}")
                        break
                
                # 额外滚动几次确保加载所有内容
                for i in range(5):  # 增加到5次
                    try:
                        page.mouse.wheel(0, 5000)  # 增加滚动距离
                        page.wait_for_timeout(3000)  # 增加等待时间
                        log(f"额外滚动 {i+1}/5 完成")
                    except Exception as e:
                        log(f"额外滚动失败: {e}")
                        break
                
                # 最后再滚动到顶部然后到底部，确保所有内容都被触发
                try:
                    log("最后滚动到顶部...")
                    if scroll_container == 'body':
                        page.evaluate("window.scrollTo(0, 0)")
                    else:
                        page.evaluate("""
                            () => {
                                const candidates = [];
                                const root = document.scrollingElement || document.documentElement || document.body;
                                if (root) candidates.push(root);
                                document.querySelectorAll('div').forEach(el => {
                                    const sh = el.scrollHeight || 0;
                                    const ch = el.clientHeight || 0;
                                    if (sh > ch + 200) {
                                        const r = el.getBoundingClientRect();
                                        if (r && r.height > 400) candidates.push(el);
                                    }
                                });
                                let best = candidates[0] || (document.scrollingElement || document.body);
                                best.scrollTop = 0;
                            }
                        """)
                    page.wait_for_timeout(2000)
                    
                    log("最后滚动到底部...")
                    if scroll_container == 'body':
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    else:
                        page.evaluate("""
                            () => {
                                const candidates = [];
                                const root = document.scrollingElement || document.documentElement || document.body;
                                if (root) candidates.push(root);
                                document.querySelectorAll('div').forEach(el => {
                                    const sh = el.scrollHeight || 0;
                                    const ch = el.clientHeight || 0;
                                    if (sh > ch + 200) {
                                        const r = el.getBoundingClientRect();
                                        if (r && r.height > 400) candidates.push(el);
                                    }
                                });
                                let best = candidates[0] || (document.scrollingElement || document.body);
                                best.scrollTop = best.scrollHeight;
                            }
                        """)
                    page.wait_for_timeout(4000)
                    log("最终滚动完成")
                except Exception as e:
                    log(f"最终滚动失败: {e}")
                
                # 提取标题
                try:
                    title_selectors = [
                        'h1', 
                        '.note-title', 
                        '[data-e2e="note-title"]', 
                        '.title', 
                        'header h1',
                        '.article-title',
                        '[data-e2e="article-title"]',
                        '.title-wrapper h1',
                        '.article-header h1',
                        '.content h1',
                        '.main-title',
                        '.article__title',
                        '.title-section h1',
                        'h2.title',
                        'h1.title',
                        'div[class*="title"]',
                        'div[data-e2e*="title"]'
                    ]
                    for selector in title_selectors:
                        try:
                            title_selector = page.locator(selector)
                            if title_selector.count() > 0:
                                title = title_selector.first.inner_text().strip()
                                if title and len(title) > 5:
                                    break
                        except Exception:
                            continue
                    
                    # 如果没有找到标题，尝试从页面标题中提取
                    if not title or len(title) <= 5:
                        page_title = page.title()
                        if page_title and len(page_title) > 5:
                            # 移除可能的后缀
                            title = page_title.replace(' - 抖音', '').replace('抖音', '').replace('在抖音记录美好生活', '').strip()
                    
                    # 如果仍然没有标题，尝试从meta标签中提取
                    if not title or len(title) <= 5:
                        try:
                            meta_title = page.locator('meta[name="title"]').first.get_attribute('content')
                            if meta_title and len(meta_title) > 5:
                                title = meta_title.strip()
                        except Exception:
                            pass
                    
                    # 如果仍然没有标题，使用JavaScript提取
                    if not title or len(title) <= 5:
                        try:
                            js_title = page.evaluate("""() => {
                                const titleElements = document.querySelectorAll('h1, h2, .title, [data-e2e*="title"]');
                                for (const el of titleElements) {
                                    const text = el.innerText.trim();
                                    if (text && text.length > 5) {
                                        return text;
                                    }
                                }
                                return document.title;
                            }""")
                            if js_title and len(js_title) > 5:
                                title = js_title.replace(' - 抖音', '').replace('抖音', '').replace('在抖音记录美好生活', '').strip()
                        except Exception:
                            pass
                except Exception as e:
                    log(f"提取标题失败: {e}")
                
                # 提取正文（使用多 fallback 选择器）
                content_selectors = [
                    'div[data-e2e="note-detail"]', 
                    'article', 
                    '.note-content', 
                    '.xg-player-container ~ div', 
                    'main',
                    '.content',
                    '.article-content',
                    '.note-body',
                    '.article-body',
                    '.content-inner',
                    '.article-content-inner',
                    '.rich-text',
                    '.content-wrapper',
                    '.article-main',
                    '.article-content-area',
                    '.note-main-content',
                    '.detail-content',
                    '.post-content',
                    '.note-detail-content',
                    '.article-detail',
                    '.content-container',
                    '.main-content',
                    '.article-content-wrap',
                    '.article-content-area',
                    '.content__body',
                    '.post__content',
                    '.note__content',
                    '.article__content',
                    '.content-body',
                    '.article-body-content',
                    '[class*="content"]',
                    '[class*="article"]'
                ]
                content_found = False
                
                # 等待动态内容加载
                for _ in range(10):  # 增加尝试次数到10次
                    for selector in content_selectors:
                        try:
                            elements = page.locator(selector).all()
                            if elements:
                                for el in elements:
                                    text = el.inner_text().strip()
                                    if text and len(text) > 20:  # 进一步降低内容长度要求
                                        # 避免重复添加相同内容
                                        if text not in content:
                                            content += text + "\n\n"
                                            content_found = True
                                if content_found:
                                    break
                        except Exception as e:
                            log(f"尝试选择器 {selector} 失败: {e}")
                            continue
                    if content_found:
                        break
                    # 等待更多内容加载
                    page.wait_for_timeout(2000)
                
                # 使用JavaScript提取所有段落文本
                if not content_found or len(content) < 200:
                    log("使用JavaScript提取内容...")
                    try:
                        js_content = page.evaluate("""() => {
                            // 首先尝试获取文章主体内容
                            const articleSelectors = [
                                'div[data-e2e="note-detail"]',
                                'article',
                                'main',
                                '[class*="content"]',
                                '[class*="article"]',
                                'body'
                            ];
                            
                            let text = '';
                            
                            // 尝试从文章主体获取内容
                            for (const selector of articleSelectors) {
                                const element = document.querySelector(selector);
                                if (element) {
                                    const elementText = element.innerText.trim();
                                    if (elementText.length > 100) {
                                        text = elementText;
                                        break;
                                    }
                                }
                            }
                            
                            // 如果没有找到足够的内容，获取所有可见文本
                            if (text.length < 200) {
                                const allText = document.body.innerText.trim();
                                if (allText.length > text.length) {
                                    text = allText;
                                }
                            }
                            
                            // 清理文本，移除常见的UI元素文本
                            const uiTexts = ['推荐', '关注', '朋友', '消息', '我', '首页', '搜索', '下载客户端'];
                            uiTexts.forEach(uiText => {
                                text = text.replace(new RegExp(uiText, 'g'), '');
                            });
                            
                            return text;
                        }""")
                        if js_content and len(js_content) > 50:
                            content = js_content
                            content_found = True
                            log(f"JavaScript提取到 {len(content)} 字符内容")
                    except Exception as e:
                        log(f"JavaScript提取失败: {e}")
                
                # 如果没有找到内容，尝试获取所有可见文本
                if not content_found or len(content) < 500:
                    try:
                        full_text = page.inner_text('body').strip()
                        if len(full_text) > len(content):
                            content = full_text
                            content_found = True
                            log(f"从body获取到 {len(content)} 字符内容")
                    except Exception as e:
                        log(f"获取页面文本失败: {e}")
                
                # 清理内容，移除常见的UI元素
                if content:
                    # 移除常见的导航和UI文本
                    ui_patterns = [
                        '推荐\s+关注\s+朋友',
                        '首页\s+搜索\s+消息',
                        '下载客户端',
                        '登录\s+注册',
                        '点赞\s+评论\s+分享',
                        '收藏',
                        '更多',
                        '返回顶部'
                    ]
                    import re
                    for pattern in ui_patterns:
                        content = re.sub(pattern, '', content, flags=re.IGNORECASE)
                
                # 图片嗅探
                try:
                    # 首先使用JavaScript提取文章正文区域的图片URL，包括动态加载的图片
                    log("使用JavaScript提取图片...")
                    try:
                        js_img_urls = page.evaluate("""
                            () => {
                                const urls = [];
                                const seen = new Set();
                                
                                // 函数：检查元素是否在文章正文区域内
                                function isInArticleContent(element) {
                                    // 常见的文章正文选择器
                                    const articleSelectors = [
                                        'div[data-e2e="note-detail"]',
                                        'article',
                                        'main',
                                        '.note-content',
                                        '.article-content',
                                        '.content',
                                        '.note-body',
                                        '.article-body',
                                        '.content-inner',
                                        '.article-content-inner',
                                        '.rich-text',
                                        '.content-wrapper',
                                        '.article-main',
                                        '.article-content-area',
                                        '.note-main-content',
                                        '.detail-content',
                                        '.post-content',
                                        '.note-detail-content',
                                        '.article-detail',
                                        '.content-container',
                                        '.main-content',
                                        '.article-content-wrap',
                                        '.content__body',
                                        '.post__content',
                                        '.note__content',
                                        '.article__content',
                                        '.content-body',
                                        '.article-body-content'
                                    ];
                                    
                                    // 常见的非正文区域选择器
                                    const nonContentSelectors = [
                                        'header',
                                        'footer',
                                        'nav',
                                        '.sidebar',
                                        '.aside',
                                        '.comment',
                                        '.comments',
                                        '.comment-list',
                                        '.comment-section',
                                        '.sidebar-container',
                                        '.side-panel',
                                        '.related-content',
                                        '.recommendation',
                                        '.suggestion',
                                        '.advertisement',
                                        '.ad',
                                        '.footer-container',
                                        '.header-container',
                                        '.nav-container',
                                        '.top-bar',
                                        '.bottom-bar',
                                        '.comment-box',
                                        '.comment-input',
                                        '.comment-reply',
                                        '.comment-form',
                                        '.share-box',
                                        '.share-section',
                                        '.like-section',
                                        '.collect-section',
                                        '.author-info',
                                        '.profile-info',
                                        '.user-info',
                                        '.related-posts',
                                        '.recommended-posts',
                                        '.sidebar-content',
                                        '.aside-content'
                                    ];
                                    
                                    // 检查元素是否在非正文区域内
                                    let current = element;
                                    while (current && current !== document.body) {
                                        // 检查是否在非正文区域
                                        for (const selector of nonContentSelectors) {
                                            if (current.matches(selector) || current.classList.contains(selector.replace('.', '')) || current.id === selector.replace('#', '')) {
                                                return false;
                                            }
                                        }
                                        current = current.parentElement;
                                    }
                                    
                                    // 检查是否在正文区域内
                                    current = element;
                                    while (current && current !== document.body) {
                                        for (const selector of articleSelectors) {
                                            if (current.matches(selector) || current.classList.contains(selector.replace('.', '')) || current.id === selector.replace('#', '')) {
                                                return true;
                                            }
                                        }
                                        current = current.parentElement;
                                    }
                                    
                                    // 过滤掉特定尺寸（如太小）的图片，去除图标、表情、白板
                                    const rect = element.getBoundingClientRect();
                                    if (rect.width > 0 && rect.height > 0) {
                                        // 抖音笔记核心图片一般宽度大于200，高度大于200
                                        if (rect.width < 100 || rect.height < 100) return false;
                                        // 过滤比例过于极端的图（如 1:10 的分割线）
                                        const ratio = rect.width / rect.height;
                                        if (ratio > 5 || ratio < 0.2) return false;
                                    }

                                    // 对于抖音长文章，尝试检查图片是否在主内容区域
                                    // 检查图片URL是否来自抖音的图片CDN
                                    const imgSrc = element.src || element.getAttribute('data-src') || element.getAttribute('data-original') || element.getAttribute('data-lazy');
                                    if (imgSrc) {
                                        // 过滤掉常见的 UI 元素、默认头像、白板图片
                                        const ignorePatterns = [
                                            'aweme-avatar', 'ui-element', 'icon', 'blank', 
                                            'playing_effect', 'play_effect', 'commentTmptyList'
                                        ];
                                        for (const pattern of ignorePatterns) {
                                            if (imgSrc.includes(pattern)) return false;
                                        }

                                        // 抖音图片CDN域名
                                        const douyinImageDomains = [
                                            'p3-ugc',
                                            'p9-ugc',
                                            'douyinpic',
                                            'tos-cn',
                                            'amemv',
                                            'aweme'
                                        ];
                                        
                                        // 检查图片是否来自抖音图片CDN
                                        for (const domain of douyinImageDomains) {
                                            if (imgSrc.includes(domain)) {
                                                // 对于抖音CDN图片，默认认为是正文内容
                                                return true;
                                            }
                                        }
                                    }
                                    
                                    // 默认返回false，只确保在正文区域内的图片
                                    return false;
                                }
                                
                                // 函数：提取图片URL
                                function extractImages() {
                                    // 查找所有img元素
                                    const images = document.querySelectorAll('img');
                                    images.forEach(img => {
                                        // 检查是否在文章正文区域内
                                        if (!isInArticleContent(img)) {
                                            return;
                                        }
                                        
                                        // 尝试获取各种可能的图片源
                                        const sources = [
                                            img.src,
                                            img.getAttribute('data-src'),
                                            img.getAttribute('data-original'),
                                            img.getAttribute('data-lazy'),
                                            img.getAttribute('data-srcset'),
                                            img.getAttribute('srcset')
                                        ];
                                        
                                        sources.forEach(src => {
                                            if (src && !seen.has(src) && !src.startsWith('data:')) {
                                                // 处理srcset格式
                                                if (src.includes(',')) {
                                                    const srcsetUrls = src.split(',').map(s => s.trim().split(' ')[0]);
                                                    srcsetUrls.forEach(url => {
                                                        if (url && !seen.has(url)) {
                                                            seen.add(url);
                                                            urls.push(url);
                                                        }
                                                    });
                                                } else {
                                                    seen.add(src);
                                                    urls.push(src);
                                                }
                                            }
                                        });
                                    });
                                    
                                    // 查找文章正文区域内的背景图片
                                    const articleSelectors = [
                                        'div[data-e2e="note-detail"]',
                                        'article',
                                        'main',
                                        '.note-content',
                                        '.article-content',
                                        '.content'
                                    ];
                                    
                                    articleSelectors.forEach(selector => {
                                        const articleElements = document.querySelectorAll(selector);
                                        articleElements.forEach(articleEl => {
                                            const elements = articleEl.querySelectorAll('div, section, figure');
                                            elements.forEach(el => {
                                                const style = window.getComputedStyle(el);
                                                const bgImage = style.backgroundImage;
                                                if (bgImage && bgImage !== 'none') {
                                                    const matches = bgImage.match(/url\(['"]?(.*?)['"]?\)/g);
                                                    if (matches) {
                                                        matches.forEach(match => {
                                                            const url = match.replace(/url\(['"]?(.*?)['"]?\)/, '$1');
                                                            if (url && !seen.has(url) && !url.startsWith('data:')) {
                                                                seen.add(url);
                                                                urls.push(url);
                                                            }
                                                        });
                                                    }
                                                }
                                            });
                                        });
                                    });
                                }
                                
                                // 提取当前页面的图片
                                extractImages();
                                
                                // 尝试滚动页面以触发更多图片加载
                                window.scrollTo(0, document.body.scrollHeight);
                                
                                // 等待一下再提取
                                setTimeout(() => {
                                    extractImages();
                                }, 1000);
                                
                                return urls;
                            }
                        """)
                        log(f"JavaScript提取到 {len(js_img_urls)} 张图片")
                    except Exception as e:
                        log(f"JavaScript提取图片失败: {e}")
                        js_img_urls = []
                    
                    # 针对抖音长文章的图片结构，使用更精确的选择器，只选择文章正文区域内的图片
                    img_selectors = [
                        'div[data-e2e="note-detail"] img',  # 文章详情中的图片
                        'article img',  # 文章中的图片
                        'main img',  # 主内容区的图片
                        '.note-content img',  # 笔记内容中的图片
                        '.article-content img',  # 文章内容中的图片
                        '.content img',  # 内容区域中的图片
                        '.note-body img',  # 笔记正文中的图片
                        '.article-body img',  # 文章正文中的图片
                        '.content-inner img',  # 内容内部的图片
                        '.article-content-inner img',  # 文章内容内部的图片
                        '.rich-text img',  # 富文本中的图片
                        '.content-wrapper img',  # 内容包装器中的图片
                        '.article-main img',  # 文章主体中的图片
                        '.article-content-area img',  # 文章内容区域中的图片
                        '.note-main-content img',  # 笔记主要内容中的图片
                        '.detail-content img',  # 详情内容中的图片
                        '.post-content img',  # 帖子内容中的图片
                        '.note-detail-content img',  # 笔记详情内容中的图片
                        '.article-detail img',  # 文章详情中的图片
                        '.content-container img',  # 内容容器中的图片
                        '.main-content img',  # 主要内容中的图片
                        '.article-content-wrap img',  # 文章内容包装中的图片
                        '.content__body img',  # 内容主体中的图片
                        '.post__content img',  # 帖子内容中的图片
                        '.note__content img',  # 笔记内容中的图片
                        '.article__content img',  # 文章内容中的图片
                        '.content-body img',  # 内容主体中的图片
                        '.article-body-content img'  # 文章主体内容中的图片
                    ]
                    
                    img_urls = []
                    seen_urls = set()
                    
                    # 先添加JavaScript提取的图片（已经过滤了非正文区域的图片）
                    for src in js_img_urls:
                        if src and src not in seen_urls:
                            # 过滤明显的非内容图片和推荐内容图片
                            filter_keywords = [
                                'avatar', 'emoji', 'profile', 'icon', 'logo', 'badge', 'rank', 'sticker', 'gif',
                                'related', 'recommend', 'suggestion', ' PackSourceEnum_WEBPC_RELATED_AWEME',
                                'biz_tag=pcweb_cover', 'image-cut-tos', 'flat_bg', 'passport-fe'
                            ]
                            if not any(keyword in src.lower() for keyword in filter_keywords):
                                seen_urls.add(src)
                                img_urls.append(src)
                    
                    # 再使用选择器提取（选择器已经限定在正文区域内）
                    for selector in img_selectors:
                        try:
                            img_elements = page.locator(selector).all()
                            for img in img_elements:
                                # 尝试获取各种可能的图片源
                                src = img.get_attribute('src') or img.get_attribute('data-src') or img.get_attribute('data-original') or img.get_attribute('data-lazy')
                                if src and src not in seen_urls:
                                    # 过滤明显的非内容图片和推荐内容图片
                                    filter_keywords = [
                                        'avatar', 'emoji', 'profile', 'icon', 'logo', 'badge', 'rank', 'sticker', 'gif',
                                        'related', 'recommend', 'suggestion', ' PackSourceEnum_WEBPC_RELATED_AWEME',
                                        'biz_tag=pcweb_cover', 'image-cut-tos', 'flat_bg', 'passport-fe'
                                    ]
                                    if any(keyword in src.lower() for keyword in filter_keywords):
                                        continue
                                    # 过滤掉base64图片
                                    if src.startswith('data:'):
                                        continue
                                    seen_urls.add(src)
                                    img_urls.append(src)
                        except Exception as e:
                            log(f"尝试图片选择器 {selector} 失败: {e}")
                            continue
                    
                    # 去重和清理
                    img_urls = list(set(img_urls))
                    log(f"总共找到 {len(img_urls)} 张图片")
                    
                    # 同步下载图片
                    if img_urls:
                        import requests
                        import urllib3
                        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                        
                        # 创建图片目录
                        title_safe = sanitize_windows_filename(title, max_length=80)
                        img_dir = os.path.join(output_dir, title_safe, "images")
                        os.makedirs(img_dir, exist_ok=True)
                        
                        # 同步下载函数
                        def download_image_sync(img_url, idx):
                            try:
                                # 跳过base64图片
                                if img_url.startswith('data:'):
                                    return None
                                
                                # 处理相对URL
                                if img_url.startswith('//'):
                                    img_url = 'https:' + img_url
                                elif not img_url.startswith('http'):
                                    # 相对路径，使用页面URL作为基础
                                    from urllib.parse import urljoin
                                    img_url = urljoin(url, img_url)
                                
                                # 优化抖音图片URL，获取高清版本
                                if 'p3-ugc' in img_url or 'p9-ugc' in img_url or 'douyinpic' in img_url:
                                    # 抖音图片CDN，替换参数获取高清版本
                                    img_url = img_url.replace('?x-oss-process=image/resize,m_fill,w_720,h_720,limit_0', '')
                                    img_url = img_url.replace('?x-oss-process=image/resize,m_fill,w_1080,h_1080,limit_0', '')
                                    img_url = img_url.replace('?x-oss-process=image/resize,m_fill,w_480,h_480,limit_0', '')
                                
                                headers = {
                                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
                                    'Referer': 'https://www.douyin.com/',
                                    'Accept': 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8'
                                }
                                
                                # 增加重试机制
                                max_retries = 5  # 增加重试次数
                                for retry in range(max_retries):
                                    try:
                                        response = requests.get(img_url, headers=headers, timeout=60, verify=False)
                                        if response.status_code == 200:
                                            # 确定文件扩展名
                                            ext = ".jpg"
                                            if ".png" in img_url:
                                                ext = ".png"
                                            elif ".webp" in img_url:
                                                ext = ".webp"
                                            elif ".gif" in img_url:
                                                ext = ".gif"
                                            elif 'image/png' in response.headers.get('Content-Type', ''):
                                                ext = ".png"
                                            elif 'image/webp' in response.headers.get('Content-Type', ''):
                                                ext = ".webp"
                                            elif 'image/gif' in response.headers.get('Content-Type', ''):
                                                ext = ".gif"
                                            
                                            img_path = os.path.join(img_dir, f"image_{idx:03d}{ext}")
                                            with open(img_path, 'wb') as f:
                                                f.write(response.content)
                                            log(f"成功下载图片: {img_url}")
                                            return img_path
                                    except Exception as e:
                                        log(f"下载图片尝试 {retry+1} 失败: {e}")
                                        if retry < max_retries - 1:
                                            time.sleep(2)  # 增加等待时间
                            except Exception as e:
                                log(f"下载图片失败: {e}")
                            return None
                        
                        # 下载所有找到的图片，取消数量限制
                        for i, img_url in enumerate(img_urls):
                            img_path = download_image_sync(img_url, i)
                            if img_path:
                                images.append(img_path)
                        
                        log(f"成功下载 {len(images)} 张图片")
                except Exception as e:
                    log(f"图片处理失败: {e}")
                
                # 防风控延迟
                import random
                time.sleep(random.uniform(1.5, 3.5))
                
            except Exception as e:
                log(f"处理笔记失败: {e}")
                # 即使失败，也要尝试生成基本的 Markdown
                content = f"处理失败: {str(e)}"
                # 尝试获取页面的基本信息
                try:
                    title = page.title() or "未命名"
                    # 尝试获取页面的部分文本
                    try:
                        page_text = page.inner_text('body').strip()[:1000]
                        content = f"处理失败: {str(e)}\n\n页面预览: {page_text}"
                    except:
                        pass
                except:
                    pass
            finally:
                try:
                    context.close()
                    browser.close()
                except Exception:
                    pass
        
        # 生成 Markdown
        title_safe = sanitize_windows_filename(title, max_length=80)
        md_path = os.path.join(output_dir, f"{title_safe}.md")
        
        # 生成图片 Markdown
        img_md = ""
        for img_path in images:
            rel_path = os.path.relpath(img_path, output_dir).replace("\\", "/")
            img_md += f"\n![图片]({rel_path})\n"
        
        # 直接使用 LM Studio 排版
        try:
            polished = polish_text(content)
        except Exception as e:
            log(f"文本排版失败: {e}")
            polished = {'polished': content, 'summary': ''}
        
        # 生成 Markdown 内容
        md_content = f"""---
title: '{yaml_single_quote(title)}'
source: "douyin"
type: "article"
url: '{yaml_single_quote(url)}'
date: "{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
---

# {title}

{polished.get('summary', '')}

## 核心内容

{polished.get('polished', content)}

## 图片
{img_md if img_md.strip() else '（无图片）'}

## 原始链接

{url}
"""
        
        try:
            with open(md_path, 'w', encoding='utf-8') as f:
                f.write(md_content)
            log(f"生成笔记 Markdown: {md_path}")
        except Exception as e:
            log(f"生成 Markdown 失败: {e}")
            md_path = None

        platform, content_id = _content_key(url)
        if platform == "douyin" and content_id and md_path and os.path.exists(md_path) and os.path.getsize(md_path) > 0:
            cache_md = _cached_md_path(output_dir, platform, content_id)
            try:
                os.makedirs(os.path.dirname(cache_md), exist_ok=True)
                shutil.copyfile(md_path, cache_md)
            except Exception:
                pass
        
        return {
            'title': title,
            'content': polished.get('polished', content),
            'media_paths': ([md_path] if md_path else []) + images,
            'tags': ['长文笔记', '抖音'],
            'source_url': url,
            'content_type': 'article'
        }


class XTextImageProcessor(BaseContentProcessor):
    """X平台文本图片处理器"""
    
    async def process(self, item: dict, progress_display: 'ProgressDisplay' = None) -> dict:
        url = item.get('url')
        output_dir = item.get('output_dir', str(Path(__file__).parent.absolute() / "output"))
        
        if progress_display:
            progress_display.advance_step("正在处理X内容", f"请求页面: {url}")
            
        # 使用现有的 X 平台文本图片处理逻辑
        import asyncio
        md_path = await asyncio.to_thread(run_x_text_image_pipeline, url, output_dir)
        
        # 提取图片路径
        images = []
        if md_path:
            md_dir = os.path.dirname(md_path)
            # 查找 x_images 目录
            x_images_dir = str(Path(md_dir) / "x_images")
            if os.path.exists(x_images_dir):
                for root, dirs, files in os.walk(x_images_dir):
                    for file in files:
                        if file.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif')):
                            images.append(os.path.join(root, file))
        
        return {
            'title': os.path.splitext(os.path.basename(md_path))[0] if md_path else '未命名',
            'content': 'X平台内容',
            'media_paths': [md_path] + images,
            'tags': ['X平台', '文本', '图片'],
            'source_url': url,
            'content_type': 'x_text_image'
        }


# ============ 工具函数 ============
def sanitize_single_line(text: str) -> str:
    if text is None:
        return ""
    s = str(text)
    s = s.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    s = re.sub(r"[\x00-\x1f\x7f]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def sanitize_windows_filename(name: str, max_length: int = 120) -> str:
    s = sanitize_single_line(name)
    s = re.sub(r'[\\/:*?"<>|]', "_", s)
    s = s.rstrip(" .")
    if not s:
        s = "未命名"
    base = s.split(".", 1)[0].upper()
    if base in _WIN_RESERVED_NAMES:
        s = "_" + s
    if len(s) > max_length:
        s = s[:max_length].rstrip(" .")
    if not s:
        s = "未命名"
    return s


def yaml_single_quote(text: str) -> str:
    return sanitize_single_line(text).replace("'", "''")


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def extract_url(text: str) -> str:
    """从分享文案中提取抖音URL"""
    patterns = [
        r'https?://v\.douyin\.com/[a-zA-Z0-9/?=&]+',
        r'https?://www\.douyin\.com/video/\d+',
        r'https?://www\.iesdouyin\.com/share/video/\d+',
        r'https?://x\.com/(?:i/web/)?[^/\s]+/(?:status|post)/\d+',
        r'https?://x\.com/(?:i/web/)?(?:status|post)/\d+',
        r'https?://twitter\.com/(?:i/web/)?[^/\s]+/(?:status|post)/\d+',
        r'https?://twitter\.com/(?:i/web/)?(?:status|post)/\d+',
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            return m.group(0)
    return text.strip()


def extract_douyin_video_id(url: str) -> str:
    m = re.search(r"/(?:video|note|photo)/(\d+)", url)
    return m.group(1) if m else ""


def purge_zero_byte_media(output_dir: str, video_id: str) -> int:
    if not video_id:
        return 0
    removed = 0
    for root, dirs, files in os.walk(output_dir):
        for f in files:
            if video_id not in f:
                continue
            lf = f.lower()
            if not lf.endswith((".mp4", ".mkv", ".webm", ".mp3", ".m4a")):
                continue
            p = os.path.join(root, f)
            try:
                if os.path.getsize(p) == 0:
                    os.remove(p)
                    removed += 1
            except Exception:
                pass
    return removed


def _cache_dir(output_dir: str, platform: str, content_id: str) -> str:
    if not content_id:
        return ""
    return str(Path(output_dir) / "_cache" / platform / content_id)


def _cached_md_path(output_dir: str, platform: str, content_id: str) -> str:
    base = _cache_dir(output_dir, platform, content_id)
    return str(Path(base) / "result.md") if base else ""


def _load_json(path: str):
    try:
        if path and os.path.exists(path) and os.path.getsize(path) > 0:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        return None
    return None


def _save_json(path: str, data) -> bool:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def _content_key(url: str) -> tuple:
    domain = urlparse(url).netloc.lower()
    if "douyin.com" in domain or "iesdouyin.com" in domain:
        cid = extract_douyin_video_id(url)
        if cid:
            return ("douyin", cid)
    if "x.com" in domain or "twitter.com" in domain:
        cid = extract_x_status_id(url)
        if cid:
            return ("x", cid)
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    return ("url", h)


def mp4_matches_video_id(mp4_path: str, video_id: str) -> bool:
    if not video_id:
        return False
    json_path = mp4_path[:-4] + "_data.json" if mp4_path.lower().endswith(".mp4") else ""
    if not json_path or not os.path.exists(json_path):
        return False
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        candidates = [
            str(data.get("aweme_id", "")),
            str(data.get("item_id", "")),
            str(data.get("id", "")),
            str(data.get("share_url", "")),
        ]
        return any(video_id and video_id in c for c in candidates if c)
    except Exception:
        return False


def get_proxy_server_for_ytdlp() -> str:
    for k in ("YTDLP_PROXY", "PW_PROXY", "PLAYWRIGHT_PROXY_SERVER", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
        v = (os.environ.get(k) or "").strip()
        if not v:
            continue
        if "://" not in v:
            v = "http://" + v
        return v
    return ""


def download_douyin_note(url: str, output_dir: str) -> tuple:
    """下载抖音图文笔记（使用 jiji262/douyin-downloader）
    返回 (metadata_dict, image_paths_list)
    """
    log(f"下载图文笔记: {url}")
    os.makedirs(output_dir, exist_ok=True)
    
    # 确保cookies是最新的
    ensure_cookies_updated()

    config_path = os.path.join(os.path.dirname(DOWNLOADER_SCRIPT), "config.yml")
    if not os.path.exists(config_path):
        log("警告：downloader 配置文件不存在，图文下载可能失败。")

    # 准备命令
    cmd = [
        sys.executable, DOWNLOADER_SCRIPT,
        "-u", url,
        "-p", output_dir,
        "-c", config_path,
        "-v"
    ]
    
    before_files = set()
    for root, dirs, files in os.walk(output_dir):
        for f in files:
            before_files.add(os.path.join(root, f))
            
    start_ts = time.time()
    max_retries = 3
    retry_count = 0
    r = None
    
    while retry_count < max_retries:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=600)
            break
        except subprocess.TimeoutExpired:
            retry_count += 1
            log(f"下载超时，重试 ({retry_count}/{max_retries})...")
            time.sleep(2)
        except Exception as e:
            retry_count += 1
            log(f"下载失败，重试 ({retry_count}/{max_retries}): {e}")
            time.sleep(2)
            
    after_files = set()
    for root, dirs, files in os.walk(output_dir):
        for f in files:
            after_files.add(os.path.join(root, f))
            
    new_files = list(after_files - before_files)
    
    # 获取 _data.json
    json_files = [f for f in new_files if f.endswith("_data.json")]
    if not json_files:
        # 如果没有新文件，按 note_id 查找最新的 json
        note_id = extract_douyin_video_id(url) # extract_douyin_video_id 也能提取 note_id
        if note_id:
            all_jsons = []
            for root, dirs, files in os.walk(output_dir):
                for f in files:
                    if f.endswith("_data.json"):
                        all_jsons.append(os.path.join(root, f))
            all_jsons.sort(key=os.path.getmtime, reverse=True)
            for jf in all_jsons[:80]:
                try:
                    with open(jf, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if note_id in str(data.get("aweme_id", "")):
                        json_files = [jf]
                        break
                except:
                    pass

    metadata = {}
    if json_files:
        json_path = json_files[0]
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                metadata = json.load(f)
        except Exception as e:
            log(f"读取JSON失败: {e}")
            
    # 图文笔记的图片通常是在与 JSON 同级目录下，文件名包含 _xx.jpg 或 _xx.webp
    # 或者我们直接收集这批新文件中的所有图片
    # dy-downloader 图集命名格式可能是：标题_aweme_id_01.jpg
    image_paths = []
    
    if json_files:
        # 根据 json 所在目录找图片
        target_dir = os.path.dirname(json_files[0])
        for f in os.listdir(target_dir):
            f_lower = f.lower()
            if f_lower.endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif')):
                # 排除头像和封面
                if "_avatar." in f_lower or "_cover." in f_lower:
                    continue
                full_path = os.path.join(target_dir, f)
                try:
                    if os.path.getsize(full_path) < 20 * 1024:
                        continue
                except OSError:
                    continue
                image_paths.append(full_path)
        image_paths.sort()
        
    return metadata, image_paths

async def download_douyin_video(url: str, output_dir: str) -> str:
    """下载抖音视频（使用 jiji262/douyin-downloader）"""
    log(f"下载视频: {url}")
    os.makedirs(output_dir, exist_ok=True)
    
    # 确保cookies是最新的
    ensure_cookies_updated()

    # 检查配置文件
    config_path = os.path.join(os.path.dirname(DOWNLOADER_SCRIPT), "config.yml")
    if not os.path.exists(config_path):
        log("警告：downloader 配置文件不存在，创建默认配置...")
        # 创建默认配置文件
        default_config = """link:
  -

path: ./Downloaded/

music: true
cover: true
avatar: true
json: true

start_time: ""
end_time: ""

folderstyle: true

mode:
  - post

number:
  post: 1
  like: 0
  allmix: 0
  mix: 0
  music: 0
  collect: 0
  collectmix: 0

increase:
  post: false
  like: false
  allmix: false
  mix: false
  music: false

thread: 5
retry_times: 3
proxy: ""
database: true
database_path: dy_downloader.db

progress:
  quiet_logs: true

cookies:
  msToken: ""
  ttwid: ""
  odin_tt: ""
  passport_csrf_token: ""
  sid_guard: ""
"""
        try:
            with open(config_path, 'w', encoding='utf-8') as f:
                f.write(default_config)
            log("成功创建默认配置文件")
        except Exception as e:
            log(f"创建配置文件失败: {e}")

    # 准备命令
    cmd = [
        sys.executable, DOWNLOADER_SCRIPT,
        "-u", url,
        "-p", output_dir,
        "-c", config_path,
        "-v" # 开启详细日志以捕获信息
    ]
    
    log(f"运行下载器...")
    # 注意：jiji262/douyin-downloader 可能会在输出目录创建子文件夹（按作者名或日期）
    # 我们需要监控输出目录的变化来找到新下载的文件
    video_id = extract_douyin_video_id(url)
    removed = purge_zero_byte_media(output_dir, video_id)
    if removed:
        log(f"已清理 {removed} 个 0 字节占坑文件（aweme_id={video_id}）")

    before_files = set()
    for root, dirs, files in os.walk(output_dir):
        for f in files:
            before_files.add(os.path.join(root, f))
    
    start_ts = time.time()
    max_retries = 3
    retry_count = 0
    r = None
    
    while retry_count < max_retries:
        try:
            # 使用 asyncio.to_thread 运行阻塞的 subprocess
            r = await asyncio.to_thread(lambda: subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=600))
            break
        except subprocess.TimeoutExpired:
            retry_count += 1
            log(f"下载超时，重试 ({retry_count}/{max_retries})...")
            await asyncio.sleep(2)
        except Exception as e:
            retry_count += 1
            log(f"下载失败，重试 ({retry_count}/{max_retries}): {e}")
            await asyncio.sleep(2)
    
    if r is None:
        log("下载器执行失败，尝试使用备用方法...")
        # 尝试使用 yt-dlp 作为备用
        try:
            return await download_ytdlp_video(url, output_dir)
        except Exception as e:
            log(f"备用方法也失败: {e}")
            raise RuntimeError("下载失败，请检查网络连接和配置")
    
    # 无论下载器是否成功，都尝试寻找MP4文件
    log("开始寻找MP4文件...")
    
    if r.returncode != 0:
        log(f"下载器返回错误码: {r.returncode}")
        log(f"错误详情: {r.stderr}")
        # 不直接抛出异常，尝试继续寻找文件，因为有时即使报错也可能下载成功了部分内容
        log("警告：下载器返回非零状态码，尝试继续寻找已下载文件。")
    else:
        # 即使 returncode 是 0，也要检查是否有跳过的提示
        if "Skipped      │     1" in (r.stdout or ""):
            log("警告：视频被跳过下载（可能之前已下载过，或存在 0 字节占坑文件）")
            removed2 = purge_zero_byte_media(output_dir, video_id)
            if removed2:
                log(f"检测到 Skipped 后清理 {removed2} 个 0 字节文件，尝试重新下载（aweme_id={video_id}）")
                r = await asyncio.to_thread(lambda: subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=600))
             
    log("下载命令执行完毕，寻找文件...")

    # 寻找新下载的文件
    after_files = set()
    for root, dirs, files in os.walk(output_dir):
        for f in files:
            after_files.add(os.path.join(root, f))
            
    new_files = list(after_files - before_files)
    
    # 1. 优先寻找新创建的MP4文件
    new_mp4s = [p for p in new_files if p.lower().endswith(".mp4") and os.path.getmtime(p) >= start_ts - 2 and os.path.getsize(p) > 0]
    if new_mp4s:
        chosen = max(new_mp4s, key=os.path.getmtime)
        log(f"找到新下载的MP4文件: {chosen}")
        return chosen

    # 2. 按视频ID查找
    if video_id:
        all_mp4s = []
        for root, dirs, files in os.walk(output_dir):
            for f in files:
                if f.lower().endswith(".mp4"):
                    full_path = os.path.join(root, f)
                    all_mp4s.append(full_path)
        all_mp4s.sort(key=os.path.getmtime, reverse=True)
        for p in all_mp4s[:80]:
            if mp4_matches_video_id(p, video_id):
                log(f"按视频ID找到MP4文件: {p}")
                # 检查文件大小，如果太小（例如 < 100KB），可能不是有效视频
                size = os.path.getsize(p)
                if size < 100 * 1024:
                    log(f"警告：找到的视频文件太小 ({size} bytes)，可能无效")
                    if size == 0:
                        try:
                            os.remove(p)
                        except:
                            pass
                        continue
                return p
    
    if video_id:
        all_mp4s = []
        for root, dirs, files in os.walk(output_dir):
            for f in files:
                if f.lower().endswith(".mp4") and video_id in f:
                    full_path = os.path.join(root, f)
                    all_mp4s.append((os.path.getmtime(full_path), full_path))
        
        if all_mp4s:
            all_mp4s.sort(reverse=True)
            for mtime, path in all_mp4s[:5]:
                if mtime >= start_ts - 300 and os.path.getsize(path) > 0:
                    log(f"找到相关的最近修改文件: {path}")
                    return path
    
    all_jsons = []
    for root, dirs, files in os.walk(output_dir):
        for f in files:
            if f.endswith("_data.json"):
                all_jsons.append(os.path.join(root, f))
    all_jsons.sort(key=os.path.getmtime, reverse=True)
    for jf in all_jsons[:5]:
        if video_id and video_id in jf and os.path.getmtime(jf) >= start_ts - 60:
            try:
                with open(jf, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("article_info") or str(data.get("aweme_type", "")) in ("163",):
                    log("检测到该链接实际为长文笔记，触发处理器切换...")
                    raise RuntimeError("IS_ARTICLE_DETECTED")
                if data.get("image_post_info") or data.get("images") or data.get("media_type") == "gallery" or len(data.get("desc", "")) > 500:
                    log("检测到该链接实际为图文或长文笔记，触发处理器切换...")
                    raise RuntimeError("IS_NOTE_DETECTED")
            except Exception as e:
                if str(e) in ("IS_NOTE_DETECTED", "IS_ARTICLE_DETECTED"):
                    raise
                pass

    log("所有方法都失败，尝试使用yt-dlp作为最终备用...")
    try:
        return await download_ytdlp_video(url, output_dir)
    except Exception as e:
        err = str(e)
        log(f"yt-dlp备用也失败: {e}")
        if ("Fresh cookies" in err) or ("Failed to parse JSON" in err):
            log("检测到风控/反爬拦截，尝试使用 Playwright 浏览器回退下载...")
            try:
                return await download_douyin_video_via_playwright(url, output_dir)
            except Exception as e2:
                log(f"Playwright 回退也失败: {e2}")
        raise RuntimeError("未找到本次下载产生的 MP4 文件")


async def download_ytdlp_video(url: str, output_dir: str) -> str:
    log(f"下载视频: {url}")
    os.makedirs(output_dir, exist_ok=True)

    before_files = set()
    for root, dirs, files in os.walk(output_dir):
        for f in files:
            before_files.add(os.path.join(root, f))

    ytdlp_bin = YTDLP_BIN
    # 优先使用生成的 cookies.txt
    cookies_path = str(Path(__file__).parent.absolute() / "cookies.txt")
    if not os.path.exists(cookies_path):
        cookies_path = YTDLP_COOKIES
    
    proxy_server = get_proxy_server_for_ytdlp()

    cmd = [
        ytdlp_bin,
        "--no-cache-dir",
        "--force-ipv4",
        "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "--add-header", "Referer:https://www.douyin.com/",
        "--add-header", "Origin:https://www.douyin.com",
        "--retries", "8",
        "--extractor-retries", "8",
        "--fragment-retries", "8",
        "--socket-timeout", "45",
        "--no-playlist",
        "--windows-filenames",
        "--trim-filenames", "180",
        "--merge-output-format", "mp4",
        "--paths", output_dir,
        "-o", "%(uploader)s/%(upload_date)s_%(title).80B_%(id)s.%(ext)s",
        "--no-check-certificate",
        url,
    ]
    if proxy_server:
        cmd.insert(1, proxy_server)
        cmd.insert(1, "--proxy")
    if cookies_path and os.path.exists(cookies_path) and os.path.getsize(cookies_path) > 0:
        cmd.insert(1, cookies_path)
        cmd.insert(1, "--cookies")

    start_ts = time.time()
    # 使用 asyncio.to_thread 运行阻塞的 subprocess
    import asyncio
    r = await asyncio.to_thread(lambda: subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=900))
    
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "")[-600:]
        raise RuntimeError(f"yt-dlp 下载失败: {tail}")

    after_files = set()
    for root, dirs, files in os.walk(output_dir):
        for f in files:
            after_files.add(os.path.join(root, f))

    new_files = list(after_files - before_files)
    candidates = []
    for p in new_files:
        ext = os.path.splitext(p)[1].lower()
        if ext in (".mp4", ".mkv", ".webm") and os.path.getmtime(p) >= start_ts - 2 and os.path.getsize(p) > 0:
            candidates.append(p)

    if candidates:
        chosen = max(candidates, key=os.path.getmtime)
        return chosen

    raise RuntimeError("yt-dlp 未找到本次下载产生的视频文件")


async def download_douyin_video_via_playwright(url: str, output_dir: str) -> str:
    os.makedirs(output_dir, exist_ok=True)
    try:
        from playwright.async_api import async_playwright
    except Exception:
        raise RuntimeError("Playwright 未安装，无法使用浏览器回退下载")

    storage_state_path = str(Path(__file__).parent.absolute() / "storage_state.json")
    user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    detail_data = {"aweme_id": "", "title": "", "play_url": "", "raw": None}

    def pick_play_url(d: dict) -> str:
        try:
            aweme = d.get("aweme_detail") or {}
            video = aweme.get("video") or {}
            play_addr = video.get("play_addr") or {}
            urls = play_addr.get("url_list") or []
            for u in urls:
                if isinstance(u, str) and u.strip():
                    return u.strip()
        except Exception:
            pass
        return ""

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, channel="chrome")
        context_kwargs = {
            "user_agent": user_agent,
            "locale": "zh-CN",
            "viewport": {"width": 1366, "height": 850},
            "timezone_id": "Asia/Shanghai",
            "ignore_https_errors": True,
        }
        if os.path.exists(storage_state_path) and os.path.getsize(storage_state_path) > 0:
            context_kwargs["storage_state"] = storage_state_path
        context = await browser.new_context(**context_kwargs)
        page = await context.new_page()

        async def on_response(resp):
            try:
                u = (resp.url or "").lower()
                # 捕获详情接口或搜索接口中的 aweme 数据
                if "/aweme/v1/web/aweme/detail/" not in u and "/aweme/v1/web/search/item/" not in u:
                    return
                if resp.status != 200:
                    return
                
                # 有时 JSON 解析会失败
                try:
                    data = await resp.json()
                except:
                    return

                play_url = pick_play_url(data)
                if not play_url and "aweme_list" in data:
                    # 如果是搜索接口，取列表第一个
                    for item in data.get("aweme_list", []):
                        play_url = pick_play_url({"aweme_detail": item})
                        if play_url:
                            data = {"aweme_detail": item}
                            break

                if play_url:
                    aweme = data.get("aweme_detail") or {}
                    detail_data["aweme_id"] = str(aweme.get("aweme_id") or "")
                    detail_data["title"] = sanitize_windows_filename((aweme.get("desc") or "douyin_video")[:60], max_length=80)
                    detail_data["play_url"] = play_url
                    detail_data["raw"] = data
                    log(f"Playwright 捕获到视频数据: {detail_data['aweme_id']}")
            except Exception as e:
                return

        page.on("response", lambda res: asyncio.create_task(on_response(res)))
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            try:
                t = await page.title()
                u = (page.url or "").lower()
                if ("验证码" in (t or "")) or ("captcha" in u) or ("verify" in u) or ("security" in u):
                    raise RuntimeError("DOUYIN_CAPTCHA")
            except Exception as e:
                if str(e) == "DOUYIN_CAPTCHA":
                    raise
            # 轮询等待数据捕获，最长等待 20s
            for _ in range(20):
                if detail_data.get("play_url"):
                    break
                await page.wait_for_timeout(1000)
        finally:
            try:
                await context.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass

    play_url = detail_data.get("play_url") or ""
    if not play_url:
        raise RuntimeError("浏览器回退未捕获到 aweme detail 数据（可能遇到验证码/需要登录 cookies）")

    aweme_id = detail_data.get("aweme_id") or extract_douyin_video_id(url) or str(int(time.time()))
    title = detail_data.get("title") or f"douyin_{aweme_id}"
    out_path = os.path.join(output_dir, f"{title}_{aweme_id}.mp4")

    try:
        import requests
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        headers = {
            "User-Agent": user_agent,
            "Referer": "https://www.douyin.com/",
            "Accept": "*/*",
            "Connection": "close",
        }
        # 这里可以使用 aiohttp 优化，但 requests 在异步函数中配合 asyncio.to_thread 也可以
        def _download():
            s = requests.Session()
            s.trust_env = False
            with s.get(play_url, headers=headers, stream=True, timeout=90, verify=False, allow_redirects=True) as r:
                r.raise_for_status()
                with open(out_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 256):
                        if chunk:
                            f.write(chunk)
            return out_path

        await asyncio.to_thread(_download)
        
        if os.path.exists(out_path) and os.path.getsize(out_path) > 1024 * 64:
            log(f"Playwright回退下载成功: {out_path}")
            return out_path
    except Exception as e:
        raise RuntimeError(f"Playwright回退下载失败: {e}")

    raise RuntimeError("Playwright回退未生成有效视频文件")


async def download_video(url: str, output_dir: str) -> str:
    domain = urlparse(url).netloc.lower()
    if "x.com" in domain or "twitter.com" in domain:
        return await download_ytdlp_video(url, output_dir)
    return await download_douyin_video(url, output_dir)


def extract_x_status_id(url: str) -> str:
    m = re.search(r"/(?:status|post)/(\d+)", url)
    return m.group(1) if m else ""


def fetch_x_tweet_json(status_id: str, lang: str = "zh") -> dict:
    api = f"https://cdn.syndication.twimg.com/tweet-result?id={status_id}&lang={lang}"
    proxy_server = get_proxy_server_for_ytdlp()
    
    try:
        import requests
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        proxies = {}
        if proxy_server:
            proxies = {"http": proxy_server, "https": proxy_server}
            
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            "Accept": "application/json,text/plain,*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Connection": "keep-alive",
        }

        s = requests.Session()
        s.trust_env = False
        resp = s.get(api, headers=headers, proxies=proxies, timeout=30, verify=False)
        if resp.status_code == 200:
            return resp.json()
        else:
            log(f"获取 tweet JSON 失败，状态码: {resp.status_code}")
            return {}
    except Exception as e:
        log(f"获取 tweet JSON 异常: {e}")
        return {}


def fetch_x_tweet_via_ytdlp(url: str) -> dict:
    ytdlp_bin = os.environ.get("YTDLP_BIN", "yt-dlp")
    cookies_path = os.environ.get("YTDLP_COOKIES", COOKIE_FILE)
    proxy_server = get_proxy_server_for_ytdlp()
    cmd = [
        ytdlp_bin,
        "--force-ipv4",
        "--retries", "8",
        "--extractor-retries", "8",
        "--socket-timeout", "45",
        "--skip-download",
        "--dump-single-json",
        "--ignore-no-formats-error",
        "--no-warnings",
        "--no-playlist",
        "--no-check-certificate",
        url,
    ]
    if proxy_server:
        cmd.insert(1, proxy_server)
        cmd.insert(1, "--proxy")
    if cookies_path and os.path.exists(cookies_path) and os.path.getsize(cookies_path) > 0:
        cmd.insert(1, cookies_path)
        cmd.insert(1, "--cookies")
    
    log(f"运行 yt-dlp 命令: {' '.join(cmd)}")
    r = subprocess.run(cmd, capture_output=True, timeout=120)
    
    if r.returncode != 0:
        log(f"yt-dlp 命令失败，返回码: {r.returncode}")
        log(f"错误输出: {r.stderr.decode('utf-8', errors='replace')[:500]}")
        return {}
    
    try:
        s = r.stdout.decode("utf-8-sig", errors="replace").strip()
        if not s:
            log("yt-dlp 输出为空")
            return {}
        result = json.loads(s)
        log(f"成功获取 tweet 数据，包含键: {list(result.keys())}")
        return result
    except Exception as e:
        log(f"解析 yt-dlp 输出失败: {e}")
        log(f"原始输出: {r.stdout.decode('utf-8', errors='replace')[:500]}")
        return {}



def resolve_short_url(url: str) -> str:
    """解析短链接，获取原始 URL"""
    try:
        import requests
        import urllib3
        import re
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        proxy_server = get_proxy_server_for_ytdlp()
        proxies = {}
        if proxy_server:
            proxies = {"http": proxy_server, "https": proxy_server}
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://www.douyin.com/",
            "Connection": "close",
        }
        
        session = requests.Session()
        session.trust_env = False
        
        # 清理 URL 中的特殊字符
        url = url.replace('ˇˇ', '').replace('➝➝', '').strip()
        log(f"开始解析短链接: {url}")
        
        # 尝试 HEAD
        try:
            resp = session.head(url, headers=headers, proxies=proxies, timeout=12, allow_redirects=True, verify=False)
            log(f"HEAD 请求状态码: {resp.status_code}")
            log(f"HEAD 请求最终 URL: {resp.url}")
            if resp.status_code < 400 and resp.url != url:
                return resp.url
        except Exception as e:
            log(f"HEAD 请求失败: {e}")
            pass
            
        # 尝试 GET (处理 meta-refresh)
        resp = session.get(url, headers=headers, proxies=proxies, timeout=15, allow_redirects=True, verify=False)
        log(f"GET 请求状态码: {resp.status_code}")
        log(f"GET 请求最终 URL: {resp.url}")
        if resp.url != url:
            return resp.url
            
        # 检查 HTML 中是否有 meta-refresh
        content = resp.text
        # <meta http-equiv="refresh" content="0;URL=https://...">
        match = re.search(r'content="0;\s*URL=([^"]+)"', content, re.I)
        if match:
            new_url = match.group(1)
            log(f"从 Meta Refresh 中发现跳转: {new_url}")
            return new_url
        
        # 检查 HTML 中是否有 script 跳转
        script_match = re.search(r'window\.location\.href\s*=\s*["\']([^"\']+)["\']', content, re.I)
        if script_match:
            new_url = script_match.group(1)
            log(f"从脚本中发现跳转: {new_url}")
            return new_url
        
        # 检查是否有其他跳转方式
        if "window.location" in content:
            log("发现 window.location 跳转脚本")
        if "location.href" in content:
            log("发现 location.href 跳转脚本")
            
    except Exception as e:
        log(f"解析短链接最终失败 ({url}): {e}")
        
    return url


def resolve_short_url_with_ytdlp(url: str) -> str:
    """使用 yt-dlp 解析短链接"""
    import subprocess
    ytdlp_bin = os.environ.get("YTDLP_BIN", "yt-dlp")
    # 清理 URL 杂质字符
    url = _clean_extracted_url(url)
    if not url: return ""
    
    cmd = [
        ytdlp_bin,
        "--no-cache-dir",
        "--force-ipv4",
        "--retries", "2",
        "--socket-timeout", "10",
        "--skip-download",
        "--get-url",
        url
    ]
    # 添加代理支持
    proxy_server = get_proxy_server_for_ytdlp()
    if proxy_server:
        cmd.insert(1, proxy_server)
        cmd.insert(1, "--proxy")
        
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            resolved_url = result.stdout.strip()
            if resolved_url:
                log(f"使用 yt-dlp 解析短链接成功: {resolved_url}")
                return resolved_url
    except Exception as e:
        log(f"使用 yt-dlp 解析短链接失败: {e}")
    return url


def _normalize_x_image_url(u: str) -> str:
    try:
        from urllib.parse import urlsplit, urlunsplit, parse_qs, urlencode
        parts = urlsplit(u)
        host = (parts.netloc or "").lower()
        if not host:
            return u
        
        # 严格排除头像、背景图、表情等干扰项
        path = parts.path.lower()
        noise_keywords = ["profile_images", "profile_banners", "emoji", "sticky", "default_profile"]
        if any(k in path for k in noise_keywords):
            return "" # 直接返回空，表示这不是我们要的媒体
            
        qs = parse_qs(parts.query, keep_blank_values=True)
        if host.endswith("twimg.com"):
            # 只有媒体内容才处理
            is_media = any(x in path for x in ["/media/", "/ext_tw_video_thumb/", "/amplify_video_thumb/"])
            if is_media:
                if "name" in qs:
                    qs["name"] = ["orig"]
                elif not path.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
                    qs["name"] = ["orig"]
            else:
                # 如果不是 media 路径，但又是 twimg.com 的图片，可能也是我们要的
                if not any(ext in path for ext in [".jpg", ".jpeg", ".png", ".webp", ".gif"]):
                    # 既没路径特征也没后缀，大概率是 UI 元素
                    return ""
        
        query = urlencode(qs, doseq=True)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))
    except Exception:
        return u


def _clean_extracted_url(u: str) -> str:
    if not u:
        return ""
    try:
        import html as _html
        u = _html.unescape(str(u))
    except Exception:
        u = str(u)
    
    # 全量替换常见的干扰字符
    for char in ["`", "'", "\"", "<", ">", "[", "]", "(", ")", "{", "}", "\t", "\r", "\n"]:
        u = u.replace(char, "")
    
    u = u.strip()
    # 再次清理前后的标点符号
    while u and u[-1] in ".,;:!?，。；：！？ \t\r\n":
        u = u[:-1]
    while u and u[0] in " \t\r\n":
        u = u[1:]
    
    u = u.strip()
    if not u.startswith(("http://", "https://")):
        return ""
    return u


def _download_url_to_file(url: str, out_path: str) -> None:
    try:
        import requests
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        proxy_server = get_proxy_server_for_ytdlp()
        proxies = {}
        if proxy_server:
            proxies = {"http": proxy_server, "https": proxy_server}
            
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://x.com/",
            "Connection": "keep-alive",
        }
        
        last_err = None
        for i in range(3):
            try:
                # 显式禁用 trust_env 解决可能的系统代理冲突
                session = requests.Session()
                session.trust_env = False
                resp = session.get(url, headers=headers, proxies=proxies, timeout=30, verify=False)
                resp.raise_for_status()
                
                # 验证是否真的是图片
                ctype = resp.headers.get("Content-Type", "").lower()
                if "image" not in ctype and "application/octet-stream" not in ctype:
                    log(f"警告: 下载的内容可能不是图片 (Content-Type: {ctype})")
                
                with open(out_path, "wb") as f:
                    f.write(resp.content)
                return
            except Exception as e:
                last_err = e
                log(f"下载尝试 {i+1} 失败: {e}")
                time.sleep(1 + i)
        raise last_err
    except Exception as e:
        log(f"下载图片最终失败: {url}, 错误: {e}")
        raise e


def _extract_x_image_urls_via_playwright(tweet_url: str, status_id: str) -> list:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return []
    proxy_server = get_proxy_server_for_ytdlp()
    url = _clean_extracted_url(tweet_url) or (tweet_url or "").strip().replace("`", "").strip()
    if status_id and "/photo/" not in url:
        url = f"https://x.com/i/status/{status_id}"
    launch_kwargs = {
        "headless": (os.environ.get("X_PW_HEADLESS", "1").strip() not in ("0", "false", "no")),
        "args": ["--disable-quic", "--ignore-certificate-errors"],
    }
    if proxy_server:
        launch_kwargs["proxy"] = {"server": proxy_server}
    storage_state_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "storage_state_x.json")
    user_data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pw_chrome_profile_x")
    srcs = []
    with sync_playwright() as p:
        context = None
        browser = None
        try:
            use_persistent = False
            try:
                use_persistent = os.path.isdir(user_data_dir) and any(os.scandir(user_data_dir))
            except Exception:
                use_persistent = False

            if use_persistent:
                context_launch = {
                    "user_data_dir": user_data_dir,
                    "headless": launch_kwargs["headless"],
                    "channel": "chrome",
                    "args": launch_kwargs["args"],
                    "locale": "zh-CN",
                    "ignore_https_errors": True,
                }
                if proxy_server:
                    context_launch["proxy"] = {"server": proxy_server}
                context = p.chromium.launch_persistent_context(**context_launch)
            else:
                browser = p.chromium.launch(**launch_kwargs)
                context_kwargs = {
                    "ignore_https_errors": True,
                    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
                    "locale": "zh-CN",
                    "viewport": {"width": 1366, "height": 850},
                    "timezone_id": "Asia/Shanghai",
                }
                if os.path.exists(storage_state_path) and os.path.getsize(storage_state_path) > 0:
                    context_kwargs["storage_state"] = storage_state_path
                context = browser.new_context(**context_kwargs)

            page = context.pages[0] if context and getattr(context, "pages", None) else None
            if not page:
                page = context.new_page()
            log(f"Playwright 正在访问: {url}")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except Exception:
                pass
            
            # 等待图片渲染
            page.wait_for_timeout(8000)
            
            # 处理“敏感内容”警告
            try:
                # 寻找包含“查看”或“View”字样的按钮
                view_buttons = page.locator('div[role="button"]:has-text("View"), div[role="button"]:has-text("查看")').all()
                for btn in view_buttons:
                    if btn.is_visible():
                        log("点击敏感内容查看按钮...")
                        btn.click()
                        page.wait_for_timeout(3000)
            except Exception as e:
                log(f"尝试处理敏感内容警告失败: {e}")
            
            # 尝试滚动一下触发懒加载
            page.mouse.wheel(0, 1500)
            page.wait_for_timeout(3000)

            try:
                ph = page.locator('[data-testid="tweetPhoto"] img').first
                if ph and ph.count() > 0:
                    try:
                        ph.click(timeout=2000)
                        page.wait_for_timeout(1500)
                    except Exception:
                        pass
            except Exception:
                pass
            
            try:
                # 针对 X 的多种图片提取策略 (使用更强大的选择器和过滤)
                srcs = page.evaluate("""() => {
                    const results = [];
                    // 1. 寻找推文正文下方的媒体容器 (pbs.twimg.com/media)
                    // 重点寻找带有 data-testid="tweetPhoto" 的容器
                    document.querySelectorAll('[data-testid="tweetPhoto"] img').forEach(img => {
                        const src = img.currentSrc || img.src;
                        if (src) results.push(src);
                    });
                    
                    // 2. 寻找大图模式下的图片
                    const modalImg = document.querySelector('div[aria-modal="true"] img');
                    if (modalImg && (modalImg.currentSrc || modalImg.src)) {
                        results.push(modalImg.currentSrc || modalImg.src);
                    }
                    
                    // 3. 寻找卡片中的图片
                    document.querySelectorAll('[data-testid="card.wrapper"] img').forEach(img => {
                        const src = img.currentSrc || img.src;
                        if (src) results.push(src);
                    });
                    
                    // 4. 寻找所有 img 标签，但排除头像
                    document.querySelectorAll('img').forEach(img => {
                        const src = img.currentSrc || img.src;
                        if (src && !src.includes('profile_images') && !src.includes('profile_banners')) {
                            // 只保留来自 twimg.com 的高清媒体或视频缩略图
                            if (src.includes('pbs.twimg.com/media') || src.includes('video_thumb')) {
                                results.push(src);
                            }
                        }
                    });
                    
                    return [...new Set(results)]; // JS 端去重
                }""")
                log(f"Playwright 页面共发现 {len(srcs)} 个潜在媒体链接: {srcs}")
            except Exception as e:
                log(f"Playwright 执行 JS 提取失败: {e}")
                srcs = []
        finally:
            try:
                if context:
                    context.close()
            except Exception:
                pass
            try:
                if browser:
                    browser.close()
            except Exception:
                pass
    
    urls = []
    for s in srcs or []:
        u = _clean_extracted_url(s) or ""
        if not u:
            continue
        
        # 使用强化后的 _normalize_x_image_url 进行标准化和过滤
        normalized = _normalize_x_image_url(u)
        if normalized:
            urls.append(normalized)
        else:
            log(f"Playwright 过滤掉干扰 URL: {u}")
                
    uniq = []
    seen = set()
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def _extract_x_photo_urls(tweet: dict) -> list:
    urls = []
    if not isinstance(tweet, dict) or not tweet:
        return urls

    def _add_from(obj: dict):
        photos = obj.get("photos")
        if not isinstance(photos, list):
            return
        for p in photos:
            if not isinstance(p, dict):
                continue
            for k in ("url", "mediaUrl", "media_url_https", "media_url", "expanded_url"):
                u = p.get(k)
                if u:
                    urls.append(u)
            image = p.get("image")
            if isinstance(image, dict):
                u = image.get("url")
                if u:
                    urls.append(u)

    _add_from(tweet)
    for k in ("quoted_tweet", "quotedTweet", "retweeted_tweet", "retweetedTweet", "tweet"):
        v = tweet.get(k)
        if isinstance(v, dict):
            _add_from(v)
    return urls


def download_x_images(status_id: str, tweet: dict, output_dir: str, tweet_url: str = "") -> list:
    import os
    urls = []

    urls.extend(_extract_x_photo_urls(tweet))
    
    # 1. 优先尝试从常见的媒体字段提取
    media_fields = [
        tweet.get("mediaDetails"),
        tweet.get("media"),
        tweet.get("extended_entities", {}).get("media"),
        tweet.get("entities", {}).get("media"),
        tweet.get("includes", {}).get("media"),
    ]
    
    for media_source in media_fields:
        if not media_source: continue
        if isinstance(media_source, list):
            for m in media_source:
                if not isinstance(m, dict): continue
                # X 的媒体对象通常有 media_url_https
                u = (m.get("media_url_https") or m.get("media_url") or m.get("url") or m.get("expanded_url"))
                if u: urls.append(u)
    
    # 2. 尝试从 thumbnails 提取 (yt-dlp 常用)
    thumbs = tweet.get("thumbnails") or []
    if isinstance(thumbs, list):
        for t in thumbs:
            if isinstance(t, dict) and t.get("url"):
                urls.append(t["url"])
    
    # 3. 尝试从卡片信息提取
    card = tweet.get("card") or tweet.get("card_data") or {}
    if isinstance(card, dict):
        # 卡片中的图片可能有多种 key
        for k in ["image", "thumbnail_image", "player_image", "media_url", "promo_image"]:
            if card.get(k): urls.append(card[k])
    
    # 4. 尝试从格式列表中提取 (如果是图片，yt-dlp 可能会放这里)
    formats = tweet.get("formats") or []
    if isinstance(formats, list):
        for fmt in formats:
            if not isinstance(fmt, dict): continue
            if fmt.get("vcodec") == "none" and fmt.get("acodec") == "none":
                u = fmt.get("url")
                if u: urls.append(u)
    
    # 5. 从描述/正文中提取可能的媒体链接
    description = tweet.get("description") or tweet.get("text") or tweet.get("full_text") or ""
    if description:
        # 寻找 t.co 链接
        found_links = re.findall(r"https?://t\.co/[a-zA-Z0-9]+", description)
        if found_links:
            log(f"从描述中发现 t.co 链接: {found_links}")
            urls.extend(found_links)
    
    # 6. 去重并初步清理
    uniq = []
    seen = set()
    for u in urls:
        u = _clean_extracted_url(u)
        if not u: continue
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    urls = uniq
    
    log(f"待处理的 JSON 提取 URL 列表: {urls}")
    
    saved = []
    img_dir = os.path.join(output_dir, "x_images", status_id)
    os.makedirs(img_dir, exist_ok=True)
    pw_hint_url = ""

    for idx, raw_u in enumerate(urls, 1):
        # 再次清理 u，确保没有任何反引号或多余字符
        u = _clean_extracted_url(raw_u)
        if not u: continue
        
        # 针对 t.co 链接进行解析
        resolved_url = u
        if "t.co" in u:
            log(f"正在解析短链接: {u}")
            resolved_url = resolve_short_url(u)
            if resolved_url == u:
                # 再次尝试 yt-dlp 解析（带更短的超时）
                resolved_url = resolve_short_url_with_ytdlp(u)
            if "/photo/" in (resolved_url or ""):
                pw_hint_url = resolved_url
            
            # 这里的逻辑改进：即使解析出来的还是 X status，只要它和当前推文不一样，
            # 且我们还没抓到图片，就值得尝试（可能是推文中引用的媒体推文）
            if re.search(r"/(status|post)/\d+", resolved_url) and resolved_url != tweet_url:
                log(f"发现引用/相关推文: {resolved_url}")
                # 后面会交给 yt-dlp 尝试抓取媒体
        
        # 标准化 X 图片 URL
        final_url = _normalize_x_image_url(_clean_extracted_url(resolved_url) or resolved_url)
        if not final_url:
            continue
        
        # 判断是否是图片
        is_image = False
        parsed = urlparse(final_url)
        path = parsed.path.lower()
        query = parsed.query.lower()
        
        # X 图片特征：pbs.twimg.com 且包含 format= 或以图片扩展名结尾
        if "pbs.twimg.com" in final_url:
            if any(fmt in query or fmt in path for fmt in ["jpg", "jpeg", "png", "webp", "gif"]):
                is_image = True
        elif any(ext in path for ext in [".jpg", ".jpeg", ".png", ".webp", ".gif"]):
            is_image = True
        
        if is_image:
            # 确定扩展名
            ext = ".jpg"
            if "png" in path or "format=png" in query: ext = ".png"
            elif "webp" in path or "format=webp" in query: ext = ".webp"
            elif "gif" in path or "format=gif" in query: ext = ".gif"
            
            out = os.path.join(img_dir, f"{idx:02d}{ext}")
            try:
                _download_url_to_file(final_url, out)
                saved.append(out)
                log(f"成功下载图片: {final_url} -> {out}")
            except Exception as e:
                log(f"下载图片失败: {final_url}, 错误: {e}")
        else:
            # 如果不是直接的图片链接，只要它是媒体相关的，就尝试使用 yt-dlp 下载
            # 移除了过于严格的关键字检查
            log(f"尝试使用 yt-dlp 抓取潜在媒体: {final_url}")
            try:
                import subprocess
                import tempfile
                import shutil
                with tempfile.TemporaryDirectory() as temp_dir:
                    cmd = [
                        os.environ.get("YTDLP_BIN", "yt-dlp"),
                        "--no-cache-dir", "--force-ipv4", "--retries", "2",
                        "--socket-timeout", "10", "--no-playlist",
                        "--windows-filenames", "--paths", temp_dir,
                        "-o", "%(id)s.%(ext)s", final_url
                    ]
                    # 注入代理和 cookies
                    proxy = get_proxy_server_for_ytdlp()
                    if proxy: cmd.insert(1, "--proxy"); cmd.insert(2, proxy)
                    cookies = os.environ.get("YTDLP_COOKIES", COOKIE_FILE)
                    if cookies and os.path.exists(cookies): cmd.insert(1, "--cookies"); cmd.insert(2, cookies)
                    
                    # 限制超时时间为 30 秒，避免长时间卡住
                    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                    if r.returncode != 0:
                        log(f"yt-dlp 下载失败 (返回码 {r.returncode}): {r.stderr[:300]}")
                    
                    # 检查是否有下载成功的图片
                    found_files = []
                    for root, _, files in os.walk(temp_dir):
                        for file in files:
                            if file.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
                                src_path = os.path.join(root, file)
                                f_ext = os.path.splitext(file)[1].lower()
                                dst = os.path.join(img_dir, f"{idx:02d}_dl{f_ext}")
                                shutil.copy2(src_path, dst)
                                saved.append(dst)
                                found_files.append(file)
                    
                    if found_files:
                        log(f"从媒体内容中成功提取到 {len(found_files)} 个媒体文件: {found_files}")
                    elif r.returncode == 0:
                        log(f"yt-dlp 成功执行但未发现任何图片文件: {final_url}")
            except Exception as e:
                log(f"yt-dlp 媒体抓取失败: {e}")
    
    # 8. 如果通过 JSON 提取处理后依然没下载到任何图片，且有推文 URL，尝试 Playwright 强力抓取
    if not saved and tweet_url:
        log("JSON 提取解析后未下载到任何媒体，尝试使用 Playwright 强力抓取页面图片...")
        pw_urls = _extract_x_image_urls_via_playwright(pw_hint_url or tweet_url, status_id)
        if pw_urls:
            log(f"Playwright 抓取到 {len(pw_urls)} 个候选 URL")
            for idx, u in enumerate(pw_urls, len(saved) + 1):
                u = _clean_extracted_url(u)
                if not u: continue
                final_u = _normalize_x_image_url(u)
                if not final_u:
                    continue
                
                # Playwright 抓取到的通常是直链，直接尝试下载
                out = os.path.join(img_dir, f"pw_{idx:02d}.jpg") # 默认扩展名
                try:
                    _download_url_to_file(final_u, out)
                    saved.append(out)
                    log(f"Playwright 成功下载图片: {final_u}")
                except Exception as e:
                    log(f"Playwright 下载失败: {final_u}, {e}")
    
    log(f"最终成功保存 {len(saved)} 个媒体文件")
    return saved
        

    
    log(f"成功下载 {len(saved)} 张图片")
    return saved


def run_x_text_image_pipeline(url: str, output_dir: str) -> str:
    url = _clean_extracted_url(url) or (url or "").strip().replace("`", "").strip()
    url = url.split("?")[0]
    status_id = extract_x_status_id(url)
    if not status_id:
        raise RuntimeError("无法解析 X/Twitter 的 status id")

    try:
        tweet = fetch_x_tweet_json(status_id)
    except Exception:
        tweet = {}
    if not isinstance(tweet, dict) or not tweet:
        tweet = {}
    if not tweet:
        try:
            tweet = fetch_x_tweet_json(status_id, lang="en")
        except Exception:
            tweet = {}
    if not tweet:
        tweet = fetch_x_tweet_via_ytdlp(url) or {}

    text = sanitize_single_line(
        tweet.get("text")
        or tweet.get("full_text")
        or tweet.get("description")
        or tweet.get("title")
        or ""
    ) or "（无正文）"

    user = tweet.get("user") if isinstance(tweet.get("user"), dict) else {}
    author = sanitize_single_line(
        user.get("name")
        or user.get("screen_name")
        or tweet.get("uploader")
        or tweet.get("uploader_id")
        or "X"
    )

    images = download_x_images(status_id, tweet, output_dir, tweet_url=url)
    title = sanitize_windows_filename(text[:60] or f"x_{status_id}", max_length=80)
    md_path = os.path.join(output_dir, f"X_{title}.md")

    img_md = ""
    for p in images:
        rel = os.path.relpath(p, output_dir).replace("\\", "/")
        img_md += f"\n![]({rel})\n"

    content = f"""---
title: '{yaml_single_quote(text[:80])}'
author: '{yaml_single_quote(author)}'
url: '{yaml_single_quote(url)}'
date: "{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
source: "x"
type: "text_image"
---

# {text[:80]}

## 正文

{text}

## 图片
{img_md if img_md.strip() else '（无图片）'}
"""
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(content)
    log(f"生成Markdown: {md_path}")
    return md_path


def extract_audio(video_path: str, audio_path: str) -> str:
    """用ffmpeg提取音频"""
    if not os.path.exists(video_path):
        raise RuntimeError(f"视频文件不存在: {video_path}")
    
    size = os.path.getsize(video_path)
    if size == 0:
        raise RuntimeError(f"视频文件为空 (0 bytes): {video_path}")
        
    log("提取音频...")
    cmd = [
        FFMPEG_BIN,
        "-i", video_path,
        "-vn",
        "-acodec", "libmp3lame",
        "-q:a", "2",
        audio_path,
        "-y",
        "-hide_banner",
        "-loglevel", "error"
    ]
    r = subprocess.run(cmd, capture_output=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"音频提取失败: {r.stderr.decode()[:300]}")
    return audio_path


def transcribe(audio_path: str) -> dict:
    """用faster-whisper转录（带VAD过滤）"""
    log("Whisper转录中...")

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise RuntimeError("faster-whisper 未安装，请运行: pip install faster-whisper")

    # 使用large-v3模型
    # 如果有GPU则用cuda，否则用cpu
    try:
        import torch
        device = "cuda" if torch.cuda.is_available() else "cpu"
        compute_type = "float16" if device == "cuda" else "int8"
    except ImportError:
        # 如果没有安装 torch，默认回退到 cpu 和 int8
        device = "cpu"
        compute_type = "int8"

    
    log(f"使用设备: {device}, 精度: {compute_type}")
    
    # 为了测试速度，改用 tiny 模型
    model = WhisperModel(
        "tiny",
        device=device,
        compute_type=compute_type,
        download_root=os.path.join(os.path.dirname(__file__), "models")
    )

    log("开始转录...")
    segments, info = model.transcribe(
        audio_path,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500),
        language="zh",
        beam_size=5
    )

    log(f"检测到语言: {info.language}，时长: {info.duration:.1f}秒")

    full_text = ""
    segments_list = []
    for seg in segments:
        text = seg.text.strip()
        full_text += text + ""
        segments_list.append({
            "start": seg.start,
            "end": seg.end,
            "text": text
        })

    return {
        "full_text": full_text.strip(),
        "segments": segments_list,
        "language": info.language,
        "duration": info.duration
    }


def polish_text(raw_text: str) -> dict:
    """用本地LLM洗稿"""
    log("LLM洗稿中...")

    system_prompt = (
        "你是一个专业的内容提炼助手。请根据以下视频转录文本，完成三个任务：\n"
        "1. 去除所有口语化废话（呃、啊、这个、那个等），添加正确标点符号\n"
        "2. 提取3-5个核心知识点（列表形式，每个知识点简洁明了）\n"
        "3. 写一段200字以内的总结\n\n"
        "请按以下JSON格式返回（不要加markdown代码块）：\n"
        "{\"polished\": \"洗稿后的正文\", \"points\": [\"知识点1\", \"知识点2\"], \"summary\": \"总结内容\"}"
    )

    payload_dict = {
        "model": LM_STUDIO_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": raw_text[:8000]}
        ],
        "temperature": 0.3
    }
    # 某些版本的 LM Studio 接口对 response_format 敏感，先移除测试
    # "response_format": {"type": "json_object"}
    
    payload = json.dumps(payload_dict).encode("utf-8")

    req = urllib.request.Request(LM_STUDIO_URL, data=payload, headers={"Content-Type": "application/json"})
    try:
        resp = urllib.request.urlopen(req, timeout=TIMEOUT)
        body = resp.read().decode("utf-8")
        result = json.loads(body)
        content = result["choices"][0]["message"]["content"].strip()
        
        # 提取 JSON (有时模型会返回 ```json ... ```)
        if "```" in content:
            m = re.search(r"```(?:json)?\s*([\s\S]*?)```", content)
            if m:
                content = m.group(1).strip()
        
        return json.loads(content)
    except urllib.error.HTTPError as e:
        if e.code == 502:
            log(f"LLM洗稿失败: HTTP Error 502 (Bad Gateway)。请检查 LM Studio 服务是否正常开启且地址 {LM_STUDIO_URL} 可达。")
        else:
            log(f"LLM洗稿失败: HTTP Error {e.code}: {e.reason}")
        return {
            "polished": raw_text,
            "points": ["核心知识点1 (Mock)", "核心知识点2 (Mock)"],
            "summary": f"（由于 LLM 服务不可达 (HTTP {e.code})，这是 Mock 摘要）"
        }
    except Exception as e:
        log(f"LLM洗稿失败: {e} (使用Mock数据)")
        return {
            "polished": raw_text,
            "points": ["核心知识点1 (Mock)", "核心知识点2 (Mock)"],
            "summary": "（由于本地LLM解析失败，这是Mock摘要）"
        }


def generate_markdown(metadata: dict, transcript: dict, polished: dict, output_path: str):
    """生成Markdown文件"""
    log(f"生成Markdown: {output_path}")

    def fmt_sec(s):
        m, sec = divmod(int(s), 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{sec:02d}"

    timestamp_lines = []
    for seg in transcript.get("segments", []):
        ts = fmt_sec(seg["start"])
        timestamp_lines.append(f"[{ts}] {seg['text']}")

    timestamp_text = "\n".join(timestamp_lines)

    title = sanitize_single_line(metadata.get("title", "未命名")) or "未命名"
    author = sanitize_single_line(metadata.get("author", "未知作者")) or "未知作者"
    url = sanitize_single_line(metadata.get("url", ""))

    content = f"""---
title: '{yaml_single_quote(title)}'
author: '{yaml_single_quote(author)}'
url: '{yaml_single_quote(url)}'
date: "{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
---

# {title}

## 内容摘要

{polished.get('summary', '（无摘要）')}

## 核心知识点

{chr(10).join(f"- {p}" for p in polished.get('points', [])) or '-（无知识点）'}

---

## 洗稿正文

{polished.get('polished', transcript.get('full_text', ''))}

---

## 原始转录（带时间戳）

{timestamp_text}
"""
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)


def get_video_metadata(video_path: str, url: str) -> dict:
    """从同名的 _data.json 文件读取元数据"""
    json_path = video_path.replace(".mp4", "_data.json")
    if os.path.exists(json_path):
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {
                "title": sanitize_single_line(data.get("desc", "未命名").split("#")[0]) or "未命名",
                "author": sanitize_single_line(data.get("author", {}).get("nickname", "未知作者")) or "未知作者",
                "url": url,
                "date": datetime.fromtimestamp(data.get("create_time", 0)).strftime('%Y-%m-%d %H:%M:%S')
            }
        except Exception as e:
            log(f"读取元数据JSON失败: {e}")
    
    filename = os.path.basename(video_path)
    title = os.path.splitext(filename)[0]
    return {
        "title": title,
        "author": "未知作者",
        "url": url
    }


async def run_pipeline(url: str, output_dir: str = None, progress_display: 'ProgressDisplay' = None):
    """主流程 (异步版本)"""
    if output_dir is None:
        output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")

    url = extract_url(url)
    log(f"处理URL: {url}")
    if progress_display:
        progress_display.update_step("任务分发", f"识别链接类型: {url[:30]}...")

    platform, content_id = _content_key(url)
    cached_md = _cached_md_path(output_dir, platform, content_id) if platform in ("douyin", "x") else ""
    if cached_md and os.path.exists(cached_md) and os.path.getsize(cached_md) > 0:
        return cached_md

    # 1. 使用 TaskRouter 确定内容类型并选择处理器
    try:
        from auto_collector import TaskRouter
        router = TaskRouter()
        content_type = router.route_by_url(url)
        processor = router.get_processor(content_type)
        
        if processor:
            log(f"使用 {content_type} 处理器")
            # 处理可能的同步/异步冲突
            import asyncio
            try:
                # 尝试判断是否是协程函数
                import inspect
                process_args = {
                    'url': url,
                    'output_dir': output_dir
                }
                
                # 传入进度显示对象
                if inspect.iscoroutinefunction(processor.process):
                    result = await processor.process(process_args, progress_display)
                else:
                    # 如果不是协程，尝试放在线程池运行
                    result = await asyncio.to_thread(processor.process, process_args, progress_display)
            except Exception as async_err:
                log(f"异步调用处理器失败，尝试同步调用: {async_err}")
                if hasattr(processor, 'process_sync'):
                    result = processor.process_sync(process_args, progress_display)
                else:
                    raise async_err
            
            md_candidates = []
            for p in (result.get("media_paths") or []):
                if isinstance(p, str) and p.lower().endswith(".md") and os.path.exists(p) and os.path.getsize(p) > 0:
                    md_candidates.append(p)
            if md_candidates:
                return md_candidates[0]

            # 为处理器生成一个简单的 Markdown 文件
            title = result.get('title', '未命名')
            content = result.get('content', '')
            source_url = result.get('source_url', url)
            
            # 生成 Markdown 文件
            title_safe = sanitize_windows_filename(title, max_length=80)
            md_path = os.path.join(output_dir, f"{title_safe}.md")
            
            md_content = f"""---
title: '{yaml_single_quote(title)}'
source: "{content_type}"
type: "{content_type}"
url: '{yaml_single_quote(source_url)}'
date: "{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
---

# {title}

{content}

## 原始链接

{source_url}
"""
            
            with open(md_path, 'w', encoding='utf-8') as f:
                f.write(md_content)
            
            log(f"生成 {content_type} 处理器的 Markdown: {md_path}")
            return md_path
    except Exception as e:
        msg = str(e)
        if "未找到本次下载产生的 MP4 文件" in msg or "Fresh cookies" in msg or "Failed to parse JSON" in msg:
            raise
        log(f"使用处理器失败，回退到原有逻辑: {e}")

    # 回退到原有逻辑
    domain = urlparse(url).netloc.lower()
    if "x.com" in domain or "twitter.com" in domain:
        try:
            # download_video 现在是异步的
            video_path = await download_video(url, output_dir)
            log(f"视频已下载: {video_path}")
        except Exception as e:
            msg = str(e)
            # 遇到任何下载错误都尝试降级到文本和图片处理
            log(f"视频下载失败，尝试降级到文本和图片处理: {msg}")
            import asyncio
            return await asyncio.to_thread(run_x_text_image_pipeline, url, output_dir)
    else:
        video_path = await download_video(url, output_dir)
        log(f"视频已下载: {video_path}")

    # 2. 获取元数据
    import asyncio
    metadata = await asyncio.to_thread(get_video_metadata, video_path, url)

    # 3. 提取音频
    audio_path = os.path.splitext(video_path)[0] + ".mp3"
    audio_path = await asyncio.to_thread(extract_audio, video_path, audio_path)

    # 4. 转录
    transcript = await asyncio.to_thread(transcribe, audio_path)

    # 5. LLM洗稿
    polished = await asyncio.to_thread(polish_text, transcript["full_text"])

    # 6. 生成Markdown
    title_safe = sanitize_windows_filename(metadata.get("title", "未命名"), max_length=80)
    md_path = os.path.join(output_dir, f"{title_safe}.md")
    await asyncio.to_thread(generate_markdown, metadata, transcript, polished, md_path)
    return md_path


def run_pipeline_sync(url: str, output_dir: str = None):
    """主流程 (同步兼容版本)"""
    import asyncio
    try:
        return asyncio.run(run_pipeline(url, output_dir))
    except Exception as e:
        log(f"run_pipeline_sync failed: {e}")
        return None


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python pipeline.py <抖音URL>")
        sys.exit(1)
    
    url_input = sys.argv[1]
    try:
        run_pipeline_sync(url_input)
    except Exception as e:
        log(f"Pipeline 出错: {e}")
        import traceback
        traceback.print_exc()
