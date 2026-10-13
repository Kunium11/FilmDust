# FilmDust

本地运行的胶片扫描照片除尘工具，包含浏览器版与 Apple Silicon Mac App 源码。导入后自动检测、保守筛选和修补，直接显示前后对比，按需修正后保存副本。

照片在本机处理，不调用云端 AI，不覆盖原文件。服务只监听127.0.0.1，原生App的文件选择和保存使用系统对话框。

## 功能

- 自动处理孤立小灰点与细丝，提供轻柔、标准、更多三档。
- 建筑边缘、密集纹理和难以可靠修补的位置保留原样。
- 前后对比、补选、整块还原、取样修补、区域精修和撤销。
- 无损TIFF导出，保留8／16位、原始尺寸、ICC配置和分辨率；8位PNG和JPEG可选。
- iOS风格的响应式界面；Mac App使用AppKit、WKWebView与内嵌图像引擎。

## 已知限制

检测仍会漏掉灰尘，也可能把反光、文字或真实细节当成灰尘。建筑边缘和复杂纹理上的修补仍需检查。候选／处理数量不代表准确率。默认轻柔更保守，疑难位置保持原样，建议放大核对关键区域。

支持JPEG、PNG及单页RGB／灰度TIFF，单张最多200MB、1亿像素。暂不支持RAW、HEIC、CMYK、多页TIFF或批量处理。刷新页面、关闭App后不恢复编辑进度，请及时保存副本。

当前Mac打包目标是M系列、macOS13及以上；实际验证环境为macOS26.6.2。使用本地临时签名，尚未做Developer ID公证；没有Intel或iPhone安装包。

## 运行浏览器版

需要Python3，建议3.11或3.12。首次准备图像组件需要联网，之后处理照片可离线。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python filmdust/server.py
```

Mac也可双击 `scripts/launch.command`。浏览器自动打开，地址以启动窗口为准。

## 构建Mac App

需要Apple Silicon Mac、Swift命令行工具以及Python。构建脚本生成独立App和DMG，普通使用不再需要终端或系统Python。

```sh
python3 scripts/build_mac.py
```

结果位于 `dist/FilmDust-Mac.dmg`。具体步骤、签名范围和检查方式见 [Mac构建说明](docs/mac-build.md)。

## 验证

```sh
PYTHONPATH=filmdust .venv/bin/python -m unittest discover -s tests -p 'verify*.py'
```

公开源码中的测试覆盖位深、透明通道、ICC、选区外像素不变、边缘保护、事务保护与撤销。原开发照片和截图保持在本机，相关3项回归测试默认跳过；如需在本机验证，可设置 `FILMDUST_PRIVATE_SAMPLE`、`FILMDUST_PRIVATE_EDGE` 指向对应原始开发夹具。内置演示图是人工构造的测试图，不包含用户照片。

## 项目结构

- `filmdust/`：原有Python引擎、仅本机HTTP服务及网页界面。
- `macos/`：原生窗口、保存桥接、冻结引擎入口及App组装脚本。
- `scripts/`：本地启动与Mac构建入口。
- `tests/`：图像质量与自动处理检查。

本仓库未授予开源许可证；依赖遵循各自许可证，Mac构建会收集组件许可文件。上传仓库不表示已完成商店发布或Apple公证。
