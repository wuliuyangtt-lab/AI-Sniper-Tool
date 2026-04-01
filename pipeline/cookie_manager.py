#!/usr/bin/env python3
"""
Cookies管理模块
实现自动获取和更新cookies的功能
"""

import json
import os
from pathlib import Path
from typing import Dict, Optional, List
from playwright.sync_api import BrowserContext

class EnhancedCookieManager:
    """增强的Cookie管理器"""
    
    def __init__(self, base_dir: Path, quiet: bool = True):
        """初始化Cookie管理器
        
        Args:
            base_dir: 基础目录
        """
        self.base_dir = Path(base_dir)
        self.cookie_file = self.base_dir / "cookies.json"
        self.downloader_cookie_file = self.base_dir / "downloader" / "config.yml"
        self.cookies: Dict[str, str] = {}
        self.quiet = bool(quiet)
        self.load_cookies()

    def _print(self, msg: str):
        if not self.quiet:
            print(msg)
    
    def load_cookies(self):
        """加载cookies"""
        if self.cookie_file.exists():
            try:
                with open(self.cookie_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        self.cookies = data
                if self.cookies:
                    self._print(f"[CookieManager] 从 {self.cookie_file.name} 加载了 {len(self.cookies)} 个cookies")
            except Exception as e:
                self._print(f"[CookieManager] 加载cookies失败: {e}")
        
        # 兜底：如果 JSON 为空，尝试从 Netscape txt 恢复（虽然只有基本信息）
        if not self.cookies:
            netscape_path = self.base_dir / "cookies.txt"
            if netscape_path.exists():
                self._print(f"[CookieManager] 尝试从 cookies.txt 恢复...")
                # 这里可以写简单的解析逻辑，但为了保持逻辑简单，先跳过
                pass
    
    def save_cookies(self):
        """保存cookies"""
        try:
            # 保存 JSON 格式
            with open(self.cookie_file, 'w', encoding='utf-8') as f:
                json.dump(self.cookies, f, ensure_ascii=False, indent=2)
            
            self._print(f"[CookieManager] 已保存 {len(self.cookies)} 个cookies到 {self.cookie_file.name}")
        except Exception as e:
            self._print(f"[CookieManager] 保存cookies失败: {e}")

    def save_playwright_cookies(self, playwright_cookies: List[Dict]):
        """保存从 Playwright 获取的完整 cookie 列表为 Netscape 格式"""
        if not playwright_cookies:
            return
            
        # 更新内存
        new_cookies = {c['name']: c['value'] for c in playwright_cookies}
        self.cookies.update(new_cookies)
        
        # 1. 保存 JSON
        self.save_cookies()

        # 2. 保存 Netscape (供 yt-dlp 使用)
        netscape_path = self.base_dir / "cookies.txt"
        try:
            with open(netscape_path, 'w', encoding='utf-8') as f:
                f.write("# Netscape HTTP Cookie File\n")
                f.write("# This is a generated file! Do not edit.\n\n")
                
                for c in playwright_cookies:
                    domain = c.get('domain', '')
                    if not domain: continue
                    
                    # Netscape 格式规范：
                    # 1. domain
                    # 2. flag (TRUE/FALSE) - 是否包含子域名
                    # 3. path
                    # 4. secure (TRUE/FALSE)
                    # 5. expiration (timestamp)
                    # 6. name
                    # 7. value
                    
                    include_subdomains = "TRUE" if domain.startswith('.') else "FALSE"
                    secure = "TRUE" if c.get('secure') else "FALSE"
                    expires = c.get('expires', 0)
                    if expires is None or expires == -1:
                        expires = 0
                    else:
                        expires = int(expires)
                        
                    path = c.get('path', '/')
                    name = c.get('name', '')
                    value = c.get('value', '')
                    
                    line = f"{domain}\t{include_subdomains}\t{path}\t{secure}\t{expires}\t{name}\t{value}\n"
                    f.write(line)
            self._print(f"[CookieManager] 已更新 Netscape cookies.txt (count={len(playwright_cookies)})")
        except Exception as e:
            self._print(f"[CookieManager] 保存 Netscape cookies 失败: {e}")
    
    def extract_from_browser(self, context: BrowserContext):
        """从浏览器上下文提取cookies (同步)"""
        try:
            cookies = context.cookies()
            self.save_playwright_cookies(cookies)
            return True
        except Exception as e:
            self._print(f"[CookieManager] 从浏览器提取cookies失败: {e}")
            return False
    
    def update_downloader_config(self):
        """更新downloader的cookies配置"""
        if not self.downloader_cookie_file.exists():
            self._print(f"[CookieManager] downloader配置文件不存在: {self.downloader_cookie_file}")
            return False
        
        try:
            # 读取配置文件
            with open(self.downloader_cookie_file, 'r', encoding='utf-8') as f:
                config_content = f.read()
            
            # 提取cookies部分
            lines = config_content.split('\n')
            new_lines = []
            in_cookies_section = False
            cookies_written = False
            
            # 我们关心的核心鉴权字段
            auth_keys = ['ttwid', 'odin_tt', 'passport_csrf_token', 'msToken', 'sid_guard', 'sessionid', 'n_mh', 'sessionid_ss', 'sid_tt']
            
            for line in lines:
                if line.strip() == 'cookies:':
                    in_cookies_section = True
                    new_lines.append(line)
                    # 写入新的cookies
                    for key in auth_keys:
                        if key in self.cookies:
                            new_lines.append(f"  {key}: \"{self.cookies[key]}\"")
                    cookies_written = True
                elif in_cookies_section and line.strip() and not line.startswith('  '):
                    # 退出cookies部分
                    in_cookies_section = False
                    new_lines.append(line)
                elif not in_cookies_section:
                    new_lines.append(line)
                elif in_cookies_section and not line.strip():
                    # 空行也算退出部分
                    in_cookies_section = False
                    new_lines.append(line)
            
            # 如果没有找到cookies部分，添加它
            if not cookies_written:
                new_lines.append('')
                new_lines.append('cookies:')
                for key in auth_keys:
                    if key in self.cookies:
                        new_lines.append(f"  {key}: \"{self.cookies[key]}\"")
            
            # 写回配置文件
            with open(self.downloader_cookie_file, 'w', encoding='utf-8') as f:
                f.write('\n'.join(new_lines))
            
            self._print(f"[CookieManager] 已更新 downloader/config.yml 核心鉴权字段")
            return True
        except Exception as e:
            self._print(f"[CookieManager] 更新downloader配置失败: {e}")
            return False
    
    def get_cookies(self) -> Dict[str, str]:
        """获取cookies"""
        return self.cookies
    
    def get_cookie_string(self) -> str:
        """获取cookie字符串"""
        return '; '.join([f"{k}={v}" for k, v in self.cookies.items()])
    
    def validate_cookies(self) -> bool:
        """验证cookies是否有效"""
        # 放宽验证条件，只需 sessionid 或 passport_csrf_token 存在即可
        has_auth = 'sessionid' in self.cookies or 'passport_csrf_token' in self.cookies or 'sessionid_ss' in self.cookies
        if not has_auth:
            found_keys = list(self.cookies.keys())
            self._print(f"[CookieManager] Cookies验证失败，缺少核心鉴权字段。当前包含: {found_keys[:10]}...")
            return False
        self._print("[CookieManager] Cookies验证成功")
        return True
    
    def clear_cookies(self):
        """清除cookies"""
        self.cookies = {}
        if self.cookie_file.exists():
            self.cookie_file.unlink()
        self._print("[CookieManager] 清除了所有cookies")
