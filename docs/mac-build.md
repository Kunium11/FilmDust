# Mac App构建与验证

`python3 scripts/build_mac.py` 会建立项目内构建环境，按已验证的版本安装图像依赖，用PyInstaller冻结引擎，再编译AppKit／WKWebView启动程序，组装并本地签名，最终创建DMG。所有构建输出在build／dist目录，App组装在本机临时目录，避免同步文件夹自动添加Finder属性造成签名失败。

原有engine.py和server.py保持原算法。Mac窗口拥有独立引擎和随机本机端口；退出时终止其引擎。NSSavePanel选择目标后，原生桥接只向本App引擎请求导出并原子保存；取消保存不写文件。浏览器版继续使用普通下载。

构建脚本支持 `--build-python /path/to/python` 使用已准备好的Python环境并跳过依赖安装。普通构建使用项目内venv，不更改全局Python组件。支持 `--verify` 在生成镜像前，用内置测试图运行真实原生窗口、自动处理及保存桥接，核对TIFF后自动退出。

```sh
python3 scripts/build_mac.py --verify
```

当前版本为Mac App1.0.0、引擎1.7.0，构建目标arm64／macOS13及以上。除本机macOS26.6.2外没有额外系统兼容性保证。这里是本地临时签名，没有Developer ID公证，公开分发前需另行处理签名与公证。

构建仅清理自己生成的App上的Finder装饰属性，不关闭系统安全功能，也不移除隔离属性。组件许可随App一起打包。
