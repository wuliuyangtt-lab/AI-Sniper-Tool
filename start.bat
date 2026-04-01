@echo off
chcp 65001 >nul
title 抖音全自动 Pipeline 启动器
echo.
echo ============================================
echo    抖音全自动 Pipeline 启动器
echo ============================================
echo.

REM 设置工作目录
set "WORKDIR=%~dp0"
cd /d "%WORKDIR%"

echo [检查] 正在检查运行环境...
echo.

REM 检查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未检测到 Python！
    echo.
    echo 请安装 Python 3.8 或更高版本：
    echo   1. 访问 https://www.python.org/downloads/
    echo   2. 下载并安装 Python 3.8+
    echo   3. 安装时勾选 "Add Python to PATH"
    echo.
    pause
    exit /b 1
)
for /f "tokens=2" %%a in ('python --version 2^>^&1') do set "PYTHON_VERSION=%%a"
echo [通过] Python 版本: %PYTHON_VERSION%

REM 检查 FFmpeg
ffmpeg -version >nul 2>&1
if errorlevel 1 (
    echo [警告] 未检测到 FFmpeg！
    echo.
    echo FFmpeg 是必需的，请安装：
    echo   1. 访问 https://ffmpeg.org/download.html
    echo   2. 下载 Windows 版本并解压
    echo   3. 将 bin 目录添加到系统 PATH
    echo   或使用包管理器：
    echo     winget install Gyan.FFmpeg
    echo     或
    echo     choco install ffmpeg
    echo.
    set /p "CONTINUE=是否继续安装其他依赖？(Y/N): "
    if /i not "%CONTINUE%"=="Y" exit /b 1
) else (
    for /f "tokens=3" %%a in ('ffmpeg -version 2^>^&1 ^| findstr "ffmpeg version"') do (
        echo [通过] FFmpeg 版本: %%a
        goto :ffmpeg_done
    )
)
:ffmpeg_done

REM 检查配置文件
if not exist "config.py" (
    echo [警告] 配置文件 config.py 不存在！
    if exist "config.py.example" (
        echo 正在从模板创建 config.py...
        copy "config.py.example" "config.py"
        echo [提示] 请编辑 config.py 填写您的配置信息
        echo.
        notepad "config.py"
    ) else (
        echo [错误] 配置文件模板也不存在！
        pause
        exit /b 1
    )
) else (
    echo [通过] 配置文件已存在
)

echo.
echo [步骤 1/3] 正在安装/更新依赖包...
echo.

python -m pip install --upgrade pip
if errorlevel 1 (
    echo [错误] pip 升级失败，尝试继续...
)

pip install -r requirements.txt
if errorlevel 1 (
    echo [错误] 依赖安装失败！
    pause
    exit /b 1
)

echo.
echo [步骤 2/3] 正在检查 Playwright 浏览器...
echo.

python -c "from playwright.sync_api import sync_playwright; print('[通过] Playwright 已安装')" 2>nul
if errorlevel 1 (
    echo [提示] 正在安装 Playwright...
    pip install playwright
    playwright install chromium
) else (
    REM 检查浏览器是否已安装
    python -c "from playwright.sync_api import sync_playwright; p = sync_playwright().start(); p.chromium.launch(); p.stop()" 2>nul
    if errorlevel 1 (
        echo [提示] 正在安装 Playwright 浏览器...
        playwright install chromium
    ) else (
        echo [通过] Playwright 浏览器已就绪
    )
)

echo.
echo [步骤 3/3] 准备启动主程序...
echo.

REM 检查输入参数
if "%~1"=="" (
    echo 用法: start.bat [模式] [URL]
    echo.
    echo 模式选项:
    echo   collect    - 启动自动采集模式
    echo   pipeline   - 启动 Pipeline 处理模式
    echo.
    echo 示例:
    echo   start.bat collect
    echo   start.bat pipeline
    echo.
    set /p "MODE=请选择模式 (collect/pipeline): "
) else (
    set "MODE=%~1"
)

if /i "%MODE%"=="collect" (
    echo 正在启动自动采集模式...
    python -m pipeline.auto_collector
) else if /i "%MODE%"=="pipeline" (
    echo 正在启动 Pipeline 处理模式...
    python -m pipeline.pipeline
) else (
    echo [错误] 未知模式: %MODE%
    echo 可用模式: collect, pipeline
    pause
    exit /b 1
)

if errorlevel 1 (
    echo.
    echo [错误] 程序运行失败！
    pause
)

echo.
echo 按任意键退出...
pause >nul