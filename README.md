# 抖音全自动 Pipeline / Douyin Auto Pipeline

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.8+-blue.svg" alt="Python 3.8+">
  <img src="https://img.shields.io/badge/Platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey.svg" alt="Platform">
  <img src="https://img.shields.io/badge/License-MIT-green.svg" alt="License">
</p>

[English](#english) | [中文](#chinese)

---

<a name="chinese"></a>
## 🇨🇳 中文介绍

### 项目简介

**抖音全自动 Pipeline** 是一个 AI 驱动的自动化采集与智能入库系统。它能够自动从抖音、X (Twitter) 等平台采集内容，通过本地 LLM 进行智能过滤和处理，最终自动入库到 AnythingLLM 知识库中，实现内容的自动化管理和知识沉淀。

### 功能特性

- **🌐 多平台支持**
  - 抖音视频、图文、长文笔记
  - X (Twitter) 文本、图片、视频
  - 支持收藏夹、用户主页等多种采集模式

- **🤖 本地 LLM 过滤**
  - 集成 LM Studio，支持本地大语言模型
  - 智能内容洗稿和摘要生成
  - 隐私保护，数据不上传云端

- **📚 自动入库 AnythingLLM**
  - 一键入库到 AnythingLLM 知识库
  - 支持增量更新和全量同步
  - 自动生成 Markdown 格式文档

- **🛠️ 更多特性**
  - 自动语音转录 (Whisper)
  - 智能图片提取和下载
  - 浏览器自动化采集 (Playwright)
  - 跨平台支持 (Windows/Linux/macOS)

### 快速开始

#### 环境要求

- Python 3.8 或更高版本
- FFmpeg (用于音频处理)
- Chrome 浏览器 (用于 Playwright)

#### 安装步骤

1. **克隆仓库**

```bash
git clone https://github.com/yourusername/douyin-pipeline.git
cd douyin-pipeline
```

2. **运行启动脚本**

**Windows:**
```cmd
start.bat
```

**Linux/Mac:**
```bash
chmod +x start.sh
./start.sh
```

启动脚本会自动检查环境并安装依赖。

#### 手动安装

如果自动安装失败，可以手动安装：

```bash
# 安装 Python 依赖
pip install -r requirements.txt

# 安装 Playwright 浏览器
playwright install chromium

# 复制配置文件
cp config.py.example config.py
```

#### 配置说明

编辑 `config.py` 文件，根据您的环境配置以下选项：

```python
# LM Studio 配置
LM_STUDIO_URL = "http://localhost:1234/v1/chat/completions"
LM_STUDIO_MODEL = "qwen/qwen3-8b"

# AnythingLLM 配置
ALLM_API_KEY = "your-api-key-here"
ALLM_WORKSPACE = "抖音收藏库"

# 代理配置 (可选)
PROXY_SERVER = "http://127.0.0.1:7890"
```

#### 使用方法

**自动采集模式：**
```bash
# Windows
start.bat collect

# Linux/Mac
./start.sh collect
```

**Pipeline 处理模式：**
```bash
# Windows
start.bat pipeline

# Linux/Mac
./start.sh pipeline
```

### 项目结构

```
.
├── config.py              # 配置文件
├── config.py.example      # 配置模板
├── requirements.txt       # Python 依赖
├── start.bat             # Windows 启动脚本
├── start.sh              # Linux/Mac 启动脚本
├── pipeline/             # 核心代码
│   ├── pipeline.py       # 主 Pipeline
│   ├── auto_collector.py # 自动采集器
│   ├── cookie_manager.py # Cookie 管理
│   └── downloader/       # 下载器模块
└── README.md             # 说明文档
```

---

<a name="english"></a>
## 🇺🇸 English Introduction

### Project Overview

**Douyin Auto Pipeline** is an AI-driven automated collection and intelligent ingestion system. It automatically collects content from platforms like Douyin (TikTok China) and X (Twitter), processes it through local LLM for intelligent filtering, and automatically ingests it into the AnythingLLM knowledge base for automated content management and knowledge沉淀.

### Features

- **🌐 Multi-Platform Support**
  - Douyin videos, image galleries, and long-form notes
  - X (Twitter) text, images, and videos
  - Support for favorites, user profiles, and more collection modes

- **🤖 Local LLM Filtering**
  - Integrated with LM Studio, supporting local large language models
  - Intelligent content polishing and summary generation
  - Privacy protection, data never uploaded to cloud

- **📚 Auto Ingest to AnythingLLM**
  - One-click ingestion to AnythingLLM knowledge base
  - Support for incremental updates and full synchronization
  - Automatic Markdown document generation

- **🛠️ More Features**
  - Automatic speech transcription (Whisper)
  - Intelligent image extraction and download
  - Browser automation collection (Playwright)
  - Cross-platform support (Windows/Linux/macOS)

### Quick Start

#### Requirements

- Python 3.8 or higher
- FFmpeg (for audio processing)
- Chrome browser (for Playwright)

#### Installation

1. **Clone the repository**

```bash
git clone https://github.com/yourusername/douyin-pipeline.git
cd douyin-pipeline
```

2. **Run the startup script**

**Windows:**
```cmd
start.bat
```

**Linux/Mac:**
```bash
chmod +x start.sh
./start.sh
```

The startup script will automatically check the environment and install dependencies.

#### Manual Installation

If automatic installation fails, you can install manually:

```bash
# Install Python dependencies
pip install -r requirements.txt

# Install Playwright browsers
playwright install chromium

# Copy configuration file
cp config.py.example config.py
```

#### Configuration

Edit the `config.py` file and configure the following options according to your environment:

```python
# LM Studio Configuration
LM_STUDIO_URL = "http://localhost:1234/v1/chat/completions"
LM_STUDIO_MODEL = "qwen/qwen3-8b"

# AnythingLLM Configuration
ALLM_API_KEY = "your-api-key-here"
ALLM_WORKSPACE = "Douyin Collection"

# Proxy Configuration (Optional)
PROXY_SERVER = "http://127.0.0.1:7890"
```

#### Usage

**Auto Collection Mode:**
```bash
# Windows
start.bat collect

# Linux/Mac
./start.sh collect
```

**Pipeline Processing Mode:**
```bash
# Windows
start.bat pipeline

# Linux/Mac
./start.sh pipeline
```

### Project Structure

```
.
├── config.py              # Configuration file
├── config.py.example      # Configuration template
├── requirements.txt       # Python dependencies
├── start.bat             # Windows startup script
├── start.sh              # Linux/Mac startup script
├── pipeline/             # Core code
│   ├── pipeline.py       # Main Pipeline
│   ├── auto_collector.py # Auto collector
│   ├── cookie_manager.py # Cookie manager
│   └── downloader/       # Downloader module
└── README.md             # Documentation
```

---

## ⚖️ 免责声明 / Disclaimer

### 中文

**重要提示：在使用本项目前，请仔细阅读以下免责声明。**

1. **教育与研究目的**：本项目仅供教育和研究目的使用，旨在展示自动化数据采集和处理的技术实现。

2. **合规使用**：用户在使用本项目时，必须遵守相关法律法规，包括但不限于《中华人民共和国网络安全法》、《中华人民共和国数据安全法》、《中华人民共和国个人信息保护法》等。用户应确保其使用行为符合目标平台的服务条款和 robots.txt 协议。

3. **数据隐私**：用户应尊重数据隐私权利，不得非法收集、使用、处理或传输他人的个人信息。对于采集的数据，用户应承担全部法律责任。

4. **知识产权**：用户应尊重知识产权，不得侵犯他人的著作权、商标权等合法权益。本项目仅作为技术工具提供，不对用户使用过程中产生的任何知识产权纠纷承担责任。

5. **责任限制**：本项目按"原样"提供，不提供任何形式的担保。开发者不对因使用本项目而产生的任何直接、间接、偶然、特殊或后果性损害承担责任，包括但不限于数据丢失、业务中断、法律责任等。

6. **风险自担**：使用本项目的风险由用户自行承担。用户应自行评估使用本项目的合法性和风险，并采取适当的安全措施。

7. **第三方服务**：本项目可能涉及第三方服务（如 Douyin、X、AnythingLLM、LM Studio 等），用户应遵守这些第三方服务的使用条款。开发者不对第三方服务的可用性、准确性或安全性承担责任。

8. **修改与分发**：用户在修改或分发本项目时，应保留本免责声明，并明确告知接收方相关风险和责任。

**继续使用本项目即表示您已阅读、理解并同意接受本免责声明的所有条款。如您不同意本免责声明的任何部分，请立即停止使用本项目。**

### English

**IMPORTANT: Please read the following disclaimer carefully before using this project.**

1. **Educational and Research Purposes**: This project is provided solely for educational and research purposes, demonstrating the technical implementation of automated data collection and processing.

2. **Compliant Use**: Users must comply with all applicable laws and regulations when using this project, including but not limited to local data protection laws, cybersecurity laws, and privacy laws. Users should ensure their usage complies with the target platforms' Terms of Service and robots.txt protocols.

3. **Data Privacy**: Users must respect data privacy rights and shall not illegally collect, use, process, or transmit others' personal information. Users bear full legal responsibility for any data collected.

4. **Intellectual Property**: Users must respect intellectual property rights and shall not infringe upon others' copyrights, trademarks, or other legitimate rights. This project is provided solely as a technical tool, and the developers assume no responsibility for any intellectual property disputes arising from user usage.

5. **Limitation of Liability**: This project is provided "as is" without any form of warranty. The developers shall not be liable for any direct, indirect, incidental, special, or consequential damages resulting from the use of this project, including but not limited to data loss, business interruption, or legal liability.

6. **Assumption of Risk**: The use of this project is at the user's own risk. Users should independently assess the legality and risks of using this project and implement appropriate security measures.

7. **Third-Party Services**: This project may involve third-party services (such as Douyin, X, AnythingLLM, LM Studio, etc.). Users must comply with the terms of service of these third-party services. The developers assume no responsibility for the availability, accuracy, or security of third-party services.

8. **Modification and Distribution**: When modifying or distributing this project, users must retain this disclaimer and clearly inform recipients of the associated risks and liabilities.

**BY CONTINUING TO USE THIS PROJECT, YOU ACKNOWLEDGE THAT YOU HAVE READ, UNDERSTOOD, AND AGREE TO ACCEPT ALL TERMS OF THIS DISCLAIMER. IF YOU DO NOT AGREE WITH ANY PART OF THIS DISCLAIMER, PLEASE CEASE USING THIS PROJECT IMMEDIATELY.**

---

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## 🤝 Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## 📧 Contact

If you have any questions or suggestions, please open an issue on GitHub.

---

## ⚖️ 免责声明 (Disclaimer)

**仅限研究使用**：本工具仅供技术研究、学术交流及自动化技术探讨之用，严禁用于任何商业目的或非法盈利。

**合规性风险**：用户在使用本工具进行数据采集时，必须遵守目标平台（如抖音、知乎等）的 Robots 协议及相关法律法规。因不当使用导致的账号封禁、法律纠纷等后果，均由使用者自行承担，开发者不承担任何责任。

**数据安全**：本工具为本地运行，不存储任何用户信息。用户需妥善保管个人 API Key 及账号 Cookie，严禁在公共环境下泄露配置文件。

**侵权处理**：若本工具的功能涉及侵犯相关平台或个人的合法权益，请联系开发者进行处理。

---

<p align="center">
  Made with ❤️ by the community
</p>