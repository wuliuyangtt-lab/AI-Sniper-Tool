#!/bin/bash

# 抖音全自动 Pipeline 启动器

# 设置颜色
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 设置工作目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo "============================================"
echo "   抖音全自动 Pipeline 启动器"
echo "============================================"
echo ""

echo "[检查] 正在检查运行环境..."
echo ""

# 检查 Python
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}[错误] 未检测到 Python3！${NC}"
    echo ""
    echo "请安装 Python 3.8 或更高版本："
    echo "  Ubuntu/Debian: sudo apt update && sudo apt install python3 python3-pip"
    echo "  macOS: brew install python3"
    echo "  或访问 https://www.python.org/downloads/"
    echo ""
    read -p "按回车键退出..."
    exit 1
fi

PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
echo -e "${GREEN}[通过] Python 版本: $PYTHON_VERSION${NC}"

# 检查 FFmpeg
if ! command -v ffmpeg &> /dev/null; then
    echo -e "${YELLOW}[警告] 未检测到 FFmpeg！${NC}"
    echo ""
    echo "FFmpeg 是必需的，请安装："
    echo "  Ubuntu/Debian: sudo apt update && sudo apt install ffmpeg"
    echo "  macOS: brew install ffmpeg"
    echo "  CentOS/RHEL: sudo yum install ffmpeg"
    echo ""
    read -p "是否继续安装其他依赖？(y/N): " CONTINUE
    if [[ ! "$CONTINUE" =~ ^[Yy]$ ]]; then
        exit 1
    fi
else
    FFMPEG_VERSION=$(ffmpeg -version 2>&1 | head -n1 | awk '{print $3}')
    echo -e "${GREEN}[通过] FFmpeg 版本: $FFMPEG_VERSION${NC}"
fi

# 检查配置文件
if [ ! -f "config.py" ]; then
    echo -e "${YELLOW}[警告] 配置文件 config.py 不存在！${NC}"
    if [ -f "config.py.example" ]; then
        echo "正在从模板创建 config.py..."
        cp "config.py.example" "config.py"
        echo -e "${YELLOW}[提示] 请编辑 config.py 填写您的配置信息${NC}"
        echo ""
        # 尝试使用默认编辑器
        if command -v nano &> /dev/null; then
            nano "config.py"
        elif command -v vim &> /dev/null; then
            vim "config.py"
        elif command -v vi &> /dev/null; then
            vi "config.py"
        else
            echo "请使用文本编辑器手动编辑 config.py"
            read -p "按回车键继续..."
        fi
    else
        echo -e "${RED}[错误] 配置文件模板也不存在！${NC}"
        read -p "按回车键退出..."
        exit 1
    fi
else
    echo -e "${GREEN}[通过] 配置文件已存在${NC}"
fi

echo ""
echo "[步骤 1/3] 正在安装/更新依赖包..."
echo ""

python3 -m pip install --upgrade pip
if [ $? -ne 0 ]; then
    echo -e "${YELLOW}[警告] pip 升级失败，尝试继续...${NC}"
fi

pip3 install -r requirements.txt
if [ $? -ne 0 ]; then
    echo -e "${RED}[错误] 依赖安装失败！${NC}"
    read -p "按回车键退出..."
    exit 1
fi

echo ""
echo "[步骤 2/3] 正在检查 Playwright 浏览器..."
echo ""

if ! python3 -c "from playwright.sync_api import sync_playwright" 2>/dev/null; then
    echo "[提示] 正在安装 Playwright..."
    pip3 install playwright
    python3 -m playwright install chromium
else
    # 检查浏览器是否已安装
    if ! python3 -c "from playwright.sync_api import sync_playwright; p = sync_playwright().start(); p.chromium.launch(); p.stop()" 2>/dev/null; then
        echo "[提示] 正在安装 Playwright 浏览器..."
        python3 -m playwright install chromium
    else
        echo -e "${GREEN}[通过] Playwright 浏览器已就绪${NC}"
    fi
fi

echo ""
echo "[步骤 3/3] 准备启动主程序..."
echo ""

# 检查输入参数
MODE="${1:-}"

if [ -z "$MODE" ]; then
    echo "用法: ./start.sh [模式]"
    echo ""
    echo "模式选项:"
    echo "  collect    - 启动自动采集模式"
    echo "  pipeline   - 启动 Pipeline 处理模式"
    echo ""
    echo "示例:"
    echo "  ./start.sh collect"
    echo "  ./start.sh pipeline"
    echo ""
    read -p "请选择模式 (collect/pipeline): " MODE
fi

case "$MODE" in
    collect|COLLECT)
        echo "正在启动自动采集模式..."
        python3 -m pipeline.auto_collector
        ;;
    pipeline|PIPELINE)
        echo "正在启动 Pipeline 处理模式..."
        python3 -m pipeline.pipeline
        ;;
    *)
        echo -e "${RED}[错误] 未知模式: $MODE${NC}"
        echo "可用模式: collect, pipeline"
        read -p "按回车键退出..."
        exit 1
        ;;
esac

if [ $? -ne 0 ]; then
    echo ""
    echo -e "${RED}[错误] 程序运行失败！${NC}"
    read -p "按回车键退出..."
fi

echo ""
read -p "按回车键退出..."