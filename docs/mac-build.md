# Mac App构建与验证

`python3 scripts/build_mac.py` 会建立项目内构建环境，按已验证的版本安装图像依赖，用PyInstaller冻结引擎，再编译AppKit／WKWebView启动程序，组装并本地签名，最终创建DMG。所有构建输出在build／dist目录，App组装在本机临时目录，避免同步文件夹自动添加Finder属性造成签名失败。

网页与 Mac App 共用同一个图像引擎。Mac窗口拥有独立引擎和随机本机端口；退出时终止其引擎。NSSavePanel选择目标后，原生桥接只向本App引擎请求导出并原子保存；取消保存不写文件。浏览器版继续使用普通下载。

构建脚本支持 `--build-python /path/to/python` 使用已准备好的Python环境并跳过依赖安装。普通构建使用项目内venv，不更改全局Python组件。支持 `--verify` 在生成镜像前，用内置测试图运行真实原生窗口、自动处理、关闭未保存提醒及保存桥接，核对 TIFF 后自动退出。

```sh
python3 scripts/build_mac.py --verify
```

当前版本为Mac App1.1.1、引擎1.8.6，构建目标arm64／macOS13及以上。除本机macOS26.6.2外没有额外系统兼容性保证。这里是本地临时签名，没有Developer ID公证，公开分发前需另行处理签名与公证。

构建仅清理自己生成的App上的Finder装饰属性，不关闭系统安全功能，也不移除隔离属性。组件许可随App一起打包。

## 使用实际照片验证安装包

挂载生成的 DMG 后，可运行其中 App 的 `Contents/MacOS/FilmDust`，传入：

- `--verify-app /tmp/filmdust-check`：可写的验证输出目录。
- `--verify-input /path/to/scan.jpeg`：显式指定的本机照片，不会加入 App。
- `--verify-preferences '{}'`：本次使用默认设置；也可传入设置 JSON。
- `--verify-repeat`：保存完成后再次导入同一照片，检查连续处理。
- `--verify-brush x,y,diameter`：在原图像素坐标处补画，检查结果、撤销后像素、再次补画和保存；目前应与 `--verify-repeat` 分开运行。

`native-runs.json` 记录从开始导入到全分辨率对比绘制完成的时间，包括上传、识别、修补、预览编码、传输和显示；不包含用户选择文件的时间或 App 冷启动。`native-export.tif` 是通过真实原生保存桥接写出的副本。

补画验证时，`brush.firstSeconds`、`undoSeconds`、`repeatSeconds` 分别记录每次操作到预览更新完成的耗时，`autoSeconds` 保留首次整张处理的时间。两种时间不能混作同一项性能指标。验证坐标需由调用者选择；执行成功不代表该位置一定是灰尘，也不能推算全图识别准确率。

验证模式通过与正常使用相同的原生设置桥接写入独立测试偏好域，不改变用户的设置。下次验证时省略 `--verify-preferences`，可检查重新启动和随机端口改变后是否沿用设置。

浏览器设置储存在当前地址的 localStorage；Mac App 通过 UserDefaults 记忆检测设置。照片和编辑历史只存在本次运行内存中。
