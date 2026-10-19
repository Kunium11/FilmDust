# FilmDust

本地运行的胶片扫描照片除尘工具，包含浏览器版与 Apple Silicon Mac App 源码。导入后自动识别并清理灰点、淡灰痕与细丝，直接显示前后对比，按需修正后保存副本。

照片在本机处理，不调用云端 AI，不覆盖原文件。服务只监听127.0.0.1，原生App的文件选择和保存使用系统对话框。

## 功能

- 一个自动除尘流程，减少漏掉的淡灰痕和弯曲细丝；高级设置可调整灵敏度、尺寸和灰尘颜色。
- 设置在逐张换图和 Mac App 重启后沿用。换图、关闭窗口和退出前提醒保存未保存结果。
- 建筑边缘、密集纹理和难以可靠修补的位置保留原样。
- 前后对比、局部补去灰尘、整块还原、取样修补、撤销和重做；可逐区定位已处理位置和保留疑点。
- 补画只修补笔触及碰到的完整标记，不重新修补整张照片。手动确认的区域优先执行修补，不再被自动保护静默跳过；撤销恢复修改前的实际像素。
- 编辑失败会回滚；预览失败可重新加载，预览更新前禁止保存。连续导入失败仍保留当前照片。
- 无损TIFF导出，保留8／16位、原始尺寸、ICC配置和分辨率；8位PNG和JPEG可选。
- iOS风格的响应式界面；Mac App使用AppKit、WKWebView与内嵌图像引擎。

## 已知限制

检测仍会漏掉灰尘，也可能把反光、文字或真实细节当成灰尘。建筑边缘和复杂纹理上的修补仍需检查。候选／处理数量不代表准确率。当前默认更重视去除完整性，部分疑难位置仍保持原样，建议快速比较并放大核对关键区域。

手动补画需要覆盖完整灰尘，点到已有标记会确认整块区域。强制修补复杂纹理时可能留下平滑痕迹，可撤销或改用取样修补。局部修正的耗时与首次整张自动处理分开显示；约 6000 万像素扫描件的识别仍可能超过 10 秒。大图会先显示低分辨率对比，完整清晰度在后台替换。撤销和重做只保存改动区域的像素，步数按这些区域的内存占用限制。可选的大图计时检查默认不运行，只报告耗时；机器速度会影响结果，10 秒目标并未记为已经达到。

支持JPEG、PNG及单页RGB／灰度TIFF，单张最多200MB、1亿像素。暂不支持RAW、HEIC、CMYK、多页TIFF或批量处理。照片编辑进度不会在重启后恢复，请保存副本；检测设置会保留。浏览器设置按当前地址保存，Mac App 设置不受随机端口影响。

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

公开源码中的测试覆盖位深、透明通道、ICC、选区外像素不变、边缘保护、事务保护与撤销，以及 16 位 RGB/RGBA PNG、灰度透明 TIFF、预乘透明度、过期导出拦截和连续失败换图。局部修补另验证手动确认生效、只计算局部、连续笔触及精确撤销、失败回滚、裁剪预览与完整导出的一致性。撤销测试检查改动区域可以逐值恢复，并可以重做回同一结果。原开发照片和截图保持在本机，相关4项回归测试默认跳过；如需在本机验证，可设置 `FILMDUST_PRIVATE_SAMPLE`、`FILMDUST_PRIVATE_EDGE` 指向对应原始开发夹具。内置演示图是人工构造的测试图，不包含用户照片。

约 6000 万像素的计时检查默认跳过，照片不要放进仓库。需要时：

```sh
FILMDUST_TIMING_GATE=1 FILMDUST_TIMING_SAMPLE=/path/to/scan.jpg \
  PYTHONPATH=filmdust .venv/bin/python -m unittest tests.verify_timing
```

或 `PYTHONPATH=filmdust .venv/bin/python scripts/timing_gate.py /path/to/scan.jpg`。命令只打印从导入到第一次前后对比、以及到完整预览的秒数，并与 10 秒对照。加上 `FILMDUST_TIMING_ENFORCE=1` 才会在超时的时候返回失败。数字随机器变化，输出本身不是“已经达到 10 秒”的结论。

## 项目结构

- `filmdust/`：原有Python引擎、仅本机HTTP服务及网页界面。
- `macos/`：原生窗口、保存桥接、冻结引擎入口及App组装脚本。
- `scripts/`：本地启动与Mac构建入口。
- `tests/`：图像质量与自动处理检查。

本仓库以 MIT 许可证发布，许可全文见根目录 `LICENSE`。依赖遵循各自许可证，Mac构建会收集组件许可文件。上传仓库不表示已完成商店发布或Apple公证。
